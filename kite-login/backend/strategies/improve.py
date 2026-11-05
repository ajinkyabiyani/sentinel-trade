"""Strategy improvement loop: schedules the four agents (improve_worker.py) and applies only what you approve.

Every weekday after the close (16:10) the worker runs for ORB in a separate process, on cached candles. Its
findings and any proposal (one parameter change that beat the current rules on unseen sessions and passed the code
agent's checks) wait on the Improvement Loop page. Approving a proposal records it in strategy_params.json; it takes
effect at the next pre-open (before 09:10 on a weekday), never in the middle of a session, so a live paper day is
never replayed under different rules. Every change is logged with a one-click rollback. Paper only: nothing here
places an order.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import orb

log = logging.getLogger("kite-desk.improve")
IST = orb.IST
BACKEND = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("improve_worker.py")
CACHE = orb.CACHE_DIR
PARAMS_FILE = CACHE / "strategy_params.json"
STRATEGIES = {"orb": ("ORB Strategy", orb)}
NIGHTLY = "16:10"
_lock = threading.Lock()
status = {k: {"running": False, "started": None, "error": ""} for k in STRATEGIES}


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    CACHE.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _state() -> dict:
    st = _read(PARAMS_FILE, {})
    for k, v in (("applied", {}), ("pending", []), ("history", []), ("dismissed", [])):
        st.setdefault(k, v)
    return st


def result(k: str) -> dict | None:
    return _read(CACHE / f"improve_{k}.json", None)


def _market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and "09:10" <= now.strftime("%H:%M") <= "15:35"


def _next_preopen(now: datetime) -> date:
    d = now.date()
    if now.weekday() < 5 and now.strftime("%H:%M") < "09:10":
        return d
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def apply_overrides() -> None:
    """Set the approved values on the live modules (startup and each pre-open)."""
    for k, params in _state()["applied"].items():
        mod = STRATEGIES.get(k, (None, None))[1]
        for name, v in (params or {}).items():
            if mod is not None and hasattr(mod, name):
                setattr(mod, name, v)


def _apply_due(now: datetime) -> None:
    if _market_hours(now):
        return
    with _lock:
        st = _state()
        due = [p for p in st["pending"] if p["effective"] <= now.date().isoformat()]
        if not due:
            return
        for p in due:
            st["applied"].setdefault(p["strategy"], {})[p["param"]] = p["to"]
            st["pending"].remove(p)
            for h in st["history"]:
                if h["id"] == p["id"]:
                    h.update(status="live", applied_at=now.isoformat(timespec="seconds"))
        _write(PARAMS_FILE, st)
    apply_overrides()
    log.info("improvement loop: applied %d approved change(s)", len(due))


def run(k: str) -> None:
    """One worker run for strategy k (blocking); the result file is replaced only on success."""
    if status[k]["running"]:
        return
    status[k] = {"running": True, "started": datetime.now(IST).isoformat(timespec="seconds"), "error": ""}
    out = CACHE / f"improve_{k}.tmp.json"
    try:
        st = _state()
        env_params = json.dumps(st["applied"].get(k, {}))
        p = subprocess.run([sys.executable, "-I", str(WORKER), k, str(out)], cwd=str(BACKEND), capture_output=True, text=True,
                           timeout=7200, env={**__import__("os").environ, "ST_BACKEND": str(BACKEND), "ST_APPLIED": env_params},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0 or not out.exists():
            raise RuntimeError((p.stderr or p.stdout or "worker failed").strip().splitlines()[-1][:300])
        out.replace(CACHE / f"improve_{k}.json")
    except Exception as exc:  # noqa: BLE001
        log.exception("improvement loop %s failed", k)
        status[k]["error"] = str(exc)
    finally:
        status[k]["running"] = False


def start(k: str | None = None) -> None:
    keys = [k] if k else list(STRATEGIES)
    if any(status[x]["running"] for x in keys):
        raise RuntimeError("The improvement loop is already running")
    threading.Thread(target=lambda: [run(x) for x in keys], daemon=True, name="improve").start()


def loop() -> None:
    import time
    time.sleep(60)
    while True:
        try:
            now = datetime.now(IST)
            _apply_due(now)
            if now.weekday() < 5 and now.strftime("%H:%M") >= NIGHTLY:
                stale = [k for k in STRATEGIES if not (result(k) or {}).get("built", "").startswith(now.date().isoformat())]
                if stale and not any(s["running"] for s in status.values()):
                    for k in stale:
                        run(k)
        except Exception:  # noqa: BLE001
            log.exception("improvement loop tick failed")
        time.sleep(300)


def approve(k: str) -> dict:
    r = result(k)
    prop = (r or {}).get("quant", {}).get("proposal")
    if not prop:
        raise ValueError("There is no proposal to approve")
    if (r.get("code") or {}).get("status") != "pass":
        raise ValueError("The code agent did not pass this change")
    now = datetime.now(IST)
    with _lock:
        st = _state()
        if any(p["strategy"] == k for p in st["pending"]):
            raise ValueError("A change for this strategy is already scheduled")
        live = {p: getattr(STRATEGIES[k][1], p) for p in (r.get("current") or {})}
        if r.get("current") and r["current"] != live:
            raise ValueError("The rules changed since this run: run the agents again before approving")
        eff = _next_preopen(now).isoformat()
        changes = prop.get("changes") or {prop["param"]: prop["to"]}
        from strategies.improve_worker import BOUNDS   # re-checked here: the result file is not trusted for safety bounds
        for name, v in changes.items():
            lo, hi = BOUNDS.get(k, {}).get(name, (None, None))
            if lo is None or not isinstance(v, (int, float)) or not lo <= v <= hi or not hasattr(STRATEGIES[k][1], name):
                raise ValueError(f"{name} = {v} is outside the allowed range")
        group = now.strftime("%Y%m%d%H%M%S")
        for name, v in changes.items():     # one history line per parameter, so each can be rolled back on its own
            item = {"id": f"{k}-{name}-{group}", "strategy": k, "param": name, "from": live.get(name, getattr(STRATEGIES[k][1], name, None)),
                    "to": v, "approved_at": now.isoformat(timespec="seconds"), "effective": eff, "group": group,
                    "evidence": {"oos": prop["oos"], "base_oos": prop["base_oos"], "run": r["built"], "label": prop.get("label")},
                    "status": "scheduled"}
            st["pending"].append(item)
            st["history"].append(dict(item))
        _write(PARAMS_FILE, st)
    _apply_due(now)
    return view()


def rollback(change_id: str) -> dict:
    now = datetime.now(IST)
    with _lock:
        st = _state()
        h = next((x for x in st["history"] if x["id"] == change_id), None)
        if not h:
            raise ValueError("No such change")
        if h["status"] == "scheduled":       # not live yet: just cancel it
            st["pending"] = [p for p in st["pending"] if p["id"] != change_id]
            h["status"] = "cancelled"
        elif h["status"] == "live":
            item = {"id": f"{h['strategy']}-{h['param']}-rollback-{now.strftime('%Y%m%d%H%M%S')}", "strategy": h["strategy"],
                    "param": h["param"], "from": h["to"], "to": h["from"], "approved_at": now.isoformat(timespec="seconds"),
                    "effective": _next_preopen(now).isoformat(), "status": "scheduled", "rollback_of": change_id}
            st["pending"].append(item)
            st["history"].append(dict(item))
            h["status"] = "rolled back"
        else:
            raise ValueError(f"This change is {h['status']}")
        _write(PARAMS_FILE, st)
    _apply_due(now)
    return view()


def dismiss(k: str) -> dict:
    r = result(k)
    prop = (r or {}).get("quant", {}).get("proposal")
    if prop:
        with _lock:
            st = _state()
            st["dismissed"].append({"strategy": k, "param": prop["param"], "to": prop["to"], "run": r["built"]})  # noqa: E501
            st["dismissed"] = st["dismissed"][-50:]
            _write(PARAMS_FILE, st)
    return view()


def view() -> dict:
    st = _state()
    out = []
    for k, (label, mod) in STRATEGIES.items():
        r = result(k)
        prop = (r or {}).get("quant", {}).get("proposal")
        dismissed = bool(prop and any(d["strategy"] == k and d["param"] == prop["param"] and d["to"] == prop["to"]
                                      and d["run"] == r["built"] for d in st["dismissed"]))
        out.append({"key": k, "label": label, "status": status[k], "result": r, "dismissed": dismissed,
                    "live": st["applied"].get(k, {}),
                    "scheduled": [p for p in st["pending"] if p["strategy"] == k]})
    return {"strategies": out, "history": st["history"][::-1][:40], "nightly": NIGHTLY}


apply_overrides()
