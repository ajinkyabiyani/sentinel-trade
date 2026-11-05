"""Live ticks: one KiteTicker connection to Kite, fanned out to browsers as server-sent events."""
from __future__ import annotations

import asyncio
import json
import os
import threading

from fastapi import HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from kiteconnect import KiteConnect, KiteTicker

from core import app, kite_client, log, store
from routes.market import EXCHANGES, _instrument_list


# ---------------------------------------------------------------------------
# Live ticks: one KiteTicker WebSocket to Kite, fanned out to browsers as server-sent events
# ---------------------------------------------------------------------------
def _slim_tick(t: dict) -> dict:
    ohlc = t.get("ohlc") or {}
    depth = t.get("depth") or {}
    bid = next((x["price"] for x in depth.get("buy", []) if x.get("price")), None)
    ask = next((x["price"] for x in depth.get("sell", []) if x.get("price")), None)
    last, prev = t.get("last_price"), ohlc.get("close")
    ts = t.get("exchange_timestamp") or t.get("last_trade_time")
    return {"last_price": last, "ohlc": ohlc, "volume": t.get("volume_traded"), "bid": bid, "ask": ask, "oi": t.get("oi"),
            "change": round(last - prev, 4) if last and prev else None,
            "change_pct": round((last / prev - 1) * 100, 2) if last and prev else None,
            "time": ts.isoformat() if hasattr(ts, "isoformat") else None}


class TickHub:
    """Keeps a single KiteTicker connection (threaded Twisted reactor) and the latest tick per instrument.
    Browser streams subscribe by instrument token; reference counts unsubscribe tokens nobody watches."""

    def __init__(self):
        self._lock = threading.Lock()
        self.kt: KiteTicker | None = None
        self.creds: tuple[str, str] | None = None
        self.connected = False
        self.error = ""
        self.ticks: dict[int, dict] = {}
        self.refs: dict[int, int] = {}
        self.seq = 0

    def _on_connect(self, ws, _resp) -> None:
        self.connected, self.error = True, ""
        tokens = [t for t, n in self.refs.items() if n > 0]
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

    def _on_ticks(self, _ws, ticks) -> None:
        with self._lock:
            for t in ticks:
                self.seq += 1
                self.ticks[t["instrument_token"]] = {**_slim_tick(t), "seq": self.seq}

    def _on_close(self, _ws, code, reason) -> None:
        self.connected = False
        self.error = f"closed ({code}) {reason or ''}".strip()

    def _on_error(self, _ws, code, reason) -> None:
        self.error = f"error ({code}) {reason or ''}".strip()

    def _ensure(self, d: dict) -> None:
        creds = (d["api_key"], d["access_token"])
        if self.kt is not None and self.creds == creds:
            return
        if self.kt is not None:
            try:
                self.kt.close()
            except Exception:
                pass
        kt = KiteTicker(d["api_key"], d["access_token"], reconnect=True, reconnect_max_tries=300)
        kt.on_connect, kt.on_ticks, kt.on_close, kt.on_error = self._on_connect, self._on_ticks, self._on_close, self._on_error
        self.kt, self.creds, self.connected = kt, creds, False
        kt.connect(threaded=True)

    def subscribe(self, d: dict, tokens: list[int]) -> None:
        with self._lock:
            self._ensure(d)
            new = [t for t in tokens if self.refs.get(t, 0) == 0]
            for t in tokens:
                self.refs[t] = self.refs.get(t, 0) + 1
        if new and self.connected and self.kt:
            self.kt.subscribe(new)
            self.kt.set_mode(self.kt.MODE_FULL, new)

    def release(self, tokens: list[int]) -> None:
        gone = []
        with self._lock:
            for t in tokens:
                self.refs[t] = self.refs.get(t, 1) - 1
                if self.refs[t] <= 0:
                    gone.append(t)
                    self.refs.pop(t, None)
        if gone and self.connected and self.kt:
            try:
                self.kt.unsubscribe(gone)
            except Exception:
                pass

    def since(self, tokmap: dict[int, str], seq: int) -> tuple[dict, int]:
        with self._lock:
            out = {key: tick for tok, key in tokmap.items() if (tick := self.ticks.get(tok)) and tick["seq"] > seq}
            return out, self.seq


hub = TickHub()


@app.on_event("shutdown")
def _stop_ticker() -> None:
    """Close the Kite WebSocket and its Twisted reactor so `--reload` can replace this process promptly."""
    try:
        if hub.kt is not None:
            hub.kt.close()
        from twisted.internet import reactor
        if reactor.running:
            reactor.callFromThread(reactor.stop)
    except Exception:
        log.exception("ticker shutdown failed")
    # The ticker's reactor thread can keep the worker alive after shutdown, which stalls `--reload` on Windows:
    # make sure the old worker is gone so the reloader can start the new one.
    threading.Timer(2.0, lambda: os._exit(0)).start()


def _resolve_keys(kite: KiteConnect, keys: list[str]) -> dict[int, str]:
    """EXCHANGE:TRADINGSYMBOL -> instrument_token using the day's cached instrument dumps."""
    out = {}
    for exch in sorted({k.split(":", 1)[0] for k in keys if ":" in k}):
        if exch not in EXCHANGES:
            continue
        wanted = {k.split(":", 1)[1] for k in keys if k.startswith(exch + ":")}
        for r in _instrument_list(kite, exch):
            if r["tradingsymbol"] in wanted:
                out[int(r["instrument_token"])] = f"{exch}:{r['tradingsymbol']}"
    return out


@app.get("/api/stream")
async def stream(request: Request, i: list[str] = Query(max_length=600)):
    """Server-sent events: {"ticks": {KEY: tick}, "connected": bool}, pushed as Kite ticks arrive (<= 4/s).
    Up to 600 instruments per stream (one KiteTicker connection carries up to 3000)."""
    d = store.load()
    if not d:
        raise HTTPException(401, "Not logged in to Kite")
    tokmap = await run_in_threadpool(_resolve_keys, kite_client(), i[:600])
    await run_in_threadpool(hub.subscribe, d, list(tokmap))

    async def events():
        seq, idle = 0, 0
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                out, seq_now = hub.since(tokmap, seq)
                if out or idle >= 40:
                    seq, idle = seq_now, 0
                    yield f"data: {json.dumps({'ticks': out, 'connected': hub.connected, 'error': hub.error})}\n\n"
                else:
                    idle += 1
                await asyncio.sleep(0.25)
        finally:
            hub.release(list(tokmap))

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stream/status")
def stream_status():
    return {"connected": hub.connected, "error": hub.error, "instruments": len(hub.refs)}


