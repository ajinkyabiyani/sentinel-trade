"""Market data: instruments, quotes, indices, market regime, movers, stock page and peers."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
import csv
import re
import threading
import time

from fastapi import Depends, HTTPException, Query
from kiteconnect import KiteConnect

from core import IST, REPORTS_DIR, _kite_call, app, kite_client, log, store
from strategies import momentum
from strategies import regime


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX")
Exchange = Literal["NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX"]
_instruments: dict[str, tuple[date, list[dict]]] = {}
_instruments_lock = threading.Lock()


def _instrument_list(kite: KiteConnect, exchange: str) -> list[dict]:
    """kite.instruments(exchange), cached for the day (the dump changes once each morning)."""
    today = datetime.now(IST).date()
    with _instruments_lock:
        hit = _instruments.get(exchange)
        if hit and hit[0] == today:
            return hit[1]
    rows = _kite_call(kite.instruments, exchange)
    with _instruments_lock:
        _instruments[exchange] = (today, rows)
    return rows


@app.get("/api/instruments/search")
def instrument_search(q: str = Query(min_length=1, max_length=40), exchange: Exchange = "NSE",
                      kite: KiteConnect = Depends(kite_client)):
    words = q.upper().split()
    hits = []
    for r in _instrument_list(kite, exchange):
        ts, name = r["tradingsymbol"], (r.get("name") or "").upper()
        if all(w in ts or w in name for w in words):
            exp = r.get("expiry")
            hits.append(((ts != words[0], not ts.startswith(words[0]), exp or date.min, len(ts)), r))
    hits.sort(key=lambda h: h[0])
    return [{"tradingsymbol": r["tradingsymbol"], "name": r.get("name"), "exchange": r["exchange"],
             "instrument_type": r.get("instrument_type"), "segment": r.get("segment"),
             "lot_size": r.get("lot_size") or 1, "tick_size": r.get("tick_size"),
             "expiry": r["expiry"].isoformat() if r.get("expiry") else None,
             "strike": r.get("strike") or None, "instrument_token": r.get("instrument_token")}
            for _, r in hits[:25]]


@app.get("/api/quote")
def quote(i: list[str] = Query(max_length=200), kite: KiteConnect = Depends(kite_client)):
    """Live quote for EXCHANGE:SYMBOL keys (repeat ?i=)."""
    data = _kite_call(kite.quote, *i[:200])
    out = {}
    for k, v in data.items():
        depth = v.get("depth") or {}
        bid = next((d for d in depth.get("buy", []) if d.get("price")), None)
        ask = next((d for d in depth.get("sell", []) if d.get("price")), None)
        out[k] = {"last_price": v.get("last_price"), "net_change": v.get("net_change"), "ohlc": v.get("ohlc"),
                  "volume": v.get("volume"), "bid": bid and bid["price"], "ask": ask and ask["price"],
                  "lower_circuit": v.get("lower_circuit_limit"), "upper_circuit": v.get("upper_circuit_limit")}
    return out



_index_history: dict[str, tuple[date, list, list]] = {}


@app.get("/api/market/indices")
def market_indices(kite: KiteConnect = Depends(kite_client)):
    """The five core indices: live quote plus ~2 years of daily closes (history cached for the day)."""
    today = datetime.now(IST).date()
    keys = ["BSE:SENSEX", "NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:NIFTY MIDCAP 100", "NSE:NIFTY SMLCAP 100"]
    tokens = {}
    for exch in ("NSE", "BSE"):
        for r in _instrument_list(kite, exch):
            k = f"{exch}:{r['tradingsymbol']}"
            if k in keys and r.get("segment") == "INDICES":
                tokens[k] = r["instrument_token"]
    extras = {}   # quote key -> commodity / currency tile (near-month future)
    for exch, name, label, unit in COMMODITY_TILES:
        inst = _near_future(kite, exch, name)
        if inst:
            k = f"{exch}:{inst['tradingsymbol']}"
            tokens[k] = inst["instrument_token"]
            extras[k] = {"name": name, "label": label, "unit": unit, "contract": inst["tradingsymbol"],
                         "expiry": inst["expiry"].isoformat(), "kind": "commodity"}
    quotes = _kite_call(kite.quote, *keys, *extras)
    out = []
    for k in [*keys, *extras]:
        hit = _index_history.get(k)
        if (not hit or hit[0] != today) and k in tokens:
            rows = _kite_call(kite.historical_data, tokens[k], today - timedelta(days=730), today, "day",
                              continuous=k in extras)   # continuous: stitched front-month history across expiries
            hit = (today, [[r["date"].strftime("%Y-%m-%d"), r["close"]] for r in rows], regime.to_rows(rows))
            _index_history[k] = hit
        q = quotes.get(k) or {}
        last, prev = q.get("last_price"), (q.get("ohlc") or {}).get("close")
        exch, name = k.split(":", 1)
        o = q.get("ohlc") or {}
        rg = regime.classify(regime.splice(hit[2], today.isoformat(), o.get("open"), o.get("high"), o.get("low"), last)) if hit else None
        out.append({"key": k, "name": name, "symbol": name, "exchange": exch, "kind": "index", "last": last, "prev_close": prev, "regime": rg,
                    "change": round(last - prev, 4) if last and prev else None,
                    "change_pct": round((last / prev - 1) * 100, 2) if last and prev else None,
                    "ohlc": q.get("ohlc"), "series": hit[1] if hit else [], **extras.get(k, {})})
    return out


_vix_history: tuple[date, list] | None = None
_regime_view: tuple[float, dict] | None = None


def _india_vix(kite: KiteConnect) -> dict | None:
    """India VIX now, ranked against its own last year."""
    global _vix_history
    today = datetime.now(IST).date()
    if not _vix_history or _vix_history[0] != today:
        inst = next((r for r in _instrument_list(kite, "NSE") if r["tradingsymbol"] == "INDIA VIX"), None)
        if not inst:
            return None
        rows = _kite_call(kite.historical_data, inst["instrument_token"], today - timedelta(days=380), today, "day")
        _vix_history = (today, [float(r["close"]) for r in rows][-250:])
    last = ((_kite_call(kite.quote, "NSE:INDIA VIX") or {}).get("NSE:INDIA VIX") or {}).get("last_price")
    closes = _vix_history[1]
    last = last or (closes[-1] if closes else None)
    if last is None or not closes:
        return None
    return {"last": round(float(last), 2), "pct": round(100 * sum(c <= last for c in closes) / len(closes)),
            "chg_5d": round((last / closes[-6] - 1) * 100, 1) if len(closes) > 5 else None}


def _breadth() -> dict | None:
    """Regime breadth of the liquid stocks; until the first regime file exists, the sweep's trend flags stand in."""
    rg = regime.latest()
    if rg and rg.get("stocks"):
        return {**regime.breadth_of(rg["stocks"]), "day": rg["day"], "source": "regime"}
    sw = momentum.latest()
    pool = [x for x in (sw or {}).get("stocks", []) if x.get("liquid")]
    if not pool:
        return None
    bull = sum(bool(x["above50"] and x["rising50"]) for x in pool)
    bear = sum(bool(not x["above50"] and not x["rising50"]) for x in pool)
    return {"total": len(pool), "bull_pct": round(bull / len(pool) * 100, 1), "bear_pct": round(bear / len(pool) * 100, 1),
            "regimes": {}, "day": sw["day"], "source": "trend flags"}


