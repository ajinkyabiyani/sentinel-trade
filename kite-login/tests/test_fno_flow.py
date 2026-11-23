"""F&O flow: futures OI build-up (near plus next month), volume surges and breadth from Kite quotes. Plain code, no Claude call."""
from datetime import date, datetime

import pytest

from strategies import fno_flow as ff

DAY = date(2026, 11, 19)
CONS = {"AAA": [{"key": "NFO:AAA26NOVFUT", "token": 1}, {"key": "NFO:AAA26DECFUT", "token": 2}],
        "BBB": [{"key": "NFO:BBB26NOVFUT", "token": 3}, {"key": "NFO:BBB26DECFUT", "token": 4}]}
PREV = {1: 900_000, 2: 100_000, 3: 500_000, 4: 0}          # yesterday's closing OI per contract: AAA 1.0M, BBB 0.5M


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "DIR", tmp_path)
    monkeypatch.setattr(ff, "contracts", lambda names, today: {k: v for k, v in CONS.items() if k in names})
    monkeypatch.setattr(ff, "history_source", lambda token, frm, to, interval, oi: [{"date": "2026-11-18 15:25:00+05:30", "close": 100.0, "oi": PREV[token], "volume": 0}])
    monkeypatch.setattr(ff, "equity_tokens", lambda names: {})
    ff._state.clear()
    ff._state["day"] = None
    yield
    ff._state.clear()
    ff._state["day"] = None


def at(hh, mm, ss=30):
    return datetime(2026, 11, 19, hh, mm, ss)


def market(monkeypatch, aaa, bbb=(200.0, 500_000, 0, 200.0, 1_000)):
    """aaa / bbb = (futures price, near OI, next OI, cash price, cash cumulative volume)."""
    def q(keys):
        out = {}
        for name, (fp, oi1, oi2, cp, vol), pc in (("AAA", aaa, 100.0), ("BBB", bbb, 200.0)):
            out[f"NFO:{name}26NOVFUT"] = {"last_price": fp, "oi": oi1, "ohlc": {"close": pc}}
            out[f"NFO:{name}26DECFUT"] = {"last_price": fp, "oi": oi2, "ohlc": {"close": pc}}
            out[f"NSE:{name}"] = {"last_price": cp, "volume": vol, "ohlc": {"close": pc}}
        return {k: v for k, v in out.items() if k in keys}
    monkeypatch.setattr(ff, "quote_source", q)


def run(monkeypatch, path):
    """path: [(hh, mm, aaa_tuple)], one poll each."""
    for hh, mm, aaa in path:
        market(monkeypatch, aaa)
        ff.poll(at(hh, mm), ["AAA", "BBB"], force=True)
    th = [t for t in __import__("threading").enumerate() if t.name == "flow-prev-oi"]
    for t in th:
        t.join(5)


def test_classify_the_four_cases():
    assert ff.classify(1.0, 5.0) == "long build-up"
    assert ff.classify(-1.0, 5.0) == "short build-up"
    assert ff.classify(1.0, -5.0) == "short covering"
    assert ff.classify(-1.0, -5.0) == "long unwinding"
    assert ff.classify(1.0, 0.5) == "no real change in OI"
    assert ff.classify(None, 5.0) == "no real change in OI"


def test_oi_adds_near_and_next_month_and_compares_with_yesterday(monkeypatch):
    run(monkeypatch, [(10, 0, (104.0, 800_000, 350_000, 104.0, 10_000)), (10, 5, (104.0, 800_000, 350_000, 104.0, 20_000))])
    r = ff.of("AAA", at(10, 10))
    assert r["oi_pct"] == 15.0 and r["price_pct"] == 4.0 and r["today"] == "long build-up"   # 1.15M vs 1.0M: rollover isn't read as closing
    b = ff.board(at(10, 10))
    assert [x["name"] for x in b["futures_oi_today"]["long_buildup"]] == ["AAA"]


def test_fresh_build_up_in_the_last_hour_fires_once_then_rearms(monkeypatch):
    path = [(10, m, (100.0, 1_000_000, 0, 100.0, 1000 * m)) for m in range(0, 60, 5)]
    path += [(11, 0, (101.3, 1_070_000, 0, 101.3, 70_000))]
    run(monkeypatch, path)
    state = {}
    ev = ff.events(at(11, 5), set(), state)
    keys = [k for k, _ in ev]
    assert "AAA|oi|long|1" in keys and "fresh long build-up" in dict(ev)["AAA|oi|long|1"]
    assert not [k for k, _ in ff.events(at(11, 5), set(keys), state) if "|oi|" in k]          # still running: no repeat
    assert all("BBB" not in k for k in keys)                                                   # flat OI and price: nothing


