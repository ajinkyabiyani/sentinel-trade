"""Mounts the market terminal (see the terminal package)."""
from __future__ import annotations

from kiteconnect import KiteConnect

from core import _kite_call, app, kite_client, store
from routes.market import _instrument_list
from terminal.routes import create_router as _terminal_router


# ---------------------------------------------------------------------------
# Market terminal (home, calendar, fundamentals, heatmap, insiders, scanner): see terminal/
# ---------------------------------------------------------------------------


def _session_kite() -> KiteConnect | None:
    d = store.load()
    if not d:
        return None
    kite = KiteConnect(api_key=d["api_key"])
    kite.set_access_token(d["access_token"])
    return kite


app.include_router(_terminal_router(kite_client, _instrument_list, _kite_call, _session_kite))
