"""Expiry Range Agent: the three ranges, one prediction per weekly cycle, settling after the close, and the replay."""
from datetime import date, datetime, timedelta

import pytest

from strategies import expiry_range as er

NOW = datetime(2026, 11, 11, 9, 45, tzinfo=er.common.IST)


def chain(sym, spot, expiry, iv=16.0, straddle=270.0, support=24000.0, resistance=26000.0):
    return {"spot": spot, "expiry": expiry, "atm_iv": iv, "straddle": straddle, "support": support, "resistance": resistance, "pcr": 1.0, "max_pain": spot}


@pytest.fixture
def book(monkeypatch, tmp_path):
    monkeypatch.setattr(er, "DIR", tmp_path)
    return er


def test_ranges_snap_outward_to_strikes_and_scale_a_monthly_straddle():
    r = er.ranges(25144.3, 16.3, 6.0, 274.35, 6.0, 24000, 26000, 50)
    half = 25144.3 * 0.163 * (6 / 365) ** 0.5
    assert r["iv"]["lo"] <= 25144.3 - half and r["iv"]["hi"] >= 25144.3 + half and r["iv"]["lo"] % 50 == 0
    assert r["straddle"] == {"lo": 24850.0, "hi": 25450.0} and r["oi"] == {"lo": 24000.0, "hi": 26000.0}
    monthly = er.ranges(61000, 16, 6, 1200, 24, None, None, 100)
    assert monthly["straddle"]["hi"] - 61000 < 1200 / 2 + 100 and "oi" not in monthly        # sqrt(6/24) = half the monthly straddle


def test_one_prediction_a_cycle_then_settled_after_the_close(book):
    chains = {"NIFTY": chain("NIFTY", 25000, "2026-11-17"), "BANKNIFTY": chain("BANKNIFTY", 61000, "2026-11-23", straddle=1200, support=60000, resistance=62000)}
    rec = book.predict(NOW, chain_of=lambda s, e: chains[s])
    assert rec["expiry"] == "2026-11-17" and set(rec["indices"]) == {"NIFTY", "BANKNIFTY"}
    assert book.predict(NOW, chain_of=lambda s, e: chains[s]) is None                      # already made for this expiry
    candles = {"NIFTY": [{"date": "2026-11-11", "high": 25100, "low": 24900, "close": 25050}, {"date": "2026-11-17", "high": 25200, "low": 24950, "close": 25150}],
               "BANKNIFTY": [{"date": "2026-11-17", "high": 63500, "low": 60900, "close": 63400}]}
    assert book.settle(datetime(2026, 11, 17, 15, 0, tzinfo=er.common.IST), daily=lambda s: candles[s]) == 0     # not before 15:31
    assert book.settle(datetime(2026, 11, 17, 15, 40, tzinfo=er.common.IST), daily=lambda s: candles[s]) == 2
    n = book.records()[0]["indices"]["NIFTY"]["result"]["by"]
    b = book.records()[0]["indices"]["BANKNIFTY"]["result"]["by"]
    assert n["iv"]["won"] and n["oi"]["won"] and not b["oi"]["won"] and b["oi"]["miss_by"] == 1400
    s = {(x["index"], x["method"]): x for x in book.stats(book.records())}
    assert s[("NIFTY", "iv")]["win_rate"] == 100.0 and s[("BANKNIFTY", "oi")]["won"] == 0


def test_replay_uses_tuesday_expiries_after_the_switch_and_moves_them_before_holidays():
    start = date(2026, 9, 1)
    days = [start + timedelta(days=i) for i in range(70) if (start + timedelta(days=i)).weekday() < 5 and (start + timedelta(days=i)).isoformat() != "2026-10-20"]
    mk = lambda base: [{"date": d.isoformat(), "high": base + i + 5, "low": base + i - 5, "close": base + i} for i, d in enumerate(days)]  # noqa: E731
    vix = [{"date": d.isoformat(), "high": 15, "low": 15, "close": 15} for d in days]
    out = er.replay(mk(25000), mk(60000), vix)
    exp = [r["expiry"] for r in out]
    assert "2026-10-19" in exp and all(date.fromisoformat(e).weekday() in (0, 1) for e in exp)        # Tuesday 20 Oct holiday -> Monday
    assert all(set(r["indices"]["NIFTY"]["result"]["by"]) == {"iv", "straddle"} for r in out)
