"""Every system's paper trades in one portfolio: the Agent Pipeline, the Multi Agent Team and the Claude Skill Desk. Read only, paper only."""
from __future__ import annotations

import logging

from strategies import multi_agent_team as team, notify, paper_desk, risk_settings

log = logging.getLogger("kite-desk.all-desks")
INDEPENDENT = {"claude_trader"}     # teams that stand alone: the shared gate neither counts their trades nor holds them back
SWING_CAPITAL = 200000.0
DAY_LOSS_PCT = 1.0            # of all systems' capital together: past this loss today, no system opens a new paper trade


def _trade(day: str, symbol: str, key: str, e: dict) -> dict:
    """One trade in the shape the portfolio page reads. An open trade's net is its mark at the last known price, before exit charges."""
    opened = e["status"] == "open"
    ltp = e.get("ltp") if e.get("ltp") is not None else e["entry"]
    way = -1 if e.get("side") == "SHORT" else 1
    return {"symbol": symbol, "key": key, "side": "SHORT" if way < 0 else "LONG", "session": day, "entry_time": e.get("entry_at"), "entry": e["entry"], "qty": e["qty"], "stop": e["stop"], "target": e["target"],
            "status": "open" if opened else "closed", "ltp": ltp if opened else None, "exit": e.get("exit"), "exit_time": e.get("exit_at"), "exit_reason": e.get("exit_reason"), "r": e.get("r"),
            "costs": e.get("costs"), "net": round((ltp - e["entry"]) * e["qty"] * way, 2) if opened else (e.get("net") or 0.0)}


def _view(desk_id: str, name: str, capital: float, trades: list[dict], today: str) -> dict:
    closed = [t for t in trades if t["status"] == "closed"]
    opened = [t for t in trades if t["status"] == "open"]
    eq, curve = capital, []
    for day in sorted({t["session"] for t in trades}):
        eq += sum(t["net"] for t in trades if t["session"] == day)
        curve.append([day, round(eq, 2)])
    curve = [["start", capital], *curve]
    realized, unreal = sum(t["net"] for t in closed), sum(t["net"] for t in opened)
    return {"id": desk_id, "name": name, "capital": capital, "equity": round(capital + realized + unreal, 2), "realized": round(realized, 2), "unrealized": round(unreal, 2),
            "open": opened[::-1], "closed": closed[::-1][:300], "curve": curve, "stats": paper_desk._stats(closed, capital, curve),
            "started": min((t["session"] for t in trades), default=today), "today": round(sum(t["net"] for t in trades if t["session"] == today), 2)}


def team_trades() -> list[dict]:
    days = sorted(p.stem[4:] for p in team.DIR.glob("day_*.json")) if team.DIR.exists() else []
    return [_trade(day, e["instrument"], e["key"], e) for day in days for e in team.positions(day)]


def portfolio() -> dict:
    out = paper_desk.portfolio()
    capital = float((risk_settings.settings() or {}).get("capital") or paper_desk.CAPITAL)
    today = paper_desk.datetime.now(paper_desk.IST).date().isoformat()
    try:
        from strategies import claude_trader
        out["desks"].append(_view(claude_trader.DESK_ID, claude_trader.DESK_NAME, claude_trader.CAPITAL, claude_trader.trades(), today))
        out["capital"] += claude_trader.CAPITAL
    except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
        log.exception("desk claude_trader")
        out["desks"].append({"id": "claude_trader", "name": "Claude Trader", "error": str(exc)})
    for desk_id, name, trades in (("multi_agent_team", team.DESK_NAME, team_trades),):
        try:
            out["desks"].append(_view(desk_id, name, capital, trades(), today))
            out["capital"] += capital
        except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
            log.exception("desk %s", desk_id)
            out["desks"].append({"id": desk_id, "name": name, "error": str(exc)})
    return out


# ---------------------------------------------------------------- one risk gate for every system

def underlying_of(symbol: str) -> str:
    """NIFTY26N1725400CE -> NIFTY, PNB26NOV120CE -> PNB, RELIANCE -> RELIANCE."""
    m = notify.CONTRACT.match(str(symbol))
    return m[1] if m else str(symbol)


def kind_of(symbol: str) -> str:
    return "option" if notify.CONTRACT.match(str(symbol)) else "shares"


def is_option(symbol: str) -> bool:
    return bool(notify.CONTRACT.match(str(symbol)))


def gate(symbol: str, kind: str | None = None) -> str | None:
    """Why no system may open a new paper trade on this underlying now, or None. Getting out is never blocked.
    One option trade and one share trade per underlying across every system: a trade is blocked only by another of its own kind.
    kind is "option" or "shares"; without it, a contract symbol says its own kind and a plain name is checked against every open trade."""
    underlying = underlying_of(symbol)
    if kind is None and kind_of(symbol) != "shares":
        kind = kind_of(symbol)
    try:
        p = portfolio()
    except Exception:  # noqa: BLE001 - a gate that can't read the books must not stop the desks
        log.exception("risk gate")
        return None
    desks = [d for d in p["desks"] if not d.get("error") and d.get("id") not in INDEPENDENT]
    limit = (p["capital"] - sum(d.get("capital") or 0 for d in p["desks"] if d.get("id") in INDEPENDENT)) * DAY_LOSS_PCT / 100.0
    today = sum(d.get("today") or 0 for d in desks)
    if today <= -limit:
        return f"all systems together are down ₹{-today:,.0f} today, past the shared limit of ₹{limit:,.0f}"
    for d in desks:
        same = [t for t in d["open"] if underlying_of(t["symbol"]) == underlying and (kind is None or kind_of(t["symbol"]) == kind)]
        if same:
            what = {"option": "an open paper option trade", "shares": "an open paper share trade"}.get(kind, "an open paper trade")
            return f"{d['name']} already has {what} on {underlying}"
    return None
