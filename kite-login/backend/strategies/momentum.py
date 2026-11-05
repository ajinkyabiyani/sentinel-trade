"""Daily momentum sweep over every NSE equity, feeding the ORB universe beyond F&O stocks.

Once a day (after the close, or at start-up when the last sweep is stale) one year of daily candles is read for each
NSE EQ-series stock (ETFs and T2T "-BE" series excluded) and scored:
  turnover   median 20-day close x volume (liquidity floor shared with ORB: INR10 crore)
  trend      close above its 20- and 50-day averages, 50-day average rising
  returns    1M / 3M / 6M; RS = 1-99 percentile of 0.4*3M + 0.3*6M + 0.3*1M across the liquid stocks
A stock is a momentum name when it is liquid, in an uptrend and RS >= 80. The top MAX_PICK by RS join the ORB
universe for the next session. Read-only: historical data only, nothing is ordered.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategies import regime

log = logging.getLogger("kite-desk.momentum")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"
TURNOVER_MIN = 1e8
RS_MIN = 80
MAX_PICK = 150


def _file(day: str) -> Path:
    return CACHE_DIR / f"momentum_{day}.json"


def latest() -> dict | None:
    files = sorted(CACHE_DIR.glob("momentum_*.json"))
    for f in reversed(files):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def picks(exclude: set[str]) -> list[dict]:
    """Momentum names for the next ORB session, strongest first, not already in `exclude` (the F&O list)."""
    d = latest()
    if not d:
        return []
    rows = [r for r in d["stocks"] if r.get("momentum") and r["symbol"] not in exclude]
    return sorted(rows, key=lambda r: -r["rs"])[:MAX_PICK]


def gauge(symbol: str) -> dict | None:
    d = latest()
    if not d:
        return None
    return next((r for r in d["stocks"] if r["symbol"] == symbol), None)


def _universe(instruments) -> list[dict]:
    return [r for r in instruments("NSE")
            if r.get("instrument_type") == "EQ" and r.get("segment") == "NSE" and "-" not in r["tradingsymbol"]
            and "ETF" not in (r.get("name") or "").upper() and not r["tradingsymbol"].endswith("BEES")]


def _score(rows: list[dict]) -> dict | None:
    if len(rows) < 60:
        return None
    c = [float(r["close"]) for r in rows]
    v = [float(r["volume"]) for r in rows]
    last = c[-1]
    tv = sorted(c[i] * v[i] for i in range(len(c) - 20, len(c)))
    turnover = (tv[9] + tv[10]) / 2
    sma = lambda n, end=len(c): sum(c[end - n:end]) / n if end >= n else math.nan  # noqa: E731
    s20, s50, s50_prev = sma(20), sma(50), sma(50, len(c) - 10)
    ret = lambda n: (last / c[-1 - n] - 1) * 100 if len(c) > n else math.nan  # noqa: E731
    r1, r3, r6 = ret(21), ret(63), ret(126)
    parts = [(w, x) for w, x in ((0.3, r1), (0.4, r3), (0.3, r6)) if math.isfinite(x)]
    raw = sum(w * x for w, x in parts) / sum(w for w, _ in parts) if parts else math.nan
    hi = max(float(r["high"]) for r in rows[-250:])
    return {"close": round(last, 2), "turnover_cr": round(turnover / 1e7, 1), "above20": last > s20, "above50": last > s50,
            "rising50": bool(math.isfinite(s50_prev) and s50 > s50_prev), "r1m": round(r1, 2), "r3m": round(r3, 2),
            "r6m": round(r6, 2) if math.isfinite(r6) else None, "from_high": round((last / hi - 1) * 100, 2), "_raw": raw}


class Sweep:
    def __init__(self):
        self.lock = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0, "day": None, "error": ""}

    def due(self, now: datetime) -> str | None:
        """The session day the next sweep should describe, or None when the cache is current."""
        day = now.date()
        if now.weekday() >= 5 or now.strftime("%H:%M") < "15:45":   # before today's close: describe the last session
            day -= timedelta(days=1)
            while day.weekday() >= 5:
                day -= timedelta(days=1)
        d = latest()
        fresh = d and d.get("day", "") >= day.isoformat()
        return None if fresh else day.isoformat()

    def run(self, kite, instruments, day: str, pace: float = 0.34) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            uni = _universe(instruments)
            self.progress = {"running": True, "done": 0, "total": len(uni), "day": day, "error": ""}
            end = datetime.fromisoformat(day).date()
            out = []
            regimes = []
            for i, r in enumerate(uni):
                rows = []
                for attempt in range(4):
                    try:
                        rows = kite.historical_data(r["instrument_token"], end - timedelta(days=380), end, "day")
                        break
                    except Exception as exc:  # noqa: BLE001 - rate limit / network
                        if "Too many" in str(exc) and attempt < 3:
                            time.sleep(1.5)
                            continue
                        if type(exc).__name__ == "TokenException":   # the login is gone; a bad instrument token is skipped
                            raise
                        break
                time.sleep(pace)
                sc = _score(rows)
                if sc:
                    out.append({"symbol": r["tradingsymbol"], "name": r.get("name"), **sc})
                rg = regime.classify(regime.to_rows(rows))
                if rg:
                    regimes.append({"symbol": r["tradingsymbol"], "name": r.get("name"), **regime.slim(rg)})
                self.progress["done"] = i + 1
            liquid = [x for x in out if x["turnover_cr"] * 1e7 >= TURNOVER_MIN and math.isfinite(x["_raw"])]
            ranked = sorted(liquid, key=lambda x: x["_raw"])
            for k, x in enumerate(ranked):
                x["rs"] = max(1, min(99, round((k + 1) / len(ranked) * 99)))
            for x in out:
                x.setdefault("rs", None)
                x["liquid"] = x in liquid
                x["momentum"] = bool(x["liquid"] and x["above20"] and x["above50"] and x["rising50"] and (x["rs"] or 0) >= RS_MIN)
                del x["_raw"]
            CACHE_DIR.mkdir(exist_ok=True)
            data = {"day": day, "built": datetime.now(IST).isoformat(timespec="seconds"), "scanned": len(uni),
                    "liquid": len(liquid), "momentum": sum(x["momentum"] for x in out), "stocks": out}
            tmp = _file(day).with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(_file(day))
            meta = {x["symbol"]: x for x in out}
            regime.save(day, [{**g, "liquid": meta.get(g["symbol"], {}).get("liquid", False), "rs": meta.get(g["symbol"], {}).get("rs")}
                              for g in regimes])
            for old in sorted(CACHE_DIR.glob("momentum_*.json"))[:-5]:   # keep a week
                old.unlink(missing_ok=True)
            log.info("momentum sweep %s: %d scanned, %d liquid, %d momentum", day, len(uni), len(liquid), data["momentum"])
        except Exception as exc:  # noqa: BLE001
            log.exception("momentum sweep failed")
            self.progress["error"] = str(exc)
        finally:
            self.progress["running"] = False
            self.lock.release()


sweep = Sweep()
