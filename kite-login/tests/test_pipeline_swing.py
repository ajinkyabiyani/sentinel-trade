"""The Agent Pipeline team's swing option book: the multi-timeframe read, the option checks, the gates, the follow-up, and no order path."""
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from strategies import common, pipeline_swing as ps

SRC = Path(ps.__file__).read_text(encoding="utf-8")
NOW = datetime(2026, 11, 12, 14, 50, tzinfo=common.IST)
DAY = "2026-11-12"
EXP = "2026-12-29"                       # 47 days out


def uptrend(n=230, last_jump=True):
    """A steady climb with a small dip, so the trend is in place, then a day that clears yesterday's high."""
    rows, px = [], 100.0
    d = date(2026, 11, 11) - timedelta(days=n)
    for i in range(n):
        px *= 1.008 if i % 2 == 0 else 0.9945                 # up days and down days, drifting up: a trend with an ordinary RSI
        rows.append({"date": (d + timedelta(days=i)).isoformat(), "open": px * 0.998, "high": px * 1.004, "low": px * 0.994, "close": px})
    # the last closed day pauses, so today's push is a fresh confirmation
    rows[-1].update(open=rows[-2]["close"], high=rows[-2]["close"] * 1.001, low=rows[-2]["close"] * 0.992, close=rows[-2]["close"] * 0.996)
    return rows


def today_bar(rows, pct=1.2):
    c = rows[-1]["high"] * (1 + pct / 100)
    return {"date": DAY, "open": rows[-1]["close"], "high": c * 1.001, "low": rows[-1]["close"] * 0.999, "close": c}


def chain(spot, lot=100, delta=0.6, bid=20.0, ask=20.4, oi=500000, iv=14.0, expiries=("2026-11-23", EXP)):
    def make(expiry):
        return {"expiry": expiry, "expiries": list(expiries), "lot_size": lot, "forward": spot, "rows": [
            {"strike": round(spot * 0.97), "ce": {"tradingsymbol": "AAA26DEC97CE", "bid": bid, "ask": ask, "ltp": ask, "oi": oi, "iv": iv, "delta": delta}, "pe": {"tradingsymbol": "AAA26DEC97PE", "bid": 2, "ask": 2.1, "ltp": 2, "oi": oi, "iv": iv, "delta": -0.2}},
            {"strike": round(spot * 1.05), "ce": {"tradingsymbol": "AAA26DEC105CE", "bid": 4, "ask": 4.2, "ltp": 4, "oi": oi, "iv": iv, "delta": 0.3}, "pe": {"tradingsymbol": "AAA26DEC105PE", "bid": 30, "ask": 30.5, "ltp": 30, "oi": oi, "iv": iv, "delta": -0.7}}]}
    return lambda sym, expiry=None: make(expiry or expiries[0])


def desk(tmp_path, rows, ch=None, hist=None, bars=None, pct=1.2):
    sent = []
    d = ps.SwingDesk(cache_dir=tmp_path, universe=lambda: ["AAA"], daily=lambda s: rows, today=lambda s: today_bar(rows, pct), chain=ch or chain(rows[-1]["close"]),
                     bars=bars or (lambda k: {}), send=lambda mid, text, now=None: sent.append((mid, text)) or True)
    d.sent = sent
    if hist is not None:
        d.history = lambda way, session, charts: hist
    return d


GOOD = {"sample": 120, "names": 200, "target_first_pct": 30.0, "won_pct": 55.0, "avg_R": 0.25}


def test_there_is_no_order_path():
    assert not re.search(r"place_order|modify_order|cancel_order|place_gtt|orders/place", SRC)


def test_the_read_needs_every_timeframe_and_only_fires_on_the_first_day():
    rows = uptrend()
    full = rows + [today_bar(rows)]
    ind = ps.indicators(full)
    t = ps.read(ind, len(full) - 1, 1)
    assert t["weekly"] and t["daily"] and t["momentum"] and t["confirm"] and t["room"] and t["ok"]
    assert ps.fresh(ind, len(full) - 1, 1) and ps.read(ind, len(full) - 1, -1)["ok"] is False
    flat = rows + [dict(today_bar(rows), close=rows[-1]["close"])]                       # today doesn't clear yesterday's high
    assert ps.read(ps.indicators(flat), len(flat) - 1, 1)["confirm"] is False
    assert ps.read(ps.indicators(rows[:100]), 99, 1) is None                              # too little history for the 30-week trend


def test_a_clean_setup_buys_a_call_with_three_weeks_or_more_and_goes_to_telegram(tmp_path):
    rows = uptrend()
    d = desk(tmp_path, rows, hist=GOOD)
    rec = d.scan(NOW)
    c = rec["candidates"][0]
    assert c["decision"] == "traded" and [s["agent"] for s in c["steps"]] == ["scanner", "trend", "options", "timemachine", "budget", "risk", "broker"]
    assert c["expiry"] == EXP and c["days_to_expiry"] >= ps.MIN_DTE and ps.DELTA_LOW <= c["delta"] <= ps.DELTA_HIGH and c["contract"].endswith("CE")
    assert c["stop"] < c["entry"] < c["target"] and c["max_loss"] <= ps.MAX_LOSS and c["qty"] == c["lots"] * 100
    p = d.positions()[0]
    assert p["status"] == "open" and p["side"] == "LONG" and p["instrument"] == c["contract"]
    assert len(d.sent) == 1 and "Agent Pipeline" in d.sent[0][1] and "Swing" in d.sent[0][1]
    assert d.scan(NOW)["candidates"] == rec["candidates"] and len(d.positions()) == 1 and len(d.sent) == 1          # a second scan takes nothing twice


