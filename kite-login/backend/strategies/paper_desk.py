"""Paper desks: every strategy paper-trades its own calls on its own ₹1 lakh.

Paper only: nothing in this module places, modifies or cancels an order.

The intraday desk (ORB) records each session's trades exactly as the strategy's live engine simulates them
on real candles. The Darvas desk keeps a position book across days:
  Darvas  a BUY breakout from the scan becomes a stop-limit order (trigger / limit / stop from the scan), valid for
          3 sessions; once filled, the stop is raised each morning to the bottom of the latest completed box, and
          the trade exits when the stop is hit (trail only, no target).
It sizes at 1% of the desk's equity at risk, at most 20% of equity in one stock and at most 5 open positions.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("paper_desk")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"
ALGO_DIR = Path(__file__).resolve().parents[3]   # holds darvas_algo.py and reports/
REPORTS_DIR = ALGO_DIR / "reports"
CAPITAL = 100000.0
RISK_PCT = 0.01
MAX_POS_PCT = 0.20
MAX_OPEN = 5
DESKS = {"orb": "ORB", "darvas": "Darvas Box"}
COST_SIDE = {"darvas": 0.00119}   # delivery charges per side, as in the backtest
DARVAS_VALID = 3
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
        b = {"capital": CAPITAL, "started": datetime.now(IST).date().isoformat()}
    for key, empty in (("sessions", {}), ("open", []), ("closed", []), ("pending", []), ("seen", []), ("curve", {}), ("notes", [])):
        b.setdefault(key, empty)
    return b


def reset(k: str) -> dict:
    with _lock:
        _file(k).unlink(missing_ok=True)
        b = _book(k)
        _write(k, b)
    return view(k)


# ---------------- intraday desks: one record per session, rewritten while the session is live ----------------
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


def _orb_sessions() -> dict:
    """ORB keeps its own per-session ledger (paper_ledger.json, sized on its ₹1 lakh paper capital)."""
    try:
        led = json.loads((CACHE_DIR / "paper_ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for s, d in led.items():
        out[s] = {"trades": [{"symbol": t["symbol"], "side": "LONG", "entry_time": t.get("entry_time"), "entry": t.get("entry_ref"),
                              "stop": t.get("sl"), "target": t.get("target"), "qty": t.get("qty"), "exit": t.get("exit_ref"),
                              "exit_time": t.get("exit_time"), "exit_reason": t.get("exit_reason"), "status": t["status"],
                              "ltp": t.get("ltp"), "r": round((t["exit_ref"] - t["entry_ref"]) / t["R"], 2) if t.get("R") and t.get("exit_ref") else t.get("r_now"),
                              "costs": t.get("costs"), "net": t.get("net") or 0.0} for t in d.get("trades", [])],
                  "note": "" if d.get("trades") else "no ORB setup qualified"}
    return out


# ---------------- swing desks ----------------
def _darvas_calls() -> list[dict]:
    files = sorted(REPORTS_DIR.glob("breakouts_*.csv"))
    if not files:
        return []
    out = []
    try:
        with files[-1].open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("signal") != "BUY" or r.get("filters") != "PASS" or not r.get("entry") or not r.get("stop"):
                    continue
                out.append({"symbol": r["symbol"], "signal_date": r["breakout_date"], "trigger": float(r["entry"]),
                            "limit": float(r.get("max_entry") or r["entry"]), "stop": float(r["stop"]),
                            "box_top": float(r["box_top"]), "token": int(float(r["nse_token"])) if r.get("nse_token") else None})
    except (OSError, ValueError) as exc:
        log.warning("darvas calls: %s", exc)
    return out


def _equity(b: dict, marks: dict[str, float] | None = None) -> tuple[float, float, float]:
    """(realized, unrealized, invested) of a swing book."""
    realized = sum(t["net"] for t in b["closed"])
    unreal, invested = 0.0, 0.0
    for p in b["open"]:
        px = (marks or {}).get(p["symbol"], p.get("ltp") or p["entry"])
        unreal += (px - p["entry"]) * p["qty"] - p["entry"] * p["qty"] * COST_SIDE.get(b.get("kind", ""), 0.0012) * 2
        invested += p["entry"] * p["qty"]
    return realized, unreal, invested


def _size(b: dict, entry: float, stop: float) -> int:
    realized, unreal, invested = _equity(b)
    eq = b["capital"] + realized
    R = entry - stop
    if R <= 0 or entry <= 0:
        return 0
    cash = eq - invested
    return max(0, int(min(eq * RISK_PCT / R, eq * MAX_POS_PCT / entry, cash / entry)))


def _close(b: dict, p: dict, px: float, day: str, why: str, k: str) -> None:
    c = COST_SIDE[k]
    costs = round((p["entry"] + px) * p["qty"] * c, 2)
    gross = (px - p["entry"]) * p["qty"]
    b["open"].remove(p)
    b["closed"].append({**p, "exit": round(px, 2), "exit_date": day, "exit_reason": why, "status": "closed",
                        "costs": costs, "net": round(gross - costs, 2), "r": round((px - p["entry"]) / p["R0"], 2) if p["R0"] else None})


def _trail_stop(kite, token: int, upto: date) -> float | None:
    """Bottom of the latest completed Darvas box (the quant rules' trailing stop), from daily candles."""
    try:
        if str(ALGO_DIR) not in sys.path:
            sys.path.append(str(ALGO_DIR))
        import darvas_algo as da
        import numpy as np
        rows = kite.historical_data(token, upto - timedelta(days=400), upto, "day")
        if len(rows) < 30:
            return None
        h = np.array([float(r["high"]) for r in rows])
        lo = np.array([float(r["low"]) for r in rows])
        c = np.array([float(r["close"]) for r in rows])
        res = da.detect_boxes(h, lo, c)
        v = res.trail_bottom[-1]
        return None if np.isnan(v) else float(v)
    except Exception as exc:  # noqa: BLE001 - a failed trail keeps the current stop
        log.warning("darvas trail %s: %s", token, exc)
        return None


def tick_swing(kite, now: datetime) -> None:
    """Runs every minute in market hours (and once after the close): fills, stops, targets, time exits."""
    if now.weekday() >= 5:
        return
    hm = now.strftime("%H:%M")
    if not ("09:16" <= hm <= "15:40"):
        return
    today = now.date().isoformat()
    for k in ("darvas",):
        with _lock:    # held for the whole tick: a reset or another writer cannot be overwritten by a stale copy
            _tick_desk(kite, now, k, today, hm)


def _tick_desk(kite, now: datetime, k: str, today: str, hm: str) -> None:
    b = _book(k)
    b["kind"] = k
    syms = sorted({p["symbol"] for p in b["open"]} | {o["symbol"] for o in b["pending"]})
    newcalls = [c for c in _darvas_calls() if f"{c['symbol']}:{c['signal_date']}" not in b["seen"]
                and c["signal_date"] <= today and c["signal_date"] >= (date.fromisoformat(today) - timedelta(days=6)).isoformat()]
    syms = sorted(set(syms) | {c["symbol"] for c in newcalls})
    q = {}
    if syms:
        try:
            q = kite.quote(*[f"NSE:{s}" for s in syms][:400])
        except Exception as exc:  # noqa: BLE001
            log.warning("desk %s quotes: %s", k, exc)
            return
    qt = {key.split(":", 1)[1]: v for key, v in q.items()}

    # new calls
    for c in newcalls:
        b["seen"].append(f"{c['symbol']}:{c['signal_date']}")
        x = qt.get(c["symbol"])
        # placed at the first tick of the day it stands in for an order resting from the open; placed later in the
        # session it may only fill on prices seen after it existed (the day's earlier high would be look-ahead)
        late = hm > "09:16"
        b["pending"].append({**c, "placed": today, "placed_at": hm, "valid_days": 0, "last_day": today,
                             "hi_since": float(x["last_price"]) if (late and x) else None})

    # Darvas pending stop-limit orders
    if k == "darvas":
        for o in list(b["pending"]):
            x = qt.get(o["symbol"])
            if not x:
                continue
            if o["last_day"] != today:
                o["valid_days"] += 1
                o["last_day"] = today
            if o["valid_days"] >= DARVAS_VALID:
                b["pending"].remove(o)
                b["notes"].append(f"{today}: {o['symbol']} order expired unfilled")
                continue
            ltp, hi = float(x["last_price"]), float(x["ohlc"]["high"])
            op = float(x["ohlc"]["open"] or ltp)
            if o.get("placed") == today and o.get("hi_since") is not None:
                o["hi_since"] = max(o["hi_since"], ltp)
                hi, op = o["hi_since"], ltp   # only prices since the order was placed; no opening gap applies
            if hi >= o["trigger"]:
                px = max(op, o["trigger"]) if op <= o["limit"] else None
                if px is None or px > o["limit"]:
                    if ltp <= o["limit"] and ltp >= o["trigger"]:
                        px = ltp
                    else:
                        continue    # gapped above the limit: the order waits (it is not chased)
                if len(b["open"]) >= MAX_OPEN:
                    b["pending"].remove(o)
                    b["notes"].append(f"{today}: {o['symbol']} skipped, {MAX_OPEN} positions already open")
                    continue
                qty = _size(b, px, o["stop"])
                b["pending"].remove(o)
                if qty <= 0:
                    b["notes"].append(f"{today}: {o['symbol']} skipped, no cash or risk too small")
                    continue
                b["open"].append({"symbol": o["symbol"], "signal_date": o["signal_date"], "entry_date": today,
                                  "entry_time": now.strftime("%H:%M"), "entry": round(px, 2), "stop": o["stop"],
                                  "R0": round(px - o["stop"], 2), "target": None, "qty": qty, "days": 1, "last_day": today,
                                  "ltp": ltp, "token": o.get("token"), "trailed_on": today})
            elif hm >= "15:25" and ltp < o["box_top"]:
                b["pending"].remove(o)
                b["notes"].append(f"{today}: {o['symbol']} order cancelled, closed back inside the box")

    # open positions: stop / target / time exit
    for p in list(b["open"]):
        x = qt.get(p["symbol"])
        if not x:
            continue
        if p["last_day"] != today:
            p["days"] += 1
            p["last_day"] = today
        if k == "darvas" and p.get("token") and p.get("trailed_on") != today:
            ts = _trail_stop(kite, p["token"], date.fromisoformat(today) - timedelta(days=1))
            p["trailed_on"] = today
            if ts and ts > p["stop"] and ts < float(x["last_price"]):
                b["notes"].append(f"{today}: {p['symbol']} stop raised {p['stop']} to {round(ts, 2)} (new box)")
                p["stop"] = round(ts, 2)
        ltp, op = float(x["last_price"]), float(x["ohlc"]["open"] or x["last_price"])
        hi, lo = float(x["ohlc"]["high"] or ltp), float(x["ohlc"]["low"] or ltp)
        p["ltp"] = ltp
        if p["entry_date"] == today and k == "darvas":
            lo = min(ltp, lo) if p["entry_time"] == "09:15" else ltp   # a fill today: only prices after it count
        if lo <= p["stop"]:
            _close(b, p, min(op, p["stop"]) if op < p["stop"] else p["stop"], today, "stop", k)
        elif p.get("target") and hi >= p["target"]:
            _close(b, p, max(op, p["target"]), today, "target", k)

    realized, unreal, invested = _equity(b)
    b["curve"][today] = round(b["capital"] + realized + unreal, 2)
    b["notes"] = b["notes"][-60:]
    b["seen"] = b["seen"][-500:]
    b["checked"] = now.isoformat(timespec="seconds")
    _write(k, b)


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
    if k == "orb":
        sessions = {s: v for s, v in _orb_sessions().items() if s >= "2026-10-30"}
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
        return {"id": k, "name": DESKS[k], "kind": "intraday", "capital": cap, "equity": round(cap + realized + unreal, 2),
                "realized": round(realized, 2), "unrealized": round(unreal, 2), "invested": None,
                "open": opened[::-1], "closed": closed[::-1][:300], "pending": [], "days": days[::-1],
                "curve": [["start", cap], *curve], "stats": _stats(closed, cap, [["start", cap], *curve]),
                "started": min(sessions) if sessions else b["started"], "notes": [], "checked": None,
                "today": round(sum(t["net"] for t in (sessions.get(today) or {}).get("trades", [])), 2)}
    realized, unreal, invested = _equity({**b, "kind": k})
    curve = [["start", cap], *sorted(b["curve"].items())]
    prev = ([eq for d, eq in curve[1:] if d < today] or [cap])[-1]   # equity marked at the last close before today
    return {"id": k, "name": DESKS[k], "kind": "swing", "capital": cap, "equity": round(cap + realized + unreal, 2),
            "realized": round(realized, 2), "unrealized": round(unreal, 2), "invested": round(invested, 2),
            "cash": round(cap + realized - invested, 2), "open": b["open"], "closed": b["closed"][::-1][:300],
            "pending": b["pending"], "days": [], "curve": curve, "stats": _stats(b["closed"], cap, curve),
            "started": b["started"], "notes": b["notes"][::-1][:20], "checked": b.get("checked"),
            "today": round(cap + realized + unreal - prev, 2)}


def portfolio() -> dict:
    """Every desk in full, for the combined Portfolio & Risk page: ₹1 lakh per strategy."""
    out = []
    for k in DESKS:
        try:
            out.append(view(k))
        except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
            log.exception("desk %s", k)
            out.append({"id": k, "name": DESKS[k], "error": str(exc)})
    return {"capital": CAPITAL * len(DESKS), "desks": out}


def overview() -> list[dict]:
    out = []
    for k in DESKS:
        try:
            v = view(k)
        except Exception as exc:  # noqa: BLE001 - one broken book must not hide the others
            log.exception("desk %s", k)
            out.append({"id": k, "name": DESKS[k], "error": str(exc)})
            continue
        out.append({x: v.get(x) for x in ("id", "name", "kind", "capital", "equity", "realized", "unrealized", "today", "stats", "started")}
                   | {"open_n": len(v["open"]), "pending_n": len(v["pending"]), "spark": [c[1] for c in v["curve"]][-60:]})
    return out
