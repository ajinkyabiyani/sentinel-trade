"""1-minute and 5-minute candles from Kite for Nifty, Bank Nifty, India VIX and the most liquid F&O stocks, kept on disk.

Files: C:\\Algo\\market_data\\candles_<N>m\\<SYMBOL>.csv.gz with date,time,open,high,low,close,volume (09:15-15:29 IST).
"Most liquid" = the F&O stocks with the highest average daily traded value (close x volume) over the last LIQUID_DAYS
sessions; the chosen list and each name's value are saved in liquid.json. A symbol whose file exists is skipped.

    python -m quant.candles --from 2026-01-01 --to 2026-11-20 --top 50
Load one:  from quant.candles import load; df = load("RELIANCE", 1)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("MARKET_DATA_ROOT") or Path(__file__).resolve().parents[3] / "market_data")   # MARKET_DATA_ROOT: another year's folder
INDICES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "INDIAVIX": "INDIA VIX"}
CHUNK = {1: 58, 5: 95}          # Kite: 60 days of 1-minute, 100 days of 5-minute per request
KITE_INTERVAL = {1: "minute", 5: "5minute"}
LIQUID_DAYS = 60
SECTORS = ["NIFTY PVT BANK", "NIFTY PSU BANK", "NIFTY FIN SERVICE", "NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY HEALTHCARE",
           "NIFTY FMCG", "NIFTY METAL", "NIFTY ENERGY", "NIFTY OIL AND GAS", "NIFTY REALTY", "NIFTY INFRA", "NIFTY MEDIA",
           "NIFTY CONSR DURBL", "NIFTY CPSE", "NIFTY MID SELECT", "NIFTY NEXT 50"]
GAP = 0.36                      # Kite: 3 historical requests a second


def _file(sym: str, minutes: int) -> Path:
    return ROOT / f"candles_{minutes}m" / (sym.replace("&", "and") + ".csv.gz")


def _kite():
    from strategies import fno_flow
    k = fno_flow._kite()
    if not k:
        raise SystemExit("Kite isn't logged in: log in through the app first")
    return k


def _call(fn, *a, **kw):
    for attempt in range(5):
        try:
            out = fn(*a, **kw)
            time.sleep(GAP)
            return out
        except Exception as exc:  # noqa: BLE001 - rate limit or a blip: wait and retry
            if "Too many" not in str(exc) and attempt >= 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    return []


def tokens(k) -> dict[str, int]:
    from routes.market import _instrument_list
    return {r["tradingsymbol"]: r["instrument_token"] for r in _instrument_list(k, "NSE") if r.get("segment") in ("NSE", "INDICES")}


def most_liquid(k, tok: dict, top: int, upto: date) -> list[tuple[str, float]]:
    """[(stock, average daily traded value in ₹ crore)] for the top F&O stocks, highest first."""
    from routes import breakout
    rows = []
    for n in breakout.fno_names():
        if n not in tok:
            continue
        d = _call(k.historical_data, tok[n], upto - timedelta(days=LIQUID_DAYS * 7 // 5 + 10), upto, "day")[-LIQUID_DAYS:]
        if d:
            rows.append((n, round(sum(r["close"] * r["volume"] for r in d) / len(d) / 1e7, 1)))
    rows.sort(key=lambda x: -x[1])
    return rows[:top]


def fetch(k, token: int, minutes: int, start: date, end: date) -> list[list]:
    rows, a = [], start
    while a <= end:
        b = min(a + timedelta(days=CHUNK[minutes]), end)
        for r in _call(k.historical_data, token, a, b, KITE_INTERVAL[minutes]):
            t = r["date"].strftime("%H:%M")
            if "09:15" <= t <= "15:29":
                rows.append([r["date"].date().isoformat(), t, r["open"], r["high"], r["low"], r["close"], r["volume"]])
        a = b + timedelta(days=1)
    return rows


def save(sym: str, minutes: int, rows: list[list]) -> None:
    path = _file(sym, minutes)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "time", "open", "high", "low", "close", "volume"])
    w.writerows(sorted({(r[0], r[1]): r for r in rows}.values()))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(buf.getvalue().encode()))
    tmp.replace(path)


def load(sym: str, minutes: int = 5):
    import pandas as pd
    df = pd.read_csv(_file(sym, minutes))
    df.index = pd.to_datetime(df["date"] + " " + df["time"])
    return df.drop(columns=["date", "time"])


def check(minutes: int) -> dict:
    out = {}
    expect = 375 // minutes
    for p in sorted((ROOT / f"candles_{minutes}m").glob("*.csv.gz")):
        days: dict[str, int] = {}
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                days[r["date"]] = days.get(r["date"], 0) + 1
        out[p.name[:-7]] = {"rows": sum(days.values()), "days": len(days), "first": min(days, default=None), "last": max(days, default=None),
                            "short_days": sorted(d for d, n in days.items() if n < expect * 0.9)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--all5", action="store_true", help="5-minute candles for every F&O stock and the sector indices")
    a = ap.parse_args()
    if a.check:
        for m in (1, 5):
            res = check(m)
            print(f"{m}m: {len(res)} symbols, {sum(v['rows'] for v in res.values())} candles, days {sorted({v['days'] for v in res.values()})}")
            print("   short days:", {k: v["short_days"] for k, v in res.items() if v["short_days"]})
        return
    start, end = date.fromisoformat(a.frm), date.fromisoformat(a.to)
    k = _kite()
    tok = tokens(k)
    if a.all5:
        from routes import breakout
        todo = [(n.replace(" ", "_"), tok[n]) for n in SECTORS if n in tok] + [(n, tok[n]) for n in sorted(breakout.fno_names()) if n in tok]
        t0 = time.time()
        for i, (sym, t) in enumerate(todo):
            if not _file(sym, 5).exists():
                save(sym, 5, fetch(k, t, 5, start, end))
            if i % 20 == 0:
                print(f"{i + 1}/{len(todo)} {sym} {round(time.time() - t0)}s", flush=True)
        print("done", round(time.time() - t0), "s")
        return
    liquid = most_liquid(k, tok, a.top, end)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "liquid.json").write_text(json.dumps({"as_of": end.isoformat(), "days": LIQUID_DAYS, "crore_per_day": dict(liquid)}, indent=1), encoding="utf-8")
    print("liquid:", ", ".join(n for n, _ in liquid), flush=True)
    todo = [(s, tok[n]) for s, n in INDICES.items() if n in tok] + [(n, tok[n]) for n, _ in liquid]
    t0 = time.time()
    for i, (sym, t) in enumerate(todo):
        for m in (1, 5):
            if not _file(sym, m).exists():
                save(sym, m, fetch(k, t, m, start, end))
        print(f"{i + 1}/{len(todo)} {sym} {round(time.time() - t0)}s", flush=True)
    print("done", round(time.time() - t0), "s")


if __name__ == "__main__":
    sys.exit(main())
