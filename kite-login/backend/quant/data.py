"""Quant Desk data store: three years of Kite daily candles for Nifty 50, Bank Nifty, NIFTYBEES and every F&O stock.

Saved one file per symbol under .orb_cache/quant/daily, as [date, open, high, low, close, volume]. A refresh downloads a symbol
once in full, then only the days since its last candle. Kite allows about 3 requests a second; this paces itself well under that.
Read only: nothing here places an order, and the access token never leaves this process.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from kiteconnect import KiteConnect

from core import store
from strategies import common

log = logging.getLogger("kite-desk.quant.data")
DIR = common.CACHE_DIR / "quant" / "daily"
YEARS = 3
PACE = 0.4
INDICES = {"NIFTY 50": "NIFTY 50", "NIFTY BANK": "NIFTY BANK"}       # NSE tradingsymbol -> file name
BENCHMARK = "NIFTYBEES"
SKIP = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def kite() -> KiteConnect:
    d = store.load()
    if not d:
        raise RuntimeError("Not logged in to Kite.")
    k = KiteConnect(api_key=d["api_key"])
    k.set_access_token(d["access_token"])
    return k


def _file(sym: str) -> Path:
    return DIR / f"{sym.replace(' ', '_').replace('&', 'and')}.json"


def load(sym: str) -> list[list]:
    try:
        return json.loads(_file(sym).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def symbols() -> list[str]:
    """Every symbol on file."""
    return sorted(json.loads(p.read_text(encoding="utf-8")) and p.stem.replace("_", " ") for p in DIR.glob("*.json")) if DIR.exists() else []


def universe(k: KiteConnect) -> dict[str, int]:
    """{symbol: NSE instrument token} for the indices, the benchmark and every F&O stock."""
    nse = {r["tradingsymbol"]: r["instrument_token"] for r in k.instruments("NSE")}
    fno = sorted({r["name"] for r in k.instruments("NFO") if r.get("instrument_type") == "FUT" and r.get("name") and r["name"] not in SKIP})
    out = {name: nse[name] for name in INDICES if name in nse}
    if BENCHMARK in nse:
        out[BENCHMARK] = nse[BENCHMARK]
    out.update({s: nse[s] for s in fno if s in nse})
    return out


def _fetch(k: KiteConnect, token: int, start: date, end: date) -> list[list]:
    for attempt in range(4):
        try:
            rows = k.historical_data(token, start, end, "day")
            return [[str(r["date"])[:10], r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0] for r in rows]
        except Exception as exc:  # noqa: BLE001 - a rate limit or a network blip
            if type(exc).__name__ == "TokenException":
                raise
            time.sleep(1.5 * (attempt + 1))
    return []


def refresh(only: list[str] | None = None, progress=print) -> dict:
    """Downloads what's missing. -> {"symbols", "fetched", "failed", "from", "to"}."""
    k = kite()
    DIR.mkdir(parents=True, exist_ok=True)
    uni = universe(k)
    if only:
        uni = {s: t for s, t in uni.items() if s in only}
    today = datetime.now(common.IST).date()
    first = today - timedelta(days=365 * YEARS + 10)
    fetched, failed = 0, []
    for i, (sym, token) in enumerate(uni.items()):
        have = load(sym)
        start = date.fromisoformat(have[-1][0]) + timedelta(days=1) if have else first
        if start > today:
            continue
        rows = _fetch(k, token, start, today)
        time.sleep(PACE)
        if not rows and not have:
            failed.append(sym)
            continue
        merged = {r[0]: r for r in have}
        merged.update({r[0]: r for r in rows})
        _file(sym).write_text(json.dumps([merged[d] for d in sorted(merged)]), encoding="utf-8")
        fetched += 1
        if progress and i % 25 == 0:
            progress(f"{i + 1}/{len(uni)} {sym}")
    out = {"symbols": len(uni), "fetched": fetched, "failed": failed, "from": first.isoformat(), "to": today.isoformat()}
    (DIR.parent / "data_status.json").write_text(json.dumps({**out, "at": datetime.now(common.IST).isoformat(timespec="seconds")}), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    print(refresh(sys.argv[1:] or None))
