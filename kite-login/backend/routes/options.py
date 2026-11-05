"""Option chain, built from the day's instrument list plus one quote call; Black-76 IV and Greeks."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import json
import math
import threading

from fastapi import Depends, HTTPException, Query
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from core import IST, _kite_call, app, kite_client, store
from routes.market import _instrument_list


# ---------------------------------------------------------------------------
# Option chain: Kite Connect has no chain endpoint, so it is built from the day's instrument dump + one quote
# call; IV and delta come from Black-76 on the forward (same-expiry future, else spot carried at RATE).
# ---------------------------------------------------------------------------
RATE = 0.065                     # ASSUMED risk-free rate for carrying spot to expiry when no future exists
OPTION_INDICES = {               # option `name` in the NFO/BFO dump -> (options exchange, underlying quote key)
    "NIFTY": ("NFO", "NSE:NIFTY 50"), "BANKNIFTY": ("NFO", "NSE:NIFTY BANK"),
    "FINNIFTY": ("NFO", "NSE:NIFTY FIN SERVICE"), "MIDCPNIFTY": ("NFO", "NSE:NIFTY MID SELECT"),
    "NIFTYNXT50": ("NFO", "NSE:NIFTY NEXT 50"), "SENSEX": ("BFO", "BSE:SENSEX"), "BANKEX": ("BFO", "BSE:BANKEX"),
}


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _b76(fwd: float, k: float, t: float, vol: float, call: bool) -> float:
    """Black-76 undiscounted price (discounting is < 1% for these tenors and cancels in the IV solve)."""
    if t <= 0 or vol <= 0:
        return max(0.0, fwd - k) if call else max(0.0, k - fwd)
    sd = vol * math.sqrt(t)
    d1 = (math.log(fwd / k) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    return fwd * _ncdf(d1) - k * _ncdf(d2) if call else k * _ncdf(-d2) - fwd * _ncdf(-d1)


def _iv_delta(price: float | None, fwd: float, k: float, t: float, call: bool) -> tuple[float | None, float | None]:
    """Implied vol (bisection) and delta; None when the price is below intrinsic or there is no price."""
    if not price or price <= 0 or t <= 0 or fwd <= 0:
        return None, None
    intrinsic = max(0.0, fwd - k) if call else max(0.0, k - fwd)
    if price <= intrinsic + 1e-9:
        return None, (1.0 if call else -1.0) if intrinsic > 0 else None
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _b76(fwd, k, t, mid, call) > price:
            hi = mid
        else:
            lo = mid
    vol = (lo + hi) / 2
    if vol >= 4.99:
        return None, None
    d1 = (math.log(fwd / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    return round(vol * 100, 2), round(_ncdf(d1) if call else _ncdf(d1) - 1, 3)


OI_CACHE_DIR = Path(__file__).resolve().parents[1] / ".oi_cache"
OI_WINDOW = 12                   # strikes each side of ATM whose previous-day OI is fetched (change in OI)
_prev_oi: dict[str, dict] = {}   # "EXCH:SYMBOL" -> {"oi", "close"} as of the previous session (today's file)
_prev_oi_lock = threading.Lock()
_prev_oi_jobs: set[str] = set()


def _prev_oi_file() -> Path:
    return OI_CACHE_DIR / f"prev_oi_{datetime.now(IST).date().isoformat()}.json"


def _load_prev_oi() -> None:
    f = _prev_oi_file()
    with _prev_oi_lock:
        if not _prev_oi and f.exists():
            try:
                _prev_oi.update(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                pass


def _fill_prev_oi(job: str, items: list[tuple[int, str]]) -> None:
    """Previous session's closing OI and price per contract, from Kite daily candles (oi=True). Kite allows ~3
    historical calls a second, so this runs in the background and the chain shows OI change once it lands."""
    try:
        d = store.load()
        if not d:
            return
        kite = KiteConnect(api_key=d["api_key"])
        kite.set_access_token(d["access_token"])
        today = datetime.now(IST).date()
        for token, key in items:
            if key in _prev_oi:
                continue
            for attempt in range(3):
                try:
                    candles = kite.historical_data(token, today - timedelta(days=10), today - timedelta(days=1), "day", oi=True)
                    last = candles[-1] if candles else None
                    with _prev_oi_lock:
                        _prev_oi[key] = {"oi": last.get("oi") if last else None, "close": last.get("close") if last else None,
                                         "date": last["date"].date().isoformat() if last and last.get("date") else None}
                    break
                except TokenException:
                    return
                except Exception:
                    import time
                    time.sleep(1.5 * (attempt + 1))
            import time
            time.sleep(0.36)
        OI_CACHE_DIR.mkdir(exist_ok=True)
        with _prev_oi_lock:
            _prev_oi_file().write_text(json.dumps(_prev_oi), encoding="utf-8")
    finally:
        _prev_oi_jobs.discard(job)


def _option_rows(kite: KiteConnect, name: str) -> tuple[str, list[dict]]:
    exch = OPTION_INDICES.get(name, ("NFO", None))[0]
    rows = [r for r in _instrument_list(kite, exch) if r.get("name") == name and r.get("instrument_type") in ("CE", "PE")]
    return exch, rows


@app.get("/api/options/underlyings")
def option_underlyings(kite: KiteConnect = Depends(kite_client)):
    stocks = sorted({r["name"] for r in _instrument_list(kite, "NFO")
                     if r.get("instrument_type") == "CE" and r.get("name") and r["name"] not in OPTION_INDICES})
    return {"indices": list(OPTION_INDICES), "stocks": stocks}


@app.get("/api/options/chain")
def option_chain(underlying: str = Query(min_length=1, max_length=30), expiry: str | None = None,
                 strikes: int = Query(15, ge=3, le=60), kite: KiteConnect = Depends(kite_client)):
    name = underlying.upper()
    exch, rows = _option_rows(kite, name)
    if not rows:
        raise HTTPException(404, f"No options found for {name}")
    today = datetime.now(IST).date()
    expiries = sorted({r["expiry"] for r in rows if r["expiry"] >= today})
    if not expiries:
        raise HTTPException(404, f"No live expiries for {name}")
    exp = next((e for e in expiries if e.isoformat() == expiry), expiries[0])
    chain = [r for r in rows if r["expiry"] == exp]
    spot_key = OPTION_INDICES.get(name, ("NFO", f"NSE:{name}"))[1]
    fut = next((r for r in _instrument_list(kite, exch) if r.get("name") == name and r.get("instrument_type") == "FUT"
                and r.get("expiry") == exp), None)
    # futures flow (price and OI build-up) is read from the near-month future, weekly expiries have none of their own
    near_fut = min((r for r in _instrument_list(kite, exch) if r.get("name") == name and r.get("instrument_type") == "FUT"
                    and r.get("expiry") and r["expiry"] >= today), key=lambda r: r["expiry"], default=None)
    keys = ([spot_key] + ([f"{exch}:{fut['tradingsymbol']}"] if fut else [])
            + ([f"{exch}:{near_fut['tradingsymbol']}"] if near_fut and near_fut is not fut else [])
            + [f"{exch}:{r['tradingsymbol']}" for r in chain])
    quotes: dict = {}
    for i in range(0, len(keys), 450):   # Kite quote: up to 500 instruments per call
        quotes.update(_kite_call(kite.quote, *keys[i:i + 450]))
    spot = (quotes.get(spot_key) or {}).get("last_price")
    if not spot:
        raise HTTPException(502, f"No quote for {spot_key}")
    expiry_dt = datetime.combine(exp, datetime.min.time()).replace(hour=15, minute=30, tzinfo=IST)
    t_years = max((expiry_dt - datetime.now(IST)).total_seconds(), 60.0) / (365.25 * 86400)
    fut_px = (quotes.get(f"{exch}:{fut['tradingsymbol']}") or {}).get("last_price") if fut else None

    def mid(sym: str) -> float | None:
        q = quotes.get(f"{exch}:{sym}") or {}
        depth = q.get("depth") or {}
        b = next((x["price"] for x in depth.get("buy", []) if x.get("price")), None)
        a = next((x["price"] for x in depth.get("sell", []) if x.get("price")), None)
        return (a + b) / 2 if a and b and a >= b else q.get("last_price")

    # Forward the market implies: put-call parity F = K + C - P at the 3 strikes nearest spot (falls back to the
    # same-expiry future, then spot carried at RATE). Keeps call and put IVs consistent with each other.
    legs: dict[float, dict] = {}
    for r in chain:
        legs.setdefault(float(r["strike"]), {})[r["instrument_type"]] = r["tradingsymbol"]
    parity = [k + c - p for k in sorted(legs, key=lambda k: abs(k - spot))[:3]
              if "CE" in legs[k] and "PE" in legs[k] and (c := mid(legs[k]["CE"])) and (p := mid(legs[k]["PE"]))]
    fwd = sorted(parity)[len(parity) // 2] if parity else (fut_px or spot * math.exp(RATE * t_years))

    by_strike: dict[float, dict] = {}
    for r in chain:
        q = quotes.get(f"{exch}:{r['tradingsymbol']}") or {}
        depth = q.get("depth") or {}
        bid = next((x["price"] for x in depth.get("buy", []) if x.get("price")), None)
        ask = next((x["price"] for x in depth.get("sell", []) if x.get("price")), None)
        ltp, prev = q.get("last_price"), (q.get("ohlc") or {}).get("close")
        call = r["instrument_type"] == "CE"
        mid = (bid + ask) / 2 if bid and ask and ask >= bid else ltp
        iv, delta = _iv_delta(mid, fwd, float(r["strike"]), t_years, call)
        by_strike.setdefault(float(r["strike"]), {})["ce" if call else "pe"] = {
            "tradingsymbol": r["tradingsymbol"], "key": f"{exch}:{r['tradingsymbol']}", "ltp": ltp,
            "change_pct": round((ltp / prev - 1) * 100, 2) if ltp and prev else None, "oi": q.get("oi") or 0,
            "volume": q.get("volume") or 0, "bid": bid, "ask": ask, "iv": iv, "delta": delta}
    strikes_all = sorted(by_strike)
    atm = min(strikes_all, key=lambda k: abs(k - fwd))

    # change in OI vs the previous session for strikes near the money (+ the future): cached per day, filled in
    # the background on first view, so the first load may come back with prev_oi still filling
    _load_prev_oi()
    ai = strikes_all.index(atm)
    near = set(strikes_all[max(0, ai - OI_WINDOW): ai + OI_WINDOW + 1])
    # always include the OI walls (the 3 biggest call OI strikes up to 10% and 15% above spot, put OI below), so their
    # change in OI is known even when they sit outside the window; the page's R1-R3 / S1-S3 use the 10% band
    for side, lo_, hi_ in (("ce", spot, spot * 1.10), ("ce", spot, spot * 1.15), ("pe", spot * 0.90, spot), ("pe", spot * 0.85, spot)):
        cand = sorted((k for k in strikes_all if lo_ <= k <= hi_), key=lambda k: -(by_strike[k].get(side) or {}).get("oi", 0))
        near.update(cand[:3])
    wanted = [(int(r["instrument_token"]), f"{exch}:{r['tradingsymbol']}") for r in chain if float(r["strike"]) in near]
    if near_fut:
        wanted.append((int(near_fut["instrument_token"]), f"{exch}:{near_fut['tradingsymbol']}"))
    missing = [w for w in wanted if w[1] not in _prev_oi]
    job = f"{name}:{exp}"
    if missing and job not in _prev_oi_jobs:
        _prev_oi_jobs.add(job)
        threading.Thread(target=_fill_prev_oi, args=(job, missing), daemon=True).start()
    for k in near:
        for leg in by_strike[k].values():
            p = _prev_oi.get(leg["key"])
            if p:
                leg["prev_oi"], leg["prev_close"], leg["prev_date"] = p.get("oi"), p.get("close"), p.get("date")
    fut_q = quotes.get(f"{exch}:{near_fut['tradingsymbol']}") if near_fut else None
    future = None
    if near_fut and fut_q:
        fp = _prev_oi.get(f"{exch}:{near_fut['tradingsymbol']}") or {}
        prev_c = (fut_q.get("ohlc") or {}).get("close")
        future = {"tradingsymbol": near_fut["tradingsymbol"], "key": f"{exch}:{near_fut['tradingsymbol']}",
                  "expiry": near_fut["expiry"].isoformat(), "ltp": fut_q.get("last_price"),
                  "change_pct": round((fut_q["last_price"] / prev_c - 1) * 100, 2) if fut_q.get("last_price") and prev_c else None,
                  "oi": fut_q.get("oi"), "prev_oi": fp.get("oi")}

    ce_oi = sum((v.get("ce") or {}).get("oi", 0) for v in by_strike.values())
    pe_oi = sum((v.get("pe") or {}).get("oi", 0) for v in by_strike.values())

    def pain(px: float) -> float:   # total value option writers pay out if expiry settles at px
        return sum((v.get("ce") or {}).get("oi", 0) * max(0.0, px - k) + (v.get("pe") or {}).get("oi", 0) * max(0.0, k - px)
                   for k, v in by_strike.items())

    max_pain = min(strikes_all, key=pain) if ce_oi + pe_oi else None
    # OI walls near the money: biggest call OI above spot (resistance), biggest put OI below spot (support)
    above = [k for k in strikes_all if spot <= k <= spot * 1.10] or strikes_all
    below = [k for k in strikes_all if spot * 0.90 <= k <= spot] or strikes_all
    res_k = max(above, key=lambda k: (by_strike[k].get("ce") or {}).get("oi", 0))
    sup_k = max(below, key=lambda k: (by_strike[k].get("pe") or {}).get("oi", 0))
    atm_ce, atm_pe = by_strike[atm].get("ce") or {}, by_strike[atm].get("pe") or {}
    straddle = (atm_ce.get("ltp") or 0) + (atm_pe.get("ltp") or 0)
    return {"underlying": name, "exchange": exch, "spot_key": spot_key, "spot": spot, "future": fut_px,
            "spot_prev_close": ((quotes.get(spot_key) or {}).get("ohlc") or {}).get("close"),
            "forward": round(fwd, 2), "expiry": exp.isoformat(), "expiries": [e.isoformat() for e in expiries[:12]],
            "days_to_expiry": round(t_years * 365.25, 2), "lot_size": int(chain[0].get("lot_size") or 1),
            "atm": atm, "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "call_oi": ce_oi, "put_oi": pe_oi,
            "max_pain": max_pain, "resistance": res_k, "support": sup_k, "straddle": round(straddle, 2),
            "implied_move_pct": round(straddle / spot * 100, 2) if straddle else None,
            "atm_iv": round(sum(ivs) / len(ivs), 2) if (ivs := [v for v in (atm_ce.get("iv"), atm_pe.get("iv")) if v]) else None,
            # every strike of the expiry (the page shows a window around ATM but streams all of them, so PCR,
            # max pain and the OI walls stay live); expiry_at lets the page recompute IV / delta on each tick
            "rows": [{"strike": k, **by_strike[k]} for k in strikes_all], "window": strikes,
            "expiry_at": expiry_dt.isoformat(), "rate": RATE, "future_detail": future,
            "prev_oi_status": {"have": len(wanted) - len(missing), "need": len(wanted), "loading": job in _prev_oi_jobs},
            "as_of": datetime.now(IST).isoformat(timespec="seconds")}


