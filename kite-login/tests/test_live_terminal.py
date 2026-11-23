"""The live terminal: market phase, every strategy's feed shape, and nothing shown as working after the close."""
from datetime import datetime

import pytest

from strategies import live_terminal as lt

IST = lt.common.IST


def test_phase_is_live_only_in_market_hours_on_trading_days(monkeypatch):
    monkeypatch.setattr(lt.india, "trading_day", lambda d: d.weekday() < 5)
    assert lt.phase(datetime(2026, 11, 16, 10, 0, tzinfo=IST))["live"]
    pre = lt.phase(datetime(2026, 11, 16, 8, 30, tzinfo=IST))
    assert not pre["live"] and pre["next"] == "starts today 09:15"
    sunday = lt.phase(datetime(2026, 11, 15, 12, 0, tzinfo=IST))
    assert not sunday["live"] and "Mon 16 Nov" in sunday["next"]


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(lt.mat, "DIR", tmp_path / "mat")
    monkeypatch.setattr(lt.mat, "live", lambda: None)
    monkeypatch.setattr(lt.mat.risk_settings, "settings", lambda: {"capital": 100000})
    return lt


@pytest.mark.parametrize("name", ["multi_agent_team"])
def test_each_feed_has_the_terminal_shape(quiet, name):
    f = quiet.FEEDS[name](datetime(2026, 11, 16, 10, 0, tzinfo=IST))
    assert {"agents", "stages", "flow", "candidates", "trade", "tail", "log", "balance", "trades", "session"} <= set(f)
    assert [a["id"] for a in f["agents"]] == f["stages"] and all(a["state"] == "idle" or a["badge"] for a in f["agents"])




def test_only_the_multi_agent_team_has_a_live_terminal():
    assert list(lt.FEEDS) == ["multi_agent_team"]
