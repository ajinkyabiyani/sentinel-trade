"""The live terminal for each strategy: one shape, built only from the strategy's real state.

Every field comes from what the strategy has actually done or is doing: its agents' last outputs, the agent at work right now,
the candidate moving through its checks, the scan list, the open trade, its activity log and its trades. Nothing is replayed or
made up. Outside market hours the terminal says it has stopped and when it starts again; nothing moves. Read only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from strategies import common, multi_agent_team as mat

india = mat.india
log = logging.getLogger("kite-desk.live-terminal")
OPEN, CLOSE = "09:15", "15:30"
TAIL, LOG = 14, 40


def _clip(text, n: int = 90) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def phase(now: datetime) -> dict:
    """Live during market hours on a trading day; otherwise stopped, with the next start."""
    d, t = now.date(), now.strftime("%H:%M")
    if india.trading_day(d) and OPEN <= t <= CLOSE:
        return {"live": True, "label": "running", "next": None}
    nxt = d if india.trading_day(d) and t < OPEN else d + timedelta(days=1)
    while not india.trading_day(nxt):
        nxt += timedelta(days=1)
    when = "today" if nxt == d else nxt.strftime("%a %d %b")
    return {"live": False, "label": "stopped · market closed", "next": f"starts {when} {OPEN}"}


def _book(trades_open: list[dict], trades_closed: list[dict], capital: float) -> dict:
    realised = sum(t.get("net") or 0 for t in trades_closed)
    marks = sum(t.get("net") or 0 for t in trades_open)
    won = sum(1 for t in trades_closed if (t.get("net") or 0) > 0)
    return {"balance": {"start": capital, "now": round(capital + realised + marks, 2)},
            "trades": {"total": len(trades_closed) + len(trades_open), "won": won, "lost": len(trades_closed) - won, "open": len(trades_open)}}


def _trade(t: dict | None, symbol_key: str = "symbol") -> dict | None:
    if not t:
        return None
    entry, now = t.get("entry"), t.get("ltp") if t.get("status") == "open" else t.get("exit")
    pnl_pct = round((now / entry - 1) * 100, 2) if entry and now else None
    return {"symbol": t.get(symbol_key), "entry": entry, "now": now, "stop": t.get("stop"), "target": t.get("target"), "qty": t.get("qty"), "status": t.get("status"),
            "pnl": t.get("net"), "pnl_pct": pnl_pct, "reason": t.get("exit_reason") or t.get("setup") or t.get("reason"), "at": t.get("entry_time") or t.get("entry_at")}


# ---------------------------------------------------------------- Agent Pipeline

STEP_BADGE = {"ok": ("pass", "PASS"), "no": ("fail", "STOP"), "info": ("done", "READ"), "note": ("done", "NOTE")}


TEAM_AGENTS = [("scanner", "Scanner", "Chart setups after every 5-minute candle"), ("technical", "Technical", "Claude reads the chart and the chain"),
               ("risk", "Risk", "Claude approves or rejects the idea"), ("trader", "Trader", "Claude picks BUY or HOLD, contract and levels"),
               ("guard", "Code checks", "VIX, strike, liquidity, loss limit"), ("executor", "Executor", "Paper fill at the live price"), ("watcher", "Watcher", "Follows stop, target and 15:15")]


def _last_day(folder, prefix: str, today: str, live: bool = False) -> str:
    """Today while the market is open (even before the first scan has written anything), or today when it has records;
    otherwise the last session with records, so a closed market shows how the last session ended."""
    days = sorted(p.stem[len(prefix):] for p in folder.glob(f"{prefix}*.json")) if folder.exists() else []
    return today if live or today in days or not days else days[-1]


def _waiting(now: datetime, first_scan: str) -> list[dict]:
    """The status line for an open market before the strategy's first scan of the day."""
    t = now.strftime("%H:%M")
    text = (f"Armed for today. The first scan runs after the {first_scan} candle closes, then after every 5-minute candle."
            if t < first_scan else "Waiting for the next 5-minute candle to close.")
    return [{"at": t, "text": text, "kind": "info"}]


