"""Sector rotation by RSI, and the wins-by-confidence log."""
from datetime import date, timedelta

from strategies import confidence_log as cl
from terminal import market


def rows_from(closes, start=date(2026, 6, 1)):
    return [[(start + timedelta(days=i)).isoformat(), c, c, c, c, 0] for i, c in enumerate(closes)]


def test_rsi_series_matches_the_one_step_wilder_rsi():
    closes = [100 + ((i * 7) % 11) - 5 + i * 0.3 for i in range(60)]
    series = market.rsi_series(closes)
    state = market.wilder_rsi_state(closes[:-1])
    assert abs(series[-1] - market.rsi_with_last(state, closes[-2], closes[-1])) < 1e-9
    assert series[13] is None and series[14] is not None


def test_quadrants_and_a_rising_index_leads():
    assert market.quadrant(60, 2) == "Leading" and market.quadrant(60, -2) == "Weakening"
    assert market.quadrant(40, -2) == "Lagging" and market.quadrant(40, 2) == "Improving"
    up = rows_from([100 + i * 0.5 + (i % 3) * 0.2 for i in range(80)])
    r = market.rotation_row("Banks", "NSE:NIFTY BANK", up, None, "2027-01-01")
    assert r["rsi"] > 50 and r["quadrant"] in ("Leading", "Weakening") and len(r["tail"]) == market.ROTATION_TAIL
    assert market.rotation_row("New", "NSE:X", rows_from([100] * 10), None, "2027-01-01") is None      # too little history


def test_the_live_price_counts_and_today_s_candle_is_not_double_counted():
    hist = rows_from([100 + i * 0.2 for i in range(60)])
    today = hist[-1][0]
    with_live = market.rotation_row("X", "k", hist, 90.0, today)          # a sharp drop today, on the live price
    assert with_live["last"] == 90.0 and with_live["rsi_change"] < 0


def test_confidence_bands_and_summary():
    assert cl.band_of(None) == "Not stated" and cl.band_of(49.5) == "Under 50%" and cl.band_of(55) == "50–59%" and cl.band_of(69.9) == "60–69%" and cl.band_of(88) == "70% and up"
    rows = [cl._row("2026-11-13", "A", "X", "option", 55, "s", "closed", 100), cl._row("2026-11-13", "A", "Y", "option", 56, "s", "closed", -50),
            cl._row("2026-11-13", "B", "Z", "option", 62, "s", "closed", -10), cl._row("2026-11-14", "B", "W", "option", 65, "s", "open", None)]
    s = cl.summary(rows)
    band = {b["band"]: b for b in s["bands"]}
    assert band["50–59%"]["won"] == 1 and band["50–59%"]["closed"] == 2 and band["60–69%"]["open"] == 1
    assert s["total"] == {"closed": 3, "won": 1, "lost": 2, "open": 1, "win_rate": 33.3, "net": 40.0}
    assert any("won most often" in line for line in s["lines"]) and any("can still swing" in line for line in s["lines"])


def test_the_log_is_written_as_a_csv(tmp_path):
    rows = [cl._row("2026-11-13", "A", "X", "option", 55, "s", "closed", 100)]
    path = cl.write_csv(rows, tmp_path / "log.csv")
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("date,strategy,instrument") and "50–59%" in text
