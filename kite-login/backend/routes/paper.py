"""Paper trades: ORB controls, the option-strategy book and screener trades. Nothing here reaches Kite."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
import json
import threading

from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from core import IST, _kite_call, app, kite_client, log, store
from routes.market import _instrument_list
from routes.options import OPTION_INDICES
from strategies import orb


# ---------------------------------------------------------------------------
# Paper desk: ORB pause / emergency stop, and a paper book for option strategies built on the Options tab.
# Paper only: nothing here places, modifies or cancels a Kite order. Fills use the live bid / ask from a Kite quote.
# ---------------------------------------------------------------------------
class PauseBody(BaseModel):
    paused: bool


@app.post("/api/orb/pause")
def orb_pause(body: PauseBody, kite: KiteConnect = Depends(kite_client)):
    try:
        orb.engine.pause(body.paused)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"snapshot": orb.engine.snapshot}


@app.post("/api/orb/kill")
def orb_kill(kite: KiteConnect = Depends(kite_client)):
    try:
        orb.engine.kill()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"snapshot": orb.engine.snapshot}


PAPER_OPT_FILE = orb.CACHE_DIR / "paper_options.json"
_paper_lock = threading.Lock()


STT_OPT_SELL = 0.0015       # STT on the sell premium of options, 0.15% from 1 Apr 2026 (Finance Act 2026; was 0.1%)
STT_OPT_EXERCISE = 0.0015   # STT on the intrinsic value of long options exercised at expiry, 0.15% from 1 Apr 2026 (was 0.125%)


def _opt_costs(buy_val: float, sell_val: float, orders: int, exercised: float = 0.0, stt_sell: float | None = None) -> float:
    """Zerodha F&O options charges for the given premium turnover: ₹20 an order, STT 0.15% of sell premium (and 0.15%
    of the intrinsic value of long legs exercised at expiry), NSE transaction 0.03503% of premium, SEBI ₹10/crore,
    GST 18% on brokerage + transaction + SEBI, stamp 0.003% on buys."""
    brk = 20.0 * orders
    txn = 0.0003503 * (buy_val + sell_val)
    sebi = 1e-6 * (buy_val + sell_val)
    return round(brk + STT_OPT_SELL * (sell_val if stt_sell is None else stt_sell) + STT_OPT_EXERCISE * exercised + txn + sebi + 0.18 * (brk + txn + sebi)
                 + 0.00003 * buy_val, 2)


def _fill(q: dict, side: str) -> float | None:
    """Paper fill at the touch: buys pay the best ask, sells get the best bid (last price if the book is empty)."""
    depth = q.get("depth") or {}
    book = depth.get("sell" if side == "BUY" else "buy") or []
    px = next((x["price"] for x in book if x.get("price")), None)
    return px or q.get("last_price")


class PaperLeg(BaseModel):
    tradingsymbol: str = Field(min_length=3, max_length=40)
    side: Literal["BUY", "SELL"]
    lots: int = Field(ge=1, le=100)


class PaperStrategy(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    underlying: str = Field(min_length=1, max_length=30)
    exchange: Literal["NFO", "BFO"] = "NFO"
    legs: list[PaperLeg] = Field(min_length=1, max_length=8)


def _paper_book() -> dict:
    try:
        return json.loads(PAPER_OPT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"positions": []}


def _save_paper_book(book: dict) -> None:
    orb.CACHE_DIR.mkdir(exist_ok=True)
    tmp = PAPER_OPT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, indent=1), encoding="utf-8")
    tmp.replace(PAPER_OPT_FILE)


def _leg_values(legs: list[dict], prices: dict[str, float]) -> tuple[float, float]:
    """(buy premium, sell premium) turnover of opening the legs at entry and closing them at `prices`."""
    buy = sell = 0.0
    for l in legs:
        e, x = l["entry"] * l["qty"], prices.get(l["key"], l["entry"]) * l["qty"]
        if l["side"] == "BUY":
            buy, sell = buy + e, sell + x
        else:
            sell, buy = sell + e, buy + x
    return buy, sell


def _gross(legs: list[dict], prices: dict[str, float]) -> float:
    return sum((prices.get(l["key"], l["entry"]) - l["entry"]) * l["qty"] * (1 if l["side"] == "BUY" else -1) for l in legs)


@app.get("/api/paper/options")
def paper_options():
    book = _paper_book()
    d = store.load()
    open_keys = sorted({l["key"] for p in book["positions"] if p["status"] == "open" for l in p["legs"]})
    quotes: dict = {}
    if d and open_keys:
        try:
            kite = KiteConnect(api_key=d["api_key"])
            kite.set_access_token(d["access_token"])
            with _paper_lock:
                fresh = _paper_book()
                if _settle_expired(fresh, kite):
                    _save_paper_book(fresh)
                    book = fresh
            open_keys = sorted({l["key"] for p in book["positions"] if p["status"] == "open" for l in p["legs"]})
            quotes = kite.ltp(*open_keys[:450]) if open_keys else {}
        except Exception as exc:  # noqa: BLE001 - marks fall back to entry prices
            log.warning("paper option marks failed: %s", exc)
    for p in book["positions"]:
        if p["status"] != "open":
            continue
        marks = {k: v["last_price"] for k, v in quotes.items()}
        for l in p["legs"]:
            l["ltp"] = marks.get(l["key"])
        buy, sell = _leg_values(p["legs"], marks)
        p["gross"] = round(_gross(p["legs"], marks), 2)
        p["costs"] = _pos_costs(p, buy, sell)
        p["net"] = round(p["gross"] - p["costs"], 2)
    return {"positions": book["positions"][::-1], "marked": bool(quotes)}


@app.post("/api/paper/options")
def paper_options_open(body: PaperStrategy, kite: KiteConnect = Depends(kite_client)):
    by_sym = {r["tradingsymbol"]: r for r in _instrument_list(kite, body.exchange)
              if r.get("name") == body.underlying.upper() and r.get("instrument_type") in ("CE", "PE")}
    legs = []
    for l in body.legs:
        r = by_sym.get(l.tradingsymbol)
        if not r:
            raise HTTPException(400, f"{l.tradingsymbol} is not a {body.underlying} option")
        legs.append((l, r))
    keys = [f"{body.exchange}:{l.tradingsymbol}" for l, _ in legs]
    q = _kite_call(kite.quote, *keys)
    out = []
    for (l, r), key in zip(legs, keys):
        px = _fill(q.get(key) or {}, l.side)
        if not px:
            raise HTTPException(400, f"No price for {l.tradingsymbol}")
        lot = int(r.get("lot_size") or 1)
        out.append({"tradingsymbol": l.tradingsymbol, "key": key, "strike": float(r["strike"]), "type": r["instrument_type"],
                    "expiry": r["expiry"].isoformat(), "side": l.side, "lots": l.lots, "lot_size": lot, "qty": l.lots * lot,
                    "entry": round(float(px), 2)})
    now = datetime.now(IST)
    pos = {"id": now.strftime("%Y%m%d%H%M%S%f"), "name": body.name, "underlying": body.underlying.upper(),
           "exchange": body.exchange, "opened": now.isoformat(timespec="seconds"), "status": "open", "legs": out,
           "premium": round(sum(l["entry"] * l["qty"] * (-1 if l["side"] == "BUY" else 1) for l in out), 2)}
    with _paper_lock:
        book = _paper_book()
        book["positions"].append(pos)
        _save_paper_book(book)
    return pos


@app.post("/api/paper/options/{pid}/exit")
def paper_options_exit(pid: str, kite: KiteConnect = Depends(kite_client)):
    return _close_paper(pid, kite)


@app.delete("/api/paper/options/{pid}")
def paper_options_delete(pid: str):
    """Remove a closed paper position from the journal."""
    with _paper_lock:
        book = _paper_book()
        keep = [x for x in book["positions"] if not (x["id"] == pid and x["status"] == "closed")]
        if len(keep) == len(book["positions"]):
            raise HTTPException(404, "No closed paper position with that id")
        book["positions"] = keep
        _save_paper_book(book)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Paper equity trades from the Strategy Screener (single leg, stop / target managed by the alert checker).
# ---------------------------------------------------------------------------
def _eq_costs(product: str, buy_val: float, sell_val: float) -> float:
    if product == "MIS":
        return round(orb.costs(buy_val, sell_val), 2)
    # delivery: no brokerage, STT 0.1% both sides, NSE txn, SEBI, stamp 0.015% on buys, DP ₹15.93 on the sell
    txn = 0.0000297 * (buy_val + sell_val)
    sebi = 1e-6 * (buy_val + sell_val)
    return round(0.001 * (buy_val + sell_val) + txn + sebi + 0.18 * (txn + sebi) + 0.00015 * buy_val + 15.93, 2)


def _pos_costs(p: dict, buy: float, sell: float, settled: bool = False) -> float:
    if p.get("kind") == "equity":
        return _eq_costs(p.get("product", "MIS"), buy, sell)
    if settled:   # legs that expire are settled by the exchange: no exit order, STT on the exercised intrinsic value
        exercised = sum(l["exit"] * l["qty"] for l in p["legs"] if l["side"] == "BUY")
        # sell STT only on the premium of legs sold to open; exercised long legs pay the exercise STT instead
        sold = sum(l["entry"] * l["qty"] for l in p["legs"] if l["side"] == "SELL")
        return _opt_costs(buy, sell, len(p["legs"]), exercised, stt_sell=sold)
    return _opt_costs(buy, sell, 2 * len(p["legs"]))


def _settle_expired(book: dict, kite: KiteConnect) -> bool:
    """Open paper option positions whose expiry has passed (or is today after 15:30) are settled the way the exchange
    does it: each leg at its intrinsic value on the underlying's official closing price for the expiry day."""
    now = datetime.now(IST)
    today = now.date().isoformat()
    def expired(p: dict) -> bool:
        last = max(l["expiry"] for l in p["legs"])
        return last < today or (last == today and now.strftime("%H:%M") >= "15:30")

    due = [p for p in book["positions"] if p["status"] == "open" and p.get("kind") != "equity" and p["legs"] and expired(p)]
    changed = False
    for p in due:
        try:
            key = OPTION_INDICES.get(p["underlying"], ("NFO", f"NSE:{p['underlying']}"))[1]
            tok = kite.ltp(key)[key]["instrument_token"]
            prices = {}
            for l in p["legs"]:
                bars = kite.historical_data(tok, l["expiry"], l["expiry"], "day")
                if not bars:
                    raise ValueError(f"no {key} close for {l['expiry']}")
                close = float(bars[-1]["close"])
                iv = max(0.0, close - l["strike"]) if l["type"] == "CE" else max(0.0, l["strike"] - close)
                prices[l["key"]] = round(iv, 2)
                l["exit"], l["settle_px"] = prices[l["key"]], close
        except Exception as exc:  # noqa: BLE001 - try again on the next read
            log.warning("paper option settlement %s: %s", p["id"], exc)
            continue
        buy, sell = _leg_values(p["legs"], prices)
        p.update(status="closed", closed=f"{max(l['expiry'] for l in p['legs'])}T15:30:00+05:30",
                 exit_reason="expiry settlement", gross=round(_gross(p["legs"], prices), 2),
                 costs=_pos_costs(p, buy, sell, settled=True))
        p["net"] = round(p["gross"] - p["costs"], 2)
        changed = True
    return changed


