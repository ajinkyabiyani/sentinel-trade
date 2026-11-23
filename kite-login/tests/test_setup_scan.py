"""Team 1's scanner after ORB was removed, and the shared paper risk settings."""
import datetime as dtm

import pytest

from strategies import risk_settings, setup_scan


def test_only_vwap_reclaim_and_momentum_are_scanned_and_momentum_needs_a_strong_trend():
    assert list(setup_scan.RULES) == ["vwap_reclaim", "momentum_rvol"]
    assert setup_scan.rules_for({"day_type": "Mild Trend"}) == ["vwap_reclaim"]
    assert setup_scan.rules_for({"day_type": "Strong Trend"}) == ["vwap_reclaim", "momentum_rvol"]
    assert setup_scan.rules_for(None) == ["vwap_reclaim"]


def test_find_never_asks_for_an_orb_setup(monkeypatch):
    bars = [{"slot": i, "start": "x", "o": 100, "h": 101, "l": 99, "c": 100, "v": 100} for i in range(10)]
    monkeypatch.setattr(setup_scan.india, "chart", lambda *a, **k: {})
    monkeypatch.setattr(setup_scan.india, "session_bars", lambda *a, **k: {"2026-11-05": bars})
    monkeypatch.setattr(setup_scan.india, "future_volumes", lambda *a, **k: {})
    asked = []
    monkeypatch.setattr(setup_scan.setups, "detect", lambda name, *a: asked.append(name))
    assert setup_scan.find("NIFTY", dtm.date(2026, 11, 5), None, set(), {"day_type": "Strong Trend"}) == []
    assert set(asked) == {"vwap_reclaim", "momentum_rvol"}


def test_the_day_type_is_worked_out_once_a_day(monkeypatch):
    rows = [{"date": f"d{i}", "h": 101.0, "l": 99.0, "c": 100.0, "o": 100.0} for i in range(30)]
    calls = []
    monkeypatch.setattr(setup_scan, "_regimes", {})
    monkeypatch.setattr(setup_scan, "daily_rows", lambda s, d: calls.append(s) or rows)
    monkeypatch.setattr(setup_scan.india, "chart", lambda *a, **k: {"meta": {"regularMarketPrice": 14.0}})
    day = dtm.date(2026, 11, 12)
    assert setup_scan.regime(day) == setup_scan.regime(day) == {"day_type": "Mild Trend", "bias": "Neutral"} and calls == ["NIFTY"]


def test_risk_settings_need_capital_and_the_confirmed_limits(monkeypatch, tmp_path):
    monkeypatch.setattr(risk_settings, "FILE", tmp_path / "risk.json")
    assert risk_settings.settings() is None
    with pytest.raises(ValueError, match="Confirm the risk limits"):
        risk_settings.save(100000, False)
    with pytest.raises(ValueError, match="above zero"):
        risk_settings.save(0, True)
    s = risk_settings.save(100000, True)
    assert s["max_risk_inr"] == 2000.0 and s["daily_loss_inr"] == 4000.0 and s["paper"] is True and risk_settings.settings() == s
