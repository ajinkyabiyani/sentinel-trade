"""Claude Skill Desk: Claude trades the India Quant Desk skill pack on paper. Intraday option buys, swing option buys and cash buys.

The skill pack (.claude/skills/india-quant-desk) is the Everything Claude Trading plugin rewritten for NSE, BSE and SEBI. Its decision
playbook is Claude's instructions here. Code finds the setups and checks every answer; Claude only chooses among real contracts:
  Intraday   after each 5-minute candle from 09:35 to 14:30, the app's setup scanner on Nifty, Bank Nifty and the day's biggest F&O movers
  Swing      once a day at 14:40, the daily-trend read (30-week and daily trend, momentum, room to run) on the same names
  Claude     one locked-down call per setup (no tools, an empty folder): BUY an option or shares, intraday or swing, or SKIP
  Checks     the contract must be in the snapshot; stop below and target above the live price; reward at least 1.5x risk; size cut to
             the rupee cap; liquidity, strike distance, expiry and India VIX rules; the book's open-trade, day-loss and money limits
  Paper      a fill at the live price plus slippage, followed on 5-minute candles to its stop, target, 15:15 (intraday), or its
             session limit and expiry rule (swing)

Buy side only: a put is the falling view. Nothing is sold short or written, and this module has no way to place an order.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import agent_desk, common, live_stops, multi_agent_team as mat, notify, paper_desk, pipeline_swing as swing, setup_scan

india, setups = mat.india, mat.setups

log = logging.getLogger("kite-desk.skill-desk")
DESK_ID, DESK_NAME = "skill_desk", "Claude Skill Desk"
DIR = paper_desk.CACHE_DIR / DESK_ID
SKILL_DIR = agent_desk.SKILLS / "india-quant-desk"
PLAYBOOK = SKILL_DIR / "references" / "desk-playbook.md"
MODEL, EFFORT, TIMEOUT = "claude-opus-5-5", "high", 360

CAPITAL = 500000.0
MAX_RISK = 3000.0             # rupees lost at the stop, with charges
MAX_OPEN = 3
DAY_LOSS = 10000.0            # realised plus open marks: past this, no new trade today
MAX_CASH_POSITION = 150000.0  # one share position's value: no margin
MAX_PREMIUM = 100000.0        # one option position's premium
MIN_RR = 1.5
MAX_RUNS = 24                 # Claude calls a day, intraday and swing together
INDICES = ("NIFTY", "BANKNIFTY")
STOCKS_EACH_WAY = 8
FIRST_SCAN, LAST_INTRADAY = "09:35", "14:30"
SWING_SCAN, LAST_SWING = "14:40", "15:10"
EOD_AT = "15:31"
STALE_MINUTES = 15
SWING_MAX = 4                 # swing setups sent to Claude a day
SWING_MIN_DTE, SWING_MAX_DTE, SWING_EXIT_DTE = 21, 75, 7
HOLD_OPTION, HOLD_SHARES = 10, 15     # sessions a swing trade may be held
LATE_EXPIRY = "13:00"         # on expiry day, intraday buys move to the next expiry after this
MAX_STRIKES_AWAY = 2
SPREAD_INTRADAY, SPREAD_SWING = 3.0, 4.0     # bid to ask, % of the mid price
MIN_OI_LOTS = 25              # stock options
MIN_VIX = 12.0                # intraday option buys need some movement
SLIP = {"index": 0.003, "stock": 0.01, "shares": 0.0005}
STRIKES_EACH_SIDE = 6
BANDS = ((0, 49), (50, 59), (60, 69), (70, 100))
CALIBRATION_TRADES, BAND_TRADES = 50, 10
DELIVERY = {"stt": 0.001, "txn": 0.0000307, "stamp_buy": 0.00015}    # shares held overnight: no brokerage at Zerodha

stock_source = None           # set by the route: () -> {"up": [...], "down": [...]}, the day's F&O movers
_lock = threading.RLock()
_running: dict = {"underlying": None, "since": None}
_scan: dict = {"bar": None}

# The desk roles Claude writes a finding for, in the playbook's order (the repo's agents, India edition). The page shows each one
# as Claude writes it: this is the run's real progress, streamed from the Claude tool, not an animation.
ROLES = [("regime", "Market regime"), ("technical", "Technical"), ("strategy", "Strategy"), ("options", "Options"), ("volatility", "Volatility"),
         ("risk", "Risk"), ("compliance", "Compliance"), ("portfolio", "Portfolio manager")]
VERDICTS = ("supports", "against", "neutral", "veto")
# What the page shows live: the scanner's sweep and the run Claude is on. In memory only; the records hold the finished results.
_live: dict = {"scan": None, "run": None}

def inr(x: float) -> str:
    """Rupees with Indian digit grouping: 150000 -> ₹1,50,000."""
    s = f"{abs(x):.0f}"
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail, head = head[-2:] + "," + tail, head[:-2]
    return ("−" if x < 0 else "") + "₹" + (head + "," + tail if head else tail)


def rules_text() -> list[str]:
    """The rules as plain sentences, from the current values (they can be changed in the app's Strategy Rules page)."""
    return [
    "Paper only, buy side only: an option (call or put) or shares. Nothing is sold short or written.",
    f"Loss at the stop at most {inr(MAX_RISK)} with charges: the code resizes Claude's quantity to fit, in whole lots.",
    f"Reward at least {MIN_RR}x risk, with the stop below and the target above the live price at the fill.",
    f"Up to {MAX_OPEN} open trades, one per underlying. No new trade after a {inr(DAY_LOSS)} loss in the day.",
    f"Shares: at most {inr(MAX_CASH_POSITION)} a position, no margin. Options: at most {inr(MAX_PREMIUM)} of premium.",
    f"Options: a contract from the snapshot, within {MAX_STRIKES_AWAY} strikes of the money, spread under {SPREAD_INTRADAY:g}% ({SPREAD_SWING:g}% swing), stock options with {MIN_OI_LOTS}+ lots of open interest.",
    f"Intraday options need India VIX at {MIN_VIX:g} or more, and none open after {LAST_INTRADAY}. Intraday trades close at 15:15.",
    f"Swing options need {SWING_MIN_DTE}+ days to expiry and close after {HOLD_OPTION} sessions or with {SWING_EXIT_DTE} days left. Swing shares close after {HOLD_SHARES} sessions.",
    ]


RULES = rules_text()


# ---------------------------------------------------------------- files

def _path(kind: str, day: str | None = None) -> Path:
    return DIR / (f"{kind}_{day}.json" if day else f"{kind}.json")


def _read(kind: str, day: str | None, default):
    try:
        return json.loads(_path(kind, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(kind: str, day: str | None, data) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(kind, day).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_path(kind, day))


def runs(day: str) -> list[dict]:
    return _read("runs", day, [])


def positions() -> list[dict]:
    return _read("positions", None, [])


def _save_positions(rows: list[dict]) -> None:
    _write("positions", None, rows)


def _save_run(day: str, record: dict) -> None:
    with _lock:
        _write("runs", day, runs(day) + [record])


def queue(day: str) -> list[dict]:
    return _read("queue", day, [])


def activity(day: str) -> list[dict]:
    return _read("activity", day, [])


def last_scanned(day: str) -> str | None:
    """The candle the day's last intraday scan covered ("... after the 10:20 candle ..."), so a restart doesn't scan it again."""
    for a in reversed(activity(day)):
        text = a.get("text") or ""
        if a.get("kind") == "scan" and "after the " in text and " candle" in text:
            return text.split("after the ", 1)[1].split(" candle", 1)[0]
    return None


def _note(day: str, at: str, kind: str, text: str) -> None:
    with _lock:
        _write("activity", day, (activity(day) + [{"at": at, "kind": kind, "text": text}])[-300:])


def _state() -> dict:
    return _read("state", None, {})


def _set_state(**kw) -> None:
    with _lock:
        _write("state", None, {**_state(), **kw})


# ---------------------------------------------------------------- charges and money

def _slip(e: dict) -> float:
    return SLIP["shares"] if e["vehicle"] == "shares" else SLIP["index" if e["underlying"] in INDICES else "stock"]


def fees(e: dict, buy_value: float, sell_value: float) -> float:
    """Round-trip charges for one position, by what it is: an option, intraday shares, or shares held overnight."""
    if e["vehicle"] == "option":
        return agent_desk.option_fees(buy_value, sell_value)
    if e["horizon"] == "intraday":
        return india.costs("equity_intraday", buy_value, sell_value)
    turnover = buy_value + sell_value
    exch = DELIVERY["txn"] * turnover + india.SEBI * turnover
    return round(DELIVERY["stt"] * turnover + exch + india.GST * exch + DELIVERY["stamp_buy"] * buy_value, 2)


def mark(e: dict) -> float:
    """An open trade at its last price, after exit slippage, before exit charges. A closed one at its net."""
    if e["status"] == "closed":
        return e.get("net") or 0.0
    px = e.get("ltp") if e.get("ltp") is not None else e["entry"]
    return round((px * (1 - _slip(e)) - e["entry"]) * e["qty"], 2)


def day_pnl(day: str, rows: list[dict] | None = None) -> float:
    """Today's result: trades closed today, plus every open trade's mark."""
    rows = positions() if rows is None else rows
    return round(sum(mark(e) for e in rows if (e["status"] == "open") or e.get("exit_day") == day), 2)


def money_in_use(rows: list[dict]) -> float:
    return sum(e["entry"] * e["qty"] for e in rows if e["status"] == "open")


# ---------------------------------------------------------------- setups

def _minutes(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


def _movers() -> dict:
    try:
        m = stock_source() if stock_source else {}
    except Exception:  # noqa: BLE001 - the indices still get scanned
        log.exception("F&O movers failed")
        m = {}
    return m if isinstance(m, dict) else {}


def universe() -> list[tuple[str, str | None]]:
    """(symbol, the way to look): the two indices both ways, then the top F&O movers the way they're moving."""
    m = _movers()
    return [(s, None) for s in INDICES] + [(s, "up") for s in m.get("up", [])[:STOCKS_EACH_WAY]] + [(s, "down") for s in m.get("down", [])[:STOCKS_EACH_WAY]]


def _sweep(kind: str, names: list[tuple[str, str | None]], label: str) -> dict:
    """Starts the live view of a scan: every name queued, in the order it will be checked."""
    sweep = {"kind": kind, "label": label, "state": "scanning", "started": datetime.now(common.IST).strftime("%H:%M:%S"), "finished": None, "current": None,
             "names": [{"symbol": s, "want": w, "state": "queued", "note": ""} for s, w in names]}
    _live["scan"] = sweep
    return sweep


def _mark(sweep: dict, i: int, state: str, note: str = "") -> None:
    sweep["names"][i].update(state=state, note=note)
    sweep["current"] = sweep["names"][i]["symbol"] if state == "scanning" else sweep["current"]


def _done(sweep: dict) -> None:
    sweep.update(state="done", current=None, finished=datetime.now(common.IST).strftime("%H:%M:%S"))


def intraday_setups(now: datetime) -> tuple[list[dict], dict]:
    """New setups on 5-minute candles that pass the chart check. Each is looked at once a day."""
    day = now.date().isoformat()
    seen = set(_read("seen", day, []))
    reg = setup_scan.regime(now.date())
    found, counts = [], {"scanned": 0, "fired": 0, "passed": 0}
    names = universe()
    sweep = _sweep("intraday", names, f"after the {common.completed_bar(now) or now.strftime('%H:%M')} candle")
    for i, (sym, want) in enumerate(names):
        _mark(sweep, i, "scanning")
        try:
            fired = setup_scan.find(sym, now.date(), now.replace(tzinfo=None), seen, reg)
        except india.IndiaError:
            _mark(sweep, i, "nodata", "no candles")
            continue
        counts["scanned"] += 1
        hits = []
        for c in fired:
            seen.add(c["key"])
            if want and c["direction"] != want:
                continue
            counts["fired"] += 1
            if not c["technical"]["confirmed"]:
                hits.append(f"{c['direction']}: failed the chart check")
                continue
            counts["passed"] += 1
            found.append({"underlying": sym, "kind": "index" if sym in INDICES else "stock", "horizon": "intraday", "setup": setup_scan.RULES[c["setup"]], "direction": c["direction"], "at": c["at"], "why": c["why"]})
            hits.append(f"{setup_scan.RULES[c['setup']]}, {c['direction']}")
        passed = [h for h in hits if "failed" not in h]
        _mark(sweep, i, ("setup_up" if "up" in passed[0].rsplit(", ", 1)[-1] else "setup_down") if passed else ("near" if hits else "none"), "; ".join(hits) or "no setup")
    _done(sweep)
    _write("seen", day, sorted(seen))
    found.sort(key=lambda t: t["kind"] != "index")
    return found, counts


def daily_read(sym: str) -> tuple[list[dict], dict | None, dict | None]:
    """A year of daily candles with today's so far, and the swing desk's trend read for a rise and for a fall."""
    rows = swing.fetch_daily(sym)
    today = swing.fetch_today(sym)
    if today and rows and today["date"] > rows[-1]["date"]:
        rows = rows + [today]
    if len(rows) < 175:
        return rows, None, None
    ind = swing.indicators(rows)
    return rows, swing.read(ind, len(rows) - 1, 1), swing.read(ind, len(rows) - 1, -1)


def swing_setups(now: datetime) -> list[dict]:
    """Names whose daily and weekly trend agree, with room to run: the strongest few, both ways."""
    found = []
    names = universe()
    sweep = _sweep("swing", names, "daily and weekly trend")
    for i, (sym, want) in enumerate(names):
        _mark(sweep, i, "scanning")
        try:
            _, up, down = daily_read(sym)
        except Exception:  # noqa: BLE001 - one missing chart never stops the scan
            _mark(sweep, i, "nodata", "no daily candles")
            continue
        got = []
        for way, r in (("up", up), ("down", down)):
            if r and r["ok"] and want in (None, way):
                got.append(way)
                found.append({"underlying": sym, "kind": "index" if sym in INDICES else "stock", "horizon": "swing", "setup": "Daily trend and momentum (swing)", "direction": way, "at": now.strftime("%H:%M"),
                              "why": f"close {r['close']}, above/below the 20, 50 and 30-week averages, RSI {r['rsi']}, {r['room_in_atr']} ATR of room to the 6-month extreme", "room": r.get("room_in_atr") or 0})
        _mark(sweep, i, f"setup_{got[0]}" if got else "none", f"trend in line, {got[0]}" if got else "trend not in line")
    _done(sweep)
    found.sort(key=lambda t: (t["kind"] != "index", -(t["room"] or 0)))
    return found[:SWING_MAX]


# ---------------------------------------------------------------- the snapshot Claude reads

def _expiry_chain(sym: str, horizon: str, now: datetime) -> dict | None:
    """The option chain Claude may choose from: the nearest expiry intraday (the next one late on expiry day), 3 to 10 weeks out for swing."""
    first = swing.fetch_chain(sym, None)
    today = now.date()
    exps = [(e, (date.fromisoformat(e) - today).days) for e in (first.get("expiries") or [])]
    if horizon == "swing":
        pick = next(((e, d) for e, d in exps if SWING_MIN_DTE <= d <= SWING_MAX_DTE), None)
    else:
        live = [(e, d) for e, d in exps if d > 0 or now.strftime("%H:%M") < LATE_EXPIRY]
        pick = live[0] if live else None
    if not pick:
        return None
    chain = first if first.get("expiry") == pick[0] else swing.fetch_chain(sym, pick[0])
    return dict(chain, days_to_expiry=pick[1])


def snapshot(trigger: dict, now: datetime) -> dict:
    sym, day = trigger["underlying"], now.date().isoformat()
    cut = now.replace(tzinfo=None)
    five = india.session_bars(india.chart(sym, "5d", "5m"), 5, cut).get(day) or []
    india.apply_volumes(five, india.future_volumes(sym, 5, "5d"))
    if len(five) < 4:
        raise ValueError("Too few candles today yet.")
    try:
        vix = float(india.chart("INDIAVIX", "1d", "1d")["meta"]["regularMarketPrice"])
    except (india.IndiaError, KeyError, TypeError, ValueError):
        vix = None
    vw = setups.vwap(five)[-1]
    daily, up, down = daily_read(sym)
    closes = [r["close"] for r in daily]
    try:
        chain = _expiry_chain(sym, trigger["horizon"], now)
    except Exception:  # noqa: BLE001 - without a chain Claude can still choose shares, or skip
        chain = None
    rows = positions()
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M"), "session_phase": india.phase(cut)[1], "trigger": trigger, "underlying": sym, "instrument_type": trigger["kind"],
        "spot": five[-1]["c"], "vwap": None if vw is None else round(vw, 2), "opening_range": {"high": max(b["h"] for b in five[:3]), "low": min(b["l"] for b in five[:3])},
        "candles_5m": mat._candles(five[-40:]), "india_vix": vix,
        "daily": {"last_20_closes": [round(c, 2) for c in closes[-20:]], "realised_vol_20d_pct": swing.realised_vol(closes), "rising_read": up, "falling_read": down},
        "option_chain": mat.trim_chain(chain, STRIKES_EACH_SIDE) if chain else None,
        "book": {"open": [{k: e.get(k) for k in ("instrument", "underlying", "vehicle", "horizon", "entry", "stop", "target", "ltp", "qty")} for e in rows if e["status"] == "open"], "today_pnl": day_pnl(day, rows)},
        "limits": {"capital": CAPITAL, "max_risk_inr": MAX_RISK, "max_open": MAX_OPEN, "daily_loss_inr": DAY_LOSS, "max_cash_position_inr": MAX_CASH_POSITION, "max_premium_inr": MAX_PREMIUM, "min_reward_risk": MIN_RR},
    }


# ---------------------------------------------------------------- Claude

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["walkthrough", "action", "vehicle", "horizon", "instrument", "entry", "stop_loss", "target", "quantity", "confidence", "skills_used", "thesis", "risks", "invalidation"],
    "properties": {
        "walkthrough": {"type": "array", "description": "Written first: one entry per desk role, in this order: " + ", ".join(k for k, _ in ROLES),
                        "items": {"type": "object", "additionalProperties": False, "required": ["role", "verdict", "finding"],
                                  "properties": {"role": {"type": "string", "enum": [k for k, _ in ROLES]}, "verdict": {"type": "string", "enum": list(VERDICTS)},
                                                 "finding": {"type": "string", "description": "One or two sentences with the numbers that matter"}}}},
        "action": {"type": "string", "enum": ["BUY", "SKIP"]},
        "vehicle": {"type": "string", "enum": ["option", "shares", "none"]},
        "horizon": {"type": "string", "enum": ["intraday", "swing", "none"]},
        "instrument": {"type": "string", "description": "An option's tradingsymbol from option_chain, or the underlying's symbol for shares; empty for SKIP"},
        "entry": {"type": "number"}, "stop_loss": {"type": "number"}, "target": {"type": "number"},
        "quantity": {"type": "integer", "description": "Units (lots x lot size for an option)"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "skills_used": {"type": "array", "items": {"type": "string"}},
        "thesis": {"type": "string"}, "risks": {"type": "array", "items": {"type": "string"}}, "invalidation": {"type": "string"},
    },
}


def system_prompt() -> str:
    return ("You are the Claude Skill Desk's decision maker, trading Indian markets on paper. Follow this playbook from the India Quant Desk skill pack exactly. "
            "Answer only with the JSON object the schema asks for.\n\n" + PLAYBOOK.read_text(encoding="utf-8"))


def walkthrough_so_far(buf: str) -> tuple[list[dict], dict | None]:
    """Reads the answer while Claude is still writing it. -> (the role entries finished so far, the one being written: role and its text so far)."""
    start = buf.find('"walkthrough"')
    if start < 0:
        return [], None
    i = buf.find("[", start)
    if i < 0:
        return [], None
    done, depth, open_at, in_str, esc = [], 0, None, False, False
    for j in range(i + 1, len(buf)):
        ch = buf[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
            open_at = j if depth == 1 else open_at
        elif ch == "}":
            depth -= 1
            if depth == 0 and open_at is not None:
                try:
                    done.append(json.loads(buf[open_at:j + 1]))
                except ValueError:
                    pass
                open_at = None
        elif ch == "]" and depth == 0:
            return done, None
    partial = None
    if open_at is not None:
        tail = buf[open_at:]
        role = re.search(r'"role"\s*:\s*"(\w+)"', tail)
        text = re.search(r'"finding"\s*:\s*"((?:[^"\\]|\\.)*)', tail)
        if role:
            raw = text.group(1) if text else ""
            try:
                shown = json.loads('"' + raw.rstrip("\\") + '"')
            except ValueError:
                shown = raw
            partial = {"role": role.group(1), "finding": shown}
    return done, partial


def ask_claude(context: dict, tool: str, progress=None) -> dict:
    """One structured call through the signed-in Claude Code tool: no tools, no settings, no MCP, an empty folder. -> the decision.
    The answer is streamed, so progress(stage, done_roles, writing_role) can show each desk role's finding as Claude writes it."""
    args = [tool, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--json-schema", json.dumps(SCHEMA),
            "--system-prompt", system_prompt(), "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
    user = ("Here is the setup and the live snapshot. Work through the playbook's checks in order and decide.\n\n" + json.dumps(context, ensure_ascii=False))
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY" and not k.startswith("CLAUDE")}
    tell = progress or (lambda *a: None)
    buf, result, last = "", None, None
    with tempfile.TemporaryDirectory() as empty:
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=empty, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        late = threading.Event()
        timer = threading.Timer(TIMEOUT, lambda: (late.set(), p.kill()))
        timer.start()
        try:
            p.stdin.write(user.encode("utf-8"))
            p.stdin.close()
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "thinking":
                        tell("thinking", [], None)
                    elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "input_json_delta":
                        buf += e["delta"].get("partial_json") or ""
                        now = walkthrough_so_far(buf)
                        if now != last:
                            last = now
                            tell("writing", *now)
                elif kind == "result":
                    result = ev
            err = p.stderr.read().decode("utf-8", "replace")
            p.wait()
        finally:
            timer.cancel()
    if late.is_set():
        raise ValueError("Claude took too long.")
    answer = (result or {}).get("structured_output")
    if not result or result.get("is_error") or not isinstance(answer, dict):
        raise ValueError(_tool_error(str((result or {}).get("result") or (result or {}).get("subtype") or err)))
    return answer


def _tool_error(text: str) -> str:
    """Plain words for the page. Never echoes what the tool said."""
    low = text.lower()
    if "limit" in low:
        return "Your Claude usage limit has been reached."
    if "log in" in low or "login" in low or "auth" in low:
        return "Claude isn't signed in on this computer."
    return "Claude gave no usable answer."


# ---------------------------------------------------------------- the code's checks

def _row_for(chain: dict | None, symbol: str) -> tuple[dict, str] | None:
    for r in (chain or {}).get("rows") or []:
        for leg in ("ce", "pe"):
            if (r.get(leg) or {}).get("tradingsymbol") == symbol:
                return r, leg
    return None


def check(d: dict, ctx: dict, now: datetime, live_price, rows: list[dict] | None = None) -> dict:
    """Every rule, on Claude's answer. -> {"ok", "why", "checks": [...], "position": {...} when ok}. live_price(key) -> the last traded price."""
    checks: list[dict] = []
    rows = positions() if rows is None else rows

    def fail(name: str, why: str) -> dict:
        checks.append({"name": name, "ok": False, "note": why})
        return {"ok": False, "why": why, "checks": checks}

    def ok(name: str, note: str) -> None:
        checks.append({"name": name, "ok": True, "note": note})

    if d.get("action") != "BUY":
        return {"ok": False, "why": "Claude skipped this setup.", "checks": checks}
    t, day, sym = now.strftime("%H:%M"), now.date().isoformat(), ctx["underlying"]
    vehicle, horizon = d.get("vehicle"), d.get("horizon")
    if vehicle not in ("option", "shares") or horizon not in ("intraday", "swing"):
        return fail("answer", "Claude's answer didn't name a vehicle and a horizon.")
    if horizon == "intraday" and t > LAST_INTRADAY:
        return fail("time", f"no new intraday trade after {LAST_INTRADAY}.")
    if t > LAST_SWING:
        return fail("time", f"no new trade after {LAST_SWING}.")
    ok("time", f"{t}, inside the {horizon} window")

    open_rows = [e for e in rows if e["status"] == "open"]
    if len(open_rows) >= MAX_OPEN:
        return fail("book", f"{MAX_OPEN} trades are already open.")
    if any(e["underlying"] == sym for e in open_rows):
        return fail("book", f"the book already has an open trade on {sym}.")
    today = day_pnl(day, rows)
    if today <= -DAY_LOSS:
        return fail("book", f"the book is down ₹{-today:,.0f} today, past the ₹{DAY_LOSS:,.0f} day limit.")
    ok("book", f"{len(open_rows)} open, today {today:+,.0f}")

    e = {"vehicle": vehicle, "horizon": horizon, "underlying": sym}
    if vehicle == "shares":
        if ctx["instrument_type"] == "index":
            return fail("instrument", "an index can't be bought as shares.")
        if d.get("instrument") != sym:
            return fail("instrument", f"shares must be {sym} itself.")
        key = chart_key = sym
        e.update(instrument=sym, key=f"NSE:{sym}", lot_size=1, product="MIS" if horizon == "intraday" else "CNC")
        ok("instrument", f"{sym} shares ({e['product']})")
    else:
        chain = ctx.get("option_chain")
        hit = _row_for(chain, d.get("instrument"))
        if not hit:
            return fail("instrument", f"{d.get('instrument') or 'no contract'} isn't in the option chain the desk gave Claude.")
        r, leg = hit
        o, lot = r[leg], int(chain.get("lot_size") or 0)
        strikes = sorted(x["strike"] for x in chain["rows"])
        step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=0)
        away = round(abs(r["strike"] - chain["atm"]) / step) if step else 0
        if not lot:
            return fail("instrument", "the chain has no lot size.")
        if away > MAX_STRIKES_AWAY:
            return fail("liquidity", f"strike {r['strike']} is {away} strikes from the money, over {MAX_STRIKES_AWAY}.")
        bid, ask = o.get("bid") or 0, o.get("ask") or 0
        mid = (bid + ask) / 2
        spread = round((ask - bid) / mid * 100, 1) if bid and ask else None
        limit = SPREAD_SWING if horizon == "swing" else SPREAD_INTRADAY
        if spread is None or spread > limit:
            return fail("liquidity", f"the bid-ask spread is {'unknown' if spread is None else f'{spread}%'}, over {limit:g}%.")
        if ctx["instrument_type"] == "stock" and (o.get("oi") or 0) < MIN_OI_LOTS * lot:
            return fail("liquidity", f"open interest is {round((o.get('oi') or 0) / lot)} lots, under {MIN_OI_LOTS}.")
        dte = chain.get("days_to_expiry")
        if horizon == "swing" and (dte is None or dte < SWING_MIN_DTE):
            return fail("expiry", f"a swing option needs {SWING_MIN_DTE}+ days to expiry; this one has {dte}.")
        vix = ctx.get("india_vix")
        if horizon == "intraday" and vix is not None and vix < MIN_VIX:
            return fail("regime", f"India VIX is {vix:.1f}, under {MIN_VIX:g}: too little movement for an intraday option buy.")
        key, chart_key = f"NFO:{o['tradingsymbol']}", f"NFO:{o['tradingsymbol']}"
        e.update(instrument=o["tradingsymbol"], key=key, lot_size=lot, product="NRML" if horizon == "swing" else "MIS", expiry=chain.get("expiry"), strike=r["strike"], option_type=leg.upper(), delta=o.get("delta"), iv=o.get("iv"))
        ok("liquidity", f"{o['tradingsymbol']}: {away} strike(s) from ATM, spread {spread}%, {dte} days to expiry")

    last = float(live_price(chart_key))
    e["slip"] = _slip(e)
    fill = round(last * (1 + e["slip"]), 2)
    stop, target = float(d.get("stop_loss") or 0), float(d.get("target") or 0)
    if not stop < last < target:
        return fail("levels", f"the live price {last:.2f} isn't between the stop {stop:.2f} and the target {target:.2f}.")
    rr = (target - fill) / (fill - stop) if fill > stop else 0
    if rr < MIN_RR:
        return fail("levels", f"reward is {rr:.2f}x risk at the fill of {fill:.2f}, under {MIN_RR}x.")
    ok("levels", f"fill {fill:.2f}, stop {stop:.2f}, target {target:.2f}: {rr:.2f}R")

    lot = e["lot_size"]
    out = stop * (1 - e["slip"])
    per_unit = fill - out
    per_lot = per_unit * lot + fees(e, fill * lot, out * lot)
    lots_cap = int(MAX_RISK // per_lot) if per_lot > 0 else 0
    asked = max(0, int(d.get("quantity") or 0)) // lot
    lots = min(asked or lots_cap, lots_cap)
    cap_value = MAX_CASH_POSITION if vehicle == "shares" else MAX_PREMIUM
    lots = min(lots, int(cap_value // (fill * lot)), int((CAPITAL - money_in_use(rows)) // (fill * lot)))
    if lots < 1:
        unit = "share" if vehicle == "shares" else "lot"
        return fail("size", f"one {unit} would lose about ₹{per_lot:,.0f} at the stop with charges, or cost more than the book allows; the cap is ₹{MAX_RISK:,.0f}.")
    qty = lots * lot
    max_loss = round(per_unit * qty + fees(e, fill * qty, out * qty), 2)
    resized = asked and lots < asked
    ok("size", f"{qty} {'shares' if vehicle == 'shares' else f'units ({lots} lot(s))'}, ₹{fill * qty:,.0f}, loss at the stop about ₹{max_loss:,.0f}" + (f" (cut from Claude's {asked * lot})" if resized else ""))

    e.update(qty=qty, lots=None if vehicle == "shares" else lots, entry=fill, stop=stop, target=target, max_loss=max_loss, ltp=last, chart_key=chart_key, status="open", day=day,
             entry_at=t, entry_bar=mat._entry_bar(t), sessions=0, confidence=d.get("confidence"))
    return {"ok": True, "why": "", "checks": checks, "position": e}


# ---------------------------------------------------------------- following trades

def _close(e: dict, price: float, reason: str, day: str, at: str) -> None:
    out = price * (1 - e["slip"])
    cost = fees(e, e["entry"] * e["qty"], out * e["qty"])
    gross = (out - e["entry"]) * e["qty"]
    risk = (e["entry"] - e["stop"]) * e["qty"]
    e.update(status="closed", exit=round(out, 2), exit_day=day, exit_at=at, exit_reason=reason, costs=round(cost, 2), net=round(gross - cost, 2), r=round((gross - cost) / risk, 2) if risk > 0 else None)


def walk(e: dict, bars: dict, day: str, upto: str) -> None:
    """One open trade over today's closed 5-minute bars: the stop first, then the target; intraday trades out at 15:15.
    A swing trade opened on an earlier day is checked from the open, so an overnight gap through its stop fills at the open."""
    start = e["entry_bar"] if e["day"] == day else common.LABELS[0]
    for t in common.LABELS:
        if t < start or t > upto:
            continue
        if t in bars:
            o, h, lo, c = bars[t]
            e["ltp"] = c
            at = common.plus_minutes(t, 5)
            if lo <= e["stop"]:
                return _close(e, min(o, e["stop"]), "stop", day, at)
            if h >= e["target"]:
                return _close(e, max(o, e["target"]), "target", day, at)
        if t == agent_desk.FLAT_BAR and e["horizon"] == "intraday" and e.get("ltp") is not None:
            return _close(e, e["ltp"], "15:15 exit", day, "15:15")
    return None


def live_check(e: dict, now: datetime, px: float | None) -> None:
    """Closes an open trade as soon as its last traded price crosses the stop or the target, without waiting for the candle.
    An intraday trade's 15:15 exit stays with the candle walk."""
    if px is None or (e["horizon"] == "intraday" and now.strftime("%H:%M") >= "15:15"):
        return
    e["ltp"] = px
    got = live_stops.hit(px, e["stop"], e.get("target"))
    if got:
        _close(e, got[1], got[0], now.date().isoformat(), now.strftime("%H:%M"))


def end_of_day(e: dict, day: str) -> None:
    """After the close: a swing trade has held one more session; it closes at the last price on its session limit or expiry rule."""
    if e["status"] != "open" or e["horizon"] != "swing" or e.get("counted") == day:
        return
    e["counted"] = day
    e["sessions"] = e.get("sessions", 0) + 1
    limit = HOLD_OPTION if e["vehicle"] == "option" else HOLD_SHARES
    if e.get("ltp") is None:
        return
    if e["sessions"] >= limit:
        _close(e, e["ltp"], f"{limit} sessions", day, "15:30")
    elif e["vehicle"] == "option" and e.get("expiry") and (date.fromisoformat(e["expiry"]) - date.fromisoformat(day)).days <= SWING_EXIT_DTE:
        _close(e, e["ltp"], f"{SWING_EXIT_DTE} days to expiry", day, "15:30")


def follow(now: datetime) -> None:
    day, bar = now.date().isoformat(), common.completed_bar(now)
    rows = positions()
    changed = False
    live = live_stops.prices([e["key"] for e in rows if e["status"] == "open"], now) if bar else {}
    for e in rows:
        if e["status"] != "open" or not bar:
            continue
        before = json.dumps(e, sort_keys=True)
        try:
            walk(e, mat.bars_of(e["chart_key"], day), day, bar)
        except Exception:  # noqa: BLE001 - the live check below still runs
            log.warning("no candles for %s", e["instrument"])
        if e["status"] == "open":
            live_check(e, now, live.get(e["key"]))
        if json.dumps(e, sort_keys=True) != before:
            changed = True
            if e["status"] == "closed":
                _note(day, now.strftime("%H:%M"), "exit", f"{e['instrument']} closed at {e['exit']:.2f} ({e['exit_reason']}): net ₹{e['net']:,.2f}.")
    if now.strftime("%H:%M") >= EOD_AT and _state().get("eod") != day:
        for e in rows:
            was = e["status"]
            end_of_day(e, day)
            if was == "open" and e["status"] == "closed":
                _note(day, EOD_AT, "exit", f"{e['instrument']} closed at {e['exit']:.2f} ({e['exit_reason']}): net ₹{e['net']:,.2f}.")
        _set_state(eod=day)
        changed = True
    if changed:
        with _lock:
            fresh = {e["id"]: e for e in rows}
            _save_positions([fresh.get(e["id"], e) for e in positions()])


# ---------------------------------------------------------------- one run

def claude_tool() -> str | None:
    return mat.claude_tool()


def clock() -> datetime:
    return datetime.now(common.IST)


def _live_price(key: str) -> float:
    return float(india.chart(key, "1d", "1d")["meta"]["regularMarketPrice"])


def _announce(e: dict, now: datetime) -> None:
    what = f"{'option' if e['vehicle'] == 'option' else 'shares'}, {e['horizon']}"
    notify.send_once(f"{DESK_ID}|{e['id']}", notify.signal_text({"desk": DESK_NAME, "instrument": e["instrument"], "side": "BUY", "setup": f"Claude skill · {what}", "time": e["entry_at"], "date": e["day"],
                                                                "entry": e["entry"], "stop": e["stop"], "target": e["target"], "qty": e["qty"], "lots": e.get("lots"), "max_loss": e["max_loss"], "expiry": e.get("expiry"),
                                                                "confidence": None if e.get("confidence") is None else f"{e['confidence']}% · AI view"}), now)


def _work(trigger: dict, now: datetime, tool: str) -> None:
    day = now.date().isoformat()
    record = {"id": f"{day}|{trigger['underlying']}|{trigger['horizon']}|{now.strftime('%H:%M:%S')}", "at": now.strftime("%H:%M"), "underlying": trigger["underlying"], "trigger": trigger}
    run = {"id": record["id"], "underlying": trigger["underlying"], "trigger": trigger, "stage": "snapshot", "since": clock().strftime("%H:%M:%S"), "roles": [], "writing": None, "decision": None, "checks": None}
    _live["run"] = run

    def progress(stage: str, done: list[dict], writing: dict | None) -> None:
        run.update(stage=stage, roles=done, writing=writing)
    try:
        ctx = snapshot(trigger, now)
        chain = ctx.get("option_chain") or {}
        record["market"] = {"spot": ctx["spot"], "vwap": ctx["vwap"], "india_vix": ctx["india_vix"], "expiry": chain.get("expiry"), "days_to_expiry": chain.get("days_to_expiry"), "lot_size": chain.get("lot_size")}
        run.update(stage="asking", market=record["market"])
        record["decision"] = ask_claude(ctx, tool, progress)
        run.update(stage="checks", roles=record["decision"].get("walkthrough") or run["roles"], writing=None, decision=record["decision"])
        res = check(record["decision"], ctx, clock(), _live_price)
        record["checks"] = res["checks"]
        if res["ok"]:
            e = dict(res["position"], id=record["id"])
            with _lock:
                _save_positions(positions() + [e])
            record["paper"] = {"taken": True}
            _announce(e, now)
        else:
            record["paper"] = {"taken": False, "why": res["why"]}
    except subprocess.TimeoutExpired:
        record["error"] = "Claude took too long."
    except (india.IndiaError, ValueError, KeyError) as exc:
        record["error"] = str(exc) or type(exc).__name__
    except Exception:  # noqa: BLE001 - a failed run is shown on the page and must not kill the thread
        log.exception("skill desk run failed")
        record["error"] = "The run failed. See the backend log."
    finally:
        _save_run(day, record)
        _running.update(underlying=None, since=None)
        run.update(stage="done", writing=None, checks=record.get("checks"), paper=record.get("paper"), error=record.get("error"), finished=clock().strftime("%H:%M:%S"))
    d = record.get("decision") or {}
    if record.get("error"):
        what = "failed: " + record["error"]
    elif d.get("action") == "BUY":
        what = f"BUY {d.get('instrument')} ({d.get('vehicle')}, {d.get('horizon')})" + (" · paper trade taken" if record["paper"]["taken"] else f" · not taken: {record['paper']['why']}")
    else:
        what = "SKIP: " + str(d.get("thesis") or "")[:200]
    _note(day, clock().strftime("%H:%M"), "result", f"{trigger['underlying']}: {what}")


def start(trigger: dict, now: datetime) -> None:
    tool = claude_tool()
    if not tool:
        raise ValueError("Claude isn't signed in on this computer.")
    if not PLAYBOOK.exists():
        raise ValueError("The india-quant-desk skill pack is missing.")
    with _lock:
        if _running["underlying"]:
            raise ValueError(f"A run on {_running['underlying']} is still going.")
        _running.update(underlying=trigger["underlying"], since=now.isoformat(timespec="seconds"))
    threading.Thread(target=_work, args=(trigger, now, tool), daemon=True, name="skill-desk").start()


def tick(now: datetime) -> None:
    """Every 15 seconds: follow open trades, scan after each candle and at the swing time, then send the next waiting setup to Claude."""
    t, day = now.strftime("%H:%M"), now.date().isoformat()
    if not india.trading_day(now.date()):
        return
    if t >= "09:20":
        follow(now)
    if not FIRST_SCAN <= t <= LAST_SWING or not claude_tool():
        return
    bar = common.completed_bar(now)
    if _scan["bar"] is None:                 # after a restart: the candle already scanned today, read back from the log
        _scan["bar"] = last_scanned(day)
    if t <= LAST_INTRADAY and bar and bar != _scan["bar"]:
        _scan["bar"] = bar
        found, counts = intraday_setups(now)
        with _lock:
            _write("queue", day, queue(day) + found)
        _note(day, t, "scan", f"Scanned {counts['scanned']} charts after the {bar} candle: {counts['fired']} setups, {counts['passed']} passed the chart check.")
    if t >= SWING_SCAN and _state().get("swing_scan") != day:
        _set_state(swing_scan=day)
        found = swing_setups(now)
        with _lock:
            _write("queue", day, queue(day) + found)
        _note(day, t, "scan", f"Swing scan: {len(found)} name(s) with the daily and weekly trend in line" + (": " + ", ".join(f"{f['underlying']} {f['direction']}" for f in found) if found else "."))
    if _running["underlying"]:
        return
    with _lock:
        waiting = queue(day)
        if not waiting:
            return
        trig = waiting.pop(0)
        _write("queue", day, waiting)
    if trig["horizon"] == "intraday" and (_minutes(t) - _minutes(trig["at"]) > STALE_MINUTES or t > LAST_INTRADAY):
        _note(day, t, "skip", f"{trig['underlying']} {trig['setup']} dropped: no longer fresh.")
        return
    if len(runs(day)) >= MAX_RUNS:
        _note(day, t, "skip", f"{trig['underlying']} not sent: Claude has done its {MAX_RUNS} runs for today.")
        return
    try:
        start(trig, now)
        _note(day, t, "run", f"Claude is working on {trig['underlying']} ({trig['setup']}, {trig['direction']}, {trig['horizon']}).")
    except ValueError as exc:
        _note(day, t, "skip", f"{trig['underlying']} couldn't start: {exc}")


# ---------------------------------------------------------------- the page and the portfolio

def calibration() -> dict:
    closed = [e for e in positions() if e["status"] == "closed" and e.get("confidence") is not None]
    bands = []
    for lo, hi in BANDS:
        rows = [e for e in closed if lo <= e["confidence"] <= hi]
        won = sum(1 for e in rows if (e["net"] or 0) > 0)
        bands.append({"band": f"{lo}–{hi}%", "trades": len(rows), "won": won, "win_rate": round(100 * won / len(rows), 1) if rows else None, "net": round(sum(e["net"] or 0 for e in rows), 2)})
    return {"trades": len(closed), "needed": CALIBRATION_TRADES, "ready": len(closed) >= CALIBRATION_TRADES, "bands": bands}


def trades() -> list[dict]:
    """Every position in the portfolio page's shape. An open one counts on today, a closed one on its exit day."""
    from strategies import all_desks
    today = datetime.now(common.IST).date().isoformat()
    return [all_desks._trade(e.get("exit_day") or today, e["instrument"], e["key"], {**e, "entry_at": e["entry_at"], "exit_at": e.get("exit_at")}) for e in positions()]


def next_scan(now: datetime) -> dict:
    """When the scanner next sweeps by itself: after the next 5-minute candle in the intraday window, the swing time, or the next trading day's first scan."""
    t, d = now.strftime("%H:%M"), now.date()
    if india.trading_day(d):
        if t < FIRST_SCAN:
            return {"day": d.isoformat(), "at": FIRST_SCAN, "what": "first intraday sweep"}
        if t < LAST_INTRADAY:
            m = _minutes(t) + 5 - _minutes(t) % 5
            return {"day": d.isoformat(), "at": f"{m // 60:02d}:{m % 60:02d}", "what": "intraday sweep after the next candle"}
        if t < SWING_SCAN or _state().get("swing_scan") != d.isoformat() and t <= LAST_SWING:
            return {"day": d.isoformat(), "at": SWING_SCAN, "what": "swing sweep"}
    nxt = d + timedelta(days=1)
    while not india.trading_day(nxt):
        nxt += timedelta(days=1)
    return {"day": nxt.isoformat(), "at": FIRST_SCAN, "what": "first intraday sweep"}


def view(day: str | None = None) -> dict:
    now = datetime.now(common.IST)
    today = now.date().isoformat()
    day = day or today
    rows = positions()
    closed = [e for e in rows if e["status"] == "closed"]
    realized = round(sum(e["net"] or 0 for e in closed), 2)
    open_marks = round(sum(mark(e) for e in rows if e["status"] == "open"), 2)
    shown = [e for e in rows if e["status"] == "open" or e.get("exit_day") == day or e["day"] == day]
    files = sorted(p.name for p in (SKILL_DIR / "references").glob("*.md")) if SKILL_DIR.exists() else []
    sessions = sorted({p.stem[5:] for p in DIR.glob("runs_*.json")} | {today}, reverse=True)[:60] if DIR.exists() else [today]
    return {
        "paper_mode": True, "name": DESK_NAME, "day": day, "today": today, "model": MODEL, "signed_in": bool(claude_tool()), "skill_ready": PLAYBOOK.exists(),
        "market_day": india.trading_day(date.fromisoformat(today)), "running": dict(_running), "rules": rules_text(),
        "live": {"scan": _live["scan"], "run": _live["run"], "next": next_scan(now), "roles": [{"id": k, "label": v} for k, v in ROLES]},
        "skill": {"name": "india-quant-desk", "path": ".claude/skills/india-quant-desk", "references": files},
        "auto": {"first_scan": FIRST_SCAN, "last_intraday": LAST_INTRADAY, "swing_scan": SWING_SCAN, "max_runs": MAX_RUNS, "used": len(runs(day)), "queued": len(queue(day)), "last_bar": _scan["bar"] if day == today else None, "swing_done": _state().get("swing_scan") == day},
        "book": {"capital": CAPITAL, "equity": round(CAPITAL + realized + open_marks, 2), "realized": realized, "open_pnl": open_marks, "today": day_pnl(today, rows), "day_loss": DAY_LOSS, "max_open": MAX_OPEN, "max_risk": MAX_RISK,
                 "open": sum(1 for e in rows if e["status"] == "open"), "closed": len(closed), "won": sum(1 for e in closed if (e["net"] or 0) > 0), "in_use": round(money_in_use(rows), 2)},
        "positions": shown[::-1], "runs": runs(day)[::-1], "activity": activity(day)[::-1][:80], "calibration": calibration(), "sessions": sessions,
    }
