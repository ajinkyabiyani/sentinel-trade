"""What the option strategies share: session time, candle helpers, option pricing and Zerodha's option charges."""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"
BAR_MINUTES = 5
SESSION_BARS = 72                              # 09:15 ... 15:10: a complete retained session (09:15-15:14)


def costs(buy_val: float, sell_val: float) -> float:
    """Zerodha intraday equity (MIS) charges for one round trip, INR."""
    brk = min(20.0, 0.0003 * buy_val) + min(20.0, 0.0003 * sell_val)
    txn = 0.0000307 * (buy_val + sell_val)
    sebi = 1e-6 * (buy_val + sell_val)
    return brk + 0.00025 * sell_val + txn + sebi + 0.18 * (brk + txn + sebi) + 0.00003 * buy_val
RATE = 0.065                  # ASSUMED risk-free rate, as on the Option Chain page


def plus_minutes(t: str, minutes: int) -> str:
    h, m = map(int, t.split(":"))
    total = h * 60 + m + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


LABELS = [plus_minutes("09:15", BAR_MINUTES * i) for i in range(SESSION_BARS)]


def completed_bar(now: datetime) -> str | None:
    """Label of the latest candle that has closed and is settled at Kite, or None before the first one."""
    minutes = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    if minutes < BAR_MINUTES or now.weekday() >= 5:
        return None
    n = min(minutes // BAR_MINUTES, SESSION_BARS)
    if minutes < SESSION_BARS * BAR_MINUTES and minutes % BAR_MINUTES == 0 and now.second < 8:
        n -= 1                                             # the candle closed seconds ago: give Kite a moment
    return plus_minutes("09:15", (n - 1) * BAR_MINUTES) if n >= 1 else None


def bars_from_candles(rows: list[dict]) -> dict:
    """Kite candles as {HH:MM: (open, high, low, close)}."""
    out = {}
    for r in rows:
        ts = pd.Timestamp(r["date"])
        if ts.tz is not None:
            ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
        out[ts.strftime("%H:%M")] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    return out


def option_rows(instruments) -> dict[str, list[dict]]:
    """Kite's option contracts, by underlying name."""
    by_name: dict[str, list[dict]] = {}
    for r in instruments("NFO"):
        if r.get("instrument_type") in ("CE", "PE") and r.get("name"):
            by_name.setdefault(r["name"], []).append(r)
    return by_name


# ---------------- Black-Scholes ----------------
def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _d1(s: float, k: float, t: float, vol: float) -> float:
    return (math.log(s / k) + (RATE + vol * vol / 2) * t) / (vol * math.sqrt(t))


def bs_price(s: float, k: float, t: float, vol: float, call: bool) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, s - k) if call else max(0.0, k - s)
    d1 = _d1(s, k, t, vol)
    d2 = d1 - vol * math.sqrt(t)
    disc = math.exp(-RATE * t)
    return s * _ncdf(d1) - k * disc * _ncdf(d2) if call else k * disc * _ncdf(-d2) - s * _ncdf(-d1)


def bs_delta(s: float, k: float, t: float, vol: float, call: bool) -> float:
    """Absolute delta."""
    if t <= 0 or vol <= 0:
        return float((s > k) if call else (s < k))
    d1 = _d1(s, k, t, vol)
    return _ncdf(d1) if call else _ncdf(-d1)


def implied_vol(price: float, s: float, k: float, t: float, call: bool) -> float | None:
    if not price or price <= 0 or t <= 0 or price < bs_price(s, k, t, 1e-4, call):
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(s, k, t, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def years_to(expiry: date, day: date, t: str = "09:15") -> float:
    h, m = map(int, t.split(":"))
    now = datetime.combine(day, dtime(h, m))
    return max((datetime.combine(expiry, dtime(15, 30)) - now).total_seconds(), 60.0) / (365.25 * 86400)


def charges(orders: list[tuple[bool, float]]) -> float:
    """Zerodha option charges. orders: (is_buy, premium value). ₹20 an order, STT 0.15% on sells (from 1 Apr 2026),
    exchange 0.03553%, SEBI ₹10 a crore, GST 18%, stamp 0.003% on buys."""
    buy = sum(v for b, v in orders if b)
    sell = sum(v for b, v in orders if not b)
    brokerage = 20.0 * len(orders)
    txn = 0.0003553 * (buy + sell)
    sebi = 1e-6 * (buy + sell)
    return brokerage + 0.0015 * sell + txn + sebi + 0.18 * (brokerage + txn + sebi) + 0.00003 * buy
