"""The daily momentum sweep and the paper desks. Paper only."""
from __future__ import annotations

from datetime import datetime
import threading

from fastapi import HTTPException
from kiteconnect import KiteConnect

from core import IST, app, log, store
from routes.market import _instrument_list
from strategies import all_desks
from strategies import momentum
from strategies import paper_desk


# Daily momentum sweep (momentum.py): every NSE equity scored once a day, for the market pages and the teams.
# Runs after the close, or at start-up when stale; during market hours only when there is no sweep at all, and then at half pace.


def _momentum_loop() -> None:
    import time as _t
    _t.sleep(30)
    while True:
        try:
            d = store.load()
            now = datetime.now(IST)
            day = momentum.sweep.due(now)
            market = now.weekday() < 5 and "09:00" <= now.strftime("%H:%M") <= "15:40"
            if d and day and (not market or momentum.latest() is None):
                kite = KiteConnect(api_key=d["api_key"])
                kite.set_access_token(d["access_token"])
                momentum.sweep.run(kite, lambda exch: _instrument_list(kite, exch), day, pace=0.7 if market else 0.34)
        except Exception:  # noqa: BLE001
            log.exception("momentum loop failed")
        _t.sleep(600)


@app.on_event("startup")
def _start_momentum() -> None:
    threading.Thread(target=_momentum_loop, daemon=True, name="momentum").start()


@app.get("/api/momentum")
def momentum_status():
    d = momentum.latest()
    top = sorted((x for x in (d or {}).get("stocks", []) if x.get("momentum")), key=lambda x: -x["rs"])[:60]
    return {"progress": momentum.sweep.progress, "day": d and d["day"], "built": d and d["built"],
            "scanned": d and d["scanned"], "liquid": d and d["liquid"], "momentum": d and d["momentum"], "top": top}




@app.get("/api/desk")
def desks():
    """One paper desk per strategy, ₹1 lakh each. Paper only: nothing here places an order."""
    return {"capital": paper_desk.CAPITAL, "capitals": {k: paper_desk.CAPITALS.get(k, paper_desk.CAPITAL) for k in paper_desk.DESKS}, "desks": paper_desk.overview()}


@app.get("/api/portfolio")
def portfolio():
    """Every system combined: the Agent Pipeline and the Multi Agent Team, with every open and closed trade. Paper only."""
    return all_desks.portfolio()


@app.get("/api/desk/{k}")
def desk(k: str):
    try:
        return paper_desk.view(k)
    except KeyError:
        raise HTTPException(404, "No such desk") from None


@app.post("/api/desk/{k}/reset")
def desk_reset(k: str):
    if k not in paper_desk.DESKS:
        raise HTTPException(400, "This desk cannot be reset here")
    return paper_desk.reset(k)


@app.get("/api/confidence")
def confidence():
    """Every strategy's trades with the confidence each was taken at, and the win rate per confidence band. Read only."""
    from strategies import confidence_log
    return confidence_log.view()


@app.get("/api/live-terminal/{strategy}")
def live_terminal_feed(strategy: str):
    """One strategy's live terminal: its agents, the candidate in flow, scans, trade, log. Built from its real state only."""
    from strategies import live_terminal
    if strategy not in live_terminal.FEEDS:
        raise HTTPException(404, "Unknown strategy")
    return live_terminal.feed(strategy)