def test_short_build_up_fires_down(monkeypatch):
    path = [(10, m, (100.0, 1_000_000, 0, 100.0, 1000 * m)) for m in range(0, 60, 5)] + [(11, 0, (98.7, 1_070_000, 0, 98.7, 70_000))]
    run(monkeypatch, path)
    assert "AAA|oi|short|1" in [k for k, _ in ff.events(at(11, 5), set(), {})]


def test_oi_without_a_price_move_is_not_an_event(monkeypatch):
    path = [(10, m, (100.0, 1_000_000, 0, 100.0, 1000 * m)) for m in range(0, 60, 5)] + [(11, 0, (100.2, 1_080_000, 0, 100.2, 70_000))]
    run(monkeypatch, path)
    assert not [k for k, _ in ff.events(at(11, 5), set(), {}) if "|oi|" in k]


def test_a_volume_surge_with_a_move_fires_on_the_last_closed_candle(monkeypatch):
    vol, path = 0, []
    for m in range(0, 45, 5):
        vol += 1000
        path.append((10, m, (100.0, 1_000_000, 0, 100.0, vol)))
    path.append((10, 45, (100.9, 1_000_000, 0, 100.9, vol + 6000)))                          # 6x the usual candle, +0.9%
    run(monkeypatch, path)
    ev = dict(ff.events(at(10, 50), set(), {}))
    assert "AAA|vol|10:45" in ev and "6x its usual" in ev["AAA|vol|10:45"]
    assert ff.board(at(10, 50))["volume_surges_last_30m"][0]["name"] == "AAA"
    assert "AAA|vol|10:45" not in dict(ff.events(at(10, 55), set(), {}))                       # only on the candle just closed


def test_volume_without_a_price_move_is_not_a_surge(monkeypatch):
    vol, path = 0, []
    for m in range(0, 45, 5):
        vol += 1000
        path.append((10, m, (100.0, 1_000_000, 0, 100.0, vol)))
    path.append((10, 45, (100.1, 1_000_000, 0, 100.1, vol + 8000)))
    run(monkeypatch, path)
    assert ff.surges(at(10, 50)) == []


def test_breadth_now_and_an_hour_ago(monkeypatch):
    path = [(10, m, (99.0, 1_000_000, 0, 99.0, 1000)) for m in range(0, 60, 5)] + [(11, 0, (101.0, 1_000_000, 0, 101.0, 2000))]
    run(monkeypatch, path)
    b = ff.breadth(at(11, 5))
    assert b["now"]["up"] == 1 and b["now"]["total"] == 2 and b["an_hour_ago"]["down"] == 1      # AAA went from red to green; BBB flat


def test_a_restart_keeps_todays_flow(monkeypatch):
    run(monkeypatch, [(10, 0, (104.0, 800_000, 350_000, 104.0, 10_000))])
    ff._state.clear()
    ff._state["day"] = None
    ff._fresh("2026-11-19")
    assert ff._state["fut"]["AAA"]["10:00"]["oi"] == 1_150_000 and ff._state["prev_oi"]["AAA"] == 1_000_000


def test_poll_paces_itself(monkeypatch):
    calls = []
    market(monkeypatch, (100.0, 1, 0, 100.0, 1))
    q = ff.quote_source
    monkeypatch.setattr(ff, "quote_source", lambda keys: calls.append(1) or q(keys))
    ff.poll(at(10, 0), ["AAA"])
    ff.poll(at(10, 0, 45), ["AAA"])
    assert len(calls) == 1


def test_claude_trader_reads_the_flow_and_flags_its_stocks():
    from strategies import claude_trader as ct
    assert "fno_flow" in open(ct.__file__, encoding="utf-8").read()
    assert ct.flagged_names([("AAA|oi|long|1", "x"), ("BBB|vol|10:45", "y"), ("NIFTY|oi|long|1", "z")]) == ["AAA", "BBB"]


