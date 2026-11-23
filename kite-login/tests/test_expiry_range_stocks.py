"""Expiry Range Agent for F&O stocks: a week's build, settling, the replay, and the page's totals."""
from datetime import date, datetime, timedelta

import pytest

from strategies import expiry_range_stocks as ers

NOW = datetime(2026, 11, 16, 9, 35, tzinfo=ers.common.IST)


def chain(spot, expiry, iv=30.0, straddle=40.0, step=10):
    rows = [{"strike": spot - 50 + i * step} for i in range(11)]
    return {"spot": spot, "expiry": expiry, "atm_iv": iv, "straddle": straddle, "support": spot - 60, "resistance": spot + 60, "pcr": 0.9, "rows": rows}


@pytest.fixture
def book(monkeypatch, tmp_path):
    monkeypatch.setattr(ers.er, "DIR", tmp_path)
    return ers


def test_a_week_is_built_for_every_stock_with_a_chain_and_settled_after_the_close(book):
    chains = {"NIFTY": chain(25000, "2026-11-17"), "TCS": chain(3000, "2026-11-23"), "INFY": chain(1500, "2026-11-23", step=20)}
    def chain_of(s, e):
        if s == "BAD":
            raise RuntimeError("no chain")
        return chains[s]
    rec = book.build(NOW, ["TCS", "INFY", "BAD"], chain_of=chain_of, pace=0)
    assert set(rec["stocks"]) == {"TCS", "INFY"} and rec["failed"] == ["BAD"] and rec["expiry"] == "2026-11-17"
    assert rec["stocks"]["INFY"]["ranges"]["iv"]["lo"] % 20 == 0                       # snapped to the stock's own strike step
    book._save([rec])
    candles = {"TCS": [{"date": "2026-11-16", "high": 3010, "low": 2990, "close": 3005}, {"date": "2026-11-17", "high": 3020, "low": 2980, "close": 3010}],
               "INFY": [{"date": "2026-11-17", "high": 1700, "low": 1490, "close": 1690}]}
    assert book.settle(datetime(2026, 11, 17, 15, 45, tzinfo=ers.common.IST), daily=lambda s, e: candles[s]) == 2
    got = book.records()[0]["stocks"]
    assert got["TCS"]["result"]["by"]["iv"]["won"] and not got["INFY"]["result"]["by"]["iv"]["won"]


def test_the_replay_scales_realised_volatility_by_the_market_premium():
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(120) if (start + timedelta(days=i)).weekday() < 5]
    wave = lambda base, amp: [{"date": d.isoformat(), "high": base + amp * ((i % 4) - 1.5) + 3, "low": base + amp * ((i % 4) - 1.5) - 3, "close": base + amp * ((i % 4) - 1.5)} for i, d in enumerate(days)]  # noqa: E731
    vix = [{"date": d.isoformat(), "close": 15.0} for d in days]
    out = ers.replay({"CALM": wave(1000, 2), "WILD": wave(1000, 20)}, wave(25000, 50), vix)
    assert set(out) == {"CALM", "WILD"}
    assert out["WILD"][-1]["iv"] > out["CALM"][-1]["iv"]
    assert all(set(w["result"]["by"]) == {"iv", "straddle"} for w in out["CALM"])


def test_the_totals_count_every_stock_week():
    rg = {"iv": {"lo": 1, "hi": 2}, "straddle": {"lo": 1, "hi": 2}}
    won = {"won": True, "stayed_inside": True, "miss_by": 0}
    lost = {"won": False, "stayed_inside": False, "miss_by": 5}
    t = {x["method"]: x for x in ers._agg([(rg, {"iv": won, "straddle": lost}), (rg, {"iv": won, "straddle": won})])}
    assert t["iv"]["win_rate"] == 100.0 and t["straddle"]["won"] == 1 and t["straddle"]["weeks"] == 2
