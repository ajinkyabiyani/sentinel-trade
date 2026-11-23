"""The helpers the option strategies share."""
from datetime import date, datetime

import pytest

from strategies import common


def at(hm, second=30, day=date(2026, 11, 5)):
    return datetime(day.year, day.month, day.day, int(hm[:2]), int(hm[3:]), second, tzinfo=common.IST)


def test_labels_cover_the_session():
    assert common.LABELS[0] == "09:15" and common.LABELS[-1] == "15:10" and len(common.LABELS) == 72
    assert common.plus_minutes("09:55", 10) == "10:05" and common.plus_minutes("10:00", -5) == "09:55"


@pytest.mark.parametrize("hm,second,want", [("09:19", 30, None), ("09:20", 30, "09:15"), ("09:20", 3, None), ("10:03", 0, "09:55"), ("15:15", 2, "15:10"), ("17:30", 0, "15:10")])
def test_completed_bar(hm, second, want):
    assert common.completed_bar(at(hm, second)) == want


def test_no_bars_at_the_weekend():
    assert common.completed_bar(at("10:00", day=date(2026, 11, 7))) is None


def test_bars_from_candles():
    bars = common.bars_from_candles([{"date": datetime(2026, 11, 5, 9, 15), "open": 1, "high": 3, "low": 0.5, "close": 2}])
    assert bars == {"09:15": (1.0, 3.0, 0.5, 2.0)}


def test_option_rows_groups_by_underlying_and_skips_futures():
    rows = common.option_rows(lambda exch: [{"name": "NIFTY", "instrument_type": "CE"}, {"name": "NIFTY", "instrument_type": "FUT"}, {"name": "SBIN", "instrument_type": "PE"}])
    assert len(rows["NIFTY"]) == 1 and len(rows["SBIN"]) == 1


def test_put_call_parity():
    s, k, t, v = 24000.0, 24100.0, 7 / 365, 0.15
    c, p = common.bs_price(s, k, t, v, True), common.bs_price(s, k, t, v, False)
    assert c - p == pytest.approx(s - k * 2.718281828 ** (-common.RATE * t), abs=0.01)


def test_expired_option_is_worth_its_intrinsic_value():
    assert common.bs_price(24100.0, 24000.0, 0, 0.15, True) == 100
    assert common.bs_price(24100.0, 24000.0, 0, 0.15, False) == 0


def test_delta_is_in_range_and_higher_in_the_money():
    assert common.bs_delta(24000.0, 23800.0, 7 / 365, 0.15, True) > common.bs_delta(24000.0, 24200.0, 7 / 365, 0.15, True) > 0


def test_implied_vol_recovers_the_input():
    px = common.bs_price(24000.0, 24000.0, 7 / 365, 0.18, True)
    assert common.implied_vol(px, 24000.0, 24000.0, 7 / 365, True) == pytest.approx(0.18, abs=1e-4)
    assert common.implied_vol(0, 24000.0, 24000.0, 7 / 365, True) is None


def test_years_to_has_a_floor():
    assert common.years_to(date(2026, 11, 5), date(2026, 11, 5), "15:30") == pytest.approx(60 / (365.25 * 86400))


def test_charges():
    fee = common.charges([(True, 10000.0), (False, 12000.0)])
    txn = 0.0003553 * 22000
    sebi = 22000e-6
    assert fee == pytest.approx(40 + 18 + txn + sebi + 0.18 * (40 + txn + sebi) + 0.3)