def test_backfill_fills_the_gap_a_restart_left_and_never_overwrites_live_buckets(monkeypatch):
    hist = {1: [{"date": "2026-11-18 15:25:00+05:30", "close": 100.0, "oi": 900_000},
                {"date": "2026-11-19 09:15:00+05:30", "close": 101.0, "oi": 950_000},
                {"date": "2026-11-19 09:20:00+05:30", "close": 102.0, "oi": 990_000}],
            2: [{"date": "2026-11-18 15:25:00+05:30", "close": 100.0, "oi": 100_000},
                {"date": "2026-11-19 09:15:00+05:30", "close": 101.0, "oi": 110_000},
                {"date": "2026-11-19 09:20:00+05:30", "close": 102.0, "oi": 120_000}],
            9: [{"date": "2026-11-18 15:25:00+05:30", "close": 50.0, "volume": 10},
                {"date": "2026-11-19 09:15:00+05:30", "close": 51.0, "volume": 300},
                {"date": "2026-11-19 09:20:00+05:30", "close": 52.0, "volume": 200}]}
    monkeypatch.setattr(ff, "history_source", lambda token, frm, to, interval, oi: hist.get(token, []))
    monkeypatch.setattr(ff, "contracts", lambda names, today: {"AAA": CONS["AAA"]})
    monkeypatch.setattr(ff, "equity_tokens", lambda names: {"AAA": 9})
    run(monkeypatch, [(9, 20, (102.5, 1_000_000, 125_000, 52.5, 600))])     # the live pass lands in 09:20
    f = ff._state["fut"]["AAA"]
    assert f["09:15"] == {"p": 101.0, "oi": 1_060_000, "pc": 100.0}          # filled from history, near plus next
    assert f["09:20"]["p"] == 102.5 and f["09:20"]["oi"] == 1_125_000         # the live bucket stays
    assert ff._state["prev_oi"]["AAA"] == 1_060_000                         # today's first reading, not yesterday's stale 5-minute OI
    assert ff._state["cash"]["AAA"]["09:15"] == {"p": 51.0, "v": 300, "pc": 50.0}


def test_a_thin_next_month_with_no_candle_keeps_its_last_oi(monkeypatch):
    hist = {1: [{"date": "2026-11-18 15:25:00+05:30", "close": 100.0, "oi": 900_000},
                {"date": "2026-11-19 09:15:00+05:30", "close": 101.0, "oi": 950_000},
                {"date": "2026-11-19 09:20:00+05:30", "close": 102.0, "oi": 990_000}],
            2: [{"date": "2026-11-18 15:25:00+05:30", "close": 100.0, "oi": 100_000},
                {"date": "2026-11-19 09:20:00+05:30", "close": 102.0, "oi": 130_000}]}     # no trade in the 09:15 slot
    monkeypatch.setattr(ff, "history_source", lambda token, frm, to, interval, oi: hist.get(token, []))
    monkeypatch.setattr(ff, "contracts", lambda names, today: {"AAA": CONS["AAA"]})
    run(monkeypatch, [(9, 30, (102.5, 1_000_000, 130_000, 52.5, 600))])
    f = ff._state["fut"]["AAA"]
    assert f["09:15"]["oi"] == 1_050_000 and f["09:20"]["oi"] == 1_120_000                # 950k + yesterday's 100k, not 950k + 0


def test_a_build_up_the_whole_market_shares_is_not_an_event(monkeypatch):
    """In the first hour every name builds OI; a name has to beat the median F&O name."""
    def q(keys, oi_a, oi_b, pa):
        return {"NFO:AAA26NOVFUT": {"last_price": pa, "oi": oi_a, "ohlc": {"close": 100.0}}, "NFO:AAA26DECFUT": {"last_price": pa, "oi": 0, "ohlc": {"close": 100.0}},
                "NFO:BBB26NOVFUT": {"last_price": 200.0, "oi": oi_b, "ohlc": {"close": 200.0}}, "NFO:BBB26DECFUT": {"last_price": 200.0, "oi": 0, "ohlc": {"close": 200.0}},
                "NSE:AAA": {"last_price": pa, "volume": 1, "ohlc": {"close": 100.0}}, "NSE:BBB": {"last_price": 200.0, "volume": 1, "ohlc": {"close": 200.0}}}
    for m in range(0, 60, 5):
        monkeypatch.setattr(ff, "quote_source", lambda keys: q(keys, 1_000_000, 1_000_000, 100.0))
        ff.poll(at(10, m), ["AAA", "BBB"], force=True)
    monkeypatch.setattr(ff, "quote_source", lambda keys: q(keys, 1_080_000, 1_050_000, 101.5))   # AAA +8%, but BBB +5% too
    ff.poll(at(11, 0), ["AAA", "BBB"], force=True)
    assert not [k for k, _ in ff.events(at(11, 5), set(), {}) if "|oi|" in k]                       # only 3% more than the market
