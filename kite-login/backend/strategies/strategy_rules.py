"""Configurable rules for every strategy: capital, risk, limits, timing and filters, edited from the app.

Each rule maps to a value a strategy reads on every pass (a module constant, or the Multi Agent Team's capital file), so a change
takes effect on the strategy's next pass without a restart. Open trades keep the stop, target and size they were opened with.
Every rule has a default, an allowed range and a plain explanation; cross-checks stop combinations that can't work (a per-trade
size above the book, a first scan after the last entry). Changes are kept with a history of who-changed-what-when (by time).
Paper only: no rule here can place a real order.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from strategies import common, multi_agent_team as mat, risk_settings

log = logging.getLogger("kite-desk.strategy-rules")
FILE = common.CACHE_DIR / "strategy_rules.json"
HISTORY = 100
_lock = threading.Lock()


def _f(key, label, kind, target, lo, hi, step, why, group):
    """One rule: kind is inr, int, num, time or limit (a whole number where 0 means no limit); target is (module, attribute)."""
    return {"key": key, "label": label, "kind": kind, "target": target, "min": lo, "max": hi, "step": step, "help": why, "group": group}


T = mat
STRATEGIES = {
    "multi_agent_team": {"name": "Multi Agent Team", "about": "Claude's Technical, Risk and Trader agents on Nifty, Bank Nifty and the day's top F&O movers, after every 5-minute candle.", "rules": [
        _f("capital", "Capital", "inr", ("risk", "capital"), 10_000, 10_000_000, 10_000, "The team's paper money.", "Money and risk"),
        _f("max_loss", "Max loss per trade", "inr", (T, "MAX_RISK_INR"), 500, 200_000, 500, "Loss at the stop. The code resizes the Trader's quantity to fit.", "Money and risk"),
        _f("daily_loss", "Daily loss limit", "inr", ("risk", "daily_loss_inr"), 500, 1_000_000, 500, "No new trade once the day's loss reaches this.", "Money and risk"),
        _f("max_open", "Open trades at a time", "int", (T, "MAX_OPEN"), 1, 10, 1, "New setups wait while this many trades are open.", "Money and risk"),
        _f("runs", "Claude runs a day", "int", (T, "MAX_AUTO_RUNS"), 1, 100, 1, "Each run is up to three Claude calls on your plan.", "Activity"),
        _f("stocks_each_way", "Movers scanned each way", "int", (T, "STOCKS_EACH_WAY"), 0, 30, 1, "Top F&O gainers and losers scanned besides Nifty and Bank Nifty. 0 scans the indices only.", "Activity"),
        _f("stale", "Drop a setup after, minutes", "int", (T, "STALE_MINUTES"), 5, 60, 5, "A setup still waiting this long after its candle is dropped.", "Activity"),
        _f("first_scan", "First scan", "time", (T, "FIRST_SCAN"), "09:20", "14:00", None, "The first scan of the day, after this candle closes.", "Activity"),
        _f("last_run", "Last new run", "time", (T, "LAST_RUN"), "09:35", "15:00", None, "No new Claude run after this time.", "Activity"),
    ]},
}


def _read_raw(rule) -> object:
    mod, attr = rule["target"]
    if mod == "risk":
        s = risk_settings.settings() or {}
        if attr == "capital":
            return s.get("capital")
        return s.get("daily_loss_inr")
    return getattr(mod, attr)


def _write_raw(rule, value) -> None:
    mod, attr = rule["target"]
    if mod == "risk":
        return                                      # written together in _apply_risk
    setattr(mod, attr, value)


# the values the code shipped with, captured once before any saved change is applied
DEFAULTS = {sid: {r["key"]: _read_raw(r) for r in st["rules"]} for sid, st in STRATEGIES.items()}
DEFAULTS["multi_agent_team"]["capital"] = DEFAULTS["multi_agent_team"]["capital"] or 100_000.0
DEFAULTS["multi_agent_team"]["daily_loss"] = DEFAULTS["multi_agent_team"]["daily_loss"] or round(DEFAULTS["multi_agent_team"]["capital"] * risk_settings.DAILY_LOSS_PCT / 100, 2)


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"values": {}, "history": []}


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def _coerce(rule, raw):
    kind = rule["kind"]
    if kind == "time":
        t = str(raw).strip()
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"{rule['label']}: use HH:MM, like 09:35.") from exc
        if not rule["min"] <= t <= rule["max"]:
            raise ValueError(f"{rule['label']}: between {rule['min']} and {rule['max']}.")
        return t
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{rule['label']}: enter a number.") from exc
    if not rule["min"] <= v <= rule["max"]:
        raise ValueError(f"{rule['label']}: between {rule['min']:,} and {rule['max']:,}.")
    return int(round(v)) if kind in ("int", "limit") else (round(v, 2) if kind == "num" else float(round(v)))


def _cross(sid: str, v: dict) -> list[str]:
    """Combinations that can't work."""
    errs = []
    if sid == "multi_agent_team":
        if v["max_loss"] > v["capital"]:
            errs.append("Max loss per trade can't be more than the capital.")
        if v["daily_loss"] < v["max_loss"]:
            errs.append("The daily loss limit must be at least one trade's max loss, or no trade could pass.")
        if v["first_scan"] >= v["last_run"]:
            errs.append("The first scan must come before the last new run.")
    return errs


