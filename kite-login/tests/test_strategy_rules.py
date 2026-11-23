"""Strategy Rules: changes reach the strategy code, ranges and cross-checks hold, reset restores the defaults."""
import pytest

from strategies import multi_agent_team as mat, risk_settings, strategy_rules as sr


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "FILE", tmp_path / "rules.json")
    monkeypatch.setattr(risk_settings, "FILE", tmp_path / "risk.json")
    keep = {(m, a): getattr(m, a) for st in sr.STRATEGIES.values() for r in st["rules"] for m, a in [r["target"]] if m != "risk"}
    yield
    for (m, a), v in keep.items():
        setattr(m, a, v)


def test_only_the_multi_agent_team_is_listed():
    assert list(sr.STRATEGIES) == ["multi_agent_team"]


@pytest.mark.parametrize("strategy, change, message", [
    ("multi_agent_team", {"daily_loss": 1000}, "at least one trade's max loss"),
    ("multi_agent_team", {"first_scan": "14:00", "last_run": "13:00"}, "before the last new run"),
])
def test_bad_values_are_refused_and_nothing_changes(strategy, change, message):
    before = sr.current(strategy)
    with pytest.raises(ValueError, match=message):
        sr.update(strategy, change)
    assert sr.current(strategy) == before


def test_the_team_capital_and_daily_loss_go_to_its_risk_settings():
    sr.update("multi_agent_team", {"capital": 250000, "daily_loss": 9000, "max_loss": 4000})
    s = risk_settings.settings()
    assert s["capital"] == 250000.0 and s["daily_loss_inr"] == 9000.0 and mat.MAX_RISK_INR == 4000.0


def test_reset_restores_every_default():
    sr.update("multi_agent_team", {"capital": 250000, "daily_loss": 9000, "max_loss": 4000})
    sr.reset("multi_agent_team")
    assert sr.current("multi_agent_team") == sr.DEFAULTS["multi_agent_team"]


def test_unknown_rules_and_strategies_are_refused():
    with pytest.raises(ValueError, match="Unknown rule"):
        sr.update("multi_agent_team", {"leverage": 10})
    with pytest.raises(ValueError, match="Unknown strategy"):
        sr.update("pipeline", {})