def team_feed(now: datetime) -> dict:
    day = _last_day(mat.DIR, "day_", now.date().isoformat(), phase(now)["live"])
    runs = mat.runs(day)
    held = mat.positions(day)
    acts = mat.activity(day)
    lv = mat.live()
    stage = (lv or {}).get("stage") or ""
    working = None
    if lv:
        working = "guard" if stage.startswith("guard") else next((a for a in ("trader", "risk", "technical") if stage.startswith(a)), "technical")
        if stage.endswith("_done"):
            order = ["technical", "risk", "trader", "guard"]
            base = stage.replace("_done", "")
            working = order[min(order.index(base) + 1, 3)] if base in order else working
    last = runs[-1] if runs else None
    lastp = (last or {}).get("trade_payload") or {}

    def line(agent: str) -> tuple[str, str, str]:
        if working == agent:
            return "active", "WORKING", f"on {lv.get('underlying')}"
        if agent == "scanner":
            scan = next((a for a in reversed(acts) if a["kind"] == "scan"), None)
            return ("done", "SCAN", _clip(scan["text"], 70)) if scan else ("idle", "IDLE", "waiting for the first candle")
        if not last:
            return "idle", "IDLE", "nothing yet today"
        if agent == "technical":
            r = last.get("technical_report") or {}
            return ("done", r.get("trend_bias") or "READ", _clip(r.get("evidence") or r.get("summary") or "", 70)) if r else ("idle", "IDLE", "–")
        if agent == "risk":
            r = last.get("risk_review") or {}
            return ("pass" if r.get("approved") else "fail", "PASS" if r.get("approved") else "REJECT", _clip(r.get("rejection_flag") or r.get("reasoning") or "", 70)) if r else ("idle", "IDLE", "–")
        if agent == "trader":
            return ("pass" if lastp.get("action") == "BUY" else "done", lastp.get("action") or "–", _clip(lastp.get("rationale") or lastp.get("instrument") or "", 70))
        if agent == "guard":
            paper = last.get("paper") or {}
            return ("pass", "PASS", "taken") if paper.get("taken") else (("fail", "BLOCK", _clip(paper.get("why"), 70)) if paper.get("why") else ("idle", "IDLE", "–"))
        if agent == "executor":
            e = next((p for p in reversed(held)), None)
            return ("done", "FILLED", f"{e['instrument']} at {e['entry']}") if e else ("idle", "IDLE", "no fill today")
        opened = [p for p in held if p["status"] == "open"]
        return ("active" if opened and phase(now)["live"] else "done", "LIVE" if opened else "IDLE", f"{len(opened)} open" if opened else "nothing open")

    agents = [{"id": i, "label": n, "job": j, **dict(zip(("state", "badge", "line"), line(i)))} for i, n, j in TEAM_AGENTS]
    if lv:
        flow = {"symbol": lv.get("underlying"), "at": working, "verdict": "working", "why": None}
    elif last:
        taken = (last.get("paper") or {}).get("taken")
        at = "executor" if taken else ("guard" if lastp.get("action") == "BUY" else ("trader" if (last.get("risk_review") or {}).get("approved") else "risk"))
        flow = {"symbol": last.get("underlying"), "at": at, "verdict": "pass" if taken else "filtered", "why": _clip(last.get("error") or (last.get("paper") or {}).get("why") or lastp.get("rationale"), 120)}
    else:
        flow = None
    q = mat.queue(day)
    cands = [{"symbol": t["underlying"], "state": "queued", "note": f"{t['setup']}, {t['direction']}"} for t in q[:6]]
    cands += [{"symbol": r["underlying"], "state": "traded" if (r.get("paper") or {}).get("taken") else "filtered", "note": _clip((r.get("trade_payload") or {}).get("action") or r.get("error"), 40)} for r in runs[-8:]]
    log_rows = []
    for r in runs:
        p = r.get("trade_payload") or {}
        act = "ENTER" if (r.get("paper") or {}).get("taken") else ("ERROR" if r.get("error") else ("SKIP" if p.get("action") != "BUY" else "FILTERED"))
        log_rows.append({"at": r.get("at"), "action": act, "symbol": p.get("instrument") or r.get("underlying"), "reason": _clip(r.get("error") or (r.get("paper") or {}).get("why") or p.get("rationale"), 90), "value": None})
    log_rows += [{"at": e.get("exit_at"), "action": "EXIT", "symbol": e["instrument"], "reason": e.get("exit_reason"), "value": e.get("net")} for e in held if e["status"] == "closed"]
    log_rows.sort(key=lambda r: str(r.get("at") or ""))
    days = sorted(p.stem[4:] for p in mat.DIR.glob("day_*.json")) if mat.DIR.exists() else []
    every = [e for d in days for e in mat.positions(d)]
    cap = float((mat.risk_settings.settings() or {}).get("capital") or 100000)
    opened = [e for e in held if e["status"] == "open"]
    return {"id": "multi_agent_team", "name": "Multi Agent Team", "agents": agents, "stages": [a[0] for a in TEAM_AGENTS], "flow": flow, "candidates": cands,
            "trade": _trade(opened[-1] if opened else (held[-1] if held else None), "instrument"),
            "tail": [{"at": a["at"], "text": _clip(a["text"], 140), "kind": a["kind"]} for a in acts[-TAIL:]] or (_waiting(now, mat.FIRST_SCAN) if phase(now)["live"] else []),
            "log": log_rows[-LOG:][::-1], "now_doing": {"agent": working, "text": f"{working} on {lv.get('underlying')}"} if lv else None, "scan": None,
            "session": day, **_book([e for e in every if e["status"] == "open"], [e for e in every if e["status"] == "closed"], cap)}


# ---------------------------------------------------------------- Claude Skill Desk

FEEDS = {"multi_agent_team": team_feed}


def feed(strategy: str) -> dict:
    now = datetime.now(common.IST)
    out = FEEDS[strategy](now)
    ph = phase(now)
    if not ph["live"]:                         # after hours nothing is at work, whatever the last record says
        out["now_doing"] = None
        for a in out["agents"]:
            if a["state"] == "active":
                a.update(state="done", badge="DONE" if a["badge"] == "WORKING" else a["badge"])
        if out.get("flow") and out["flow"].get("verdict") == "working":
            out["flow"]["verdict"] = None
    return {**out, "phase": ph, "asof": now.isoformat(timespec="seconds"), "today": now.date().isoformat()}
