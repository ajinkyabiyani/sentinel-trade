"""The setups Team 1 looks for, and the day type that decides which are allowed. Reads charts; places nothing.

  vwap_reclaim   VWAP reclaim or rejection with strong volume
  momentum_rvol  clear high-volume momentum continuation, on Strong Trend days only
"""
from __future__ import annotations

import datetime
import sys
import threading

from strategies import agent_desk

sys.path.insert(0, str(agent_desk.SKILLS / "_india"))
import india  # noqa: E402
import setups  # noqa: E402

RULES = {"vwap_reclaim": "VWAP Reclaim / Rejection", "momentum_rvol": "High-Volume Momentum Continuation"}
STRONG_TREND_ONLY = {"momentum_rvol"}
FRESH_BARS = 2                # a setup older than this many closed bars is stale
OR_MINUTES = 15               # the first 15 minutes, which the setups' shared context measures
STEP = 5
_regimes: dict[str, dict] = {}
_lock = threading.Lock()


def daily_rows(symbol: str, session: datetime.date) -> list[dict]:
    res = india.chart(symbol, "3mo", "1d")
    q = res["indicators"]["quote"][0]
    rows = []
    for i, ts in enumerate(res.get("timestamp") or []):
        at = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts) + 19800)
        if at.date() < session and q["close"][i] is not None:
            rows.append({"date": at.date().isoformat(), "h": q["high"][i], "l": q["low"][i], "c": q["close"][i], "o": q["open"][i]})
    return rows


def day_type(rows: list[dict], vix: float | None) -> str:
    """A rule on recent behaviour, not a forecast."""
    if len(rows) < 25:
        return "Unknown"
    rng = [(r["h"] - r["l"]) / r["c"] * 100 for r in rows]
    r5, r20 = sum(rng[-5:]) / 5, sum(rng[-20:]) / 20
    sma20 = sum(r["c"] for r in rows[-20:]) / 20
    move5 = abs(rows[-1]["c"] / rows[-6]["c"] - 1) * 100
    if (vix or 0) >= 20 or r5 >= 1.3 * r20:
        return "High Volatility"
    if r5 <= 0.8 * r20 and (vix or 99) < 15:
        return "Range"
    if move5 >= 1.5 * r20 and (rows[-1]["c"] > sma20) == (rows[-1]["c"] > rows[-6]["c"]):
        return "Strong Trend"
    return "Mild Trend"


def regime(session: datetime.date) -> dict:
    """The day type from Nifty's daily candles before today and India VIX, worked out once a day."""
    key = session.isoformat()
    with _lock:
        if key in _regimes:
            return _regimes[key]
    try:
        rows = daily_rows("NIFTY", session)
    except india.IndiaError:
        return {"day_type": "Unknown", "bias": "Neutral"}       # not cached: tried again on the next scan
    try:
        vix = float(india.chart("INDIAVIX", "1d", "1d")["meta"]["regularMarketPrice"])
    except (india.IndiaError, KeyError, TypeError, ValueError):
        vix = None
    label = day_type(rows, vix)
    bias = ("Strong Bullish" if rows[-1]["c"] > rows[-6]["c"] else "Strong Bearish") if label == "Strong Trend" else "Neutral"
    out = {"day_type": label, "bias": bias}
    with _lock:
        _regimes[key] = out
    return out


def rules_for(reg: dict | None) -> list[str]:
    strong = (reg or {}).get("day_type") == "Strong Trend"
    return [r for r in RULES if strong or r not in STRONG_TREND_ONLY]


def find(symbol: str, session: datetime.date, cutoff: datetime.datetime, seen: set, reg: dict | None = None, fresh: int = FRESH_BARS) -> list[dict]:
    """New allowed setups on the symbol's 5-minute candles up to the cutoff, each with its chart check."""
    days = india.session_bars(india.chart(symbol, "5d", "5m"), STEP, cutoff)
    vols = india.future_volumes(symbol, STEP, "5d")
    for bars in days.values():
        india.apply_volumes(bars, vols)
    today = days.get(session.isoformat()) or []
    earlier = [days[d] for d in sorted(days) if d < session.isoformat()]
    if not today:
        return []
    ctx = setups.context(STEP, OR_MINUTES, earlier[-1] if earlier else None, earlier[-5:])
    gap = today[0]["o"] / earlier[-1][-1]["c"] - 1 if earlier and earlier[-1] else 0.0
    out = []
    for name in rules_for(reg):
        for d in ("up", "down"):
            key = "%s|%s|%s|%s" % (session.isoformat(), symbol, name, d)
            if key in seen:
                continue
            t = setups.detect(name, today, ctx, d)
            if not t or len(today) - 1 - t["bar"] > fresh:
                continue
            bar = today[t["bar"]]
            end = datetime.datetime.combine(session, datetime.time(9, 15)) + datetime.timedelta(minutes=STEP * (bar["slot"] + 1))
            out.append(dict(t, key=key, symbol=symbol, setup=name, direction=d, slot=bar["slot"], gap_pct=round(gap * 100, 2), at=end.strftime("%H:%M"), technical=setups.technical_read(today, t["bar"], d)))
    return out
