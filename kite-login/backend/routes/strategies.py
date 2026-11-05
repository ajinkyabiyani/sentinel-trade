"""ORB, the daily momentum sweep, paper desks, backtests and the improvement loop. Paper only."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
import json
import threading

from fastapi import Depends, HTTPException, Query
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from core import IST, REPORTS_DIR, app, kite_client, log, store
from routes.darvas import ScriptRunner, _top500
from routes.market import _instrument_list
from strategies import improve
from strategies import momentum
from strategies import orb
from strategies import orb_backtest
from strategies import paper_desk


# ---------------------------------------------------------------------------
# ORB strategy tab (orb.py): paper signals only, nothing here places an order
# ---------------------------------------------------------------------------


def _orb_tick() -> None:
    d = store.load()
    if not d:
        return
    kite = KiteConnect(api_key=d["api_key"])
    kite.set_access_token(d["access_token"])

    def capital() -> float | None:
        try:
            net = (kite.margins("equity") or {}).get("net")
            return round(float(net), 2) if net and net > 0 else None
        except Exception:  # noqa: BLE001
            return None

    orb.engine.tick(kite, lambda exch: _instrument_list(kite, exch), capital)


def _orb_loop() -> None:
    import time
    time.sleep(8)
    while True:
        try:
            _orb_tick()
        except Exception:
            log.exception("ORB loop failed")
        time.sleep(15)


@app.on_event("startup")
def _start_orb() -> None:
    threading.Thread(target=_orb_loop, daemon=True, name="orb").start()


class OrbSettings(BaseModel):
    mode: Literal["3R", "none"] | None = None
    momentum: bool | None = None     # momentum check on new signals (from now)
    wide: bool | None = None         # F&O + momentum stocks (from the next session)
    notional: float | None = Field(None, ge=1000, le=10_000_000)
    capital: float | None = Field(None, ge=1000, le=1_000_000_000)


@app.get("/api/orb/state")
def orb_state(kite: KiteConnect = Depends(kite_client)):
    e = orb.engine
    if e.snapshot is None and not e.busy.locked():
        threading.Thread(target=_orb_tick, daemon=True).start()
    return {"snapshot": e.snapshot, "progress": e.progress, "error": e.error}


@app.post("/api/orb/settings")
def orb_settings(body: OrbSettings, kite: KiteConnect = Depends(kite_client)):
    try:
        orb.engine.update_settings(body.mode, body.notional, body.capital, body.momentum, body.wide)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"snapshot": orb.engine.snapshot, "progress": orb.engine.progress, "error": orb.engine.error}


# Daily momentum sweep (momentum.py): every NSE equity scored once a day; the strongest liquid names join the
# next ORB session's universe. Runs after the close, or at start-up when stale; during market hours only when there
# is no sweep at all, and then at half pace so the ORB loader keeps most of Kite's history rate.


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




def _desk_loop() -> None:
    """Darvas swing paper desk: fills, trailing stops and exits once a minute in market hours."""
    import time as _t
    _t.sleep(40)
    while True:
        try:
            d = store.load()
            if d:
                kite = KiteConnect(api_key=d["api_key"])
                kite.set_access_token(d["access_token"])
                paper_desk.tick_swing(kite, datetime.now(paper_desk.IST))
        except Exception:  # noqa: BLE001
            log.exception("paper desk loop failed")
        _t.sleep(60)


@app.on_event("startup")
def _start_desks() -> None:
    threading.Thread(target=_desk_loop, daemon=True, name="paper-desks").start()


@app.get("/api/desk")
def desks():
    """One paper desk per strategy, ₹1 lakh each. Paper only: nothing here places an order."""
    return {"capital": paper_desk.CAPITAL, "desks": paper_desk.overview()}


@app.get("/api/portfolio")
def portfolio():
    """All strategy desks combined (₹1 lakh each), with every open and closed trade. Paper only."""
    return paper_desk.portfolio()


@app.get("/api/desk/{k}")
def desk(k: str):
    try:
        return paper_desk.view(k)
    except KeyError:
        raise HTTPException(404, "No such desk") from None


@app.post("/api/desk/{k}/reset")
def desk_reset(k: str):
    if k not in paper_desk.DESKS or k == "orb":
        raise HTTPException(400, "This desk cannot be reset here")
    return paper_desk.reset(k)




@app.on_event("startup")
def _start_improve() -> None:
    threading.Thread(target=improve.loop, daemon=True, name="improve-loop").start()


@app.get("/api/improve")
def improve_view():
    """Strategy improvement loop: the four agents' latest findings, proposals, and the change history."""
    return improve.view()


@app.post("/api/improve/run")
def improve_run(strategy: str | None = Query(None, pattern="^(orb)$")):
    try:
        improve.start(strategy)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None
    return improve.view()


