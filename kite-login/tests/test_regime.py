"""Regime engine: candles with a known shape must land in the regime of the user's quick-reference table."""
import math
import random

import pytest

regime = pytest.importorskip("strategies.regime")


def _bars(closes, wick=0.004, seed=3, body=0.3):
    rnd = random.Random(seed)
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev + (c - prev) * (1 - body)
        h = max(o, c) * (1 + wick * (0.5 + rnd.random()))
        lo = min(o, c) * (1 - wick * (0.5 + rnd.random()))
        rows.append([f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", o, h, lo, c, 100000])
        prev = c
    return rows


def _walk(n, drift, noise, seed=5, start=100.0):
    rnd = random.Random(seed)
    out, px = [], start
    for _ in range(n):
        px *= 1 + drift + rnd.gauss(0, noise)
        out.append(px)
    return out


def test_steady_rise_is_an_uptrend():
    r = regime.classify(_bars(_walk(260, 0.006, 0.003)))
    assert r["regime"] == "Uptrend" and r["score"] >= 40 and r["view"] == "Bullish"


def test_steady_fall_is_a_downtrend():
    r = regime.classify(_bars(_walk(260, -0.006, 0.003, start=500.0)))
    assert r["regime"] == "Downtrend" and r["score"] <= -40 and r["view"] == "Bearish"


def test_oscillation_between_levels_is_range_bound():
    # noisy swings between fixed levels, read at many end dates: mostly range-bound, never a trend
    seen = []
    for seed in range(6):
        rnd = random.Random(seed)
        for n in range(240, 262, 3):
            closes = [100 + 3 * math.sin(i / 2.5) + rnd.gauss(0, 0.4) for i in range(n)]
            seen.append(regime.classify(_bars(closes, wick=0.006, seed=seed, body=0.5))["regime"])
    assert "Uptrend" not in seen and "Downtrend" not in seen
    assert seen.count("Range-bound") > len(seen) / 2


def test_shrinking_swings_are_a_squeeze():
    closes = [100 + 6 * math.sin(i / 3) for i in range(220)] + [100 + 0.15 * math.sin(i / 3) for i in range(40)]
    rows = _bars(closes[:220], wick=0.02) + _bars(closes[220:], wick=0.001, seed=9)
    assert regime.classify(rows)["regime"] == "Squeeze"


def test_wild_swings_without_direction_are_choppy():
    quiet = [100 + 0.6 * math.sin(i / 5) for i in range(220)]
    wild = [100 + (7 if i % 2 else -7) * (1 + 0.2 * math.sin(i)) for i in range(40)]
    rows = _bars(quiet, wick=0.002) + _bars(wild, wick=0.03, seed=7, body=0.15)
    assert regime.classify(rows)["regime"] == "Choppy"


def test_too_little_history_gives_nothing():
    assert regime.classify(_bars(_walk(50, 0.001, 0.01))) is None


def test_one_odd_close_does_not_flip_the_regime():
    closes = _walk(259, 0.006, 0.002)
    rows = _bars(closes + [closes[-1] * 0.97])
    r = regime.classify(rows)
    assert r["regime"] == "Uptrend"


def test_every_result_explains_itself():
    r = regime.classify(_bars(_walk(260, 0.001, 0.012)))
    assert r["regime"] in regime.NOTES and r["note"] and r["days"] >= 1
    assert all(isinstance(ok, bool) and text for ok, text in r["signs"]) and -100 <= r["score"] <= 100


def test_splice_updates_today_and_appends_a_new_day():
    rows = [["2026-11-04", 10, 11, 9, 10, 5]]
    assert regime.splice(rows, "2026-11-04", 10, 11, 9, 12)[-1][2:5] == [12, 9, 12]
    assert regime.splice(rows, "2026-11-05", 10, 11, 9, 10.5)[-1][:5] == ["2026-11-05", 10, 11, 9, 10.5]
    assert regime.splice(rows, "2026-11-05", None, None, None, None) == rows


def _idx(score, name="Uptrend"):
    return {"regime": name, "score": score}


def test_market_view_weights_and_vix_penalty():
    idx = {n: _idx(60) for n in regime.WEIGHTS}
    breadth = {"total": 100, "bull_pct": 60.0, "bear_pct": 20.0}
    calm = regime.market_view(idx, breadth, {"last": 12.0, "pct": 30})
    fear = regime.market_view(idx, breadth, {"last": 25.0, "pct": 97})
    assert calm["view"] == "Bullish" and calm["score"] == round(0.85 * 60 + 0.15 * 40)
    assert fear["score"] == calm["score"] - 20
    assert any("VIX" in x for x in calm["reasons"])


def test_market_view_without_data_says_so():
    assert regime.market_view({}, None, None)["score"] is None


def test_breadth_counts_liquid_stocks_only():
    stocks = [{"regime": "Uptrend", "score": 50, "liquid": True}, {"regime": "Downtrend", "score": -50, "liquid": True},
              {"regime": "Choppy", "score": 0, "liquid": True}, {"regime": "Uptrend", "score": 90, "liquid": False}]
    b = regime.breadth_of(stocks)
    assert b["total"] == 3 and round(b["bull_pct"], 1) == 33.3 and b["regimes"] == {"Uptrend": 1, "Downtrend": 1, "Choppy": 1}
