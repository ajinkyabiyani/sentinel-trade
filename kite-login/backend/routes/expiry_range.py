"""The Expiry Range Agent's page. Read only: it predicts and checks ranges, and never trades."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core import app, store
from routes import breakout
from strategies import common, expiry_range, expiry_range_stocks

log = logging.getLogger("kite-desk.expiry-range-route")
LOOP_SECONDS = 60
expiry_range_stocks.stock_source = breakout.fno_names


def _loop() -> None:
    time.sleep(90)
    while True:
        try:
            if store.load():
                now = datetime.now(common.IST)
                expiry_range.tick(now)
                expiry_range_stocks.tick(now)
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("expiry range pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_expiry_range() -> None:
    threading.Thread(target=_loop, daemon=True, name="expiry-range").start()


@app.get("/api/expiry-range")
def expiry_range_view():
    return expiry_range.view()


@app.get("/api/expiry-range/stocks")
def expiry_range_stocks_view():
    return expiry_range_stocks.view()