@app.post("/api/improve/{strategy}/approve")
def improve_approve(strategy: str):
    if strategy not in improve.STRATEGIES:
        raise HTTPException(404, "No such strategy")
    try:
        return improve.approve(strategy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/improve/{strategy}/dismiss")
def improve_dismiss(strategy: str):
    if strategy not in improve.STRATEGIES:
        raise HTTPException(404, "No such strategy")
    return improve.dismiss(strategy)


@app.post("/api/improve/rollback/{change_id}")
def improve_rollback(change_id: str):
    try:
        return improve.rollback(change_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/orb/paper")
def orb_paper():
    return orb.engine.paper()


@app.get("/api/orb/chart")
def orb_chart(symbol: str = Query(min_length=1, max_length=30), kite: KiteConnect = Depends(kite_client)):
    try:
        return orb.engine.chart(symbol.upper())
    except KeyError:
        raise HTTPException(404, f"{symbol} is not in today's ORB data")


# ---------------------------------------------------------------------------
# Backtest tab: ORB (orb_backtest.py, the live engine replayed over past sessions) and Darvas (darvas_algo.py
# backtest, walk-forward with unseen test windows). Historical data only, nothing is ordered.
# ---------------------------------------------------------------------------

DARVAS_BT_FILE = REPORTS_DIR / "backtest_darvas.json"
DARVAS_BT_CFG = REPORTS_DIR / "backtest_darvas_config.json"
bt_runner = ScriptRunner("A Darvas backtest is already running")


class OrbBacktestBody(BaseModel):
    sessions: int = Field(60, ge=10, le=250)
    capital: float = Field(100000, ge=10000, le=1_000_000_000)
    notional: float = Field(50000, ge=1000, le=10_000_000)
    mode: Literal["3R", "none"] = "3R"
    momentum: bool = True


class DarvasBacktestBody(BaseModel):
    universe: Literal["top500", "core", "NIFTY 50", "NIFTY BANK", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100"] = "top500"
    years: float = Field(6, ge=2, le=10)
    capital: float = Field(100000, ge=10000, le=1_000_000_000)
    risk_pct: float = Field(1.0, ge=0.25, le=3)
    tune: bool = False   # walk-forward: settings picked on each 2-year window, traded only on the next 6 months


@app.get("/api/backtest/orb")
def backtest_orb():
    return {"progress": orb_backtest.bt.progress, "result": orb_backtest.bt.result()}


@app.post("/api/backtest/orb")
def backtest_orb_run(body: OrbBacktestBody, kite: KiteConnect = Depends(kite_client)):
    if orb_backtest.bt.progress.get("running"):
        raise HTTPException(409, "An ORB backtest is already running")
    threading.Thread(target=orb_backtest.bt.run, daemon=True, name="orb-backtest",
                     args=(kite, lambda exch: _instrument_list(kite, exch), body.sessions, body.capital, body.notional,
                           body.mode, body.momentum)).start()
    orb_backtest.bt.progress = {"running": True, "done": 0, "total": 0, "stage": "Starting", "error": ""}
    return {"progress": orb_backtest.bt.progress}


@app.get("/api/backtest/darvas")
def backtest_darvas():
    res = None
    try:
        res = json.loads(DARVAS_BT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {"status": bt_runner.status(), "result": res}


@app.post("/api/backtest/darvas")
def backtest_darvas_run(body: DarvasBacktestBody):
    d = store.load()
    if not d:
        raise HTTPException(401, "Not logged in to Kite")
    REPORTS_DIR.mkdir(exist_ok=True)
    cfg = {"kite": {"history_years": body.years}, "risk": {"capital": body.capital, "risk_per_trade_pct": body.risk_pct}}
    if body.tune:
        cfg["backtest"] = {"train_years": 2.0, "test_months": 6, "min_trades_train": 20,
                           "param_grid": {"confirm_offset": [0, 1], "surge_mult": [1.0, 1.33], "failed_breakout_bars": [0, 3]}}
    DARVAS_BT_CFG.write_text(json.dumps(cfg), encoding="utf-8")
    tmp = DARVAS_BT_FILE.with_suffix(".tmp.json")

    def done(code: int, _lines) -> None:
        if code == 0 and tmp.exists():
            data = json.loads(tmp.read_text(encoding="utf-8"))
            data["params"] = body.model_dump()
            DARVAS_BT_FILE.write_text(json.dumps(data), encoding="utf-8")
        tmp.unlink(missing_ok=True)

    uni = _top500() if body.universe == "top500" else body.universe
    bt_runner.launch(["--config", str(DARVAS_BT_CFG), "backtest", "--universe", uni, "--out", str(tmp)],
                     d["api_key"], d["access_token"], body.model_dump(), "Starting", on_exit=done)
    return bt_runner.status()


@app.post("/api/backtest/darvas/stop")
def backtest_darvas_stop():
    bt_runner.stop()
    return bt_runner.status()


