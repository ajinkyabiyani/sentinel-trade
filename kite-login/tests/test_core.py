"""Regression suite for Sentinel Trade (QA_Automation_Agent maintains it). Pure functions and invariants only: no Kite
call, no order, no write outside a temporary folder. Run: python -m pytest tests -q (from C:\\Algo\\kite-login)."""
import json
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"


# ---------------- safety invariants ----------------
@pytest.mark.parametrize("module", ["orb", "momentum", "paper_desk", "orb_backtest", "improve", "improve_worker"])
def test_strategy_code_never_places_orders(module):
    path = BACKEND / "strategies" / f"{module}.py"
    if not path.exists():
        pytest.skip(f"{module}.py not present")
    src = path.read_text(encoding="utf-8")
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt)\s*\(", src), f"{module}.py calls the order API"


def test_frontend_never_handles_the_access_token():
    src = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in (BACKEND.parent / "frontend" / "src").rglob("*.js*"))
    assert "access_token" not in src, "frontend source references the Kite access_token field"


# ---------------- ORB ----------------
def test_orb_costs_positive_and_grow_with_value():
    from strategies import orb
    small, big = orb.costs(10_000, 10_100), orb.costs(100_000, 101_000)
    assert 0 < small < big
    assert big < 100_000 * 0.002, "round-trip intraday charges should stay well under 0.2%"


def test_orb_record_never_rewrites_a_final_session(tmp_path, monkeypatch):
    from strategies import orb
    monkeypatch.setattr(orb, "LEDGER_FILE", tmp_path / "ledger.json")
    monkeypatch.setattr(orb, "CACHE_DIR", tmp_path)
    (tmp_path / "ledger.json").write_text(json.dumps({"2026-11-03": {"final": True, "net": 100.0, "trades": []}}))
    eng = orb.OrbEngine()
    eng.snapshot = {"session": "2026-11-03", "asof": "x", "phase": "closed", "signals": [], "trades": [],
                    "settings": {}, "counts": {"eligible": 0},
                    "controller": {"c0": 1, "state": "CLOSED", "halt": None, "realized": -5.0, "unrealized": 0.0, "net": -5.0}}
    eng.record()
    assert json.loads((tmp_path / "ledger.json").read_text())["2026-11-03"]["net"] == 100.0


# ---------------- improvement loop ----------------
def test_improve_worker_split_and_stats():
    from strategies import improve_worker as w
    days = [f"2026-08-{i:02d}" for i in range(1, 31)] + [f"2026-09-{i:02d}" for i in range(1, 31)] + [f"2026-10-{i:02d}" for i in range(1, 31)]
    trades = [{"day": d, "r": 1.0 if i % 2 else -1.0, "net": 10.0} for i, d in enumerate(days)]
    sp = w._split(trades, days)
    assert sp["is"]["trades"] == 60 and sp["oos"]["trades"] == 30
    assert sp["all"]["win_rate"] == 50.0


def test_improve_bounds_cover_search_space():
    from strategies import improve_worker as w
    for strat, space in w.SPACE.items():
        for k, vals in space.items():
            lo, hi = w.BOUNDS[strat][k]
            assert all(lo <= v <= hi for v in vals), f"{strat}.{k} search values outside its safety bounds"
