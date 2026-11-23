"""Multi Agent Team: Technical, Risk and Trader on Claude Opus 5.5, through TradingAgents' graph, on a live option snapshot.

Fully automatic in a live market. After every 5-minute candle from 09:35 it scans Nifty, Bank Nifty and the day's biggest F&O
stock movers for the desk's allowed setups, and sends each setup that passes the chart check to the Claude team, one at a time.
A BUY is a paper call: sent to Telegram and, if the paper book has room, taken as a paper trade at the live price and followed
to its stop, target or 15:15. This module has no way to place an order and no order route.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from strategies import agent_desk, common, live_stops, notify, paper_desk, risk_settings, setup_scan

sys.path.insert(0, str(agent_desk.SKILLS / "_india"))
import india  # noqa: E402
import options  # noqa: E402
import setups  # noqa: E402

log = logging.getLogger("kite-desk.multi-agent-team")
ALGO = agent_desk.ALGO_DIR
TA_DIR = ALGO / "TradingAgents"
PYTHON = TA_DIR / ".venv" / "Scripts" / "python.exe"
DIR = paper_desk.CACHE_DIR / "multi_agent_team"
DESK_NAME = "Multi Agent Team"
SETUP = "Claude multi-agent"
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
FIRST_RUN, LAST_RUN = "09:30", "14:30"
ARM_AT = "08:59"
FIRST_SCAN = "09:35"          # the first candle after the opening range has closed
MAX_AUTO_RUNS = 30            # Claude runs a day, each up to three calls on your Pro plan
STOCKS_EACH_WAY = 10          # the day's top F&O movers scanned after each candle, up and down
STALE_MINUTES = 15            # a setup still waiting this long after its candle is dropped
MAX_OPEN = 2                  # open paper trades at a time
MAX_RISK_INR = 3000.0         # the most one Team trade may lose at its stop: room for a stop that sits beyond the chart level
SLIP_INDEX, SLIP_STOCK = 0.003, 0.01     # of the premium, per side
MODE = "options"
SLIP_CASH = 0.0005            # of the share price, per side
CASH_CAPITAL = 100000.0       # the most one cash position may be worth: no leverage
BANDS = ((0, 49), (50, 59), (60, 69), (70, 100))     # the Trader's stated confidence, grouped
CALIBRATION_TRADES, BAND_TRADES = 50, 10             # closed trades needed before a measured win rate replaces the agent's own number
STRIKES_EACH_SIDE = 6
TRAIL_ATR, TRAIL_LEN = 2.0, 14  # the what-if exit: trail the underlying's best close by 2 x ATR(14) of its 5-minute candles
TRAIL_RULE = "Same stop and target, plus an exit when the underlying closes 2 x ATR(14) back from its best close since entry"
TIMEOUT = 420
risk_gate = None              # set by the route: (underlying) -> why no new trade may open, or None
stock_source = None           # set by the route: () -> {"up": [...], "down": [...]}, the day's F&O movers
_lock = threading.RLock()
_running: dict = {"underlying": None, "since": None, "trigger": None}
_scan: dict = {"bar": None}


def claude_tool() -> str | None:
    """The Claude Code tool, if this computer is signed in to it. Checks that the sign-in file exists and never opens it."""
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        return None
    found = shutil.which("claude")
    if found:
        return found
    bundled = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")).glob("Claude-Data/*/claude-code/*/*/claude.exe")
    newest = max(bundled, key=lambda p: p.stat().st_mtime, default=None)
    return str(newest) if newest else None


# ---------------------------------------------------------------- files

def _path(kind: str, day: str) -> Path:
    return DIR / f"{kind}_{day}.json"


def _read(kind: str, day: str, default):
    try:
        return json.loads(_path(kind, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(kind: str, day: str, data) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(kind, day).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_path(kind, day))


def _file(day: str) -> Path:
    return _path("day", day)


def runs(day: str) -> list[dict]:
    return _read("day", day, [])


def _save(day: str, record: dict) -> None:
    with _lock:
        _write("day", day, runs(day) + [record])


def _update(day: str, record: dict) -> None:
    with _lock:
        _write("day", day, [record if r.get("id") == record["id"] else r for r in runs(day)])


def _seen(day: str) -> set:
    return set(_read("seen", day, []))


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


# ---------------------------------------------------------------- scanning

def _candles(bars: list[dict]) -> list[list]:
    return [[b["start"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars]


def trim_chain(chain: dict, each_side: int = STRIKES_EACH_SIDE) -> dict:
    rows = sorted(chain["rows"], key=lambda r: r["strike"])
    i = next(n for n, r in enumerate(rows) if r["strike"] == chain["atm"])
    keep = ("tradingsymbol", "ltp", "bid", "ask", "oi", "volume", "iv", "delta")
    near = [{"strike": r["strike"], **{s: {k: r[s].get(k) for k in keep} for s in ("ce", "pe") if r.get(s)}} for r in rows[max(0, i - each_side):i + each_side + 1]]
    return {k: chain.get(k) for k in ("expiry", "days_to_expiry", "lot_size", "atm", "atm_iv", "pcr", "support", "resistance")} | {"rows": near}


def universe() -> list[tuple[str, str | None]]:
    """(symbol, direction to look for): the two indices both ways, then the day's top F&O movers in the way they're moving."""
    out = [] if MODE == "cash" else [(s, None) for s in UNDERLYINGS]
    try:
        movers = stock_source() if stock_source else {}
    except Exception:  # noqa: BLE001 - the indices still get scanned
        log.exception("F&O movers failed")
        movers = {}
    if isinstance(movers, dict):
        out += [(s, "up") for s in movers.get("up", [])[:STOCKS_EACH_WAY]] + [(s, "down") for s in movers.get("down", [])[:STOCKS_EACH_WAY]]
    return out


def find_triggers(now: datetime) -> tuple[list[dict], dict]:
    """New allowed setups that pass the chart check. Each setup is looked at once a day. -> (triggers, counts)."""
    day = now.date().isoformat()
    seen = _seen(day)
    regime = setup_scan.regime(now.date())
    found, counts = [], {"scanned": 0, "fired": 0, "failed_chart": 0, "no_data": 0}
    for sym, want in universe():
        try:
            fired = setup_scan.find(sym, now.date(), now.replace(tzinfo=None), seen, regime)
            counts["scanned"] += 1
        except india.IndiaError:
            counts["no_data"] += 1
            continue
        for c in fired:
            seen.add(c["key"])
            if want and c["direction"] != want:
                continue
            counts["fired"] += 1
            if not c["technical"]["confirmed"]:
                counts["failed_chart"] += 1
                continue
            found.append({"underlying": sym, "kind": "index" if sym in UNDERLYINGS else "stock", "setup": setup_scan.RULES[c["setup"]], "direction": c["direction"], "at": c["at"], "why": c["why"]})
    _write("seen", day, sorted(seen))
    found.sort(key=lambda t: t["kind"] != "index")      # the indices first
    return found, counts


def _minutes(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


def tick(now: datetime) -> None:
    """Armed from 08:59. After each 5-minute close from 09:35 it scans, then starts the next waiting setup when the team is free."""
    t, day = now.strftime("%H:%M"), now.date().isoformat()
    if not india.trading_day(now.date()):
        return
    if t >= FIRST_SCAN:
        follow_positions(now)
    if not FIRST_SCAN <= t <= LAST_RUN or not risk_settings.settings() or not PYTHON.exists() or not claude_tool():
        return
    bar = common.completed_bar(now)
    if _scan["bar"] is None:                 # after a restart: the candle already scanned today, read back from the log
        _scan["bar"] = last_scanned(day)
    if bar and bar != _scan["bar"]:
        _scan["bar"] = bar
        found, counts = find_triggers(now)
        with _lock:
            _write("queue", day, queue(day) + found)
        _note(day, t, "scan", f"Scanned {counts['scanned']} charts after the {bar} candle: {counts['fired']} setups fired, {counts['fired'] - counts['failed_chart']} passed the chart check."
              + (f" {counts['no_data']} had no candles." if counts["no_data"] else ""))
        for f in found:
            _note(day, t, "setup", f"{f['underlying']}: {f['setup']}, {f['direction']}, at {f['at']}. Waiting for the team.")
    if _running["underlying"]:
        return
    with _lock:
        waiting = queue(day)
        if not waiting:
            return
        trigger = waiting.pop(0)
        _write("queue", day, waiting)
    used = sum(1 for r in runs(day) if r.get("trigger"))
    if _minutes(t) - _minutes(trigger["at"]) > STALE_MINUTES:
        _note(day, t, "skip", f"{trigger['underlying']} {trigger['setup']} dropped: it waited over {STALE_MINUTES} minutes, so it's no longer fresh.")
        return
    if used >= MAX_AUTO_RUNS:
        _note(day, t, "skip", f"{trigger['underlying']} {trigger['setup']} not sent: the team has done its {MAX_AUTO_RUNS} runs for today.")
        return
    try:
        start(trigger["underlying"], now, trigger)
        _note(day, t, "run", f"The team is working on {trigger['underlying']} ({trigger['setup']}, {trigger['direction']}).")
    except ValueError as exc:
        _note(day, t, "skip", f"{trigger['underlying']} {trigger['setup']} couldn't start: {exc}")


# ---------------------------------------------------------------- following a trade on 5-minute candles

FOLLOW_SLIP = 0.003           # of the premium, per side, when a call that wasn't booked is followed for the report


def bars_of(key: str, day: str) -> dict:
    """{HH:MM: (o, h, l, c)}: one session's 5-minute candles for a chart key."""
    days = india.session_bars(india.chart(key, "5d", "5m"), 5)
    return {b["start"]: (b["o"], b["h"], b["l"], b["c"]) for b in days.get(day) or []}


