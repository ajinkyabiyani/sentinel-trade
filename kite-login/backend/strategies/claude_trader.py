"""Claude Trader: Claude trading options on its own. A desk of its own, separate from every other desk in the app: its own
Claude call, prompt, rules, paper book, trade following and Telegram messages. No configured strategy and no setup scanner.

Every 15 minutes Claude reads the market (Nifty, Bank Nifty, India VIX, the day's biggest F&O movers with their candles and
option chains, and the latest headlines) and decides on its own judgement whether to buy a call or a put now, or to stay out.
Intraday option buying only, on paper: real Kite prices, simulated fills.

The code never picks a trade. It only enforces risk on Claude's answer, follows the trade on live prices and candles (stop,
target, 15:15) and sends Telegram the booked entry and the booked exit. Nothing here can place a real order.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import common, fno_flow, live_stops, notify, paper_desk

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills" / "_india"))
import india  # noqa: E402  - the app's shared market-data reader (charts, chains, news through the backend)
import setups  # noqa: E402

log = logging.getLogger("kite-desk.claude-trader")

DESK_ID, DESK_NAME = "claude_trader", "Claude Trader"
DIR = paper_desk.CACHE_DIR / DESK_ID
MODEL, EFFORT, TIMEOUT = "claude-opus-5-5", "high", 300

CAPITAL = 200000.0
MAX_RISK = 4500.0             # rupees lost at the stop, with charges
MAX_OPEN = 4
DAY_LOSS = 9000.0             # realised plus open marks: past this, no new trade today
MAX_PREMIUM = 100000.0        # one position's premium
MIN_RR = 1.5
MAX_STRIKES_AWAY = 2
MAX_SPREAD = 3.0              # bid to ask, % of the mid price
MIN_OI_LOTS = 25              # stock options
FIRST_RUN, LAST_RUN, FLAT_AT = "09:30", "14:30", "15:15"
EVERY = 15                    # minutes between Claude's looks at the market
LATE = 10                     # a look more than this many minutes late is skipped, not run late
LATE_EXPIRY = "13:00"         # on expiry day, after this the next expiry is used
INDICES = ("NIFTY", "BANKNIFTY")
MOVERS_EACH_WAY = 4           # F&O stocks rising most and falling most today, with their option chains
STRIKES = 5
HEADLINES = 25
SLIP = {"index": 0.003, "stock": 0.01}
# Early looks: between the 15-minute looks the code checks every closed 5-minute candle and wakes Claude early on a real event
EARLY_FROM = "09:35"
EARLY_MAX = 8                 # early looks a day, on top of the 21 scheduled ones
EARLY_GAP = 7                 # minutes: no early look this soon after any look; a scheduled look this soon after an early one is skipped
BIG_CANDLE = 3.0              # a candle at least this many times today's median candle range...
BIG_MIN_PCT = {"index": 0.25, "stock": 0.8}   # ...and at least this % of the price
VIX_JUMP = 3.0                # % rise in India VIX since the last check

stock_source = None           # set by the route: () -> {"up": [...], "down": [...]}, the day's F&O movers
fno_source = None             # set by the route: () -> every F&O stock's name, for the wide candle watch
_lock = threading.RLock()
_live: dict = {"run": None}
_running: dict = {"since": None}
_usage: dict = {}             # the last Claude call's tokens and the tool's own cost estimate

STEPS = [("market", "Market read"), ("candidates", "Candidates"), ("choice", "Choice"), ("option", "Option"), ("risk", "Risk")]
VERDICTS = ("supports", "against", "neutral", "veto")


def inr(x: float) -> str:
    s = f"{abs(x):.0f}"
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail, head = head[-2:] + "," + tail, head[:-2]
    return ("−" if x < 0 else "") + "₹" + (head + "," + tail if head else tail)


def mins(hm: str) -> int:
    return int(hm[:2]) * 60 + int(hm[3:5])


def rules_text() -> list[str]:
    return [
        "No configured strategy: every 15 minutes from 09:30 to 14:30 Claude reads the market and decides on its own whether to buy a call or a put, or to stay out.",
        "Separate from every other desk: its own Claude call, rules, paper book and Telegram messages.",
        f"Early looks: between the 15-minute looks the code checks every closed 5-minute candle and wakes Claude early when Nifty or Bank Nifty closes through its opening range or the previous day's high/low, any F&O stock prints a candle {BIG_CANDLE:g}x its usual size (the indices and top movers on their real candles, every other F&O stock on candles built from its last price every 15 seconds), or India VIX jumps {VIX_JUMP:g}%, or a sector turns: it moves {SECTOR_MOVE:g}% in an hour and pulls {SECTOR_VS_NIFTY:g}% away from Nifty, or crosses from red to green or back (its two strongest F&O stocks join that look), or a stock's futures show fresh build-up (OI up {fno_flow.OI_HOUR:g}% in an hour with the price moving {fno_flow.PRICE_HOUR:g}%), or a stock trades {fno_flow.VOL_SURGE:g}x its usual 5-minute volume while moving {fno_flow.VOL_MIN_MOVE:g}%. At most {EARLY_MAX} a day, never within {EARLY_GAP} minutes of another look.",
        "Paper only, intraday option buying only. Real Kite prices, simulated fills with slippage (index 0.3%, stock 1% of the premium) and full Zerodha charges.",
        f"Loss at the stop at most {inr(MAX_RISK)} with charges: the code sizes the trade in whole lots. At most {inr(MAX_PREMIUM)} of premium a trade.",
        f"Stop below and target above the live premium, reward at least {MIN_RR}x risk. A contract from the chain Claude was shown, within {MAX_STRIKES_AWAY} strikes of the money, spread under {MAX_SPREAD:g}%, stock options with {MIN_OI_LOTS}+ lots of open interest.",
        f"Up to {MAX_OPEN} open trades, one per underlying. A name that hits its stop is off limits for the rest of the day. No new trade after a {inr(DAY_LOSS)} loss in the day.",
        "Every read also carries the F&O flow: breadth across F&O stocks, futures OI build-up (near plus next month) today and in the last hour, and recent volume surges.",
        f"Each trade is checked on the live price every 15 seconds (stop and target) and on 5-minute candles; anything open closes at {FLAT_AT}.",
        "Telegram gets the booked entry and the booked exit, nothing else.",
    ]


def stopped_today(day: str, rows: list[dict]) -> list[str]:
    """Underlyings whose trade hit its stop that day: off limits until tomorrow."""
    return sorted({e["underlying"] for e in rows if e["status"] == "closed" and e.get("exit_reason") == "stop" and (e.get("exit_day") or e["day"]) == day})


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


def activity(day: str) -> list[dict]:
    return _read("activity", day, [])


def _note(day: str, at: str, kind: str, text: str) -> None:
    with _lock:
        _write("activity", day, (activity(day) + [{"at": at, "kind": kind, "text": text}])[-300:])


def _state() -> dict:
    return _read("state", None, {})


def _set_state(**kw) -> None:
    with _lock:
        _write("state", None, {**_state(), **kw})


# ---------------------------------------------------------------- money

def fees(buy_value: float, sell_value: float) -> float:
    return common.charges([(True, buy_value), (False, sell_value)])


def mark(e: dict) -> float:
    """An open trade at its last price after exit slippage, before exit charges. A closed one at its net."""
    if e["status"] == "closed":
        return e.get("net") or 0.0
    px = e.get("ltp") if e.get("ltp") is not None else e["entry"]
    return round((px * (1 - e["slip"]) - e["entry"]) * e["qty"], 2)


def day_pnl(day: str, rows: list[dict]) -> float:
    return round(sum(mark(e) for e in rows if e["status"] == "open" or e.get("exit_day") == day), 2)


# ---------------------------------------------------------------- the schedule

def slots() -> list[str]:
    out, m = [], mins(FIRST_RUN)
    while m <= mins(LAST_RUN):
        out.append(f"{m // 60:02d}:{m % 60:02d}")
        m += EVERY
    return out


def due_slot(now: datetime) -> str | None:
    """The look due now: the latest slot at or before now, if it hasn't run and is at most LATE minutes old."""
    t = now.strftime("%H:%M")
    past = [s for s in slots() if s <= t]
    if not past or mins(t) - mins(past[-1]) > LATE:
        return None
    st = _state()
    return None if st.get("day") == now.date().isoformat() and past[-1] in (st.get("done") or []) else past[-1]


