"""The Multi Agent Team's page. Fully automatic and paper only: nothing here can place an order, and no response carries a credential."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from core import app, store
from routes import breakout
from strategies import all_desks, common, multi_agent_team

log = logging.getLogger("kite-desk.multi-agent-team-route")
LOOP_SECONDS = 15
multi_agent_team.stock_source = breakout.fno_stocks
multi_agent_team.risk_gate = all_desks.gate


def _loop() -> None:
    time.sleep(60)
    while True:
        try:
            if store.load():
                multi_agent_team.tick(datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("multi agent team pass failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start_team() -> None:
    threading.Thread(target=_loop, daemon=True, name="multi-agent-team-auto").start()


@app.get("/api/multi-agent-team")
def team_view(day: str | None = None):
    return multi_agent_team.view(day)
