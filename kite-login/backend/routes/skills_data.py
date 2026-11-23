"""Kite prices for the local trader skills in .claude/skills, so those scripts never touch the Kite token.

Read only: quotes, historical candles, lists of instruments, margin figures. Nothing here places an order, and no response carries a credential. The responses
use the chart JSON shape the skills read. The price and day range come from a Kite quote on each call; candles are cached briefly.
"""
from __future__ import annotations

import threading
import time
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel

from core import IST, _kite_call, app, kite_client
from routes.market import _instrument_list
from terminal.market import store as terminal_store
from terminal.sources import nse

Interval = Literal["1m", "3m", "5m", "10m", "15m", "1d"]
Range = Literal["1d", "5d", "1mo", "60d", "3mo", "1y"]
KITE_INTERVAL = {"1m": "minute", "3m": "3minute", "5m": "5minute", "10m": "10minute", "15m": "15minute", "1d": "day"}
RANGE_DAYS = {"1d": 6, "5d": 8, "1mo": 32, "60d": 92, "3mo": 95, "1y": 370}     # calendar days; "1d" keeps only the latest session of these
MAX_DAYS = {"1m": 60, "3m": 100, "5m": 100, "10m": 100, "15m": 200, "1d": 2000}  # Kite's limit on one request
INDEX_NAMES = {"^NSEI": "NIFTY 50", "^NSEBANK": "NIFTY BANK", "^INDIAVIX": "INDIA VIX", "^CNXMIDCAP": "NIFTY MIDCAP 100"}
INDEX_ALIASES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE", "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNXT50": "NIFTY NEXT 50", "INDIAVIX": "INDIA VIX", "SENSEX": "SENSEX", "BANKEX": "BANKEX"}
BSE_INDICES = {"SENSEX", "BANKEX"}
OPTION_EXCHANGES = {"NFO", "BFO"}
CONSTITUENTS = Path(__file__).resolve().parents[3] / "reports" / "index_constituents.csv"
LISTS = {"nifty50": "NIFTY 50", "banknifty": "NIFTY BANK", "sensex": "SENSEX", "midcap100": "NIFTY MIDCAP 100", "smallcap100": "NIFTY SMLCAP 100"}
MAX_SPARK = 25
CANDLE_TTL = {"1m": 10, "3m": 12, "5m": 15, "10m": 20, "15m": 30, "1d": 300}
PACE = 0.34                   # Kite allows about 3 historical requests a second
_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()
_pace_lock = threading.Lock()


def plain_symbol(symbol: str) -> str:
    """RELIANCE.NS or reliance -> RELIANCE; ^NSEI -> NIFTY 50."""
    s = symbol.strip().upper()
    if s in INDEX_NAMES:
        return INDEX_NAMES[s]
    s = s[:-3] if s.endswith(".NS") else s
    return INDEX_ALIASES.get(s, s)


def token_of(rows: list[dict], name: str) -> int | None:
    for r in rows:
        if r["tradingsymbol"] == name and (r.get("segment") == "INDICES" or r.get("instrument_type") == "EQ"):
            return r["instrument_token"]
    return None


def epoch(value) -> int:
    """A Kite time (naive means IST) or a YYYY-MM-DD date string as epoch seconds."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value[:10])
    if not hasattr(value, "hour"):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)
    return int(value.timestamp())


def latest_session(rows: list[list]) -> list[list]:
    """Only the last session's candles. rows: [epoch, o, h, l, c, v]."""
    if not rows:
        return rows
    day = datetime.fromtimestamp(rows[-1][0], IST).date()
    return [r for r in rows if datetime.fromtimestamp(r[0], IST).date() == day]