def next_run(now: datetime) -> dict:
    t, d = now.strftime("%H:%M"), now.date()
    if india.trading_day(d):
        for s in slots():
            if s > t:
                return {"day": d.isoformat(), "at": s}
    nxt = d + timedelta(days=1)
    while not india.trading_day(nxt):
        nxt += timedelta(days=1)
    return {"day": nxt.isoformat(), "at": FIRST_RUN}


# ---------------------------------------------------------------- what Claude reads

def _movers() -> dict:
    try:
        m = stock_source() if stock_source else {}
    except Exception:  # noqa: BLE001 - the indices are still read
        log.exception("F&O movers failed")
        m = {}
    return m if isinstance(m, dict) else {}


def universe(extra=()) -> list[str]:
    """The indices, the day's top movers each way, and any stock the wide watch just flagged."""
    m = _movers()
    out = list(INDICES)
    for s in (m.get("up") or [])[:MOVERS_EACH_WAY] + (m.get("down") or [])[:MOVERS_EACH_WAY] + list(extra):
        if s not in out:
            out.append(s)
    return out


def fetch_chain(sym: str, expiry: str | None = None) -> dict:
    from urllib.parse import quote
    return india.app_get(f"/api/options/chain?underlying={quote(sym)}&strikes=12" + (f"&expiry={expiry}" if expiry else ""))


def nearest_chain(sym: str, now: datetime) -> dict | None:
    """The nearest expiry's chain; on expiry day after LATE_EXPIRY, the next one."""
    first = fetch_chain(sym)
    today = now.date()
    live = [(e, (date.fromisoformat(e) - today).days) for e in (first.get("expiries") or [])]
    live = [(e, d) for e, d in live if d > 0 or (d == 0 and now.strftime("%H:%M") < LATE_EXPIRY)]
    if not live:
        return None
    exp, dte = live[0]
    chain = first if first.get("expiry") == exp else fetch_chain(sym, exp)
    return dict(chain, days_to_expiry=dte)


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def greeks(fwd: float, k: float, t: float, iv_pct: float | None, call: bool, rate: float = 0.06) -> dict:
    """Black-76 per unit: theta per session (calendar-day theta x 365/252), gamma per ₹1 of the underlying, vega per IV point."""
    if not iv_pct or iv_pct <= 0 or t <= 0 or fwd <= 0 or k <= 0:
        return {}
    v = iv_pct / 100
    st = v * math.sqrt(t)
    d1 = (math.log(fwd / k) + 0.5 * v * v * t) / st
    d2 = d1 - st
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    disc = math.exp(-rate * t)
    price = disc * (fwd * _ncdf(d1) - k * _ncdf(d2)) if call else disc * (k * _ncdf(-d2) - fwd * _ncdf(-d1))
    theta_year = -disc * fwd * pdf * v / (2 * math.sqrt(t)) + rate * price
    return {"theta_day": round(theta_year / 252, 2), "gamma": round(disc * pdf / (fwd * st), 6), "vega": round(disc * fwd * pdf * math.sqrt(t) / 100, 2)}


def trim_chain(chain: dict, each_side: int = STRIKES) -> dict:
    """ATM +/- each_side strikes, with each contract's Greeks worked out from its own IV."""
    rows = sorted(chain["rows"], key=lambda r: r["strike"])
    i = next((n for n, r in enumerate(rows) if r["strike"] == chain["atm"]), len(rows) // 2)
    keep = ("tradingsymbol", "ltp", "bid", "ask", "oi", "volume", "iv", "delta")
    fwd = chain.get("forward") or chain.get("future") or chain.get("spot") or chain["atm"]
    t = max((chain.get("days_to_expiry") or 0) / 365.25, 0.5 / 365.25)
    near = []
    for r in rows[max(0, i - each_side):i + each_side + 1]:
        row = {"strike": r["strike"]}
        for side in ("ce", "pe"):
            o = r.get(side)
            if not o:
                continue
            leg = {k: o.get(k) for k in keep}
            bid, ask_ = o.get("bid") or 0, o.get("ask") or 0
            leg["spread_pct"] = round((ask_ - bid) / ((ask_ + bid) / 2) * 100, 1) if bid and ask_ else None
            leg.update(greeks(float(fwd), float(r["strike"]), t, o.get("iv"), side == "ce", float(chain.get("rate") or 0.06)))
            row[side] = leg
        near.append(row)
    return {k: chain.get(k) for k in ("expiry", "days_to_expiry", "lot_size", "atm", "atm_iv", "pcr", "support", "resistance", "max_pain", "straddle", "implied_move_pct", "forward")} | {"rows": near}


def realised_vol(closes: list[float]) -> float | None:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    if len(rets) < 15:
        return None
    m = sum(rets) / len(rets)
    return round(math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252) * 100, 2)


def _daily(sym: str) -> list[dict]:
    res = india.chart(sym, "3mo", "1d")
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        h, lo, c = (q.get("high") or [None])[i], (q.get("low") or [None])[i], (q.get("close") or [None])[i]
        if None not in (h, lo, c):
            out.append({"date": (datetime(1970, 1, 1) + timedelta(seconds=int(ts) + 19800)).date().isoformat(), "h": float(h), "l": float(lo), "c": float(c)})
    return out


def minutes_left(now: datetime) -> int:
    return max(0, mins("15:30") - mins(now.strftime("%H:%M")))