@app.get("/api/regime/market")
def regime_market(kite: KiteConnect = Depends(kite_client)):
    """The overall market view: the core indices' regimes, breadth and India VIX (recomputed at most every 45 s)."""
    global _regime_view
    import time as _t
    if _regime_view and _t.time() - _regime_view[0] < 45:
        return _regime_view[1]
    tiles = market_indices(kite)
    indices = {m["name"]: m["regime"] for m in tiles if m.get("regime")}
    breadth, vix = _breadth(), _india_vix(kite)
    out = {**regime.market_view(indices, breadth, vix), "asof": datetime.now(IST).isoformat(timespec="seconds"), "breadth": breadth, "vix": vix,
           "indices": [{"key": m["key"], "name": m["name"], "symbol": m.get("symbol"), "label": m.get("label"), "exchange": m["exchange"], "last": m["last"],
                        "change_pct": m["change_pct"], "kind": m["kind"], "regime": m.get("regime")} for m in tiles]}
    _regime_view = (_t.time(), out)
    return out


@app.get("/api/regime/stocks")
def regime_stocks():
    """Every scanned stock's regime (nightly from the sweep, liquid stocks refreshed hourly in market hours)."""
    rg = regime.latest() or {}
    return {"day": rg.get("day"), "built": rg.get("built"), "partial": rg.get("partial"), "stocks": rg.get("stocks", []),
            "progress": regime.refresher.progress, "breadth": regime.breadth_of(rg.get("stocks", []))}