def build_result(wire: str, rows: list[list], quote: dict | None) -> dict:
    """One chart result, in the shape the skills read. A candle still forming is included; the skills only count closed ones."""
    q = quote or {}
    ohlc = q.get("ohlc") or {}
    when = q.get("last_trade_time") or q.get("timestamp")
    last = q.get("last_price")
    if last is None and rows:
        last = rows[-1][4]
    meta = {"symbol": wire, "currency": "INR", "exchangeName": "NSE", "gmtoffset": 19800, "exchangeTimezoneName": "Asia/Kolkata", "source": "kite",
            "regularMarketPrice": last, "regularMarketTime": epoch(when) if when else (rows[-1][0] if rows else None),
            "regularMarketDayHigh": ohlc.get("high"), "regularMarketDayLow": ohlc.get("low"), "regularMarketVolume": q.get("volume"),
            "previousClose": ohlc.get("close"), "chartPreviousClose": ohlc.get("close")}
    return {"meta": meta, "timestamp": [r[0] for r in rows],
            "indicators": {"quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}]}}


def _historical(kite: KiteConnect, token: int, start, end, interval: str) -> list[dict]:
    """Shares Kite's request limit with the live engines, so a refusal waits and tries again."""
    for attempt in range(4):
        with _pace_lock:
            time.sleep(PACE)
        try:
            return _kite_call(kite.historical_data, token, start, end, KITE_INTERVAL[interval])
        except HTTPException as exc:
            if "Too many" not in str(exc.detail) or attempt == 3:
                raise
            time.sleep(1.5)
    return []


def candles(kite: KiteConnect, name: str, token: int, rng: str, interval: str) -> list[list]:
    key = (name, rng, interval)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CANDLE_TTL[interval]:
            return hit[1]
    today = datetime.now(IST).date()
    start = today - timedelta(days=min(RANGE_DAYS[rng], MAX_DAYS[interval]))
    stored = terminal_store.candles.get(name) if interval == "1d" else None
    if stored:                 # the daily candles the app already keeps: no Kite request
        first = start.isoformat()
        rows = [[epoch(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in stored if str(r[0])[:10] >= first]
    else:
        rows = [[epoch(c["date"]), c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in _historical(kite, token, start, today, interval)]
    if rng == "1d":
        rows = latest_session(rows)
    with _cache_lock:
        _cache[key] = (now, rows)
    return rows


def _nse_rows(kite: KiteConnect) -> list[dict]:
    return _instrument_list(kite, "NSE")


def _rows_for(kite: KiteConnect, name: str) -> list[dict]:
    return _instrument_list(kite, "BSE") if name in BSE_INDICES else _nse_rows(kite)


def _exchange_of(name: str) -> str:
    return "BSE" if name in BSE_INDICES else "NSE"


def _contract_chart(symbol: str, range: str, interval: str, kite: KiteConnect) -> dict | JSONResponse:  # noqa: A002
    """A chart for one F&O contract, asked for as NFO:SYMBOL. Kite keeps candles only for contracts still trading."""
    exch, name = symbol.strip().upper().split(":", 1)
    row = next((r for r in _instrument_list(kite, exch) if r["tradingsymbol"] == name), None)
    if not row:
        return JSONResponse({"chart": {"result": None, "error": {"code": "Not Found", "description": f"{name} is not a live {exch} contract"}}}, status_code=404)
    key = f"{exch}:{name}"
    quote = (_kite_call(kite.quote, key) or {}).get(key)
    rows = candles(kite, key, row["instrument_token"], range, interval)
    return {"chart": {"result": [build_result(key, rows, quote)], "error": None}}


@app.get("/api/skills/chart/{symbol}")
def skills_chart(symbol: str, range: Range = "1d", interval: Interval = "5m", kite: KiteConnect = Depends(kite_client)):  # noqa: A002 - the skills' parameter name
    if symbol.strip().upper().split(":", 1)[0] in OPTION_EXCHANGES and ":" in symbol:
        return _contract_chart(symbol, range, interval, kite)
    name = plain_symbol(symbol)
    token = token_of(_rows_for(kite, name), name)
    if not token:
        return JSONResponse({"chart": {"result": None, "error": {"code": "Not Found", "description": f"{name} is not an NSE symbol"}}}, status_code=404)
    key = f"{_exchange_of(name)}:{name}"
    quote = (_kite_call(kite.quote, key) or {}).get(key)
    rows = [] if range == "1d" and interval == "1d" else candles(kite, name, token, range, interval)
    if range == "1d" and interval == "1d" and quote:        # a price check: the day's candle comes from the quote, with no historical request
        o = quote.get("ohlc") or {}
        rows = [[int(time.time()), o.get("open"), o.get("high"), o.get("low"), quote.get("last_price"), quote.get("volume")]]
    asked = symbol.strip().upper()
    asked = asked[:-3] if asked.endswith(".NS") else asked
    wire = asked if asked in INDEX_NAMES else f"{asked}.NS"
    return {"chart": {"result": [build_result(wire, rows, quote)], "error": None}}


@app.get("/api/skills/spark")
def skills_spark(symbols: str = Query(min_length=1, max_length=2000), range: Range = "1d", interval: Interval = "5m", kite: KiteConnect = Depends(kite_client)):  # noqa: A002
    wanted = [s for s in symbols.split(",") if s.strip()]
    if len(wanted) > MAX_SPARK:
        raise HTTPException(400, f"At most {MAX_SPARK} symbols a request")
    rows = _nse_rows(kite)
    named = {s: plain_symbol(s) for s in wanted}
    known = {s: n for s, n in named.items() if token_of(rows, n)}
    quotes = _kite_call(kite.quote, *[f"NSE:{n}" for n in known.values()]) if known else {}
    out = []
    for s, n in known.items():
        result = build_result(s.strip().upper(), candles(kite, n, token_of(rows, n), range, interval), (quotes or {}).get(f"NSE:{n}"))
        out.append({"symbol": s.strip().upper(), "response": [result]})
    return {"spark": {"result": out, "error": None}}


def _list_symbols(kite: KiteConnect, which: str) -> list[str]:
    if which in LISTS:
        with CONSTITUENTS.open(encoding="utf-8-sig", newline="") as fh:
            return [r["symbol"] for r in csv.DictReader(fh) if r.get("index") == LISTS[which] and r.get("symbol")]
    if which == "indices":
        return list(INDEX_ALIASES)
    if which == "fno":
        return sorted({r["name"] for r in _instrument_list(kite, "NFO") if r.get("instrument_type") == "FUT" and r.get("name") and r["name"] not in INDEX_ALIASES})
    if which == "all":
        return sorted(r["tradingsymbol"] for r in _nse_rows(kite) if r.get("instrument_type") == "EQ" and r.get("segment") == "NSE" and "-" not in r["tradingsymbol"])
    raise HTTPException(400, f"Unknown list {which}")


@app.get("/api/skills/universe")
def skills_universe(list: str = "fno", quotes: bool = False, kite: KiteConnect = Depends(kite_client)):  # noqa: A002 - the skills' parameter name
    """A list of symbols. With quotes, one Kite quote call per 500 symbols adds the day's numbers, so thousands of stocks can be filtered before any chart is read."""
    names = _list_symbols(kite, list)
    if not quotes:
        return {"list": list, "symbols": names}
    keys = {n: f"{_exchange_of(plain_symbol(n))}:{plain_symbol(n)}" for n in names}
    got: dict = {}
    wanted = [*keys.values()]
    for i in range(0, len(wanted), 450):
        got.update(_kite_call(kite.quote, *wanted[i:i + 450]) or {})
    rows = []
    for n, k in keys.items():
        q = got.get(k) or {}
        o = q.get("ohlc") or {}
        if q.get("last_price"):
            rows.append({"symbol": n, "last": q["last_price"], "open": o.get("open"), "high": o.get("high"), "low": o.get("low"), "prev_close": o.get("close"), "volume": q.get("volume") or 0})
    return {"list": list, "symbols": names, "rows": rows}


@app.get("/api/skills/fno")
def skills_fno(kite: KiteConnect = Depends(kite_client)):
    """Each F&O underlying with its lot size and near-month future's open interest, most open interest first."""
    today = datetime.now(IST).date()
    near: dict[str, dict] = {}
    for exch in ("NFO", "BFO"):
        for r in _instrument_list(kite, exch):
            if r.get("instrument_type") == "FUT" and r.get("name") and r.get("expiry") and r["expiry"] >= today:
                if r["name"] not in near or r["expiry"] < near[r["name"]]["expiry"]:
                    near[r["name"]] = {**r, "_exch": exch}
    keys = [f"{r['_exch']}:{r['tradingsymbol']}" for r in near.values()]
    got: dict = {}
    for i in range(0, len(keys), 450):
        got.update(_kite_call(kite.quote, *keys[i:i + 450]) or {})
    rows = []
    for name, r in near.items():
        q = got.get(f"{r['_exch']}:{r['tradingsymbol']}") or {}
        rows.append({"name": name, "index": name in INDEX_ALIASES, "exchange": r["_exch"], "future": r["tradingsymbol"], "expiry": r["expiry"].isoformat(), "lot_size": int(r.get("lot_size") or 1), "last": q.get("last_price"), "oi": q.get("oi") or 0, "volume": q.get("volume") or 0})
    rows.sort(key=lambda x: -x["oi"])
    return {"rows": rows}


@app.get("/api/skills/margins")
def skills_margins(kite: KiteConnect = Depends(kite_client)):
    """Equity-segment money figures from Kite, read only."""
    m = _kite_call(kite.margins, "equity") or {}
    avail, used = m.get("available") or {}, m.get("utilised") or {}
    return {"net": m.get("net"), "cash": avail.get("live_balance", avail.get("cash")), "collateral": avail.get("collateral"), "used": used.get("debits")}


class MarginLeg(BaseModel):
    exchange: str
    tradingsymbol: str
    transaction_type: Literal["BUY", "SELL"]
    quantity: int
    product: Literal["MIS", "NRML"] = "MIS"
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    price: float = 0


@app.post("/api/skills/order-margin")
def skills_order_margin(legs: list[MarginLeg], kite: KiteConnect = Depends(kite_client)):
    """What Kite would block for these legs, hedges counted. A calculation only: nothing is sent to the exchange."""
    if not legs or len(legs) > 4:
        raise HTTPException(400, "One to four legs")
    params = [{"exchange": x.exchange, "tradingsymbol": x.tradingsymbol, "transaction_type": x.transaction_type, "variety": "regular", "product": x.product, "order_type": x.order_type, "quantity": x.quantity, "price": x.price, "trigger_price": 0} for x in legs]
    res = _kite_call(kite.basket_order_margins, params, False, "compact") or {}
    final = res.get("final") or {}
    return {"total": final.get("total"), "legs": len(params)}


@app.get("/api/skills/announcements")
def skills_announcements(symbol: str | None = None):
    """NSE's corporate announcements, through the app's existing NSE reader. May be empty if NSE doesn't answer."""
    path = "corporate-announcements?index=equities" + (f"&symbol={symbol.upper()}" if symbol else "")
    res = nse(path, ttl=300, key=f"skills_ann_{(symbol or 'all').upper()}")
    rows = res["data"] if isinstance(res["data"], list) else []
    return {"rows": [{"symbol": r.get("symbol"), "subject": r.get("desc"), "detail": r.get("attchmntText"), "time": r.get("an_dt") or r.get("sort_date"), "link": r.get("attchmntFile")} for r in rows[:200]], "error": res["error"], "stale": res["stale"]}
