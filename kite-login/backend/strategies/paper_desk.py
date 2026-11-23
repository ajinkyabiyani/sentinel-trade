"""Paper desks: every strategy paper-trades its own calls on its own ₹1 lakh. Each desk records a session's trades exactly as
the strategy's live engine simulates them on real candles.

Paper only: nothing in this module places, modifies or cancels an order.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("paper_desk")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"
CAPITAL = 100000.0
CAPITALS = {"pipeline": 500000.0}      # Agent Pipeline: ₹3 lakh cash and ₹2 lakh options
DESKS: dict = {}                 # the Agent Pipeline, its only desk, was removed on 19 Nov 2026; its records stay on disk
_lock = threading.Lock()


def _file(k: str) -> Path:
    return CACHE_DIR / f"desk_{k}.json"


def _read(k: str) -> dict:
    try:
        return json.loads(_file(k).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(k: str, d: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = _file(k).with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(_file(k))


def _book(k: str) -> dict:
    b = _read(k)
    if not b:
        b = {"capital": CAPITALS.get(k, CAPITAL), "started": datetime.now(IST).date().isoformat()}
    b.setdefault("sessions", {})
    return b


def reset(k: str) -> dict:
    with _lock:
        _file(k).unlink(missing_ok=True)
        b = _book(k)
        _write(k, b)
    return view(k)


# ---------------- one record per session, rewritten while the session is live ----------------
def _put_session(k: str, session: str, trades: list[dict], note: str = "") -> None:
    with _lock:
        b = _book(k)
        if session < b["started"] and session not in b["sessions"]:
            return                     # sessions before the desk opened are not counted
        # a closed trade is final: a later replay (e.g. after a restart, on freshly reloaded candles) never erases it
        key = lambda t: (t["symbol"], t.get("side"), t.get("entry_time"))  # noqa: E731
        new = {key(t) for t in trades}
        old = (b["sessions"].get(session) or {}).get("trades") or []
        trades = trades + [t for t in old if t["status"] == "closed" and key(t) not in new]
        b["sessions"][session] = {"trades": trades, "note": note, "updated": datetime.now(IST).isoformat(timespec="seconds")}
        _write(k, b)


on_recorded = None           # set by notify.start(): called right after any desk records its trades


def recorded() -> None:
    """A desk has just saved trades: let Telegram send any new call now. Never raises, so it can't stop a desk."""
    if on_recorded:
        try:
            on_recorded()
        except Exception:  # noqa: BLE001
            log.exception("on_recorded")


def put_session(k: str, session: str, trades: list[dict], note: str = "") -> None:
    """Records a desk's session; rewritten while the session is live."""
    _put_session(k, session, trades, note)
    recorded()


def session_trades(k: str, session: str) -> list[dict]:
    return (_book(k)["sessions"].get(session) or {}).get("trades") or []


def desk_capital(k: str) -> float:
    return float(_book(k)["capital"])


def has_session(k: str, session: str) -> bool:
    return session in _book(k)["sessions"]


def open_sessions(k: str, before: str) -> list[str]:
    """Earlier sessions still holding open trades, for example when the app was off at the close."""
    return [s for s, v in sorted(_book(k)["sessions"].items()) if s < before and any(t["status"] != "closed" for t in v["trades"])]


# ---------------- views ----------------
def _stats(closed: list[dict], capital: float, curve: list) -> dict:
    nets = [t["net"] for t in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    peak, dd = capital, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak else 0.0)
    rs = [t["r"] for t in closed if t.get("r") is not None]
    return {"trades": len(nets), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(nets) * 100, 1) if nets else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "net": round(sum(nets), 2),
            "costs": round(sum(t.get("costs") or 0 for t in closed), 2), "max_dd_pct": round(dd * 100, 2),
            "best": max(nets, default=None), "worst": min(nets, default=None)}


def view(k: str) -> dict:
    if k not in DESKS:
        raise KeyError(k)
    b = _book(k)
    cap = b["capital"]
    today = datetime.now(IST).date().isoformat()
    sessions = b["sessions"]
    eq, curve, closed, opened, days = cap, [], [], [], []
    for s in sorted(sessions):
        tr = sessions[s]["trades"]
        day_net = round(sum(t["net"] for t in tr), 2)
        eq += day_net
        curve.append([s, round(eq, 2)])
        closed += [{**t, "session": s} for t in tr if t["status"] == "closed"]
        opened += [{**t, "session": s} for t in tr if t["status"] != "closed"]
        days.append({"session": s, "trades": len(tr), "net": day_net, "note": sessions[s].get("note", ""),
                     "wins": sum(t["net"] > 0 for t in tr if t["status"] == "closed")})
    realized = sum(t["net"] for t in closed)
    unreal = sum(t["net"] for t in opened)
    return {"id": k, "name": DESKS[k], "capital": cap, "equity": round(cap + realized + unreal, 2),
            "realized": round(realized, 2), "unrealized": round(unreal, 2),
            "open": opened[::-1], "closed": closed[::-1][:300], "days": days[::-1],
            "curve": [["start", cap], *curve], "stats": _stats(closed, cap, [["start", cap], *curve]),
            "started": min(sessions) if sessions else b["started"],
            "today": round(sum(t["net"] for t in (sessions.get(today) or {}).get("trades", [])), 2)}


def portfolio() -> dict:
    """Every desk in full, for the combined Portfolio & Risk page: ₹1 lakh per strategy."""
    out = []
    for k in DESKS:
        try:
            out.append(view(k))
        except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
            log.exception("desk %s", k)
            out.append({"id": k, "name": DESKS[k], "error": str(exc)})
    return {"capital": sum(CAPITALS.get(k, CAPITAL) for k in DESKS), "desks": out}


def overview() -> list[dict]:
    out = []
    for k in DESKS:
        try:
            v = view(k)
        except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
            log.exception("desk %s", k)
            out.append({"id": k, "name": DESKS[k], "error": str(exc)})
            continue
        out.append({x: v.get(x) for x in ("id", "name", "capital", "equity", "realized", "unrealized", "today", "stats", "started")}
                   | {"open_n": len(v["open"]), "spark": [c[1] for c in v["curve"]][-60:]})
    return out
