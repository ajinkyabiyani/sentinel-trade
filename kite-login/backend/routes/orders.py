"""Orders and GTTs. The only module that places real orders; each one is confirmed by the user on the Trade page."""
from __future__ import annotations

from typing import Literal
import math

from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field, model_validator

from core import _kite_call, app, kite_client, log
from routes.market import Exchange


# ---------------------------------------------------------------------------
# Orders: every Kite variety, product, order type and validity
# ---------------------------------------------------------------------------
EQUITY = {"NSE", "BSE"}
Product = Literal["CNC", "MIS", "NRML", "MTF"]
OrderType = Literal["MARKET", "LIMIT", "SL", "SL-M"]


class OrderBody(BaseModel):
    variety: Literal["regular", "amo", "co", "iceberg", "auction"] = "regular"
    exchange: Exchange
    tradingsymbol: str = Field(min_length=1, max_length=64)
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    product: Product
    order_type: OrderType
    price: float | None = Field(None, ge=0)
    trigger_price: float | None = Field(None, ge=0)
    validity: Literal["DAY", "IOC", "TTL"] = "DAY"
    validity_ttl: int | None = Field(None, ge=1, le=1440)
    disclosed_quantity: int | None = Field(None, ge=0)
    iceberg_legs: int | None = Field(None, ge=2, le=50)
    iceberg_quantity: int | None = Field(None, gt=0)
    auction_number: str | None = Field(None, max_length=10)
    market_protection: float | None = None   # -1 = Kite's automatic protection for MARKET / SL-M
    tag: str | None = Field(None, max_length=20)

    @model_validator(mode="after")
    def _rules(self):
        ex, p, ot, v = self.exchange, self.product, self.order_type, self.variety
        if p in ("CNC", "MTF") and ex not in EQUITY:
            raise ValueError(f"{p} is for NSE/BSE equity; use NRML (overnight) or MIS (intraday) on {ex}")
        if p == "NRML" and ex in EQUITY:
            raise ValueError("NRML is for F&O, currency and commodity; use CNC, MTF or MIS for equity")
        if ot in ("LIMIT", "SL") and not self.price:
            raise ValueError(f"{ot} orders need a price")
        if ot in ("SL", "SL-M") and not self.trigger_price:
            raise ValueError(f"{ot} orders need a trigger price")
        if v == "co":
            if p != "MIS" or ot not in ("MARKET", "LIMIT") or not self.trigger_price:
                raise ValueError("Cover orders are MIS, MARKET or LIMIT, with a stop-loss trigger price")
        if v == "iceberg":
            if not self.iceberg_legs or ot not in ("LIMIT", "SL"):
                raise ValueError("Iceberg orders need 2-50 legs and a LIMIT or SL order type")
            if not self.iceberg_quantity:
                self.iceberg_quantity = math.ceil(self.quantity / self.iceberg_legs)
        if v == "auction" and (ex not in EQUITY or p != "CNC" or ot != "LIMIT" or not self.auction_number):
            raise ValueError("Auction orders are NSE/BSE, CNC, LIMIT, with an auction number")
        if self.validity == "TTL" and not self.validity_ttl:
            raise ValueError("TTL validity needs minutes (validity_ttl)")
        if ot in ("MARKET", "SL-M") and self.market_protection is None:
            self.market_protection = -1
        if ot in ("MARKET", "SL-M"):
            self.price = None
        if ot in ("MARKET", "LIMIT") and v != "co":
            self.trigger_price = None
        return self

    def kwargs(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class ModifyBody(BaseModel):
    quantity: int | None = Field(None, gt=0)
    price: float | None = Field(None, ge=0)
    order_type: OrderType | None = None
    trigger_price: float | None = Field(None, ge=0)
    validity: Literal["DAY", "IOC", "TTL"] | None = None
    disclosed_quantity: int | None = Field(None, ge=0)


class ConvertBody(BaseModel):
    exchange: Exchange
    tradingsymbol: str
    transaction_type: Literal["BUY", "SELL"]
    position_type: Literal["day", "overnight"]
    quantity: int = Field(gt=0)
    old_product: Product
    new_product: Product


@app.post("/api/orders/margin")
def order_margin(body: OrderBody, kite: KiteConnect = Depends(kite_client)):
    """Required margin and all charges for one order, before placing it."""
    params = {k: v for k, v in body.kwargs().items() if k in (
        "variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product", "order_type",
        "price", "trigger_price")}
    params.setdefault("price", 0)
    params.setdefault("trigger_price", 0)
    r = _kite_call(kite.order_margins, [params])[0]
    ch = r.get("charges") or {}
    return {"total": r.get("total"), "leverage": r.get("leverage"), "span": r.get("span"),
            "exposure": r.get("exposure"), "option_premium": r.get("option_premium"),
            "charges": {"total": ch.get("total"), "brokerage": ch.get("brokerage"),
                        "stt": ch.get("transaction_tax"), "exchange": ch.get("exchange_turnover_charge"),
                        "sebi": ch.get("sebi_turnover_charge"), "stamp_duty": ch.get("stamp_duty"),
                        "gst": (ch.get("gst") or {}).get("total")}}


@app.post("/api/orders")
def place_order(body: OrderBody, kite: KiteConnect = Depends(kite_client)):
    order_id = _kite_call(kite.place_order, **body.kwargs())
    log.info("placed %s %s %s x%d %s %s -> %s", body.variety, body.transaction_type, body.tradingsymbol,
             body.quantity, body.product, body.order_type, order_id)
    return {"order_id": order_id}


@app.get("/api/orders")
def orders(kite: KiteConnect = Depends(kite_client)):
    return _kite_call(kite.orders)


@app.put("/api/orders/{variety}/{order_id}")
def modify_order(variety: str, order_id: str, body: ModifyBody, kite: KiteConnect = Depends(kite_client)):
    _kite_call(kite.modify_order, variety, order_id, **body.model_dump(exclude_none=True))
    return {"order_id": order_id}


@app.delete("/api/orders/{variety}/{order_id}")
def cancel_order(variety: str, order_id: str, parent_order_id: str | None = None,
                 kite: KiteConnect = Depends(kite_client)):
    _kite_call(kite.cancel_order, variety, order_id, parent_order_id=parent_order_id)
    return {"order_id": order_id}


@app.get("/api/trades")
def trades(kite: KiteConnect = Depends(kite_client)):
    return _kite_call(kite.trades)


@app.get("/api/positions")
def positions(kite: KiteConnect = Depends(kite_client)):
    return _kite_call(kite.positions)


@app.post("/api/positions/convert")
def convert_position(body: ConvertBody, kite: KiteConnect = Depends(kite_client)):
    _kite_call(kite.convert_position, **body.model_dump())
    return {"ok": True}


@app.get("/api/holdings")
def holdings(kite: KiteConnect = Depends(kite_client)):
    return _kite_call(kite.holdings)


# ---------------------------------------------------------------------------
# GTT (good-till-triggered): stops and targets that survive overnight. A normal SL order is a DAY order and is
# cancelled at the close, so a swing position needs GTTs.
# ---------------------------------------------------------------------------
_NSE_SLABS = ((250.0, 0.01, True), (1000.0, 0.05, False), (5000.0, 0.10, False), (10000.0, 0.50, False),
              (20000.0, 1.00, False))
SL_LIMIT_BUFFER_PCT = 1.0      # stop leg is a LIMIT this far below its trigger, so it fills on a fast fall
BUY_LIMIT_BUFFER_PCT = 0.5     # buy-stop leg is a LIMIT this far above its trigger
GTT_MIN_GAP_PCT = 0.25         # Kite rejects triggers closer than this to the last price


def _tick(price: float, exchange: str) -> float:
    if exchange in ("BSE", "BFO"):
        return 0.01 if price < 100 else 0.05
    for limit, tick, strict in _NSE_SLABS:
        if (price < limit) if strict else (price <= limit):
            return tick
    return 5.0


def _round_tick(price: float, exchange: str, direction: str = "nearest") -> float:
    t = _tick(price, exchange)
    q = price / t
    n = math.ceil(q - 1e-9) if direction == "up" else math.floor(q + 1e-9) if direction == "down" else round(q)
    return round(n * t, 2)


def _ltp(kite: KiteConnect, exchange: str, symbol: str) -> float:
    key = f"{exchange}:{symbol}"
    px = (_kite_call(kite.ltp, key).get(key) or {}).get("last_price")
    if not px:
        raise HTTPException(400, f"No last price for {key}")
    return float(px)


class ProtectBody(BaseModel):
    exchange: Literal["NSE", "BSE"]
    tradingsymbol: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0)
    product: Literal["CNC", "MTF"] = "CNC"
    stop: float = Field(gt=0)
    targets: list[float] = Field(min_length=1, max_length=3)
    fractions: list[float] | None = None    # share of quantity per target (default: equal)


