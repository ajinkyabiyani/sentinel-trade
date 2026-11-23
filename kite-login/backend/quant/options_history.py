"""Two years of expired index option candles from Dhan, kept on disk for research (Kite keeps no expired options).

Dhan stores options by strike relative to spot. One request = one offset (ATM-10 ... ATM+10), one side, two calendar days of
1-minute candles. 1-minute is kept because Dhan's 5-minute candle mixes two strikes when the ATM moves inside it; every
1-minute row carries its own strike, so clean 5-minute candles per real strike can be built from these.

Files: C:\\Algo\\market_data\\options_1m\\<UNDERLYING>\\<YYYY-MM>.csv.gz with
    date,time,offset,type,strike,spot,open,high,low,close,iv,oi,volume
for the nearest expiry (Dhan expiryCode 1: on an expiry day, that day's contract). Nifty and Sensex use weekly expiries,
the others monthly. A month whose file exists is skipped, so a stopped run resumes; the current month is rewritten.

    python -m quant.options_history                       (Nifty and Bank Nifty)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from quant import candles
from strategies import common, dhan_data

ROOT = candles.ROOT / "options_1m"
START = date(2024, 11, 20)              # 2 years; also after SEBI cut weekly expiries to Nifty and Sensex (Nov 2024)
OFFSETS = 10
WORKERS = 3
# name: (Dhan security id, exchange segment, expiry flag)
UNDERLYINGS = {"NIFTY": (13, "NSE_FNO", "WEEK"), "BANKNIFTY": (25, "NSE_FNO", "MONTH")}
OTHERS = {"SENSEX": (51, "BSE_FNO", "WEEK"), "FINNIFTY": (27, "NSE_FNO", "MONTH"), "MIDCPNIFTY": (442, "NSE_FNO", "MONTH")}   # not downloaded by default
STOCK_OFFSETS = 3              # Dhan keeps ATM+/-3 for stock options
LIQUID = ROOT.parent / "liquid.json"


def spec(name: str) -> tuple[int, str, str, str, int]:
    """(Dhan security id, segment, expiry flag, instrument, strikes each side) for an index or an NSE stock."""
    if name in UNDERLYINGS or name in OTHERS:
        sid, seg, flag = (UNDERLYINGS | OTHERS)[name]
        return sid, seg, flag, "OPTIDX", OFFSETS
    return dhan_data.security_id(name), "NSE_FNO", "MONTH", "OPTSTK", STOCK_OFFSETS


COLS = ["date", "time", "offset", "type", "strike", "spot", "open", "high", "low", "close", "iv", "oi", "volume"]


def pair_starts(a: date, b: date) -> list[date]:
    """Request start days covering every weekday in [a, b]: each request returns its start day and the next."""
    out, d = [], a
    while d <= b:
        if d.weekday() >= 5:
            d += timedelta(days=7 - d.weekday())
            continue
        out.append(d)
        d += timedelta(days=2) if d.weekday() < 4 else timedelta(days=3)
    return out


def one(name: str, start: date, k: int, call: bool) -> list[list]:
    sid, seg, flag, inst, _ = spec(name)
    body = {"exchangeSegment": seg, "interval": "1", "securityId": str(sid), "instrument": inst, "expiryFlag": flag, "expiryCode": 1,
            "strike": dhan_data.offset_label(k), "drvOptionType": "CALL" if call else "PUT", "requiredData": dhan_data.FIELDS,
            "fromDate": start.isoformat(), "toDate": (start + timedelta(days=1)).isoformat()}
    x = (dhan_data._post("/charts/rollingoption", body).get("data") or {}).get("ce" if call else "pe") or {}
    rows = []
    for i, ts in enumerate(x.get("timestamp") or []):
        t = datetime.fromtimestamp(ts, common.IST)

        def g(f):
            v = x.get(f) or []
            return v[i] if i < len(v) else None
        rows.append([t.date().isoformat(), t.strftime("%H:%M"), k, "CE" if call else "PE", g("strike"), g("spot"),
                     g("open"), g("high"), g("low"), g("close"), None if g("iv") is None else round(g("iv"), 3), g("oi"), g("volume")])
    return rows


def month(name: str, y: int, m: int, last: date) -> int:
    a = date(y, m, 1)
    b = min(date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1), last)
    path = ROOT / name / f"{y}-{m:02d}.csv.gz"
    if path.exists() and b < last:
        return -1
    n = spec(name)[4]
    jobs = [(s, k, c) for s in pair_starts(a, b) for k in range(-n, n + 1) for c in (True, False)]
    def safe(j):
        try:
            return one(name, *j)
        except dhan_data.DhanError as exc:
            if "token" in str(exc):
                raise
            try:
                time.sleep(3)
                return one(name, *j)
            except dhan_data.DhanError:
                print(f"  gap: {name} {j[0]} {dhan_data.offset_label(j[1])} {'CE' if j[2] else 'PE'}", flush=True)
                return []
    with ThreadPoolExecutor(WORKERS) as ex:
        parts = list(ex.map(safe, jobs))
    rows = {}
    for part in parts:
        for r in part:
            if a.isoformat() <= r[0] <= b.isoformat():
                rows[(r[0], r[1], r[2], r[3])] = r
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLS)
    w.writerows(rows[key] for key in sorted(rows))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(buf.getvalue().encode()))
    tmp.replace(path)
    return len(rows)


def load(name: str, ym: str):
    """One month as a pandas DataFrame."""
    import pandas as pd
    return pd.read_csv(ROOT / name / f"{ym}.csv.gz")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--stocks", action="store_true", help="the stocks in market_data/liquid.json instead of the indices")
    ap.add_argument("--from", dest="frm", default=START.isoformat(), help="first day, YYYY-MM-DD")
    ap.add_argument("--to", default=None, help="last day, YYYY-MM-DD (default yesterday)")
    a = ap.parse_args()
    first = date.fromisoformat(a.frm)
    last = min(date.fromisoformat(a.to) if a.to else date.today(), date.today()) if a.to else date.today() - timedelta(days=1)
    import json
    names = a.only or (list(json.loads(LIQUID.read_text(encoding="utf-8"))["crore_per_day"]) if a.stocks else list(UNDERLYINGS))
    t0 = time.time()
    if a.stocks:                                    # month by month, so each month is complete for every stock before the next
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            for name in names:
                try:
                    n = month(name, y, m, last)
                except dhan_data.DhanError as exc:
                    if "token" in str(exc):
                        print(f"stopped at {name} {y}-{m:02d}: {exc}", flush=True)
                        return
                    print(f"skipped {name} {y}-{m:02d}: {exc}", flush=True)
                    continue
                if n >= 0:
                    print(f"{name} {y}-{m:02d}: {n} rows, {round(time.time() - t0)}s", flush=True)
            print(f"month {y}-{m:02d} complete", flush=True)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        print("done", round(time.time() - t0), "s")
        return
    for name in names:
        y, m = first.year, first.month
        while (y, m) <= (last.year, last.month):
            try:
                n = month(name, y, m, last)
            except dhan_data.DhanError as exc:
                print(f"stopped at {name} {y}-{m:02d}: {exc}", flush=True)
                return
            if n >= 0:
                print(f"{name} {y}-{m:02d}: {n} rows, {round(time.time() - t0)}s", flush=True)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    sys.exit(main())
