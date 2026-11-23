"""The Claude Trader's page. Fully automatic and paper only: nothing here can place an order, and no response carries a credential."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core import app, store
from routes import breakout
from strategies import claude_trader, common

log = logging.getLogger("kite-desk.claude-trader-route")
LOOP_SECONDS = 15
claude_trader.stock_source = breakout.fno_stocks
claude_trader.fno_source = breakout.fno_names


def _loop() -> None:
    time.sleep(90)
    while True:
        try:
            if store.load():
                claude_trader.tick(datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("claude trader pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_claude_trader() -> None:
    threading.Thread(target=_loop, daemon=True, name="claude-trader-auto").start()


@app.get("/api/claude-trader")
def claude_trader_view(day: str | None = None):
    return claude_trader.view(day)