class BuyStopBody(BaseModel):
    exchange: Literal["NSE", "BSE"]
    tradingsymbol: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0)
    product: Literal["CNC", "MTF"] = "CNC"
    trigger: float = Field(gt=0)


@app.get("/api/gtt")
def gtt_list(kite: KiteConnect = Depends(kite_client)):
    out = []
    for g in _kite_call(kite.get_gtts):
        cond = g.get("condition") or {}
        out.append({"id": g.get("id"), "type": g.get("type"), "status": g.get("status"),
                    "exchange": cond.get("exchange"), "tradingsymbol": cond.get("tradingsymbol"),
                    "trigger_values": cond.get("trigger_values"), "last_price": cond.get("last_price"),
                    "orders": [{k: o.get(k) for k in ("transaction_type", "quantity", "order_type", "product", "price")}
                               | {"result": (o.get("result") or {}).get("order_result", {}).get("status")
                                  if isinstance(o.get("result"), dict) else None}
                               for o in g.get("orders") or []],
                    "created_at": str(g.get("created_at") or ""), "expires_at": str(g.get("expires_at") or "")})
    return out


@app.post("/api/gtt/protect")
def gtt_protect(body: ProtectBody, kite: KiteConnect = Depends(kite_client)):
    """Stop-loss + target(s) for a held position: one OCO GTT per target tranche, each covering its share of the
    quantity with the same stop. When a tranche's target fills, that tranche's stop disappears with it."""
    ex, sym = body.exchange, body.tradingsymbol.upper()
    ltp = _ltp(kite, ex, sym)
    stop = _round_tick(body.stop, ex, "down")
    targets = sorted(_round_tick(t, ex) for t in body.targets)
    if stop >= ltp * (1 - GTT_MIN_GAP_PCT / 100):
        raise HTTPException(400, f"Stop {stop} must be below the last price {ltp} (by at least {GTT_MIN_GAP_PCT}%)."
                                 " If price is already under your stop, exit with a normal order instead.")
    if targets[0] <= ltp * (1 + GTT_MIN_GAP_PCT / 100):
        raise HTTPException(400, f"Target {targets[0]} must be above the last price {ltp} (by at least {GTT_MIN_GAP_PCT}%)")
    fr = body.fractions or [1 / len(targets)] * len(targets)
    if len(fr) != len(targets) or abs(sum(fr) - 1) > 1e-6:
        raise HTTPException(422, "fractions must match targets and sum to 1")
    sl_limit = _round_tick(stop * (1 - SL_LIMIT_BUFFER_PCT / 100), ex, "down")
    placed, left = [], body.quantity
    for k, (tp, f) in enumerate(zip(targets, fr)):
        q = left if k == len(targets) - 1 else min(left, max(1, round(body.quantity * f)))
        left -= q
        if q <= 0:
            continue
        legs = [{"transaction_type": "SELL", "quantity": q, "order_type": "LIMIT", "product": body.product, "price": sl_limit},
                {"transaction_type": "SELL", "quantity": q, "order_type": "LIMIT", "product": body.product, "price": tp}]
        r = _kite_call(kite.place_gtt, kite.GTT_TYPE_OCO, sym, ex, [stop, tp], ltp, legs)
        placed.append({"trigger_id": r.get("trigger_id") if isinstance(r, dict) else r, "quantity": q, "stop": stop,
                       "stop_limit": sl_limit, "target": tp})
    log.info("GTT protect %s:%s x%d stop %s targets %s -> %s", ex, sym, body.quantity, stop, targets, placed)
    return {"placed": placed, "last_price": ltp}


