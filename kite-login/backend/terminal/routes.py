"""HTTP routes of the market terminal, under /api/t. Read-only."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from . import calendar_data, market
from .market import store
from .sources import IST, nifty500, nse, screener_company

log = logging.getLogger("kite-desk.terminal")

STOCKS_TTL = 10               # seconds between full Nifty 500 quotes, whoever is asking
SNAPSHOT_KEYS = {"INDIA VIX": "NSE:INDIA VIX", "Nifty 50": "NSE:NIFTY 50", "Sensex": "BSE:SENSEX", "Nifty Bank": "NSE:NIFTY BANK",
                 "Nifty Midcap 100": "NSE:NIFTY MIDCAP 100", "Nifty Smallcap 100": "NSE:NIFTY SMLCAP 100"}
NORMALIZED = {"NIFTY 50": "Nifty 50", "SENSEX": "Sensex", "NIFTY BANK": "Nifty Bank", "NIFTY MIDCAP 100": "Midcap 100"}
INDEX_KEYS = {**{k: f"{v}:{k}" for k, v in market.HEADLINE_INDICES.items()}, **{k: f"NSE:{k}" for k in market.SECTOR_INDICES}, "INDIA VIX": "NSE:INDIA VIX"}
PREOPEN_ROWS = 7
DEAL_LIST_KEYS = {"bulk": "BULK_DEALS_DATA", "block": "BLOCK_DEALS_DATA", "short": "SHORT_DEALS_DATA"}


def _now() -> datetime:
    return datetime.now(IST)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def create_router(kite_dep, instruments, kite_call, session_kite) -> APIRouter:
    """kite_dep: FastAPI dependency giving a logged-in client. instruments(kite, exchange): the day's instrument list.
    kite_call(fn, *args): a Kite call with the app's error mapping. session_kite(): a client, or None when logged out."""
    router = APIRouter(prefix="/api/t", tags=["terminal"])
    cache: dict[str, tuple[float, object]] = {}
    cache_lock = threading.Lock()

    def stocks(kite) -> list[dict]:
        with cache_lock:
            hit = cache.get("stocks")
            if hit and time.time() - hit[0] < STOCKS_TTL:
                return hit[1]
            keys = [f"NSE:{u['symbol']}" for u in (store.universe or nifty500())]
            quotes: dict = {}
            for part in _chunks(keys, 500):
                quotes.update(kite_call(kite.quote, *part))
            rows = store.rows(quotes, _now())
            cache["stocks"] = (time.time(), rows)
            return rows

    def index_quotes(kite) -> dict:
        with cache_lock:
            hit = cache.get("indices")
            if hit and time.time() - hit[0] < 5:
                return hit[1]
            keys = list({*INDEX_KEYS.values(), *SNAPSHOT_KEYS.values()})
            q = kite_call(kite.quote, *keys)
            cache["indices"] = (time.time(), q)
            return q

    @router.get("/status")
    def status():
        return {"built": store.built, "candles": store.progress, "caps": store.crawl, "caps_known": len(store.caps)}

    @router.get("/stocks")
    def all_stocks(kite=Depends(kite_dep)):
        """Every Nifty 500 stock with live metrics: the heatmap and the scanner read this."""
        return {"rows": stocks(kite), "built": store.built, "candles": store.progress, "caps": store.crawl, "caps_known": len(store.caps),
                "asof": _now().isoformat(timespec="seconds")}

    @router.get("/indices")
    def indices(kite=Depends(kite_dep)):
        """Headline and sector indices with period returns, and the market snapshot."""
        q = index_quotes(kite)
        starts = market.period_starts(_now().date())

        def row(name: str, key: str, label: str) -> dict | None:
            x = q.get(key) or {}
            last, prev = x.get("last_price"), (x.get("ohlc") or {}).get("close")
            if not last or not prev:
                return None
            rows = store.candles.get(name, [])
            ret = {"1D": round((last / prev - 1) * 100, 2)}
            for k, d in starts.items():
                base = market.close_on_or_before(rows, d)
                ret[k] = round((last / base - 1) * 100, 2) if base else None
            return {"name": name, "label": label, "key": key, "last": last, "prev": prev, "change": round(last - prev, 2), "ret": ret}

        headline = [row(k, f"{v}:{k}", k) for k, v in market.HEADLINE_INDICES.items()]
        sectors = [row(k, f"NSE:{k}", label) for k, label in market.SECTOR_INDICES.items()]
        return {"headline": [x for x in headline if x], "sectors": [x for x in sectors if x],
                "snapshot": [{"label": label, "key": key, "last": (q.get(key) or {}).get("last_price"), "prev": ((q.get(key) or {}).get("ohlc") or {}).get("close")}
                             for label, key in SNAPSHOT_KEYS.items()]}

    rotation_lock = threading.Lock()

    @router.get("/rotation")
    def rotation(kind: str = "sectors", kite=Depends(kite_dep)):
        """Sectors or themes by RSI: daily RSI(14) on the live price, its 5-session change, weekly RSI, returns, and the rotation quadrant."""
        if kind not in ("sectors", "themes"):
            raise HTTPException(400, "kind is sectors or themes")
        names = market.ROTATION_SECTORS if kind == "sectors" else market.THEME_INDICES
        today = _now().date()
        missing = [n for n in names if not store.candles.get(n)]
        if missing:
            with rotation_lock:                     # first look after a restart: fetch the year of daily candles once, then keep them
                tokens = {r["tradingsymbol"]: r["instrument_token"] for r in instruments(kite, "NSE") if r.get("segment") == "INDICES"}
                for n in missing:
                    if not store.candles.get(n) and n in tokens:
                        store.candles[n] = market.fetch_daily(kite, tokens[n], today)
                        time.sleep(0.35)
        hit = cache.get(f"rot_{kind}")
        if not hit or time.time() - hit[0] > 20:
            hit = (time.time(), kite_call(kite.quote, *[f"NSE:{n}" for n in names]))
            cache[f"rot_{kind}"] = hit
        q = hit[1]
        rows = [market.rotation_row(label, f"NSE:{n}", store.candles.get(n) or [], (q.get(f"NSE:{n}") or {}).get("last_price"), today.isoformat())
                for n, label in names.items()]
        rows = sorted((x for x in rows if x), key=lambda x: -x["rsi"])
        counts = {k: sum(1 for x in rows if x["quadrant"] == k) for k in ("Leading", "Weakening", "Improving", "Lagging")}
        return {"kind": kind, "rows": rows, "counts": counts, "lookback": market.ROTATION_LOOKBACK, "asof": _now().isoformat(timespec="seconds"),
                "missing": [names[n] for n in names if not store.candles.get(n)]}

    @router.get("/normalized")
    def normalized(range: str = "1Y"):
        """Daily closes of the headline indices, each rebased to 100 at the start of the range."""
        if range not in ("1W", "1M", "3M", "6M", "YTD", "1Y"):
            raise HTTPException(400, "Unknown range")
        start = market.period_starts(_now().date())[range].isoformat()
        out = []
        for name, label in NORMALIZED.items():
            rows = [r for r in store.candles.get(name, []) if r[0] >= start]
            if rows:
                base = rows[0][4]
                out.append({"label": label, "points": [[r[0], round(r[4] / base * 100, 2)] for r in rows]})
        return {"range": range, "series": out}

    @router.get("/mood")
    def market_mood(kite=Depends(kite_dep)):
        q = index_quotes(kite)
        closes = lambda name: [r[4] for r in store.candles.get(name, [])]  # noqa: E731
        return market.mood(stocks(kite), (q.get("NSE:NIFTY 50") or {}).get("last_price"), closes("NIFTY 50"),
                           (q.get("NSE:INDIA VIX") or {}).get("last_price"), closes("INDIA VIX")) or {"score": None, "parts": []}

    @router.get("/preopen")
    def preopen():
        """Biggest pre-open volume among Nifty 500 stocks, from NSE's pre-open session."""
        res = nse("market-data-pre-open?key=ALL", ttl=120, key="preopen")
        names = {u["symbol"]: u["name"] for u in nifty500()}
        rows = []
        for r in (res["data"] or {}).get("data", []) if isinstance(res["data"], dict) else []:
            m = r.get("metadata") or {}
            if m.get("symbol") in names and m.get("finalQuantity"):
                rows.append({"symbol": m["symbol"], "name": names[m["symbol"]], "chg": m.get("pChange"), "price": m.get("lastPrice"),
                             "qty": m.get("finalQuantity"), "turnover": m.get("totalTurnover")})
        rows.sort(key=lambda x: -x["qty"])
        return {"rows": rows[:PREOPEN_ROWS], "fetched_at": res["fetched_at"], "stale": res["stale"], "error": res["error"]}

    @router.get("/flows")
    def flows():
        """FII and DII net cash-market flow for the latest day NSE has published."""
        res = nse("fiidiiTradeReact", key="calendar_flows")
        return {"rows": res["data"] if isinstance(res["data"], list) else [], "fetched_at": res["fetched_at"], "stale": res["stale"], "error": res["error"]}

    @router.get("/calendar")
    def calendar(kite=Depends(kite_dep)):
        return calendar_data.build(lambda exch: instruments(kite, exch), _now().date())

    @router.get("/fundamental/{symbol}")
    def fundamental(symbol: str, kite=Depends(kite_dep)):
        symbol = symbol.upper()
        key = f"NSE:{symbol}"
        q = (kite_call(kite.quote, key) or {}).get(key)
        if not q:
            raise HTTPException(404, f"No NSE quote for {symbol}")
        rows = store.candles.get(symbol)
        static = store.static.get(symbol)
        if not rows:   # outside the Nifty 500: fetched for this page
            hit = cache.get(f"daily_{symbol}")
            if hit and time.time() - hit[0] < 3600:
                rows = hit[1]
            else:
                rows = market.fetch_daily(kite, q["instrument_token"], _now().date())
                cache[f"daily_{symbol}"] = (time.time(), rows)
            bench = {r[0]: r[4] for r in store.candles.get("NIFTY 50", [])}
            static = market.static_metrics(rows, bench) if rows else None
        res = screener_company(symbol)
        info = next((u for u in (store.universe or nifty500()) if u["symbol"] == symbol), {"symbol": symbol, "name": symbol, "sector": None})
        cap = {"mcap_cr": res["data"].get("mcap_cr"), "price": res["data"].get("price"), "pe": res["data"].get("pe")} if res["data"] else None
        row = market.stock_row(info, static, rows, q, _now(), market.period_starts(_now().date()), cap) if static else None
        return {"symbol": symbol, "key": key, "name": info["name"], "index_sector": info.get("sector"), "live": row,
                "last": q.get("last_price"), "prev": (q.get("ohlc") or {}).get("close"), "ohlc": q.get("ohlc"), "volume": q.get("volume"),
                "last_trade_time": str(q.get("last_trade_time") or "") or None,
                "fundamentals": res["data"], "fetched_at": res["fetched_at"], "stale": res["stale"], "error": res["error"]}

    @router.get("/insiders")
    def insiders():
        """Insider-trading disclosures plus bulk, block and short deals, as published by NSE."""
        deals = nse("snapshot-capital-market-largedeal", ttl=900, key="deals")
        pit = nse("corporates-pit?index=equities", ttl=900, key="insider_pit")
        d = deals["data"] if isinstance(deals["data"], dict) else {}
        p = pit["data"] if isinstance(pit["data"], dict) else {}
        return {"as_on": d.get("as_on_date"), **{k: d.get(v) or [] for k, v in DEAL_LIST_KEYS.items()}, "pit": p.get("data") or [],
                "deals_status": {"fetched_at": deals["fetched_at"], "stale": deals["stale"], "error": deals["error"]},
                "pit_status": {"fetched_at": pit["fetched_at"], "stale": pit["stale"], "error": pit["error"]}}

    def candle_worker() -> None:
        """Rebuilds the day's candles while someone is logged in."""
        time.sleep(30)
        while True:
            try:
                kite = session_kite()
                listing = lambda exch: instruments(kite, exch)  # noqa: E731
                if kite and (store.due(_now()) or store.behind(kite, listing, _now())):
                    store.refresh(kite, listing, _now())
            except Exception:  # noqa: BLE001 - a background pass must never die
                log.exception("terminal candles failed")
            time.sleep(60)

    def caps_worker() -> None:
        """The slow Screener pass; it needs no Kite session. A pass that was refused tries again six hours later."""
        time.sleep(30)
        while True:
            try:
                store.crawl_caps()
            except Exception:  # noqa: BLE001 - a background pass must never die
                log.exception("terminal market caps failed")
            time.sleep(6 * 3600)

    started = threading.Event()

    @router.on_event("startup")
    def start_workers() -> None:
        if started.is_set():   # the router's startup can fire once for the router and once for the app
            return
        started.set()
        store.universe = store.universe or nifty500()
        threading.Thread(target=candle_worker, daemon=True, name="terminal-candles").start()
        threading.Thread(target=caps_worker, daemon=True, name="terminal-caps").start()

    return router