def _regime_loop() -> None:
    """Keeps stock regimes current: at start-up when none exist, then hourly in market hours (uses today's candle)."""
    import time as _t
    _t.sleep(90)
    while True:
        try:
            d = store.load()
            now = datetime.now(IST)
            rg = regime.latest()
            age = (now - datetime.fromisoformat(rg["built"])).total_seconds() if rg and rg.get("built") else None
            market = now.weekday() < 5 and "09:20" <= now.strftime("%H:%M") <= "15:30"
            if d and not momentum.sweep.progress.get("running") and (age is None or (market and age > 3600)):
                kite = KiteConnect(api_key=d["api_key"])
                kite.set_access_token(d["access_token"])
                tokens = {r["tradingsymbol"]: r["instrument_token"] for r in _instrument_list(kite, "NSE") if r.get("instrument_type") == "EQ"}
                syms = [{"symbol": x["symbol"], "name": x.get("name"), "rs": x.get("rs"), "token": tokens[x["symbol"]]}
                        for x in (momentum.latest() or {}).get("stocks", []) if x.get("liquid") and x["symbol"] in tokens]
                if syms:
                    regime.refresher.run(kite, syms, now.date().isoformat())
        except Exception:  # noqa: BLE001
            log.exception("regime loop failed")
        _t.sleep(300)


@app.on_event("startup")
def _start_regime() -> None:
    threading.Thread(target=_regime_loop, daemon=True, name="regime").start()


# Tiles shown beside the indices: (exchange, contract name, label, price unit). Each uses its front-month future.
COMMODITY_TILES = [("MCX", "GOLD", "Gold", "₹ / 10 g"), ("MCX", "SILVER", "Silver", "₹ / kg"),
                   ("CDS", "USDINR", "USD / INR", "₹ per $")]


def _near_future(kite: KiteConnect, exchange: str, name: str) -> dict | None:
    """Front-month future, rolled 2 days before expiry; monthly contracts only (CDS also lists thin weeklies)."""
    roll = datetime.now(IST).date() + timedelta(days=2)
    rows = [r for r in _instrument_list(kite, exchange)
            if r.get("name") == name and r.get("instrument_type") == "FUT" and r.get("expiry") and r["expiry"] >= roll
            and re.search(r"\d\d[A-Z]{3}FUT$", r["tradingsymbol"])]
    return min(rows, key=lambda r: r["expiry"]) if rows else None


# ---------------------------------------------------------------------------
# Movers: live gainers / losers / breadth across the tracked index constituents (one quote call).
# ---------------------------------------------------------------------------
_movers_cache: tuple[float, dict] | None = None


def _constituents() -> dict[str, dict]:
    out: dict[str, dict] = {}
    path = REPORTS_DIR / "index_constituents.csv"
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            s = r.get("symbol")
            if s and r.get("nse_token"):
                row = out.setdefault(s, {"name": r.get("name"), "industry": r.get("industry"), "indices": []})
                row["indices"].append(r.get("index"))
    return out


# Which tracked stocks belong to each sector index on the Home page, by NSE industry (Banks and PSU Banks by name).
SECTOR_INDUSTRIES = {
    "Financial Services": ("Financial Services",), "IT": ("Information Technology",), "Auto": ("Automobile and Auto Components",),
    "Pharma": ("Healthcare",), "FMCG": ("Fast Moving Consumer Goods",), "Metals": ("Metals & Mining",),
    "Energy": ("Oil Gas & Consumable Fuels", "Power"), "Realty": ("Realty",),
    "Infrastructure": ("Construction", "Construction Materials", "Capital Goods", "Power", "Telecommunication"),
}
PSU_BANKS = {"SBIN", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "INDIANB", "BANKINDIA", "MAHABANK", "IOB", "UCOBANK", "CENTRALBK", "PSB"}


