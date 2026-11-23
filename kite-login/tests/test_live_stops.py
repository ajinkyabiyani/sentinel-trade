"""Real-time stops: a paper trade closes as soon as Kite's last price crosses its stop, without waiting for the 5-minute candle."""
from datetime import datetime

import pytest

from strategies import agent_desk as ad, live_stops, multi_agent_team as mat, pipeline_swing as ps

DAY = "2026-11-19"
MID = datetime(2026, 11, 19, 10, 7, 20)          # a Thursday, inside the 10:05 candle


def prices(monkeypatch, table):
    monkeypatch.setattr(live_stops, "source", lambda keys: {k: table[k] for k in keys if k in table})


def test_hit_long_short_and_none():
    assert live_stops.hit(79.5, 80, 150) == ("stop", 79.5)
    assert live_stops.hit(80, 80, 150) == ("stop", 80)
    assert live_stops.hit(151, 80, 150) == ("target", 150.0)
    assert live_stops.hit(100, 80, 150) is None
    assert live_stops.hit(None, 80, 150) is None
    assert live_stops.hit(121, 120, 90, short=True) == ("stop", 121)
    assert live_stops.hit(89, 120, 90, short=True) == ("target", 90.0)


def test_prices_only_in_market_hours(monkeypatch):
    prices(monkeypatch, {"NFO:X": 50.0})
    assert live_stops.prices(["NFO:X"], MID) == {"NFO:X": 50.0}
    assert live_stops.prices(["NFO:X"], datetime(2026, 11, 19, 9, 10)) == {}
    assert live_stops.prices(["NFO:X"], datetime(2026, 11, 19, 15, 31)) == {}
    assert live_stops.prices(["NFO:X"], datetime(2026, 11, 21, 11, 0)) == {}       # Saturday


def test_kite_failure_is_not_fatal(monkeypatch):
    def boom(keys):
        raise RuntimeError("network")
    monkeypatch.setattr(live_stops, "source", boom)
    assert live_stops.prices(["NFO:X"], MID) == {}


def test_multi_agent_team_long_and_short(monkeypatch):
    e = {"key": "NFO:BANKNIFTY26N50000PE", "instrument": "BANKNIFTY26N50000PE", "kind": "index", "entry": 200.0, "stop": 160.0, "target": 280.0, "qty": 30, "side": "LONG", "status": "open"}
    prices(monkeypatch, {"NFO:BANKNIFTY26N50000PE": 158.0})
    mat.live_check(e, MID)
    assert e["status"] == "closed" and e["exit_reason"] == "stop" and e["exit_at"] == "10:07"
    s = {"key": "NSE:AAA", "instrument": "AAA", "kind": "cash", "entry": 100.0, "stop": 102.0, "target": 96.0, "qty": 10, "side": "SHORT", "status": "open"}
    prices(monkeypatch, {"NSE:AAA": 101.0})
    mat.live_check(s, MID)
    assert s["status"] == "open" and s["ltp"] == 101.0
    prices(monkeypatch, {"NSE:AAA": 102.5})
    mat.live_check(s, MID)
    assert s["status"] == "closed" and s["exit_reason"] == "stop" and s["net"] < 0


def test_swing_desk_live_stop(monkeypatch):
    p = {"key": "NFO:AAA26N100CE", "entry": 10.0, "stop": 7.0, "target": 16.0, "qty": 100, "status": "open"}
    d = ps.SwingDesk.__new__(ps.SwingDesk)
    assert d._live(p, 6.8, MID, 0.0) and p["status"] == "closed" and p["exit_reason"] == "stop" and p["exit"] == 6.8
    q = dict(p, status="open")
    q.pop("exit")
    assert d._live(q, 9.0, MID, 0.0) and q["status"] == "open" and q["ltp"] == 9.0


CAND = {"symbol": "AAA", "at": "9:35 AM", "bar_start": "09:30", "close_time": "09:35", "entry": 100.0, "stop": 99.0, "target": 102.0, "qty": 50}


def test_agent_pipeline_simulate_keeps_a_live_exit():
    c = dict(CAND, live_exit={"price": 98.8, "at": "09:37", "reason": "stop"})
    t = ad.simulate(c, {"09:35": (100.0, 100.5, 99.5, 100.2), "09:40": (100, 103, 99.6, 102.5)}, "09:40", slip=0)
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and t["exit_time"] == "09:37" and t["exit_ref"] == 98.8
    t = ad.simulate(c, {}, "09:35", slip=0)                       # no candle yet: the live exit still books it
    assert t["status"] == "closed" and t["exit_ref"] == 98.8


def test_agent_pipeline_watch_books_the_stop(monkeypatch, tmp_path):
    put = {}
    monkeypatch.setattr(ad.paper_desk, "put_session", lambda desk, session, rows, note: put.update(rows=rows))
    d = ad.AgentDesk(cache_dir=tmp_path, bars=lambda s: {}, symbols=lambda: [], desk="pipeline_test")
    c = dict(CAND, decision="traded")
    d.snapshot = {"session": DAY, "candidates": [c], "options": [], "last_bar": "09:35", "counted": True,
                  "trades": [ad.simulate(c, {}, "09:35")], "option_trades": []}
    assert d.snapshot["trades"][0]["status"] == "open"
    prices(monkeypatch, {"NSE:AAA": 100.4})
    assert not d.watch(datetime(2026, 11, 19, 9, 37))
    prices(monkeypatch, {"NSE:AAA": 98.9})
    assert d.watch(datetime(2026, 11, 19, 9, 38))
    t = d.snapshot["trades"][0]
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and t["exit_time"] == "09:38"
    assert put["rows"][0]["status"] == "closed"
    assert not d.watch(datetime(2026, 11, 19, 9, 39))            # booked once
