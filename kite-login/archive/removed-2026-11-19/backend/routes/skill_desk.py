"""The Claude Skill Desk's page. Fully automatic and paper only: nothing here can place an order, and no response carries a credential."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core import app, store
from routes import breakout
from strategies import common, skill_desk

log = logging.getLogger("kite-desk.skill-desk-route")
LOOP_SECONDS = 15
skill_desk.stock_source = breakout.fno_stocks


def _loop() -> None:
    time.sleep(75)
    while True:
        try:
            if store.load():
                skill_desk.tick(datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("skill desk pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_skill_desk() -> None:
    threading.Thread(target=_loop, daemon=True, name="skill-desk-auto").start()


@app.get("/api/skill-desk")
def skill_desk_view(day: str | None = None):
    return skill_desk.view(day)
