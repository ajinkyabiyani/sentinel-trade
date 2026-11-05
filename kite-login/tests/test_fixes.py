"""Regression tests for the high-severity fixes of the 2026-11-04 app-health review."""
from datetime import datetime, timedelta, timezone

import pytest

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------- 2/3. Darvas desk: no look-ahead fill, one lock per tick ----------------
class FakeKite:
    def __init__(self, ltp, high, open_):
        self.q = {"last_price": ltp, "ohlc": {"high": high, "open": open_, "low": min(ltp, open_)}}

    def quote(self, *keys):
        return {k: self.q for k in keys}

    def historical_data(self, *a, **k):
        return []


CALL = {"symbol": "ABC", "signal_date": "2026-11-03", "trigger": 100.0, "limit": 101.0, "stop": 92.0, "box_top": 99.5, "token": None}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    from strategies import paper_desk
    monkeypatch.setattr(paper_desk, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(paper_desk, "_darvas_calls", lambda: [dict(CALL)])
    return paper_desk


def test_darvas_late_order_ignores_the_earlier_high(desk):
    # placed at 10:30: the day's high (105) happened before the order existed; price now 98 is below the trigger
    desk.tick_swing(FakeKite(ltp=98.0, high=105.0, open_=99.0), datetime(2026, 11, 4, 10, 30, tzinfo=IST))
    b = desk._book("darvas")
    assert not b["open"] and len(b["pending"]) == 1, "filled on a high from before the order was placed"
    # later the price itself crosses the trigger: now it fills, at the live price
    desk.tick_swing(FakeKite(ltp=100.4, high=105.0, open_=99.0), datetime(2026, 11, 4, 10, 31, tzinfo=IST))
    b = desk._book("darvas")
    assert len(b["open"]) == 1 and b["open"][0]["entry"] == pytest.approx(100.4)


def test_darvas_order_from_the_open_uses_the_day_range(desk):
    # placed at the first tick it stands in for an order resting from the open: the day's high may fill it
    desk.tick_swing(FakeKite(ltp=98.0, high=100.5, open_=99.0), datetime(2026, 11, 4, 9, 16, tzinfo=IST))
    b = desk._book("darvas")
    assert len(b["open"]) == 1 and b["open"][0]["entry"] == pytest.approx(100.0)


def test_darvas_tick_holds_the_lock_throughout(desk, monkeypatch):
    seen = []
    monkeypatch.setattr(desk, "_tick_desk", lambda *a, **k: seen.append(desk._lock.locked()))
    desk.tick_swing(FakeKite(98.0, 99.0, 98.0), datetime(2026, 11, 4, 10, 0, tzinfo=IST))
    assert seen == [True]


# ---------------- 4. improvement loop: stale or out-of-bounds proposals are refused ----------------
@pytest.fixture
def imp(tmp_path, monkeypatch):
    from strategies import improve
    monkeypatch.setattr(improve, "PARAMS_FILE", tmp_path / "strategy_params.json")
    monkeypatch.setattr(improve, "CACHE", tmp_path)
    monkeypatch.setattr(improve, "apply_overrides", lambda: None)
    return improve


def _result(current, changes):
    return {"built": "2026-11-04T16:30:00+05:30", "current": current, "code": {"status": "pass"},
            "quant": {"proposal": {"changes": changes, "param": next(iter(changes)), "to": next(iter(changes.values())),
                                   "oos": {"exp_r": 0.5}, "base_oos": {"exp_r": 0.2}, "label": "x"}}}


def test_approve_refuses_when_rules_changed_since_the_run(imp, monkeypatch):
    from strategies import orb
    stale = {"TARGET_R": orb.TARGET_R + 1.0}          # the run saw a different live value
    monkeypatch.setattr(imp, "result", lambda k: _result(stale, {"TARGET_R": 2.5}))
    with pytest.raises(ValueError, match="rules changed"):
        imp.approve("orb")
    assert not (imp.PARAMS_FILE).exists()


def test_approve_refuses_out_of_bounds_values(imp, monkeypatch):
    from strategies import orb
    monkeypatch.setattr(imp, "result", lambda k: _result({"TARGET_R": orb.TARGET_R}, {"TARGET_R": 50.0}))
    with pytest.raises(ValueError, match="outside the allowed range"):
        imp.approve("orb")
