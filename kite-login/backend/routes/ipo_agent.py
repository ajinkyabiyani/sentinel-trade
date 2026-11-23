"""The IPO Agent's page. Read only: it analyses upcoming IPOs and never applies for anything."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core import app
from strategies import common, ipo_agent

log = logging.getLogger("kite-desk.ipo-agent-route")
LOOP_SECONDS = 120


def _loop() -> None:
    time.sleep(45)
    while True:
        try:
            ipo_agent.tick(datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("ipo agent pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_ipo_agent() -> None:
    threading.Thread(target=_loop, daemon=True, name="ipo-agent").start()


@app.get("/api/ipo-agent")
def ipo_agent_view():
    return ipo_agent.view()


@app.post("/api/ipo-agent/run")
def ipo_agent_run():
    threading.Thread(target=ipo_agent.run, daemon=True, name="ipo-agent-run").start()
    return {"started": True}