def _close_paper(pid: str, kite: KiteConnect, reason: str = "closed by you") -> dict:
    with _paper_lock:
        book = _paper_book()
        p = next((x for x in book["positions"] if x["id"] == pid), None)
        if not p or p["status"] != "open":
            raise HTTPException(404, "No open paper position with that id")
        q = _kite_call(kite.quote, *[l["key"] for l in p["legs"]])
        prices = {}
        for l in p["legs"]:
            px = _fill(q.get(l["key"]) or {}, "SELL" if l["side"] == "BUY" else "BUY")
            prices[l["key"]] = round(float(px), 2) if px else l["entry"]
            l["exit"] = prices[l["key"]]
        buy, sell = _leg_values(p["legs"], prices)
        p.update(status="closed", closed=datetime.now(IST).isoformat(timespec="seconds"), exit_reason=reason,
                 gross=round(_gross(p["legs"], prices), 2), costs=_pos_costs(p, buy, sell))
        p["net"] = round(p["gross"] - p["costs"], 2)
        _save_paper_book(book)
    return p


class PaperEquity(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    side: Literal["BUY", "SELL"] = "BUY"
    qty: int = Field(ge=1, le=1_000_000)
    product: Literal["MIS", "CNC"] = "MIS"
    sl: float | None = Field(None, gt=0)
    target: float | None = Field(None, gt=0)
    strategy: str = Field("Manual", max_length=60)


@app.post("/api/paper/equity")
def paper_equity_open(body: PaperEquity, kite: KiteConnect = Depends(kite_client)):
    key = f"NSE:{body.symbol.upper()}"
    q = _kite_call(kite.quote, key).get(key)
    if not q:
        raise HTTPException(400, f"{key} not found")
    px = _fill(q, body.side)
    if not px:
        raise HTTPException(400, f"No price for {key}")
    now = datetime.now(IST)
    pos = {"id": now.strftime("%Y%m%d%H%M%S%f"), "kind": "equity", "name": body.strategy, "underlying": body.symbol.upper(),
           "exchange": "NSE", "product": body.product, "opened": now.isoformat(timespec="seconds"), "status": "open",
           "sl": body.sl, "target": body.target,
           "legs": [{"tradingsymbol": body.symbol.upper(), "key": key, "side": body.side, "lots": 1, "lot_size": body.qty,
                     "qty": body.qty, "entry": round(float(px), 2)}],
           "premium": round(float(px) * body.qty * (-1 if body.side == "BUY" else 1), 2)}
    with _paper_lock:
        book = _paper_book()
        book["positions"].append(pos)
        _save_paper_book(book)
    return pos


