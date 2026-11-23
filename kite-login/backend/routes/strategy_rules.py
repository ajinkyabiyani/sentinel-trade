"""Strategy Rules: view and change every strategy's configurable rules. Paper only: no rule can place a real order."""
from __future__ import annotations

from fastapi import Body, HTTPException

from core import app
from strategies import strategy_rules

strategy_rules.apply_saved()          # saved changes go back into the strategies as the app starts


@app.get("/api/strategy-rules")
def strategy_rules_view(strategy: str | None = None):
    if strategy and strategy not in strategy_rules.STRATEGIES:
        raise HTTPException(404, "Unknown strategy.")
    return strategy_rules.view(strategy)


@app.post("/api/strategy-rules/{strategy}")
def strategy_rules_update(strategy: str, body: dict = Body(...)):
    try:
        return strategy_rules.update(strategy, body.get("values") or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/strategy-rules/{strategy}/reset")
def strategy_rules_reset(strategy: str):
    try:
        return strategy_rules.reset(strategy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
