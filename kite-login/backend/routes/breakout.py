"""The F&O stock lists the desks scan (the day's movers, every F&O name). The Agent Pipeline that lived here was removed on 19 Nov 2026."""
from __future__ import annotations

from datetime import datetime

from kiteconnect import KiteConnect

from core import _kite_call, log, store
from routes.market import _instrument_list
from strategies import agent_desk, common

LOOP_SECONDS = 10
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def fno_stocks() -> list[str]:
    """The F&O stocks up most today, for the Agent Pipeline's Scanner: Kite's list, then one quote call for all of them. The Nifty 50 if Kite can't answer."""
    d = store.load()
    if not d:
        return agent_desk.nifty50()
    try:
        kite = KiteConnect(api_key=d["api_key"])
        kite.set_access_token(d["access_token"])
        names = sorted({r["name"] for r in _instrument_list(kite, "NFO") if r.get("instrument_type") == "FUT" and r.get("name") and r["name"] not in INDEX_NAMES})
        if not names:
            return agent_desk.nifty50()
        quotes = {}
        keys = [f"NSE:{n}" for n in names]
        for i in range(0, len(keys), 450):
            quotes.update(_kite_call(kite.ohlc, *keys[i:i + 450]))
        return agent_desk.movers(quotes)
    except Exception:  # noqa: BLE001 - the desk still runs on the fallback list
        log.exception("agent desk F&O list failed")
        return agent_desk.nifty50()


_names: dict = {}


def fno_names() -> list[str]:
    """Every F&O stock's name from Kite's instrument list, once a day. Empty if Kite can't answer."""
    today = datetime.now(common.IST).date().isoformat()
    if _names.get("day") == today and _names.get("names"):
        return _names["names"]
    d = store.load()
    if not d:
        return []
    try:
        kite = KiteConnect(api_key=d["api_key"])
        kite.set_access_token(d["access_token"])
        names = sorted({r["name"] for r in _instrument_list(kite, "NFO") if r.get("instrument_type") == "FUT" and r.get("name") and r["name"] not in INDEX_NAMES})
    except Exception:  # noqa: BLE001
        log.exception("F&O names failed")
        return _names.get("names") or []
    _names.update(day=today, names=names)
    return names
