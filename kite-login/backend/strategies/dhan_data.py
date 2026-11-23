"""Dhan's data API, read-only, for research: expired option candles, which Kite does not keep.

Dhan stores expired options by strike relative to spot (ATM, ATM+1, ...), minute by minute, for five years, with OHLC,
IV, OI, volume and the spot. One request returns one strike offset and one option type for one day. Every answer is cached
on disk, so a replay never asks twice. The token is read from C:\\Algo\\dhan.env and never printed or logged.
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import common

ENV = Path(__file__).resolve().parents[3] / "dhan.env"
BASE = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE = common.CACHE_DIR / "dhan"
GAP = 0.35                     # seconds between requests from this process
INDEX_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27, "MIDCPNIFTY": 442}
FIELDS = ["open", "high", "low", "close", "iv", "volume", "oi", "spot", "strike"]

_lock = threading.Lock()
_last = [0.0]
_ids: dict = {}


class DhanError(Exception):
    pass


def _creds() -> tuple[str, str]:
    env = {}
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    cid, tok = env.get("DHAN_CLIENT_ID", ""), env.get("DHAN_ACCESS_TOKEN", "")
    if not cid or not tok:
        raise DhanError("dhan.env needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
    return cid, tok


def _post(path: str, body: dict) -> dict:
    cid, tok = _creds()
    headers = {"access-token": tok, "client-id": cid, "Accept": "application/json", "Content-Type": "application/json"}
    for attempt in range(6):
        with _lock:
            wait = _last[0] + GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            if e.code == 401 or "DH-901" in text or "DH-906" in text or "Invalid Token" in text:
                raise DhanError("the Dhan token was refused (expired?): make a new one and save it in dhan.env") from None
            if e.code in (429, 500, 502, 503, 504) or "DH-904" in text:
                time.sleep(2 ** attempt)
                continue
            raise DhanError(f"Dhan said {e.code}: {text}") from None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise DhanError(f"Dhan did not answer {path} after retries")


def security_id(name: str) -> int:
    """Dhan's id for an index (NIFTY, BANKNIFTY) or an NSE stock, from Dhan's instrument list (cached for a week)."""
    if name in INDEX_IDS:
        return INDEX_IDS[name]
    if not _ids:
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / "master.csv"
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            with urllib.request.urlopen(MASTER_URL, timeout=120) as r:
                path.write_bytes(r.read())
        for row in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8", errors="ignore"))):
            if row.get("SEM_EXM_EXCH_ID") == "NSE" and row.get("SEM_SEGMENT") == "E" and row.get("SEM_INSTRUMENT_NAME") == "EQUITY" and row.get("SEM_SERIES") == "EQ":
                _ids.setdefault(row["SEM_TRADING_SYMBOL"], int(row["SEM_SMST_SECURITY_ID"]))
    if name not in _ids:
        raise DhanError(f"{name} is not in Dhan's instrument list")
    return _ids[name]


def offset_label(k: int) -> str:
    return "ATM" if k == 0 else f"ATM{k:+d}"


def rolling(name: str, day: date, offset: int, call: bool, code: int = 1, monthly: bool | None = None, interval: int = 1) -> dict[str, dict]:
    """One day of candles for one strike offset: {HH:MM: {o, h, l, c, iv, oi, v, spot, strike}}. Empty if Dhan has none.
    Use 1-minute candles: the ATM strike can change inside a 5-minute candle, and Dhan's 5-minute candle then mixes the
    prices of two strikes (a fake gap). Each 1-minute candle carries the strike it belongs to.
    code 1 is the nearest expiry (on an expiry day, that day's contract), 2 the one after. Indices other than Nifty use monthly expiries."""
    index = name in INDEX_IDS
    if monthly is None:
        monthly = not (index and name == "NIFTY")
    key = f"{name}_{day.isoformat()}_{'M' if monthly else 'W'}{code}_{offset_label(offset)}_{'CE' if call else 'PE'}"
    path = CACHE / ("rolling" if interval == 5 else f"rolling{interval}") / day.isoformat() / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    body = {"exchangeSegment": "NSE_FNO", "interval": str(interval), "securityId": str(security_id(name)), "instrument": "OPTIDX" if index else "OPTSTK",
            "expiryFlag": "MONTH" if monthly else "WEEK", "expiryCode": code, "strike": offset_label(offset), "drvOptionType": "CALL" if call else "PUT",
            "requiredData": FIELDS, "fromDate": day.isoformat(), "toDate": (day + timedelta(days=1)).isoformat()}
    d = _post("/charts/rollingoption", body)
    side = (d.get("data") or {}).get("ce" if call else "pe") or {}
    out = {}
    ts = side.get("timestamp") or []
    for i, t in enumerate(ts):
        when = datetime.fromtimestamp(t, common.IST)
        if when.date() != day:
            continue

        def get(f):
            xs = side.get(f) or []
            return xs[i] if i < len(xs) else None
        out[when.strftime("%H:%M")] = {"o": get("open"), "h": get("high"), "l": get("low"), "c": get("close"), "iv": get("iv"),
                                       "oi": get("oi"), "v": get("volume"), "spot": get("spot"), "strike": get("strike")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(out), encoding="utf-8")
    for attempt in range(5):            # Windows: a scanner or another reader can hold the file for a moment
        try:
            tmp.replace(path)
            break
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    else:
        tmp.unlink(missing_ok=True)
    return out