def read_underlying(sym: str, now: datetime) -> dict:
    """One underlying: today's 5-minute candles so far, yesterday's levels, VWAP and the nearest-expiry option chain."""
    day, cut = now.date().isoformat(), now.replace(tzinfo=None)
    days = india.session_bars(india.chart(sym, "5d", "5m"), 5, cut)
    today = days.get(day) or []
    if len(today) < 3:
        raise ValueError("too few candles today")
    prev = [d for d in sorted(days) if d < day]
    y = days[prev[-1]] if prev else []
    vw = setups.vwap(today)[-1]
    try:
        chain = nearest_chain(sym, now)
    except Exception:  # noqa: BLE001 - without a chain this name can't be bought; Claude sees that
        chain = None
    spot = today[-1]["c"]
    try:
        daily = [d for d in _daily(sym) if d["date"] < day]
    except Exception:  # noqa: BLE001 - the intraday read still stands
        daily = []
    rv = realised_vol([d["c"] for d in daily[-21:]])
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(daily[-15:], daily[-14:])]
    atr = sum(trs) / len(trs) if len(trs) >= 10 else None
    iv = (chain or {}).get("atm_iv")
    left = minutes_left(now)
    vol = {"atm_iv": iv, "realised_vol_20d": rv, "iv_to_rv": round(iv / rv, 2) if iv and rv else None,
           "atr_14d": None if atr is None else round(atr, 2),
           "today_range_vs_atr": round((max(b["h"] for b in today) - min(b["l"] for b in today)) / atr, 2) if atr else None,
           "expected_move_rest_of_day": round(spot * iv / 100 * math.sqrt(left / 375 / 252), 2) if iv and left else None,
           "implied_move_to_expiry_pct": (chain or {}).get("implied_move_pct"),
           "days_to_expiry": (chain or {}).get("days_to_expiry"), "is_expiry_day": bool(chain and chain.get("expiry") == day)}
    return {"underlying": sym, "kind": "index" if sym in INDICES else "stock", "spot": spot, "volatility": vol,
            "change_pct_vs_yesterday": round((today[-1]["c"] / y[-1]["c"] - 1) * 100, 2) if y else None,
            "today_open": today[0]["o"], "today_high": max(b["h"] for b in today), "today_low": min(b["l"] for b in today),
            "opening_range_15m": {"high": max(b["h"] for b in today[:3]), "low": min(b["l"] for b in today[:3])},
            "vwap": None if vw is None else round(vw, 2), "futures_flow": _of(sym, now),
            "yesterday": {"high": max(b["h"] for b in y), "low": min(b["l"] for b in y), "close": y[-1]["c"]} if y else None,
            "candles_5m_today": [[b["start"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in today[-30:]],
            "option_chain": trim_chain(chain) if chain else None}


def _of(sym: str, now: datetime) -> dict | None:
    try:
        return fno_flow.of(sym, now)
    except Exception:  # noqa: BLE001
        return None


def _headlines() -> list[str]:
    try:
        items = india.app_get(f"/api/news?limit={HEADLINES}").get("items") or []
    except Exception:  # noqa: BLE001 - Claude still reads the prices
        return []
    return [f"{i.get('publisher') or ''}: {i.get('title')}" for i in items]


def _flow_board(now: datetime) -> dict:
    try:
        return fno_flow.board(now)
    except Exception as exc:  # noqa: BLE001 - a look never fails for want of the flow board
        log.exception("flow board failed")
        return {"error": str(exc) or type(exc).__name__}


def _board(now: datetime) -> dict:
    try:
        return sector_board(now)
    except Exception as exc:  # noqa: BLE001 - a look never fails for want of the sector board
        log.exception("sector board failed")
        return {"error": str(exc) or type(exc).__name__}


def snapshot(now: datetime, progress=None, extra=()) -> dict:
    tell = progress or (lambda *a: None)
    day = now.date().isoformat()
    markets, skipped = [], []
    for sym in universe(extra):
        tell(f"reading {sym}")
        try:
            markets.append(read_underlying(sym, now))
        except Exception as exc:  # noqa: BLE001 - one missing chart never stops the look
            skipped.append(f"{sym}: {exc}")
    if not markets:
        raise ValueError("no market data: " + "; ".join(skipped))
    try:
        meta = india.chart("INDIAVIX", "1d", "1d")["meta"]
        vix = float(meta["regularMarketPrice"])
        vix_prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        vix_chg = round((vix / float(vix_prev) - 1) * 100, 2) if vix_prev else None
    except (india.IndiaError, KeyError, TypeError, ValueError):
        vix, vix_chg = None, None
    rows = positions()
    m = _movers()
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M"), "session_phase": india.phase(now.replace(tzinfo=None))[1], "minutes_to_close": minutes_left(now), "india_vix": vix, "india_vix_change_pct": vix_chg,
        "top_fno_movers": {"up": (m.get("up") or [])[:10], "down": (m.get("down") or [])[:10]},
        "underlyings": markets, "not_available": skipped, "headlines": _headlines(), "sectors": _board(now), "flow": _flow_board(now),
        "book": {"open": [{k: e.get(k) for k in ("instrument", "underlying", "entry", "stop", "target", "ltp", "qty", "entry_at")} for e in rows if e["status"] == "open"],
                 "today_pnl": day_pnl(day, rows), "trades_today": sum(1 for e in rows if e["day"] == day),
                 "stopped_out_today": stopped_today(day, rows)},
        "limits": {"capital": CAPITAL, "max_loss_at_stop_inr": MAX_RISK, "max_open": MAX_OPEN, "daily_loss_inr": DAY_LOSS, "max_premium_inr": MAX_PREMIUM,
                   "no_reentry_after_stop": "an underlying in book.stopped_out_today can't be bought again today", "min_reward_risk": MIN_RR, "max_strikes_from_atm": MAX_STRIKES_AWAY, "max_spread_pct": MAX_SPREAD, "last_entry": LAST_RUN, "flat_by": FLAT_AT},
    }


# ---------------------------------------------------------------- Claude

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["walkthrough", "action", "underlying", "instrument", "entry", "stop_loss", "target", "quantity", "confidence", "thesis", "risks", "invalidation"],
    "properties": {
        "walkthrough": {"type": "array", "description": "Written first, one entry per step in this order: " + ", ".join(k for k, _ in STEPS),
                        "items": {"type": "object", "additionalProperties": False, "required": ["role", "verdict", "finding"],
                                  "properties": {"role": {"type": "string", "enum": [k for k, _ in STEPS]}, "verdict": {"type": "string", "enum": list(VERDICTS)},
                                                 "finding": {"type": "string", "description": "One or two sentences with the numbers that matter"}}}},
        "action": {"type": "string", "enum": ["BUY", "SKIP"]},
        "underlying": {"type": "string", "description": "One of the underlyings in the snapshot; empty for SKIP"},
        "instrument": {"type": "string", "description": "The option's tradingsymbol, exactly as in that underlying's option_chain; empty for SKIP"},
        "entry": {"type": "number", "description": "Premium you expect to pay"}, "stop_loss": {"type": "number", "description": "Premium level"},
        "target": {"type": "number", "description": "Premium level"},
        "quantity": {"type": "integer", "description": "Units (lots x lot size); the code may cut it to fit the risk limit"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "thesis": {"type": "string"}, "risks": {"type": "array", "items": {"type": "string"}}, "invalidation": {"type": "string"},
    },
}

SYSTEM = """You are Claude Trader: an independent intraday option buyer on India's NSE, trading a paper account on your own
judgement. Nobody has given you a strategy and no other desk's view is passed to you. Every 15 minutes you get a live snapshot
(Nifty, Bank Nifty, India VIX, the day's biggest F&O movers with their 5-minute candles, VWAP, opening range, yesterday's
levels, nearest-expiry option chains, recent headlines, your open book) and you decide whether to BUY one call or one put now,
or to SKIP.

What you should know:
- You only buy options (calls for a rise, puts for a fall). You never sell or write options. Everything closes by 15:15.
- Costs are real: slippage of about 0.3% of the premium each way on index options and 1% on stock options, plus charges.
  Time decay works against you all day. A small or unclear move does not pay for these: SKIP is a good answer and is
  usually the right one. Trade only when the read is clear and the reward clearly beats the risk.
- Prefer liquid contracts near the money; index options are far cheaper to trade than stock options.
- Set the stop and target as premium levels: the stop below the current premium, the target above it, reward at least
  1.5x risk. The code sizes the trade so a stop-out loses at most the limit, and refuses anything outside the limits.
- Don't add a second trade on an underlying you already hold. Respect the day loss limit in the snapshot.
- Fill the walkthrough first, step by step (market read, candidates, choice, option, risk), each with the numbers that
  matter, then the decision. Answer only with the JSON object the schema asks for.
- Your full derivatives playbook follows. Use the computed numbers in the snapshot (Greeks, IV vs realised vol, expected
  move, OI walls) rather than estimating them."""

PLAYBOOK = Path(__file__).with_name("claude_trader_playbook.md")


def system_prompt() -> str:
    try:
        book = PLAYBOOK.read_text(encoding="utf-8")
    except OSError:
        book = ""
    return SYSTEM + ("\n\n" + book if book else "")


INTRO = "Here is the live market snapshot. Read it, decide whether to buy one option now or skip, and fill the walkthrough as you go."


def claude_tool() -> str | None:
    """The signed-in Claude Code tool on this computer. Checks that the sign-in file exists and never opens it."""
    if not (Path.home() / ".claude" / ".credentials.json").exists():
        return None
    found = shutil.which("claude")
    if found:
        return found
    bundled = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")).glob("Claude-Data/*/claude-code/*/*/claude.exe")
    newest = max(bundled, key=lambda p: p.stat().st_mtime, default=None)
    return str(newest) if newest else None


def steps_so_far(buf: str) -> tuple[list[dict], dict | None]:
    """Reads the walkthrough while Claude is still writing it: (steps finished, the step being written so far)."""
    start = buf.find('"walkthrough"')
    i = buf.find("[", start) if start >= 0 else -1
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


def _tool_error(text: str) -> str:
    low = text.lower()
    if "limit" in low:
        return "Your Claude usage limit has been reached."
    if "log in" in low or "login" in low or "auth" in low:
        return "Claude isn't signed in on this computer."
    return "Claude gave no usable answer."


# ---------------------------------------------------------------- early looks

def _bars_today(sym: str, now: datetime) -> tuple[list[dict], list[dict]]:
    """(today's closed 5-minute candles, yesterday's) for one underlying."""
    days = india.session_bars(india.chart(sym, "5d", "5m"), 5, now.replace(tzinfo=None))
    day = now.date().isoformat()
    prev = [d for d in sorted(days) if d < day]
    return days.get(day) or [], (days[prev[-1]] if prev else [])


def candle_events(sym: str, kind: str, today: list[dict], yday: list[dict]) -> list[tuple[str, str]]:
    """Events on the last closed candle: (key, plain words). Index: a close through the opening range or yesterday's high/low.
    Any name: one candle far bigger than the day's usual."""
    if len(today) < 4:
        return []
    last, before = today[-1], today[-2]
    out = []
    if kind == "index":
        levels = [("opening-range high", max(b["h"] for b in today[:3]), 1), ("opening-range low", min(b["l"] for b in today[:3]), -1)]
        if yday:
            levels += [("previous-day high", max(b["h"] for b in yday), 1), ("previous-day low", min(b["l"] for b in yday), -1)]
        for name, lvl, way in levels:
            if (last["c"] - lvl) * way > 0 >= (before["c"] - lvl) * way:
                out.append((f"{sym}|{name}|{way}", f"{sym} closed {'above' if way > 0 else 'below'} its {name} ({lvl:,.2f}) at {last['c']:,.2f}"))
    ranges = sorted(b["h"] - b["l"] for b in today[:-1])
    med = ranges[len(ranges) // 2] if ranges else 0
    rng = last["h"] - last["l"]
    if med > 0 and rng >= BIG_CANDLE * med and rng / last["c"] * 100 >= BIG_MIN_PCT[kind]:
        way = "up" if last["c"] >= last["o"] else "down"
        out.append((f"{sym}|big|{last['start']}", f"{sym} printed a {rng / med:.1f}x candle {way} at {last['start']} ({rng / last['c'] * 100:.2f}% range)"))
    return out


def _vix() -> float | None:
    try:
        return float(india.chart("INDIAVIX", "1d", "1d")["meta"]["regularMarketPrice"])
    except (india.IndiaError, KeyError, TypeError, ValueError):
        return None


# The wide watch: every F&O stock's 5-minute candles, built from one Kite last-price call every 15 seconds.
# Cheap (one call for ~200 names) and it never wakes Claude by itself: its candles go through the same big-candle rule.
_tape: dict = {"day": None, "bars": {}}
_flow: dict = {"thread": None, "state": {}}   # the futures OI / volume / breadth poll (strategies/fno_flow.py) and its early-look state


def flow_poll(now: datetime) -> None:
    """Starts one F&O flow pass in the background (it paces itself to once a minute), so the stops are never kept waiting."""
    th = _flow["thread"]
    if th and th.is_alive():
        return
    try:
        names = list(fno_source() or []) if fno_source else []
    except Exception:  # noqa: BLE001
        names = []
    if not names:
        return
    fno_flow.DIR = DIR
    _flow["thread"] = threading.Thread(target=_flow_pass, args=(now, names), daemon=True, name="claude-trader-flow")
    _flow["thread"].start()


def _flow_pass(now: datetime, names: list[str]) -> None:
    try:
        fno_flow.poll(now, names, indices=INDICES)
    except Exception:  # noqa: BLE001 - the next pass tries again
        log.exception("F&O flow pass failed")


def _bucket(now: datetime) -> str:
    return now.replace(minute=now.minute - now.minute % 5).strftime("%H:%M")


def tape_poll(now: datetime) -> None:
    """Adds one round of last prices for every F&O stock to today's 5-minute candles."""
    day = now.date().isoformat()
    if _tape["day"] != day:
        _tape.update(day=day, bars={})
    try:
        names = list(fno_source() or []) if fno_source else []
    except Exception:  # noqa: BLE001 - the movers' own candles still run
        log.exception("F&O names for the wide watch failed")
        names = []
    if not names:
        return
    px = live_stops.prices([f"NSE:{n}" for n in names], now)
    b = _bucket(now)
    for key, p in px.items():
        bars = _tape["bars"].setdefault(key[4:], {})
        cur = bars.get(b)
        if cur is None:
            prev = bars[max(bars)]["c"] if bars else None
            o = prev if prev is not None else p
            bars[b] = {"start": b, "o": o, "h": max(o, p), "l": min(o, p), "c": p}
        else:
            cur.update(h=max(cur["h"], p), l=min(cur["l"], p), c=p)


def tape_bars(sym: str, now: datetime) -> list[dict]:
    """Today's closed tape candles for one stock (the one still forming is left out)."""
    if _tape["day"] != now.date().isoformat():
        return []
    b = _bucket(now)
    bars = _tape["bars"].get(sym) or {}
    return [bars[k] for k in sorted(bars) if k < b]


def wide_events(now: datetime, seen: set, skip=()) -> list[tuple[str, str]]:
    """Big-candle events on every F&O stock's tape, except the names already checked on their real candles."""
    found = []
    for sym in sorted(_tape["bars"] if _tape["day"] == now.date().isoformat() else {}):
        if sym in skip:
            continue
        found += [ev for ev in candle_events(sym, "stock", tape_bars(sym, now), []) if ev[0] not in seen]
    return found


# Sectors: a board in every read, and an early look when one turns. Plain code, no Claude call.
# (Kite index, label, the app's stock group for its leaders)
SECTORS = [("NIFTY BANK", "Banks", "Banks"), ("NIFTY PVT BANK", "Private banks", "Private banks"), ("NIFTY PSU BANK", "PSU banks", "PSU Banks"),
           ("NIFTY FIN SERVICE", "Financial services", "Financial Services"), ("NIFTY IT", "IT", "IT"), ("NIFTY AUTO", "Auto", "Auto"),
           ("NIFTY PHARMA", "Pharma", "Pharma"), ("NIFTY HEALTHCARE", "Healthcare", "Pharma"), ("NIFTY FMCG", "FMCG", "FMCG"),
           ("NIFTY METAL", "Metals", "Metals"), ("NIFTY ENERGY", "Energy", "Energy"), ("NIFTY OIL AND GAS", "Oil & gas", "Energy"),
           ("NIFTY REALTY", "Realty", "Realty"), ("NIFTY INFRA", "Infrastructure", "Infrastructure"), ("NIFTY MEDIA", "Media", None),
           ("NIFTY CONSR DURBL", "Consumer durables", None), ("NIFTY CPSE", "CPSE", None)]
SECTOR_BASE = "NIFTY 50"
SECTOR_WINDOW = 12            # candles: the last 60 minutes
SECTOR_MOVE = 0.8             # % a sector must move in that hour to count as a turn
SECTOR_VS_NIFTY = 0.5         # ...and % it must beat (or trail) Nifty by over the same hour, unless it crossed from red to green or back
SECTOR_LEADERS = 3
_sector_cache: dict = {"key": None, "data": None}
_sector_picks: dict = {}      # sector event key -> the F&O stocks Claude should read with it
_sector_state: dict = {}      # today's armed / fired state per sector and direction


def _sector_data(now: datetime) -> dict:
    """{index: (today's closed 5-minute candles, previous close)} for every sector and Nifty, once per closed candle."""
    key = (now.date().isoformat(), common.completed_bar(now))
    if _sector_cache["key"] == key and _sector_cache["data"] is not None:
        return _sector_cache["data"]
    out = {}
    for name in [SECTOR_BASE] + [n for n, _, _ in SECTORS]:
        try:
            today, yday = _bars_today(name, now)
        except Exception:  # noqa: BLE001 - one missing index never stops the board
            continue
        if today and yday:
            out[name] = (today, yday[-1]["c"])
    _sector_cache.update(key=key, data=out)
    return out


def _moves(today: list[dict], prev: float) -> dict:
    """Day %, last 30 and 60 minutes %, and the day % an hour ago, from closed candles."""
    last = today[-1]["c"]

    def ref(n):
        return today[-n - 1]["c"] if len(today) > n else today[0]["o"]
    hour_ago = ref(SECTOR_WINDOW)
    return {"day_pct": round((last / prev - 1) * 100, 2), "last_30m_pct": round((last / ref(6) - 1) * 100, 2),
            "last_60m_pct": round((last / hour_ago - 1) * 100, 2), "day_pct_60m_ago": round((hour_ago / prev - 1) * 100, 2)}


def sector_rows(data: dict) -> tuple[dict | None, list[dict]]:
    """(Nifty's moves, every sector's moves strongest first, with its rank now and an hour ago). full_hour is False
    until an hour of candles exists: before that 'the last hour' runs from the open and mixes in the gap."""
    base = _moves(*data[SECTOR_BASE]) if SECTOR_BASE in data else None
    rows = []
    for name, label, _ in SECTORS:
        if name in data and len(data[name][0]) >= 2:
            rows.append({"sector": label, "index": name, **_moves(*data[name]), "full_hour": len(data[name][0]) > SECTOR_WINDOW})
    then = {r["index"]: i + 1 for i, r in enumerate(sorted(rows, key=lambda r: -r["day_pct_60m_ago"]))}
    rows.sort(key=lambda r: -r["day_pct"])
    for i, r in enumerate(rows):
        r.update(rank=i + 1, rank_60m_ago=then[r["index"]])
    return base, rows


def _sector_stocks(group: str | None, best: bool) -> list[dict]:
    """The strongest (or weakest) F&O stocks in one sector today, from the app's movers."""
    if not group:
        return []
    try:
        m = india.app_get("/api/market/movers")
        fno = set(fno_source() or []) if fno_source else set()
    except Exception:  # noqa: BLE001 - the board still shows the sectors
        return []
    groups = m.get("groups") or {}
    if group == "Private banks":
        names = set(groups.get("Banks") or []) - set(groups.get("PSU Banks") or [])
    else:
        names = set(groups.get(group) or [])
    uni = m.get("universe") or {}
    rows = [{"symbol": n, "day_pct": round((uni[n][1] / uni[n][0] - 1) * 100, 2)} for n in names if n in uni and uni[n][0] and (not fno or n in fno)]
    rows.sort(key=lambda r: -r["day_pct"] if best else r["day_pct"])
    return rows[:SECTOR_LEADERS]


def sector_board(now: datetime) -> dict:
    """What Claude reads about sectors: each sector's day, last hour and rank change, and the F&O names leading the strongest and weakest."""
    base, rows = sector_rows(_sector_data(now))
    if not rows:
        return {"error": "no sector data"}
    group = {n: g for n, _, g in SECTORS}
    return {"nifty": base, "sectors": rows,
            "strongest_fno_stocks": {r["sector"]: _sector_stocks(group[r["index"]], True) for r in rows[:3]},
            "weakest_fno_stocks": {r["sector"]: _sector_stocks(group[r["index"]], False) for r in rows[-3:]},
            "note": "Day % is from yesterday's close. A sector whose rank rose a lot in the last hour is where money is moving now; one far ahead of Nifty all day may already be stretched."}


def sector_events(base: dict | None, rows: list[dict], seen: set, state: dict) -> list[tuple[str, str]]:
    """A sector turning: it moved SECTOR_MOVE% in the last hour and pulled away from Nifty, or crossed from red to green (or back).
    Only on a full hour of candles. A sector that fired can fire again the same way once its last-hour move has cooled
    below half the threshold (state keeps that per sector and direction)."""
    out = []
    nifty = (base or {}).get("last_60m_pct") or 0.0
    armed, fired = state.setdefault("armed", {}), state.setdefault("fired", {})
    for r in rows:
        if not r.get("full_hour", True):
            continue
        m = r["last_60m_pct"]
        for d in ("up", "down"):
            if abs(m) < SECTOR_MOVE / 2 or (m > 0) != (d == "up"):
                armed[f"{r['index']}|{d}"] = True
        if abs(m) < SECTOR_MOVE:
            continue
        way = 1 if m > 0 else -1
        crossed = r["day_pct_60m_ago"] * way < 0 <= r["day_pct"] * way
        if (m - nifty) * way < SECTOR_VS_NIFTY and not crossed:
            continue
        side = f"{r['index']}|{'up' if way > 0 else 'down'}"
        if not armed.get(side, True):
            continue
        key = f"SECTOR|{side}|{fired.get(side, 0) + 1}"
        if key in seen:
            continue
        armed[side], fired[side] = False, fired.get(side, 0) + 1
        cross = (", crossing from red to green" if way > 0 else ", crossing from green to red") if crossed else ""
        out.append((key, f"{r['sector']} {'gained' if way > 0 else 'lost'} {abs(m):.2f}% in the last hour (Nifty {nifty:+.2f}%), now {r['day_pct']:+.2f}% on the day, "
                         f"rank {r['rank_60m_ago']} to {r['rank']} of {len(rows)}{cross}"))
    return out


def flagged_names(events: list[tuple[str, str]]) -> list[str]:
    """The stocks an early look should add to Claude's read: a flagged stock itself, or a turning sector's leading F&O names."""
    out = []
    for k, _ in events:
        head = k.split("|")[0]
        if head == "SECTOR":
            names = _sector_picks.get(k, [])
        elif head in INDICES or head == "VIX":
            names = []
        else:
            names = [head]
        out += [n for n in names if n not in out]
    return sorted(out)


def early_events(now: datetime, seen: set, last_vix: float | None) -> tuple[list[tuple[str, str]], float | None]:
    """New events on the last closed candle across the indices and today's movers, then every other F&O stock's tape, plus a VIX jump since the last check."""
    found = []
    watched = universe()
    found += wide_events(now, seen, set(watched))
    for sym in watched:
        try:
            today, yday = _bars_today(sym, now)
        except Exception:  # noqa: BLE001 - one missing chart never stops the check
            continue
        found += [ev for ev in candle_events(sym, "index" if sym in INDICES else "stock", today, yday) if ev[0] not in seen]
    try:
        day = now.date().isoformat()
        if _flow["state"].get("day") != day:
            _flow["state"] = {"day": day}
        found += fno_flow.events(now, seen, _flow["state"])
    except Exception:  # noqa: BLE001 - the candle checks still count
        log.exception("flow check failed")
    try:
        base, rows = sector_rows(_sector_data(now))
        group = {n: g for n, _, g in SECTORS}
        day = now.date().isoformat()
        if _sector_state.get("day") != day:
            _sector_state.clear()
            _sector_state["day"] = day
        for k, w in sector_events(base, rows, seen, _sector_state):
            _sector_picks[k] = [x["symbol"] for x in _sector_stocks(group[k.split("|")[1]], k.split("|")[2] == "up")][:2]
            found.append((k, w))
    except Exception:  # noqa: BLE001 - the candle checks still count
        log.exception("sector check failed")
    vix = _vix()
    if vix and last_vix and (vix / last_vix - 1) * 100 >= VIX_JUMP:
        found.append((f"VIX|{now.strftime('%H:%M')}", f"India VIX jumped {(vix / last_vix - 1) * 100:.1f}% to {vix:.2f}"))
    return found, vix or last_vix


def ask(context: dict, tool: str, progress=None) -> dict:
    """One structured Claude call through the signed-in Claude Code tool: no tools, no settings, no MCP, an empty folder.
    Streamed, so progress(stage, steps_done, step_writing) can show each step as Claude writes it."""
    args = [tool, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--json-schema", json.dumps(SCHEMA),
            "--system-prompt", system_prompt(), "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
    user = INTRO + "\n\n" + json.dumps(context, ensure_ascii=False)
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
                if ev.get("type") == "stream_event":
                    e = ev.get("event") or {}
                    if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "thinking":
                        tell("Claude is thinking", [], None)
                    elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "input_json_delta":
                        buf += e["delta"].get("partial_json") or ""
                        now = steps_so_far(buf)
                        if now != last:
                            last = now
                            tell("Claude is writing its read", *now)
                elif ev.get("type") == "result":
                    result = ev
            err = p.stderr.read().decode("utf-8", "replace")
            p.wait()
        finally:
            timer.cancel()
    if late.is_set():
        raise ValueError("Claude took too long.")
    answer = (result or {}).get("structured_output")
    u = (result or {}).get("usage") or {}
    _usage.clear()
    _usage.update(model=MODEL, input=u.get("input_tokens"), cache_read=u.get("cache_read_input_tokens"), cache_write=u.get("cache_creation_input_tokens"),
                  output=u.get("output_tokens"), cost_usd=(result or {}).get("total_cost_usd"), seconds=round(((result or {}).get("duration_ms") or 0) / 1000, 1))
    if not result or result.get("is_error") or not isinstance(answer, dict):
        raise ValueError(_tool_error(str((result or {}).get("result") or (result or {}).get("subtype") or err)))
    return answer


# ---------------------------------------------------------------- the code's checks

def _row_for(chain: dict | None, symbol: str) -> tuple[dict, str] | None:
    for r in (chain or {}).get("rows") or []:
        for leg in ("ce", "pe"):
            if (r.get(leg) or {}).get("tradingsymbol") == symbol:
                return r, leg
    return None


def entry_bar(hm: str) -> str:
    """The first 5-minute candle that starts after this time."""
    m = int(hm[3:5])
    return common.plus_minutes(f"{hm[:3]}{m - m % 5:02d}", 5)


def check(d: dict, ctx: dict, now: datetime, live_price, rows: list[dict]) -> dict:
    """Every limit, on Claude's answer. -> {"ok", "why", "checks", "position" when ok}."""
    checks: list[dict] = []

    def fail(name: str, why: str) -> dict:
        checks.append({"name": name, "ok": False, "note": why})
        return {"ok": False, "why": why, "checks": checks}

    def ok(name: str, note: str) -> None:
        checks.append({"name": name, "ok": True, "note": note})

    if d.get("action") != "BUY":
        return {"ok": False, "why": "Claude chose to stay out.", "checks": checks}
    t, day = now.strftime("%H:%M"), now.date().isoformat()
    if t > LAST_RUN:
        return fail("time", f"no new trade after {LAST_RUN}.")
    ok("time", t)
    sym = d.get("underlying")
    mk = next((u for u in ctx["underlyings"] if u["underlying"] == sym), None)
    if not mk:
        return fail("instrument", f"{sym or 'no underlying'} wasn't in the snapshot.")
    open_rows = [e for e in rows if e["status"] == "open"]
    if len(open_rows) >= MAX_OPEN:
        return fail("book", f"{MAX_OPEN} trades are already open.")
    if any(e["underlying"] == sym for e in open_rows):
        return fail("book", f"the book already holds {sym}.")
    if sym in stopped_today(day, rows):
        return fail("book", f"{sym} already stopped the book out today; no second try in it until tomorrow.")
    today = day_pnl(day, rows)
    if today <= -DAY_LOSS:
        return fail("book", f"the book is down ₹{-today:,.0f} today, past the ₹{DAY_LOSS:,.0f} day limit.")
    ok("book", f"{len(open_rows)} open, today {today:+,.0f}")

    chain = mk.get("option_chain")
    hit = _row_for(chain, d.get("instrument"))
    if not hit:
        return fail("instrument", f"{d.get('instrument') or 'no contract'} isn't in {sym}'s option chain.")
    r, leg = hit
    o, lot = r[leg], int(chain.get("lot_size") or 0)
    if not lot:
        return fail("instrument", "the chain has no lot size.")
    strikes = sorted(x["strike"] for x in chain["rows"])
    step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=0)
    away = round(abs(r["strike"] - chain["atm"]) / step) if step else 0
    if away > MAX_STRIKES_AWAY:
        return fail("liquidity", f"strike {r['strike']:g} is {away} strikes from the money, over {MAX_STRIKES_AWAY}.")
    bid, ask_ = o.get("bid") or 0, o.get("ask") or 0
    spread = round((ask_ - bid) / ((ask_ + bid) / 2) * 100, 1) if bid and ask_ else None
    if spread is None or spread > MAX_SPREAD:
        return fail("liquidity", f"the bid-ask spread is {'unknown' if spread is None else f'{spread}%'}, over {MAX_SPREAD:g}%.")
    if mk["kind"] == "stock" and (o.get("oi") or 0) < MIN_OI_LOTS * lot:
        return fail("liquidity", f"open interest is {round((o.get('oi') or 0) / lot)} lots, under {MIN_OI_LOTS}.")
    key = f"NFO:{o['tradingsymbol']}"
    ok("liquidity", f"{o['tradingsymbol']}: {away} strike(s) from ATM, spread {spread}%")

    slip = SLIP["index" if sym in INDICES else "stock"]
    last = float(live_price(key))
    fill = round(last * (1 + slip), 2)
    stop, target = float(d.get("stop_loss") or 0), float(d.get("target") or 0)
    if not stop < last < target:
        return fail("levels", f"the live premium {last:.2f} isn't between the stop {stop:.2f} and the target {target:.2f}.")
    rr = (target - fill) / (fill - stop) if fill > stop else 0
    if rr < MIN_RR:
        return fail("levels", f"reward is {rr:.2f}x risk at the fill of {fill:.2f}, under {MIN_RR}x.")
    ok("levels", f"fill {fill:.2f}, stop {stop:.2f}, target {target:.2f}: {rr:.2f}R")

    out = stop * (1 - slip)
    per_unit = fill - out
    per_lot = per_unit * lot + fees(fill * lot, out * lot)
    cap = int(MAX_RISK // per_lot) if per_lot > 0 else 0
    asked = max(0, int(d.get("quantity") or 0)) // lot
    in_use = sum(x["entry"] * x["qty"] for x in open_rows)
    lots = min(asked or cap, cap, int(MAX_PREMIUM // (fill * lot)), int((CAPITAL - in_use) // (fill * lot)))
    if lots < 1:
        return fail("size", f"one lot would lose about ₹{per_lot:,.0f} at the stop with charges, or cost more than the book allows; the cap is ₹{MAX_RISK:,.0f}.")
    qty = lots * lot
    max_loss = round(per_unit * qty + fees(fill * qty, out * qty), 2)
    ok("size", f"{lots} lot(s), {qty} units, ₹{fill * qty:,.0f} of premium, loss at the stop about ₹{max_loss:,.0f}" + (f" (cut from Claude's {asked} lots)" if asked and lots < asked else ""))
    e = {"underlying": sym, "instrument": o["tradingsymbol"], "key": key, "lot_size": lot, "expiry": chain.get("expiry"), "strike": r["strike"], "option_type": leg.upper(),
         "slip": slip, "qty": qty, "lots": lots, "entry": fill, "stop": stop, "target": target, "max_loss": max_loss, "ltp": last, "status": "open", "day": day,
         "entry_at": t, "entry_bar": entry_bar(t), "confidence": d.get("confidence"), "thesis": d.get("thesis")}
    return {"ok": True, "why": "", "checks": checks, "position": e}


# ---------------------------------------------------------------- following trades

def close(e: dict, price: float, reason: str, day: str, at: str) -> None:
    out = price * (1 - e["slip"])
    cost = fees(e["entry"] * e["qty"], out * e["qty"])
    gross = (out - e["entry"]) * e["qty"]
    risk = (e["entry"] - e["stop"]) * e["qty"]
    e.update(status="closed", exit=round(out, 2), exit_day=day, exit_at=at, exit_reason=reason, costs=round(cost, 2), net=round(gross - cost, 2),
             r=round((gross - cost) / risk, 2) if risk > 0 else None)


def walk(e: dict, bars: dict, day: str, upto: str) -> None:
    """Closed 5-minute candles since entry: the stop first, then the target, out at 15:15. bars: {HH:MM: (o, h, l, c)}."""
    for t in common.LABELS:
        if t < e["entry_bar"] or t > upto:
            continue
        if t in bars:
            o, h, lo, c = bars[t]
            e["ltp"] = c
            at = common.plus_minutes(t, 5)
            if lo <= e["stop"]:
                return close(e, min(o, e["stop"]), "stop", day, at)
            if h >= e["target"]:
                return close(e, max(o, e["target"]), "target", day, at)
        if common.plus_minutes(t, 5) == FLAT_AT and e.get("ltp") is not None:
            return close(e, e["ltp"], "15:15 exit", day, FLAT_AT)
    return None


def live_check(e: dict, now: datetime, px: float | None) -> None:
    """The live price: closes the trade the moment it trades through the stop or the target. 15:15 stays with the candles."""
    if px is None or now.strftime("%H:%M") >= FLAT_AT:
        return
    e["ltp"] = px
    got = live_stops.hit(px, e["stop"], e.get("target"))
    if got:
        close(e, got[1], got[0], now.date().isoformat(), now.strftime("%H:%M"))


def bars_of(key: str, day: str) -> dict:
    days = india.session_bars(india.chart(key, "5d", "5m"), 5)
    return {b["start"]: (b["o"], b["h"], b["l"], b["c"]) for b in days.get(day) or []}


def live_price(key: str) -> float:
    return float(india.chart(key, "1d", "1d")["meta"]["regularMarketPrice"])


def follow(now: datetime) -> None:
    """Every pass: candles first, then the live price. A trade from an earlier day (the app was off) closes at its last price."""
    day, bar = now.date().isoformat(), common.completed_bar(now)
    rows = positions()
    if not bar or not any(e["status"] == "open" for e in rows):
        return
    live = live_stops.prices([e["key"] for e in rows if e["status"] == "open"], now)
    closed = []
    for e in rows:
        if e["status"] != "open":
            continue
        if e["day"] != day:
            close(e, e.get("ltp") or e["entry"], "15:15 exit", e["day"], FLAT_AT)
        else:
            try:
                walk(e, bars_of(e["key"], day), day, bar)
            except Exception:  # noqa: BLE001 - the live check still runs
                log.warning("no candles for %s", e["instrument"])
            if e["status"] == "open":
                live_check(e, now, live.get(e["key"]))
        if e["status"] == "closed":
            closed.append(e)
    with _lock:
        fresh = {e["id"]: e for e in rows}
        _save_positions([fresh.get(e["id"], e) for e in positions()])
    for e in closed:
        _note(day, now.strftime("%H:%M"), "exit", f"{e['instrument']} closed at {e['exit']:.2f} ({e['exit_reason']}): net ₹{e['net']:,.2f}.")
        _announce_exit(e)


# ---------------------------------------------------------------- Telegram

def entry_text(e: dict) -> str:
    return notify.signal_text({"desk": DESK_NAME, "instrument": e["instrument"], "side": "BUY", "setup": "Claude's own call", "time": e["entry_at"], "date": e["day"],
                               "entry": e["entry"], "stop": e["stop"], "target": e["target"], "qty": e["qty"], "lots": e.get("lots"), "max_loss": e["max_loss"],
                               "expiry": e.get("expiry"), "confidence": None if e.get("confidence") is None else f"{e['confidence']}% · Claude"})


def exit_text(e: dict) -> str:
    row, rule = notify._card_row, "-" * notify.CARD_WIDTH
    rs = lambda v: "–" if v is None else f"₹{float(v):,.2f}"  # noqa: E731
    why = {"stop": "Stop hit", "target": "Target hit"}.get(e["exit_reason"], e["exit_reason"])
    when = datetime.strptime(e.get("exit_day") or e["day"], "%Y-%m-%d").strftime("%d %b %Y")
    return "\n".join([f"EXIT · {when} · {e.get('exit_at') or ''}", "", e["instrument"], f"SOLD · {why}", rule,
                      row("Entry", rs(e["entry"])), row("Exit", rs(e["exit"])), row("Qty", str(e["qty"])), rule,
                      row("Charges", rs(e.get("costs"))), row("Booked P&L", ("+" if (e["net"] or 0) >= 0 else "−") + rs(abs(e["net"] or 0))),
                      row("R", f"{e['r']:+.2f}R" if e.get("r") is not None else "–"), rule, f"{DESK_NAME} · paper"])[:notify.MAX_LEN]


def _announce_entry(e: dict, now: datetime) -> None:
    try:
        notify.send_once(f"{DESK_ID}|{e['id']}|open", entry_text(e), now)
    except Exception:  # noqa: BLE001 - a Telegram problem never stops the desk
        log.exception("claude trader entry message")


def _announce_exit(e: dict) -> None:
    try:
        notify.send_once(f"{DESK_ID}|{e['id']}|exit", exit_text(e))
    except Exception:  # noqa: BLE001
        log.exception("claude trader exit message")


# ---------------------------------------------------------------- one look at the market

def clock() -> datetime:
    return datetime.now(common.IST)


def _work(now: datetime, tool: str, slot: str, early: list[str] | None = None, extra=()) -> None:
    day = now.date().isoformat()
    record = {"id": f"{day}|{now.strftime('%H:%M:%S')}", "at": now.strftime("%H:%M"), "slot": slot}
    if early:
        record["early"] = early
    run = {"id": record["id"], "early": early, "stage": "reading the market", "since": clock().strftime("%H:%M:%S"), "roles": [], "writing": None, "decision": None, "checks": None}
    _live["run"] = run

    def progress(stage: str, done: list[dict] | None = None, writing: dict | None = None) -> None:
        run.update(stage=stage, roles=done if done is not None else run["roles"], writing=writing)
    res = {"ok": False}
    try:
        ctx = snapshot(now, lambda s: progress(s), extra)
        if early:
            ctx["why_this_look"] = {"early_look": True, "events": early,
                                    "note": "An unscheduled look: the code saw these events on the last 5-minute candle. Judge them like anything else; an event is not a reason to trade by itself."}
        record["market"] = {"vix": ctx["india_vix"], "read": [u["underlying"] for u in ctx["underlyings"]], "skipped": ctx["not_available"],
                            "spots": {u["underlying"]: u["spot"] for u in ctx["underlyings"]}}
        run.update(stage="Claude is deciding", market=record["market"])
        _usage.clear()
        record["snapshot_chars"] = len(json.dumps(ctx, ensure_ascii=False))
        record["decision"] = ask(ctx, tool, progress)
        record["usage"] = dict(_usage) or None
        run.update(stage="checking the limits", roles=record["decision"].get("walkthrough") or run["roles"], writing=None, decision=record["decision"])
        with _lock:
            rows = positions()
            res = check(record["decision"], ctx, clock(), live_price, rows)
            record["checks"] = res["checks"]
            if res["ok"]:
                e = dict(res["position"], id=record["id"])
                _save_positions(rows + [e])
        record["paper"] = {"taken": True} if res["ok"] else {"taken": False, "why": res["why"]}
        if res["ok"]:
            _announce_entry(e, now)
    except subprocess.TimeoutExpired:
        record["error"] = "Claude took too long."
    except (india.IndiaError, ValueError, KeyError) as exc:
        record["error"] = str(exc) or type(exc).__name__
    except Exception:  # noqa: BLE001 - a failed look is shown on the page and never kills the thread
        log.exception("claude trader run failed")
        record["error"] = "The run failed. See the backend log."
    finally:
        with _lock:
            _write("runs", day, runs(day) + [record])
        _running.update(since=None)
        run.update(stage="done", writing=None, checks=record.get("checks"), paper=record.get("paper"), error=record.get("error"), finished=clock().strftime("%H:%M:%S"))
    d = record.get("decision") or {}
    if record.get("error"):
        what = "failed: " + record["error"]
    elif d.get("action") == "BUY":
        what = f"BUY {d.get('instrument')}" + (" · paper trade taken" if record["paper"]["taken"] else f" · not taken: {record['paper']['why']}")
    else:
        what = "stayed out: " + str(d.get("thesis") or "")[:220]
    _note(day, clock().strftime("%H:%M"), "result", what)


def start(now: datetime, slot: str, early: list[str] | None = None, extra=()) -> None:
    tool = claude_tool()
    if not tool:
        raise ValueError("Claude isn't signed in on this computer.")
    with _lock:
        if _running["since"]:
            raise ValueError("Claude is still on the last look.")
        _running.update(since=now.isoformat(timespec="seconds"))
    threading.Thread(target=_work, args=(now, tool, slot, early, tuple(extra)), daemon=True, name="claude-trader").start()


def _today_state(day: str) -> dict:
    st = _state()
    if st.get("day") != day:
        st = {"day": day, "done": [], "early": [], "seen": [], "checked_bar": None, "vix": None, "last_look": None, "last_kind": None}
    return st


def _book_open(day: str) -> bool:
    """False when Claude couldn't trade anyway: the book is full or the day's loss limit is hit."""
    rows = positions()
    return sum(1 for e in rows if e["status"] == "open") < MAX_OPEN and day_pnl(day, rows) > -DAY_LOSS


def tick(now: datetime) -> None:
    """Every 15 seconds: follow open trades; at each 15-minute slot let Claude look at the market; between slots check each
    closed 5-minute candle and wake Claude early on a real event."""
    if not india.trading_day(now.date()):
        return
    t, day = now.strftime("%H:%M"), now.date().isoformat()
    if t >= "09:20":
        follow(now)
    if "09:15" <= t < "15:30":
        tape_poll(now)
        flow_poll(now)
    if _running["since"]:
        return
    st = _today_state(day)
    since_last = (mins(t) - mins(st["last_look"])) if st.get("last_look") else None
    slot = due_slot(now)
    if slot:
        st["done"] = (st.get("done") or []) + [slot]
        if since_last is not None and since_last < EARLY_GAP and st.get("last_kind") == "early":
            _set_state(**st)
            _note(day, t, "skip", f"{slot} look skipped: Claude looked early at {st['last_look']}.")
            return
        st.update(last_look=t, last_kind="scheduled")
        _set_state(**st)
        try:
            start(now, slot)
            _note(day, t, "run", f"Claude is reading the market ({slot} look).")
        except ValueError as exc:
            _note(day, t, "skip", f"{slot} look skipped: {exc}")
        return
    bar = common.completed_bar(now)
    if not bar or bar == st.get("checked_bar") or not EARLY_FROM <= t <= LAST_RUN:
        return
    st["checked_bar"] = bar
    events, st["vix"] = early_events(now, set(st.get("seen") or []), st.get("vix"))
    st["seen"] = (st.get("seen") or []) + [k for k, _ in events]
    why = [w for _, w in events]
    used, recent = len(st.get("early") or []) >= EARLY_MAX, since_last is not None and since_last < EARLY_GAP
    if not why or used or recent or not _book_open(day):
        _set_state(**st)
        if why:
            reason = "the day's early looks are used" if used else "Claude looked minutes ago" if recent else "the book can't take a trade"
            _note(day, t, "event", "; ".join(why) + f" (no early look: {reason}).")
        return
    st["early"] = (st.get("early") or []) + [t]
    st.update(last_look=t, last_kind="early")
    _set_state(**st)
    try:
        start(now, "early", why, flagged_names(events))
        _note(day, t, "run", "Early look: " + "; ".join(why) + ".")
    except ValueError as exc:
        _note(day, t, "skip", f"early look skipped: {exc}")


# ---------------------------------------------------------------- the page and the portfolio

def trades() -> list[dict]:
    from strategies import all_desks
    today = datetime.now(common.IST).date().isoformat()
    return [all_desks._trade(e.get("exit_day") or today, e["instrument"], e["key"], {**e, "entry_at": e["entry_at"], "exit_at": e.get("exit_at")}) for e in positions()]


def view(day: str | None = None) -> dict:
    now = datetime.now(common.IST)
    today = now.date().isoformat()
    day = day or today
    rows = positions()
    closed = [e for e in rows if e["status"] == "closed"]
    realized = round(sum(e["net"] or 0 for e in closed), 2)
    open_marks = round(sum(mark(e) for e in rows if e["status"] == "open"), 2)
    shown = [e for e in rows if e["status"] == "open" or e.get("exit_day") == day or e["day"] == day]
    sessions = sorted({p.stem[5:] for p in DIR.glob("runs_*.json")} | {today}, reverse=True)[:60] if DIR.exists() else [today]
    st = _state()
    return {
        "paper_mode": True, "name": DESK_NAME, "day": day, "today": today, "model": MODEL, "signed_in": bool(claude_tool()),
        "market_day": india.trading_day(date.fromisoformat(today)), "running": bool(_running["since"]), "rules": rules_text(),
        "live": {"run": _live["run"], "next": next_run(now), "steps": [{"id": k, "label": v} for k, v in STEPS]},
        "schedule": {"first": FIRST_RUN, "last": LAST_RUN, "every": EVERY, "looks": len(slots()), "done": len(st.get("done") or []) if st.get("day") == today else 0,
                     "early": len(st.get("early") or []) if st.get("day") == today else 0, "early_max": EARLY_MAX, "watching": len(_tape["bars"]) if _tape["day"] == today else 0},
        "telegram": notify.enabled() and notify.configured(),
        "book": {"capital": CAPITAL, "equity": round(CAPITAL + realized + open_marks, 2), "realized": realized, "open_pnl": open_marks, "today": day_pnl(today, rows),
                 "day_loss": DAY_LOSS, "max_open": MAX_OPEN, "max_risk": MAX_RISK, "open": sum(1 for e in rows if e["status"] == "open"), "closed": len(closed),
                 "won": sum(1 for e in closed if (e["net"] or 0) > 0)},
        "positions": shown[::-1], "runs": runs(day)[::-1], "activity": activity(day)[::-1][:80], "sessions": sessions,
    }
