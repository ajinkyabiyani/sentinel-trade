"""Telegram controls, the end-of-day report and your shared paper risk settings. Paper only: nothing here can place an order, and no response carries a credential."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from fastapi import HTTPException
from pydantic import BaseModel, Field

from core import app, store
from strategies import common, eod_report, notify, risk_settings

log = logging.getLogger("kite-desk.telegram-route")
LOOP_SECONDS = 20


class Settings(BaseModel):
    capital: float = Field(gt=0)
    confirmed: bool


class Toggle(BaseModel):
    enabled: bool


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=3500)


def _loop() -> None:
    time.sleep(60)
    while True:
        try:
            if store.load():
                eod_report.tick(datetime.now(common.IST))
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("end-of-day report failed")
        time.sleep(LOOP_SECONDS)


@app.on_event("startup")
def _start() -> None:
    threading.Thread(target=_loop, daemon=True, name="eod-report").start()
    notify.start()


@app.get("/api/risk-settings")
def get_risk_settings():
    return {"settings": risk_settings.settings(), "limits": risk_settings.limits()}


@app.post("/api/risk-settings")
def save_risk_settings(body: Settings):
    try:
        risk_settings.save(body.capital, body.confirmed)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return get_risk_settings()


@app.get("/api/telegram/status")
def telegram_status():
    return notify.status()


@app.post("/api/telegram/enabled")
def telegram_enabled(body: Toggle):
    return notify.set_enabled(body.enabled)


@app.post("/api/telegram/test")
def telegram_test():
    if not notify.configured():
        raise HTTPException(400, "The bot token or chat id is missing from signal_bot/secrets.env.")
    notify.send("✅ Sentinel Trade: Telegram is connected. New calls from your desks will arrive here. No order is ever sent.", force=True)
    return notify.status()


@app.post("/api/telegram/signal")
def telegram_signal(body: dict):
    """A new call from a local skill, in the one layout. Only the layout's named fields are read."""
    return {"queued": notify.send(notify.signal_text(body))}


@app.post("/api/telegram/send")
def telegram_send(body: Text):
    """For the local skills, which never see the token."""
    return {"queued": notify.send(body.text)}