def current(sid: str) -> dict:
    saved = _load()["values"].get(sid, {})
    return {r["key"]: saved.get(r["key"], DEFAULTS[sid][r["key"]]) for r in STRATEGIES[sid]["rules"]}


def _apply_risk(capital: float, daily_loss: float) -> None:
    s = risk_settings.settings() or {}
    pct = s.get("max_risk_pct") or risk_settings.MAX_RISK_PCT
    s.update(capital=float(capital), max_risk_pct=pct, max_risk_inr=round(capital * pct / 100, 2), daily_loss_inr=float(daily_loss),
             daily_loss_pct=round(daily_loss / capital * 100, 2), paper=True, confirmed=True, saved=datetime.now(common.IST).isoformat(timespec="seconds"))
    risk_settings.FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = risk_settings.FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    tmp.replace(risk_settings.FILE)


def apply(sid: str, values: dict) -> None:
    """Pushes one strategy's values into the code it governs."""
    for r in STRATEGIES[sid]["rules"]:
        v = values[r["key"]]
        if r["kind"] == "limit":
            v = None if not v else int(v)
        _write_raw(r, v)
    if sid == "multi_agent_team":
        _apply_risk(values["capital"], values["daily_loss"])


def apply_saved() -> None:
    """At startup: every saved change goes back into the code."""
    for sid in STRATEGIES:
        if _load()["values"].get(sid):
            try:
                apply(sid, current(sid))
            except Exception:  # noqa: BLE001 - a bad saved file must not stop the app
                log.exception("applying saved rules for %s", sid)


def update(sid: str, changes: dict) -> dict:
    if sid not in STRATEGIES:
        raise ValueError("Unknown strategy.")
    rules = {r["key"]: r for r in STRATEGIES[sid]["rules"]}
    unknown = [k for k in changes if k not in rules]
    if unknown:
        raise ValueError(f"Unknown rule: {', '.join(unknown)}.")
    with _lock:
        before = current(sid)
        after = dict(before)
        errs = []
        for k, raw in changes.items():
            try:
                after[k] = _coerce(rules[k], raw)
            except ValueError as exc:
                errs.append(str(exc))
        errs += [] if errs else _cross(sid, after)
        if errs:
            raise ValueError(" ".join(errs))
        diff = [{"key": k, "label": rules[k]["label"], "from": before[k], "to": after[k]} for k in rules if before[k] != after[k]]
        if not diff:
            return view(sid)
        data = _load()
        data["values"][sid] = {k: after[k] for k in rules if after[k] != DEFAULTS[sid][k]}
        data["history"] = ([{"at": datetime.now(common.IST).isoformat(timespec="seconds"), "strategy": sid, "changes": diff}] + data.get("history", []))[:HISTORY]
        _save(data)
        apply(sid, after)
    return view(sid)


def reset(sid: str) -> dict:
    if sid not in STRATEGIES:
        raise ValueError("Unknown strategy.")
    return update(sid, dict(DEFAULTS[sid]))


def view(sid: str | None = None) -> dict:
    data = _load()
    out = []
    for key, st in STRATEGIES.items():
        if sid and key != sid:
            continue
        vals = current(key)
        out.append({"id": key, "name": st["name"], "about": st["about"],
                    "rules": [{k: r[k] for k in ("key", "label", "kind", "min", "max", "step", "help", "group")} | {"value": vals[r["key"]], "default": DEFAULTS[key][r["key"]]} for r in st["rules"]]})
    return {"strategies": out, "history": [h for h in data.get("history", []) if not sid or h["strategy"] == sid][:30],
            "note": "Changes apply on each strategy's next pass, without a restart. Open trades keep the stop, target and size they were opened with. Paper only."}
