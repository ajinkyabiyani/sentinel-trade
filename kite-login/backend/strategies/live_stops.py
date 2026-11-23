"""Real-time stop and target checks for the paper desks, on Kite's last traded price.

The desks follow their trades on closed 5-minute candles, so a stop hit early in a candle used to stay open until that
candle closed and its data arrived (5 minutes or more). Each desk now also calls this on every pass (every 15 to 30
seconds): once the last price trades through the stop, the trade closes at that last price (as a stop-loss market
order would fill); once it reaches the target, at the target. The candle walk still runs first, so anything missed
while the app was off is caught from the candles.

Read only: one `kite.ltp` call per pass for all of a desk's open trades. Nothing here places an order.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime

log = logging.getLogger("live-stops")
OPEN, CLOSE = dtime(9, 15), dtime(15, 30)


def _kite_ltp(keys: list[str]) -> dict[str, float]:
    from kiteconnect import KiteConnect
    from core import store
    d = store.load()
    if not d:
        return {}
    kite = KiteConnect(api_key=d["api_key"])
    kite.set_access_token(d["access_token"])
    return {k: float(v["last_price"]) for k, v in kite.ltp(keys).items() if v.get("last_price")}


source = _kite_ltp          # {EXCHANGE:SYMBOL: last price}; tests replace it


def market_open(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN <= now.time() <= CLOSE


def prices(keys, now: datetime) -> dict[str, float]:
    """Last prices for these keys during market hours; {} outside them or when Kite doesn't answer (the candles still run)."""
    keys = sorted({k for k in keys if k})
    if not keys or not market_open(now):
        return {}
    try:
        return source(keys)
    except Exception:  # noqa: BLE001 - a missed pass is retried on the next one; the candle check still runs
        log.warning("live prices unavailable for %d keys", len(keys))
        return {}


def hit(ltp: float | None, stop: float, target: float | None, short: bool = False) -> tuple[str, float] | None:
    """('stop', fill) or ('target', fill) once the last price has crossed a level, else None. The stop is checked first."""
    if ltp is None or ltp <= 0:
        return None
    if short:
        if ltp >= stop:
            return "stop", ltp
        if target is not None and ltp <= target:
            return "target", float(target)
        return None
    if ltp <= stop:
        return "stop", ltp
    if target is not None and ltp >= target:
        return "target", float(target)
    return None