def follow(e: dict, bars: dict, upto: str) -> None:
    """Follows a long call on closed 5-minute bars after its entry: stop first, then target, out at 15:15. Fills e in place."""
    def close(price: float, reason: str, at: str) -> None:
        out = price * (1 - FOLLOW_SLIP)
        fees = india.costs(e["product"], e["entry"] * e["qty"], out * e["qty"])
        gross, risk = (out - e["entry"]) * e["qty"], (e["entry"] - e["stop"]) * e["qty"]
        e.update(status="closed", exit=round(out, 2), exit_at=at, exit_reason=reason, costs=fees, net=round(gross - fees, 2), r=round((gross - fees) / risk, 2) if risk > 0 else None)
    last = None
    for t in common.LABELS:
        if t < e["entry_at"] or t > upto:
            continue
        if t in bars:
            o, h, lo, c = bars[t]
            last = c
            at = common.plus_minutes(t, 5)
            if lo <= e["stop"]:
                return close(min(o, e["stop"]), "stop", at)
            if h >= e["target"]:
                return close(e["target"], "target", at)
        if t == agent_desk.FLAT_BAR and last is not None:
            return close(last, "15:15 exit", "15:15")
    return None


# ---------------------------------------------------------------- one run

def snapshot(underlying: str, now: datetime) -> dict:
    """Everything the Claude nodes read, as of the last closed candles."""
    day = now.date().isoformat()
    cut = now.replace(tzinfo=None)
    five = india.session_bars(india.chart(underlying, "5d", "5m"), 5, cut).get(day) or []
    india.apply_volumes(five, india.future_volumes(underlying, 5, "5d"))
    one = india.session_bars(india.chart(underlying, "1d", "1m"), 1, cut).get(day) or []
    if len(five) < 4:
        raise ValueError("Too few candles today yet.")
    try:
        vix = float(india.chart("INDIAVIX", "1d", "1d")["meta"]["regularMarketPrice"])
    except (india.IndiaError, KeyError, TypeError, ValueError):
        vix = None
    vw = setups.vwap(five)[-1]
    s = risk_settings.settings() or {}
    if MODE == "cash":
        spot = float(india.chart(underlying, "1d", "1d")["meta"]["regularMarketPrice"])
        return {"market": "cash", "underlying": underlying, "instrument_type": "stock", "as_of": now.strftime("%Y-%m-%d %H:%M"), "spot": spot, "candles_5m": _candles(five), "candles_1m": _candles(one[-30:]), "vwap": None if vw is None else round(vw, 2), "opening_range": {"high": max(b["h"] for b in five[:3]), "low": min(b["l"] for b in five[:3])}, "india_vix": vix, "limits": {"capital": CASH_CAPITAL, "max_risk_inr": MAX_RISK_INR, "daily_loss_inr": s.get("daily_loss_inr")}}
    chain = options.get_chain(underlying, dict(india.load_config(None), allow_options=True))
    return {"underlying": underlying, "instrument_type": "index" if underlying in UNDERLYINGS else "stock", "as_of": now.strftime("%Y-%m-%d %H:%M"), "spot": chain.get("spot"), "candles_5m": _candles(five), "candles_1m": _candles(one[-30:]), "vwap": None if vw is None else round(vw, 2), "opening_range": {"high": max(b["h"] for b in five[:3]), "low": min(b["l"] for b in five[:3])}, "india_vix": vix, "option_chain": trim_chain(chain), "limits": {"capital": s.get("capital"), "max_risk_inr": MAX_RISK_INR, "daily_loss_inr": s.get("daily_loss_inr")}}