def sector_groups(names: dict[str, dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {label: [] for label in [*SECTOR_INDUSTRIES, "Banks", "PSU Banks"]}
    for sym, info in names.items():
        for label, inds in SECTOR_INDUSTRIES.items():
            if info.get("industry") in inds:
                out[label].append(sym)
        if "NIFTY BANK" in (info.get("indices") or []) or (info.get("industry") == "Financial Services" and "BANK" in sym and sym != "BANKBEES"):
            out["Banks"].append(sym)
        if sym in PSU_BANKS:
            out["PSU Banks"].append(sym)
    return {k: sorted(set(v)) for k, v in out.items() if v}


@app.get("/api/market/movers")
def market_movers(kite: KiteConnect = Depends(kite_client)):
    global _movers_cache
    if _movers_cache and time.time() - _movers_cache[0] < 15:
        return _movers_cache[1]
    names = _constituents()
    keys = [f"NSE:{s}" for s in names][:500]
    quotes = _kite_call(kite.quote, *keys) if keys else {}
    rows = []
    for k, q in quotes.items():
        s = k.split(":", 1)[1]
        o = q.get("ohlc") or {}
        last, prev = q.get("last_price"), o.get("close")
        if not last or not prev:
            continue
        info = names.get(s, {})
        rows.append({"symbol": s, "name": info.get("name"), "industry": info.get("industry"), "indices": info.get("indices"),
                     "last": last, "prev_close": prev, "open": o.get("open"), "high": o.get("high"), "low": o.get("low"),
                     "change": round(last - prev, 2), "change_pct": round((last / prev - 1) * 100, 2),
                     "volume": q.get("volume"),
                     "value_cr": round((q.get("volume") or 0) * (q.get("average_price") or last) / 1e7, 1)})
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    out = {"gainers": rows[:10], "losers": rows[::-1][:10],
           "active": sorted(rows, key=lambda r: -r["value_cr"])[:10],
           "breadth": {"up": sum(r["change_pct"] > 0 for r in rows), "down": sum(r["change_pct"] < 0 for r in rows),
                       "total": len(rows)},
           # previous close and last price per stock, so the dashboard can recount breadth on live ticks
           "universe": {r["symbol"]: [r["prev_close"], r["last"]] for r in rows},
           "groups": sector_groups(names),
           "asof": datetime.now(IST).isoformat(timespec="seconds")}
    _movers_cache = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# Stock page: live quote, candles for the chosen range, 52-week and average-volume stats.
# ---------------------------------------------------------------------------
_daily_hist: dict[tuple[str, int], tuple[date, list]] = {}
STOCK_RANGES = {"1D": ("5minute", 5), "5D": ("15minute", 9), "1M": ("60minute", 31), "6M": ("day", 183),
                "YTD": ("day", None), "1Y": ("day", 366), "2Y": ("day", 731), "5Y": ("day", 1830)}


def _candles(rows) -> list[list]:
    return [[r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
             r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0] for r in rows]


@app.get("/api/stock")
def stock_detail(symbol: str = Query(min_length=1, max_length=40), exchange: Literal["NSE", "BSE", "MCX", "CDS"] = "NSE",
                 range: Literal["1D", "5D", "1M", "6M", "YTD", "1Y", "2Y", "5Y"] = "1D",
                 kite: KiteConnect = Depends(kite_client)):
    sym = symbol.upper()
    inst = next((r for r in _instrument_list(kite, exchange) if r["tradingsymbol"] == sym), None)
    if not inst:
        raise HTTPException(404, f"{exchange}:{sym} not found")
    key, token = f"{exchange}:{sym}", inst["instrument_token"]
    q = (_kite_call(kite.quote, key) or {}).get(key) or {}
    today = datetime.now(IST).date()
    hit = _daily_hist.get((exchange, token))
    if not hit or hit[0] != today:
        hit = (today, _candles(_kite_call(kite.historical_data, token, today - timedelta(days=760), today, "day",
                                          continuous=exchange in ("MCX", "CDS"))))
        _daily_hist[(exchange, token)] = hit
    daily = hit[1]
    interval, days = STOCK_RANGES[range]
    if range == "5Y":   # Kite has no weekly interval: daily candles, every 5th kept
        rows = _candles(_kite_call(kite.historical_data, token, today - timedelta(days=days), today, "day",
                                   continuous=exchange in ("MCX", "CDS")))
        candles = rows[::-5][::-1]
    elif interval == "day":
        start = date(today.year, 1, 1).isoformat() if range == "YTD" else (today - timedelta(days=days)).isoformat()
        candles = [c for c in daily if c[0][:10] >= start]
    else:
        candles = _candles(_kite_call(kite.historical_data, token, today - timedelta(days=days), today, interval))
        if range == "1D" and candles:   # the latest session only (the previous one before the open)
            last_day = candles[-1][0][:10]
            candles = [c for c in candles if c[0][:10] == last_day]
    year, prior = daily[-252:], daily[-21:-1]
    o = q.get("ohlc") or {}
    depth = q.get("depth") or {}
    bid = next((d for d in depth.get("buy", []) if d.get("price")), None)
    ask = next((d for d in depth.get("sell", []) if d.get("price")), None)
    last, prev = q.get("last_price"), o.get("close")
    info = _constituents().get(sym, {})
    ltt = q.get("last_trade_time")
    return {"symbol": sym, "exchange": exchange, "name": inst.get("name") or info.get("name"),
            "industry": info.get("industry"), "indices": info.get("indices") or [],
            "tradable": inst.get("segment") != "INDICES", "tick_size": inst.get("tick_size"),
            "instrument_type": inst.get("instrument_type"), "expiry": inst["expiry"].isoformat() if inst.get("expiry") else None,
            "lot_size": inst.get("lot_size") or 1,
            "last": last, "prev_close": prev, "open": o.get("open"), "high": o.get("high"), "low": o.get("low"),
            "change": round(last - prev, 2) if last and prev else None,
            "change_pct": round((last / prev - 1) * 100, 2) if last and prev else None,
            "volume": q.get("volume"), "avg_price": q.get("average_price"),
            "avg_volume_20d": round(sum(c[5] for c in prior) / len(prior)) or None if prior else None,
            "high_52w": max((c[2] for c in year), default=None), "low_52w": min((c[3] for c in year), default=None),
            "upper_circuit": q.get("upper_circuit_limit"), "lower_circuit": q.get("lower_circuit_limit"),
            "bid": bid and {"price": bid["price"], "qty": bid["quantity"]},
            "ask": ask and {"price": ask["price"], "qty": ask["quantity"]},
            "buy_qty": q.get("buy_quantity"), "sell_qty": q.get("sell_quantity"),
            "last_trade_time": ltt.isoformat() if hasattr(ltt, "isoformat") else None,
            "range": range, "interval": interval, "candles": candles,
            "regime": regime.classify(regime.splice(daily, today.isoformat(), o.get("open"), o.get("high"), o.get("low"), last))}


# ---------------------------------------------------------------------------
# Stock Intelligence peers: same-industry index members, live quotes.
# ---------------------------------------------------------------------------
@app.get("/api/stock/peers")
def stock_peers(symbol: str = Query(min_length=1, max_length=40), kite: KiteConnect = Depends(kite_client)):
    cons = _constituents()
    sym = symbol.upper()
    me = cons.get(sym)
    if not me or not me.get("industry"):
        return {"symbol": sym, "industry": None, "peers": []}
    rank = {"NIFTY 50": 0, "SENSEX": 1, "NIFTY BANK": 2, "NIFTY MIDCAP 100": 3, "NIFTY SMLCAP 100": 4}
    same = sorted((s for s, v in cons.items() if v.get("industry") == me["industry"]),
                  key=lambda s: (s != sym, min((rank.get(i, 9) for i in cons[s]["indices"]), default=9), s))[:9]
    q = _kite_call(kite.quote, *[f"NSE:{s}" for s in same])
    peers = []
    for s in same:
        x = q.get(f"NSE:{s}") or {}
        last, prev = x.get("last_price"), (x.get("ohlc") or {}).get("close")
        peers.append({"symbol": s, "name": cons[s]["name"], "indices": cons[s]["indices"], "last": last,
                      "change_pct": round((last / prev - 1) * 100, 2) if last and prev else None,
                      "volume": x.get("volume"), "high": (x.get("ohlc") or {}).get("high"), "low": (x.get("ohlc") or {}).get("low")})
    return {"symbol": sym, "industry": me["industry"], "peers": peers}


