"""Quant Desk 5-minute store: three years of Kite 5-minute candles for Nifty 50, Bank Nifty and the 80 most traded F&O stocks.

Kite serves at most 100 days of 5-minute candles a request, so each symbol takes about 11 requests. Saved gzipped, one file
per symbol: {day: [[minute_of_day, o, h, l, c, v], ...]} with minute_of_day counted from midnight (09:15 = 555).
Read only; the access token never leaves this process.
"""
from __future__ import annotations

import gzip
import json
import statistics
import time
from datetime import date, datetime, timedelta

from quant import data
from strategies import common

DIR = common.CACHE_DIR / "quant" / "intraday"
TOP = 80
CHUNK = 95


def _file(sym: str):
    return DIR / f"{sym.replace(' ', '_').replace('&', 'and')}.json.gz"


def load(sym: str) -> dict:
    try:
        with gzip.open(_file(sym), "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def pick() -> list[str]:
    """The indices and the 80 F&O stocks with the highest average daily traded value over the last 60 sessions."""
    turnover = {}
    for p in data.DIR.glob("*.json"):
        sym = p.stem.replace("_", " ")
        if sym in ("NIFTY 50", "NIFTY BANK", "NIFTYBEES"):
            continue
        bars = json.loads(p.read_text(encoding="utf-8"))[-60:]
        if bars:
            turnover[sym] = statistics.fmean(b[4] * b[5] for b in bars)
    return ["NIFTY 50", "NIFTY BANK"] + sorted(turnover, key=lambda s: -turnover[s])[:TOP]


def refresh(progress=print) -> dict:
    k = data.kite()
    uni = data.universe(k)
    names = [s.replace("MandM", "M&M") for s in pick()]
    DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(common.IST).date()
    first = today - timedelta(days=365 * data.YEARS + 10)
    done, failed = 0, []
    for n, sym in enumerate(names):
        token = uni.get(sym)
        if not token:
            failed.append(sym)
            continue
        have = load(sym)
        start = date.fromisoformat(max(have)) + timedelta(days=1) if have else first
        while start <= today:
            end = min(start + timedelta(days=CHUNK), today)
            for attempt in range(4):
                try:
                    rows = k.historical_data(token, start, end, "5minute")
                    break
                except Exception as exc:  # noqa: BLE001 - a rate limit or a network blip
                    if type(exc).__name__ == "TokenException":
                        raise
                    rows = []
                    time.sleep(2 * (attempt + 1))
            for r in rows:
                ts = r["date"]
                d = ts.date().isoformat()
                have.setdefault(d, []).append([ts.hour * 60 + ts.minute, r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0])
            time.sleep(data.PACE)
            start = end + timedelta(days=1)
        with gzip.open(_file(sym), "wt", encoding="utf-8") as fh:
            json.dump(have, fh)
        done += 1
        if progress and n % 10 == 0:
            progress(f"{n + 1}/{len(names)} {sym}: {len(have)} sessions")
    return {"symbols": len(names), "saved": done, "failed": failed}


if __name__ == "__main__":
    print(refresh())
