"""Smart alerts: price and day-change alerts on live quotes, plus system alerts."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
import json
import threading

from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from core import IST, _kite_call, app, kite_client, log, store
from routes.paper import _close_paper, _paper_book
from strategies import orb


# ---------------------------------------------------------------------------
# Smart alerts: price / day-change alerts checked every 10 s on live quotes, plus system alerts (paper stop and
# target hits). In-app only: nothing is sent anywhere and nothing is ordered.
# ---------------------------------------------------------------------------
ALERTS_FILE = orb.CACHE_DIR / "alerts.json"
_alerts_lock = threading.Lock()
ALERT_TEXT = {"above": "crosses above", "below": "crosses below", "chg_above": "day change above", "chg_below": "day change below"}


def _alerts() -> dict:
    try:
        return json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alerts": []}


def _save_alerts(d: dict) -> None:
    orb.CACHE_DIR.mkdir(exist_ok=True)
    tmp = ALERTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(ALERTS_FILE)


def _system_alert(title: str, body: str, symbol: str | None = None) -> None:
    now = datetime.now(IST)
    with _alerts_lock:
        d = _alerts()
        d["alerts"].append({"id": now.strftime("%Y%m%d%H%M%S%f"), "kind": "system", "symbol": symbol, "title": title, "body": body,
                            "status": "triggered", "created": now.isoformat(timespec="seconds"),
                            "triggered_at": now.isoformat(timespec="seconds"), "read": False})
        _save_alerts(d)


class AlertBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    exchange: Literal["NSE", "BSE", "NFO", "BFO", "MCX", "CDS"] = "NSE"
    cond: Literal["above", "below", "chg_above", "chg_below"]
    value: float
    note: str | None = Field(None, max_length=120)


@app.get("/api/alerts")
def alerts_list():
    a = sorted(_alerts()["alerts"], key=lambda x: x.get("triggered_at") or x["created"], reverse=True)
    return {"alerts": a, "unread": sum(1 for x in a if x["status"] == "triggered" and not x.get("read"))}


@app.post("/api/alerts")
def alerts_create(body: AlertBody, kite: KiteConnect = Depends(kite_client)):
    key = f"{body.exchange}:{body.symbol.upper()}"
    q = _kite_call(kite.ltp, key).get(key)
    if not q:
        raise HTTPException(400, f"{key} not found")
    now = datetime.now(IST)
    a = {"id": now.strftime("%Y%m%d%H%M%S%f"), "kind": "price", "symbol": body.symbol.upper(), "exchange": body.exchange, "key": key,
         "cond": body.cond, "value": body.value, "note": body.note, "status": "active", "created": now.isoformat(timespec="seconds"),
         "ref_price": q["last_price"], "read": False,
         "title": f"{body.symbol.upper()} {ALERT_TEXT[body.cond]} {body.value:g}{'%' if body.cond.startswith('chg') else ''}"}
    with _alerts_lock:
        d = _alerts()
        d["alerts"].append(a)
        _save_alerts(d)
    return a


@app.delete("/api/alerts/{aid}")
def alerts_delete(aid: str):
    with _alerts_lock:
        d = _alerts()
        keep = [x for x in d["alerts"] if x["id"] != aid]
        if len(keep) == len(d["alerts"]):
            raise HTTPException(404, "No alert with that id")
        d["alerts"] = keep
        _save_alerts(d)
    return {"ok": True}


@app.post("/api/alerts/read")
def alerts_read(ids: list[str] | None = None):
    with _alerts_lock:
        d = _alerts()
        for x in d["alerts"]:
            if ids is None or x["id"] in ids:
                x["read"] = True
        _save_alerts(d)
    return {"ok": True}


def _check_alerts(kite: KiteConnect) -> None:
    with _alerts_lock:
        d = _alerts()
    active = [a for a in d["alerts"] if a["status"] == "active" and a.get("key")]
    book = _paper_book()
    eq = [p for p in book["positions"] if p["status"] == "open" and p.get("kind") == "equity"]
    keys = sorted({a["key"] for a in active} | {p["legs"][0]["key"] for p in eq})
    if not keys:
        return
    q = kite.quote(*keys[:450])
    now = datetime.now(IST)
    hits = []
    for a in active:
        x = q.get(a["key"]) or {}
        last, prev = x.get("last_price"), (x.get("ohlc") or {}).get("close")
        if not last:
            continue
        chg = (last / prev - 1) * 100 if prev else None
        ok = {"above": last >= a["value"], "below": last <= a["value"],
              "chg_above": chg is not None and chg >= a["value"], "chg_below": chg is not None and chg <= a["value"]}[a["cond"]]
        if ok:
            hits.append((a["id"], last, chg))
    if hits:
        with _alerts_lock:
            d = _alerts()
            for aid, last, chg in hits:
                for a in d["alerts"]:
                    if a["id"] == aid and a["status"] == "active":
                        a.update(status="triggered", triggered_at=now.isoformat(timespec="seconds"), price=last,
                                 body=f"{a['symbol']} at {last:,.2f}" + (f" ({chg:+.2f}% today)" if chg is not None else ""))
            _save_alerts(d)
    # paper equity: stop / target on the live price, MIS squared off at 15:20
    for p in eq:
        leg = p["legs"][0]
        last = (q.get(leg["key"]) or {}).get("last_price")
        if not last:
            continue
        long_ = leg["side"] == "BUY"
        reason = None
        if p.get("sl") and (last <= p["sl"] if long_ else last >= p["sl"]):
            reason = "stop"
        elif p.get("target") and (last >= p["target"] if long_ else last <= p["target"]):
            reason = "target"
        elif p.get("product") == "MIS" and now.strftime("%H:%M") >= "15:20" and now.date().isoformat() == p["opened"][:10]:
            reason = "15:20 square-off"
        elif p.get("product") == "MIS" and now.date().isoformat() > p["opened"][:10]:
            reason = "intraday square-off"
        if reason:
            try:
                closed = _close_paper(p["id"], kite, reason)
                _system_alert(f"Paper {closed['legs'][0]['tradingsymbol']}: {reason}", f"closed at {closed['legs'][0]['exit']:,.2f} · net ₹{closed['net']:+,.0f}",
                              closed["legs"][0]["tradingsymbol"])
            except HTTPException:
                pass


def _alerts_loop() -> None:
    import time as _t
    _t.sleep(12)
    while True:
        try:
            d = store.load()
            if d:
                kite = KiteConnect(api_key=d["api_key"])
                kite.set_access_token(d["access_token"])
                _check_alerts(kite)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert check failed: %s", exc)
        _t.sleep(10)


@app.on_event("startup")
def _start_alerts() -> None:
    threading.Thread(target=_alerts_loop, daemon=True, name="alerts").start()