@pytest.mark.parametrize("kw,agent,text", [
    ({"expiries": ("2026-11-23",)}, "options", "No expiry between"),
    ({"delta": 0.3}, "options", "delta between"),
    ({"bid": 18.0, "ask": 20.4}, "options", "spread"),
    ({"oi": 500}, "options", "open interest"),
    ({"iv": 80.0}, "options", "implied volatility"),
    ({"lot": 5000}, "budget", "One lot"),
])
def test_the_option_and_budget_checks_stop_it_before_the_broker(tmp_path, kw, agent, text):
    rows = uptrend()
    d = desk(tmp_path, rows, ch=chain(rows[-1]["close"], **kw), hist=GOOD)
    c = d.scan(NOW)["candidates"][0]
    assert c["decision"] == "skipped" and c["steps"][-1]["agent"] == agent and text in c["why"] and "broker" not in [s["agent"] for s in c["steps"]]
    assert d.positions() == [] and d.sent == []


@pytest.mark.parametrize("hist,text", [({**GOOD, "sample": 12}, "under the"), ({**GOOD, "avg_R": -0.05}, "not above zero")])
def test_the_time_machine_gate_needs_enough_history_and_a_positive_average(tmp_path, hist, text):
    rows = uptrend()
    c = desk(tmp_path, rows, hist=hist).scan(NOW)["candidates"][0]
    assert c["decision"] == "skipped" and c["steps"][-1]["agent"] == "timemachine" and text in c["why"]


def test_the_history_replays_the_same_rule_and_never_reads_past_the_end():
    rows = uptrend(260)
    h = ps.SwingDesk(cache_dir=Path("x")).history(1, DAY, {"AAA": rows})
    assert set(h) == {"sample", "names", "target_first_pct", "won_pct", "avg_R"} and h["names"] == 1
    ind = ps.indicators(rows)
    assert ps.outcome(rows, ind, len(rows) - 5, 1) is None


def test_no_setup_means_no_candidate(tmp_path):
    rows = uptrend()
    d = desk(tmp_path, rows, hist=GOOD, pct=-0.5)                                         # today stays under yesterday's high
    rec = d.scan(NOW)
    assert rec["candidates"] == [] and rec["scanned"] == 1 and rec["setups"] == 0


def test_an_open_trade_is_followed_to_its_stop_target_and_time_limits(tmp_path):
    rows = uptrend()
    d = desk(tmp_path, rows, hist=GOOD)
    d.scan(NOW)
    p = d.positions()[0]
    nxt = datetime(2026, 11, 13, 9, 26, tzinfo=common.IST)
    d.bars = lambda k: {("2026-11-13", "09:15"): (p["entry"], p["target"] + 1, p["entry"] - 0.1, p["target"])}
    d.follow(nxt)
    q = d.positions()[0]
    assert q["status"] == "closed" and q["exit_reason"] == "target" and q["net"] > 0 and q["sessions"] == 2 and q["exit_day"] == "2026-11-13"

    d2 = desk(tmp_path / "b", rows, hist=GOOD)
    d2.scan(NOW)
    p = d2.positions()[0]
    d2.bars = lambda k: {("2026-11-13", "09:15"): (p["stop"] - 2, p["stop"] - 1, p["stop"] - 3, p["stop"] - 2)}
    d2.follow(nxt)
    q = d2.positions()[0]
    assert q["exit_reason"] == "stop" and q["exit"] < p["stop"] and q["net"] < 0                                     # a gap through the stop fills at the open

    d3 = desk(tmp_path / "c", rows, hist=GOOD)
    d3.scan(NOW)
    rows3 = d3.positions()
    rows3[0]["sessions"] = ps.HOLD_SESSIONS - 1
    d3._write("positions.json", rows3)
    mid = (rows3[0]["entry"] + rows3[0]["stop"]) / 2 + 2
    d3.bars = lambda k: {("2026-11-13", "15:10"): (mid, mid + 0.1, mid - 0.1, mid)}
    d3.follow(datetime(2026, 11, 13, 15, 16, tzinfo=common.IST))
    assert d3.positions()[0]["exit_reason"] == f"{ps.HOLD_SESSIONS} sessions"


def test_decay_and_the_volatility_proxy():
    assert ps.decay_pct(20.0, 1000.0, 1000.0, 45, 25.0, True, 21) > ps.decay_pct(20.0, 1000.0, 1000.0, 90, 25.0, True, 21) > 0
    assert ps.decay_pct(20.0, 1000.0, 1000.0, 10, 25.0, True, 21) is None
    closes = [r["close"] for r in uptrend()]
    assert 0 <= ps.iv_percentile(closes, 5.0) <= ps.iv_percentile(closes, 90.0) <= 100


