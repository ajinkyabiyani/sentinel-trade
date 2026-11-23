"""Regression suite for Sentinel Trade (QA_Automation_Agent maintains it). Pure functions and invariants only: no Kite
call, no order, no write outside a temporary folder. Run: python -m pytest tests -q (from C:\\Algo\\kite-login)."""
import json
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"


# ---------------- safety invariants ----------------
@pytest.mark.parametrize("module", ["momentum", "paper_desk", "common", "setup_scan", "risk_settings"])
def test_strategy_code_never_places_orders(module):
    path = BACKEND / "strategies" / f"{module}.py"
    if not path.exists():
        pytest.skip(f"{module}.py not present")
    src = path.read_text(encoding="utf-8")
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt)\s*\(", src), f"{module}.py calls the order API"


def test_frontend_never_handles_the_access_token():
    src = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in (BACKEND.parent / "frontend" / "src").rglob("*.js*"))
    assert "access_token" not in src, "frontend source references the Kite access_token field"


# ---------------- charges ----------------
def test_intraday_costs_positive_and_grow_with_value():
    from strategies import common
    small, big = common.costs(10_000, 10_100), common.costs(100_000, 101_000)
    assert 0 < small < big
    assert big < 100_000 * 0.002, "round-trip intraday charges should stay well under 0.2%"