def _live_file() -> Path:
    return DIR / "live.json"


def live() -> dict | None:
    """The run in progress, with each agent's report as soon as it has one. This is what lets the page show the agents at work."""
    if not _running["underlying"]:
        return None
    try:
        so_far = json.loads(_live_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        so_far = {"stage": "snapshot"}
    return {**so_far, "underlying": _running["underlying"], "since": _running["since"], "trigger": _running.get("trigger")}


def run_graph(context: dict, tool: str) -> dict:
    """The graph, in TradingAgents' own environment, on the signed-in Claude Pro account."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "USERNAME", "COMPUTERNAME")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", CLAUDE_PRO_TOOL=tool, INTRADAY_PROGRESS=str(_live_file()))
    gemini = notify._secret("GEMINI_API_KEY")    # Gemini, the Challenger: from signal_bot/secrets.env, never logged; without it the step is skipped
    if gemini:
        env["GEMINI_API_KEY"] = gemini
    p = subprocess.run([str(PYTHON), "-m", "tradingagents.intraday_cli"], input=json.dumps(context).encode("utf-8"), cwd=TA_DIR, env=env, capture_output=True, timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(p.stdout.decode("utf-8"))
    except ValueError:
        return {"error": "The Claude team gave no readable answer."}


def calibration() -> dict:
    """Every closed paper trade's stated confidence against what happened. This is what tells you whether the number means anything."""
    days = sorted(p.stem[4:] for p in DIR.glob("day_*.json")) if DIR.exists() else []
    closed = [e for d in days for e in positions(d) if e["status"] == "closed" and e.get("confidence") is not None]
    bands = []
    for lo, hi in BANDS:
        rows = [e for e in closed if lo <= e["confidence"] <= hi]
        won = sum(1 for e in rows if (e["net"] or 0) > 0)
        bands.append({"band": f"{lo}–{hi}%", "lo": lo, "hi": hi, "trades": len(rows), "won": won, "win_rate": round(100 * won / len(rows), 1) if rows else None, "net": round(sum(e["net"] or 0 for e in rows), 2)})
    return {"trades": len(closed), "needed": CALIBRATION_TRADES, "band_needed": BAND_TRADES, "ready": len(closed) >= CALIBRATION_TRADES, "bands": bands}


def what_if_all() -> dict:
    """The ATR-trail what-if over every recorded day, so the comparison builds up as the weeks go by."""
    days = sorted(p.stem[4:] for p in DIR.glob("day_*.json")) if DIR.exists() else []
    held = [e for d in days for e in positions(d)]
    return what_if(held) | {"days": len({d for d in days if any(e.get("shadow") for e in positions(d))})}


def confidence_text(stated) -> str | None:
    """The agent's own number, until enough trades have closed to say how often calls at that level actually won."""
    if stated is None:
        return None
    c = calibration()
    band = next((b for b in c["bands"] if b["lo"] <= stated <= b["hi"]), None)
    if c["ready"] and band and band["trades"] >= BAND_TRADES:
        return f"{band['win_rate']:.0f}% · measured"
    return f"{stated}% · AI view"


def _announce(record: dict, day: str, now: datetime) -> None:
    p = record.get("trade_payload") or {}
    if p.get("action") not in ("BUY", "SELL"):
        return
    notify.send_once(f"{DIR.name}|{record['id']}", notify.signal_text({"desk": DESK_NAME, "instrument": p["instrument"], "side": p["action"], "setup": SETUP, "time": record["at"], "date": day, "entry": p["entry"], "stop": p["stop_loss"], "target": p["target"], "qty": p["quantity"], "lots": p.get("lots"), "max_loss": p.get("max_loss"), "expiry": p.get("expiry"), "confidence": confidence_text(p.get("confidence"))}), now)


def _work(underlying: str, now: datetime, tool: str, trigger: dict | None = None) -> None:
    day = now.date().isoformat()
    record = {"id": f"{day}|{underlying}|{now.strftime('%H:%M:%S')}", "date": day, "at": now.strftime("%H:%M"), "underlying": underlying, "trigger": trigger}
    DIR.mkdir(parents=True, exist_ok=True)
    _live_file().unlink(missing_ok=True)
    try:
        context = dict(snapshot(underlying, now), trigger=trigger)
        chain = context.get("option_chain") or {}
        record["market"] = {"spot": context["spot"], "vwap": context["vwap"], "india_vix": context["india_vix"], "opening_range": context["opening_range"], "atm": chain.get("atm"), "expiry": chain.get("expiry"), "pcr": chain.get("pcr"), "lot_size": chain.get("lot_size"), "max_risk_inr": context["limits"]["max_risk_inr"]}
        record.update(run_graph(context, tool))
    except subprocess.TimeoutExpired:
        record["error"] = "The Claude team took too long."
    except (india.IndiaError, ValueError, KeyError, StopIteration) as exc:
        record["error"] = str(exc) or type(exc).__name__
    except Exception:  # noqa: BLE001 - a failed run is shown on the page and must not kill the thread
        log.exception("multi agent team run failed")
        record["error"] = "The run failed. See the backend log."
    finally:
        try:
            _take(record, day, datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a paper fill that fails is noted, never fatal
            log.exception("paper fill failed")
            record["paper"] = {"taken": False, "why": "the live price couldn't be read."}
        _save(day, record)
        _running.update(underlying=None, since=None, trigger=None)
        _live_file().unlink(missing_ok=True)
    _announce(record, day, now)
    p = record.get("trade_payload") or {}
    what = "failed: " + record["error"] if record.get("error") else (f"{p['action']} {p['instrument']}" if p.get("action") in ("BUY", "SELL") else "HOLD: " + str(p.get("rationale") or ""))
    paper = record.get("paper") or {}
    _note(day, datetime.now(common.IST).strftime("%H:%M"), "result", f"{underlying}: {what[:220]}" + (" Paper trade taken." if paper.get("taken") else (f" Not paper-traded: {paper['why']}" if paper.get("why") else "")))


def start(underlying: str, now: datetime | None = None, trigger: dict | None = None) -> None:
    """Starts one run in the background. Raises ValueError with the reason if it can't."""
    now = now or datetime.now(common.IST)
    if underlying not in UNDERLYINGS and not trigger:
        raise ValueError("Choose NIFTY or BANKNIFTY.")
    if not india.trading_day(now.date()) or not FIRST_RUN <= now.strftime("%H:%M") <= LAST_RUN:
        raise ValueError(f"The team runs on trading days from {FIRST_RUN} to {LAST_RUN} IST.")
    if not risk_settings.settings():
        raise ValueError("Set your paper capital and risk under Team risk settings on the Portfolio & Risk page first.")
    if not PYTHON.exists():
        raise ValueError("TradingAgents' environment isn't installed.")
    tool = claude_tool()
    if not tool:
        raise ValueError("Claude isn't signed in on this computer.")
    with _lock:
        if _running["underlying"]:
            raise ValueError(f"A run on {_running['underlying']} is still going.")
        _running.update(underlying=underlying, since=now.isoformat(timespec="seconds"), trigger=trigger)
    threading.Thread(target=_work, args=(underlying, now, tool, trigger), daemon=True, name="multi-agent-team").start()


# ---------------------------------------------------------------- the paper book

def _slip(e: dict) -> float:
    if e.get("kind") == "cash":
        return SLIP_CASH
    return SLIP_INDEX if e.get("kind", "index") == "index" else SLIP_STOCK


def _dir(e: dict) -> int:
    return -1 if e.get("side") == "SHORT" else 1


def _close(e: dict, price: float, reason: str, at: str) -> None:
    d = _dir(e)
    out = price * (1 - d * _slip(e))
    bought, sold = (e["entry"], out) if d > 0 else (out, e["entry"])
    fees = india.costs("equity_intraday" if e.get("kind") == "cash" else "options", bought * e["qty"], sold * e["qty"])
    gross = (out - e["entry"]) * e["qty"] * d
    risk = (e["entry"] - e["stop"]) * e["qty"] * d
    e.update(status="closed", exit=round(out, 2), exit_at=at, exit_reason=reason, costs=round(fees, 2), net=round(gross - fees, 2), r=round((gross - fees) / risk, 2) if risk > 0 else None)


def walk(e: dict, bars: dict, upto: str) -> None:
    """Follows one open paper trade on closed 5-minute bars from its entry bar: stop first, then target, out at 15:15. bars: {HH:MM: (o, h, l, c)}."""
    for t in common.LABELS:
        if t < e["entry_bar"] or t > upto:
            continue
        if t in bars:
            o, h, lo, c = bars[t]
            e["ltp"] = c
            at = common.plus_minutes(t, 5)
            if _dir(e) < 0:
                if h >= e["stop"]:
                    _close(e, max(o, e["stop"]), "stop", at)
                    return
                if lo <= e["target"]:
                    _close(e, e["target"], "target", at)
                    return
            elif lo <= e["stop"]:
                _close(e, min(o, e["stop"]), "stop", at)
                return
            elif h >= e["target"]:
                _close(e, e["target"], "target", at)
                return
        if t == agent_desk.FLAT_BAR and e.get("ltp") is not None:
            _close(e, e["ltp"], "15:15 exit", "15:15")
            return


def _atr(bars: list[dict], n: int = TRAIL_LEN) -> float | None:
    """Wilder's average true range of these candles (dicts with h, l, c), or None if there are too few."""
    if len(bars) < n + 1:
        return None
    tr = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    avg = sum(tr[:n]) / n
    for x in tr[n:]:
        avg = (avg * (n - 1) + x) / n
    return avg


def _trend_dir(e: dict) -> int:
    """Which way the underlying has to go for this trade to win: a put needs it to fall, a short needs the share to fall."""
    if e.get("kind") == "cash":
        return _dir(e)
    return -1 if str(e["instrument"]).endswith("PE") else 1


def shadow(e: dict, bars: dict, under: list[dict], day: str, upto: str) -> dict:
    """The ATR-trail what-if for one paper trade, worked out from scratch: the same entry, stop, target and 15:15 exit, plus an
    exit at the trade's own candle close once the underlying closes TRAIL_ATR x ATR back from its best close since entry. It only
    records what would have happened: the real trade is never changed by it. bars: the traded instrument's {HH:MM: (o, h, l, c)};
    under: the underlying's 5-minute candles for this day and the days before (dicts with date, start, h, l, c)."""
    before = [b for b in under if b["date"] < day or (b["date"] == day and b["start"] < e["entry_bar"])]
    atr = _atr(before)
    out = {"rule": TRAIL_RULE, "status": "open", "atr": None if atr is None else round(atr, 2)}
    if atr is None or not before:
        return out | {"status": "none", "why": "too few candles for ATR"}
    today = {b["start"]: b["c"] for b in under if b["date"] == day}
    w = _trend_dir(e)
    best = before[-1]["c"]
    x = {k: e.get(k) for k in ("entry", "stop", "target", "qty", "kind", "side", "entry_bar")} | {"status": "open", "ltp": None}
    for t in common.LABELS:
        if t < x["entry_bar"] or t > upto:
            continue
        if t in bars:
            o, h, lo, c = bars[t]
            x["ltp"] = c
            at = common.plus_minutes(t, 5)
            if _dir(x) < 0:
                if h >= x["stop"]:
                    _close(x, max(o, x["stop"]), "stop", at)
                    break
                if lo <= x["target"]:
                    _close(x, x["target"], "target", at)
                    break
            elif lo <= x["stop"]:
                _close(x, min(o, x["stop"]), "stop", at)
                break
            elif h >= x["target"]:
                _close(x, x["target"], "target", at)
                break
            if t in today:
                best = max(best, today[t]) if w > 0 else min(best, today[t])
                out["level"] = round(best - w * TRAIL_ATR * atr, 2)
                if w * (today[t] - out["level"]) < 0:
                    _close(x, c, "ATR trail", at)
                    break
        if t == agent_desk.FLAT_BAR and x.get("ltp") is not None:
            _close(x, x["ltp"], "15:15 exit", "15:15")
            break
    return out | {k: x.get(k) for k in ("status", "exit", "exit_at", "exit_reason", "net")}


def _under_bars(e: dict, day: str) -> list[dict]:
    sym = e["instrument"] if e.get("kind") == "cash" else e["underlying"]
    s = india.session_bars(india.chart(sym, "5d", "5m"), 5, datetime.fromisoformat(f"{day}T15:30"))
    return [b for d in sorted(s) if d <= day for b in s[d] if b.get("c") is not None]


def _day_result(held: list[dict]) -> float:
    """Today's result so far: closed trades' net plus open trades marked at their last price."""
    total = 0.0
    for e in held:
        if e["status"] == "closed":
            total += e["net"] or 0
        elif e.get("ltp") is not None:
            total += (e["ltp"] * (1 - _dir(e) * _slip(e)) - e["entry"]) * e["qty"] * _dir(e)
    return total


def positions(day: str) -> list[dict]:
    return [r["position"] for r in runs(day) if r.get("position")]


def _entry_bar(hm: str) -> str:
    """The first 5-minute candle that starts after this time."""
    m = int(hm[3:5])
    return common.plus_minutes(f"{hm[:3]}{m - m % 5:02d}", 5)


def _take(record: dict, day: str, now: datetime) -> None:
    """A BUY becomes a paper trade at the live price, if the book has room and the day's loss limit isn't used up."""
    p = record.get("trade_payload") or {}
    if p.get("action") not in ("BUY", "SELL"):
        return
    s = risk_settings.settings() or {}
    held = positions(day)
    if sum(1 for e in held if e["status"] == "open") >= MAX_OPEN:
        record["paper"] = {"taken": False, "why": f"{MAX_OPEN} paper trades are already open."}
        return
    if _day_result(held) <= -float(s.get("daily_loss_inr") or 0):
        record["paper"] = {"taken": False, "why": "today's loss limit is used up."}
        return
    blocked = risk_gate(record["underlying"], "shares" if MODE == "cash" else "option") if risk_gate else None
    if blocked:
        record["paper"] = {"taken": False, "why": blocked + "."}
        return
    cash = MODE == "cash"
    key, chart_key = (f"NSE:{p['instrument']}", p["instrument"]) if cash else (f"NFO:{p['instrument']}",) * 2
    d = -1 if p["action"] == "SELL" else 1
    last = float(india.chart(chart_key, "1d", "1d")["meta"]["regularMarketPrice"])
    if (last - p["stop_loss"]) * d <= 0 or (p["target"] - last) * d <= 0:
        record["paper"] = {"taken": False, "why": f"the price had moved to {last:.2f}, past the stop or target, while the team was working."}
        return
    kind = "cash" if cash else ("index" if record["underlying"] in UNDERLYINGS else "stock")
    hm = now.strftime("%H:%M")
    e = {"id": record["id"], "instrument": p["instrument"], "underlying": record["underlying"], "key": key, "kind": kind, "qty": p["quantity"], "lots": p.get("lots"), "planned_entry": p["entry"], "stop": p["stop_loss"], "target": p["target"], "entry_at": hm, "entry_bar": _entry_bar(hm), "status": "open", "ltp": last, "confidence": p.get("confidence"), "side": "SHORT" if d < 0 else "LONG", "chart_key": chart_key}
    e["entry"] = round(last * (1 + d * _slip(e)), 2)
    record["position"] = e
    record["paper"] = {"taken": True}


def live_check(e: dict, now: datetime) -> None:
    """Closes an open trade the moment its last traded price crosses the stop or the target, without waiting for the candle."""
    if now.strftime("%H:%M") >= "15:15":      # the 15:15 exit belongs to the candle walk
        return
    px = live_stops.prices([e["key"]], now).get(e["key"])
    if px is None:
        return
    e["ltp"] = px
    got = live_stops.hit(px, e["stop"], e.get("target"), short=_dir(e) < 0)
    if got:
        _close(e, got[1], got[0], now.strftime("%H:%M"))


def follow_positions(now: datetime) -> None:
    """Moves every open paper trade on to the last closed candle. A trade left open on an earlier day is followed to that day's 15:15."""
    today = now.date().isoformat()
    bar = common.completed_bar(now)
    files = sorted(DIR.glob("day_*.json"))[-5:] if DIR.exists() else []
    for f in files:
        day = f.stem[4:]
        for r in runs(day):
            e = r.get("position")
            if not e or (day == today and not bar):
                continue
            what_if = (e.get("shadow") or {}).get("status") not in ("closed", "none")
            if e["status"] != "open" and not what_if:
                continue
            before = json.dumps(e, sort_keys=True)
            upto = bar if day == today else agent_desk.FLAT_BAR
            try:
                bars = bars_of(e.get("chart_key") or e["key"], day)
                if e["status"] == "open":
                    walk(e, bars, upto)
            except Exception:  # noqa: BLE001 - try again on the next pass
                log.warning("no candles for %s", e["instrument"])
                bars = None
            if e["status"] == "open" and day == today:
                live_check(e, now)
            if bars is None:
                if json.dumps(e, sort_keys=True) != before:
                    _update(day, r)
                    if e["status"] == "closed":
                        _note(day, now.strftime("%H:%M"), "exit", f"{e['instrument']} closed at {e['exit']:.2f} ({e['exit_reason']}): net ₹{e['net']:,.2f}.")
                continue
            if what_if:
                try:
                    e["shadow"] = shadow(e, bars, _under_bars(e, day), day, upto)
                except Exception:  # noqa: BLE001 - the what-if never holds up the real trade
                    log.warning("no ATR what-if for %s", e["instrument"])
            if json.dumps(e, sort_keys=True) == before:
                continue
            _update(day, r)
            if e["status"] == "closed":
                _note(day, now.strftime("%H:%M"), "exit", f"{e['instrument']} closed at {e['exit']:.2f} ({e['exit_reason']}): net ₹{e['net']:,.2f}.")


def book(day: str) -> dict:
    held = positions(day)
    closed = [e for e in held if e["status"] == "closed"]
    return {"positions": held, "realized": round(sum(e["net"] or 0 for e in closed), 2), "open": sum(1 for e in held if e["status"] == "open"), "closed": len(closed), "won": sum(1 for e in closed if (e["net"] or 0) > 0), "max_open": MAX_OPEN, "slip": {"index": SLIP_INDEX, "stock": SLIP_STOCK, "cash": SLIP_CASH},
            "what_if": what_if(held)}


def what_if(held: list[dict]) -> dict:
    """The ATR-trail exit against the real one, on the trades where both are finished."""
    both = [e for e in held if e["status"] == "closed" and (e.get("shadow") or {}).get("status") == "closed"]
    return {"rule": TRAIL_RULE, "trades": len(both), "actual": round(sum(e["net"] or 0 for e in both), 2), "trail": round(sum(e["shadow"]["net"] or 0 for e in both), 2),
            "better": sum(1 for e in both if (e["shadow"]["net"] or 0) > (e["net"] or 0)), "worse": sum(1 for e in both if (e["shadow"]["net"] or 0) < (e["net"] or 0))}


def view(day: str | None = None) -> dict:
    today = datetime.now(common.IST).date().isoformat()
    day = day or today
    rows = runs(day)
    return {"paper_mode": True, "mode": MODE, "name": DESK_NAME, "day": day, "today": today, "running": dict(_running), "live": live(), "installed": PYTHON.exists(), "signed_in": bool(claude_tool()), "settings_ready": bool(risk_settings.settings()), "market_day": india.trading_day(datetime.fromisoformat(today).date()), "model": "claude-opus-5-5", "window": [FIRST_SCAN, LAST_RUN],
            "auto": {"armed_from": ARM_AT, "first_scan": FIRST_SCAN, "last_scan": LAST_RUN, "max_runs": MAX_AUTO_RUNS, "used": sum(1 for r in rows if r.get("trigger")), "queued": len(queue(day)), "stocks_each_way": STOCKS_EACH_WAY, "last_bar": _scan["bar"] if day == today else None},
            "book": book(day), "calibration": calibration(), "what_if_all": what_if_all(), "activity": activity(day)[::-1][:80], "runs": rows[::-1], "sessions": sorted((p.stem[4:] for p in DIR.glob("day_*.json")), reverse=True)[:60] if DIR.exists() else []}
