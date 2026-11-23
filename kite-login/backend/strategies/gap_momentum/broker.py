"""Paper broker for Gap Momentum: real Kite quotes, simulated fills. It has no order-placing method at all.

The original package also had a KiteBroker that sends real orders. It was left out on purpose: this app trades the
strategy on paper only. A buy limit fills when it is at or above the best ask; a sell limit when it is at or below the
best bid; the stop (SL) fills at the last price once the last price trades at or below the trigger.
"""
import itertools
import logging

log = logging.getLogger("gap_momentum")


class PaperBroker:
    _ids = itertools.count(1)

    def __init__(self, kite):
        self.kite = kite
        self.orders = {}
        self._nfo = None

    # ---- market data (read only) ----
    def nfo_instruments(self):
        if self._nfo is None:
            self._nfo = self.kite.instruments("NFO")
        return self._nfo

    def quote(self, keys):
        return self.kite.quote(keys)

    def _book(self, tsym):
        q = self.kite.quote([f"NFO:{tsym}"])[f"NFO:{tsym}"]
        d = q.get("depth") or {}
        bid = (d.get("buy") or [{}])[0].get("price") or q["last_price"]
        ask = (d.get("sell") or [{}])[0].get("price") or q["last_price"]
        return q["last_price"], bid, ask

    # ---- simulated orders ----
    def _new(self, **o):
        oid = f"PAPER-{next(self._ids)}"
        o.update(status="OPEN", filled=0, average_price=0.0)
        self.orders[oid] = o
        log.info("[paper] order %s %s", oid, o)
        return oid

    def buy_limit(self, tsym, qty, price):
        return self._new(kind="BUY", tsym=tsym, qty=qty, price=price)

    def sell_limit(self, tsym, qty, price):
        return self._new(kind="SELL", tsym=tsym, qty=qty, price=price)

    def sell_stoploss(self, tsym, qty, trigger, price):
        o = self._new(kind="SL", tsym=tsym, qty=qty, price=price, trigger=trigger)
        self.orders[o]["status"] = "TRIGGER PENDING"
        return o

    def modify(self, order_id, **kw):
        o = self.orders[order_id]
        if "price" in kw:
            o["price"] = kw["price"]
        if "trigger_price" in kw:
            o["trigger"] = kw["trigger_price"]

    def cancel(self, order_id):
        o = self.orders[order_id]
        if o["status"] != "COMPLETE":
            o["status"] = "CANCELLED"

    def order(self, order_id):
        o = self.orders[order_id]
        if o["status"] in ("OPEN", "TRIGGER PENDING"):
            ltp, bid, ask = self._book(o["tsym"])
            fill = None
            if o["kind"] == "BUY" and ask and o["price"] >= ask:
                fill = ask
            elif o["kind"] == "SELL" and bid and o["price"] <= bid:
                fill = bid
            elif o["kind"] == "SL" and ltp <= o["trigger"]:
                fill = ltp
            if fill is not None:
                o.update(status="COMPLETE", filled=o["qty"], average_price=float(fill))
        return dict(status=o["status"], filled=o["filled"], average_price=o["average_price"])
