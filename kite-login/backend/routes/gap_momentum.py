"""Gap Momentum's page and scheduler. Paper only: nothing here can place an order, and no response carries a credential."""
from __future__ import annotations

import logging
import threading
import time

from core import app, kite_client, store
from strategies.gap_momentum import desk

log = logging.getLogger("kite-desk.gap-momentum-route")
LOOP_SECONDS = 5                      # the strategy polls its stop every 5 s
desk._kite_factory = kite_client     # read-only use: instruments and quotes


def _loop() -> None:
    time.sleep(20)
    while True:
        try:
            if store.load():
                desk.tick()
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("gap momentum pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_gap_momentum() -> None:
    threading.Thread(target=_loop, daemon=True, name="gap-momentum").start()


@app.get("/api/gap-momentum")
def gap_momentum_view(day: str | None = None):
    return desk.view(day)