@app.post("/api/gtt/buy-stop")
def gtt_buy_stop(body: BuyStopBody, kite: KiteConnect = Depends(kite_client)):
    """An entry that waits at the trigger: a single-leg GTT that buys (LIMIT a little above) when price trades
    at or above the trigger, on whichever day that happens (GTTs last up to a year)."""
    ex, sym = body.exchange, body.tradingsymbol.upper()
    ltp = _ltp(kite, ex, sym)
    trig = _round_tick(body.trigger, ex, "up")
    if trig <= ltp * (1 + GTT_MIN_GAP_PCT / 100):
        raise HTTPException(400, f"Trigger {trig} must be above the last price {ltp}; the stock is already through it,"
                                 " so use a normal buy order instead.")
    limit = _round_tick(trig * (1 + BUY_LIMIT_BUFFER_PCT / 100), ex, "up")
    legs = [{"transaction_type": "BUY", "quantity": body.quantity, "order_type": "LIMIT", "product": body.product, "price": limit}]
    r = _kite_call(kite.place_gtt, kite.GTT_TYPE_SINGLE, sym, ex, [trig], ltp, legs)
    log.info("GTT buy-stop %s:%s x%d trigger %s limit %s", ex, sym, body.quantity, trig, limit)
    return {"trigger_id": r.get("trigger_id") if isinstance(r, dict) else r, "trigger": trig, "limit": limit, "last_price": ltp}


@app.delete("/api/gtt/{trigger_id}")
def gtt_delete(trigger_id: int, kite: KiteConnect = Depends(kite_client)):
    _kite_call(kite.delete_gtt, trigger_id)
    return {"deleted": trigger_id}


