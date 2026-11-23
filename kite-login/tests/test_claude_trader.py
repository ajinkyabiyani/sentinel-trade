"""Claude Trader: Claude's own option-buying desk. The code enforces risk on Claude's answer and follows the trade; paper only."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from strategies import claude_trader as ct, live_stops

DAY = "2026-11-19"
NOW = datetime(2026, 11, 19, 10, 0, 5)


def chain(bid=99.0, ask=101.0, oi=500000, lot=65):
    rows = []
    for k in (25900, 25950, 26000, 26050, 26100, 26150, 26200):
        rows.append({"strike": k, "ce": {"tradingsymbol": f"NIFTY26N{k}CE", "ltp": 100, "bid": bid, "ask": ask, "oi": oi}, "pe": {"tradingsymbol": f"NIFTY26N{k}PE", "ltp": 90, "bid": bid, "ask": ask, "oi": oi}})
    return {"expiry": "2026-11-24", "lot_size": lot, "atm": 26000, "rows": rows}


def ctx(kind="index", c=None, sym="NIFTY"):
    return {"underlyings": [{"underlying": sym, "kind": kind, "spot": 26000, "option_chain": c or chain()}]}


def buy(**kw):
    return {"action": "BUY", "underlying": "NIFTY", "instrument": "NIFTY26N26000CE", "entry": 100, "stop_loss": 80, "target": 150, "quantity": 650, "confidence": 60,
            "thesis": "trend day", "risks": [], "invalidation": "", "walkthrough": []} | kw


@pytest.fixture
def book(monkeypatch, tmp_path):
    monkeypatch.setattr(ct, "DIR", tmp_path)
    sent = []
    monkeypatch.setattr(ct.notify, "send_once", lambda mid, text, now=None: sent.append((mid, text)) or True)
    return sent


def test_it_is_a_desk_of_its_own():
    src = Path(ct.__file__).read_text(encoding="utf-8")
    assert "skill_desk" not in src and "multi_agent_team" not in src and "agent_desk" not in src
    assert ct.DIR.name == "claude_trader"


def test_a_good_buy_is_sized_to_the_risk_cap(book):
    r = ct.check(buy(), ctx(), NOW, lambda k: 100.0, [])
    assert r["ok"], r
    e = r["position"]
    assert e["qty"] % 65 == 0 and e["max_loss"] <= ct.MAX_RISK and e["entry"] == pytest.approx(100.3)
    assert e["key"] == "NFO:NIFTY26N26000CE" and e["slip"] == 0.003


@pytest.mark.parametrize("change, why", [
    ({"action": "SKIP"}, "stay out"),
    ({"instrument": "NIFTY26N99999CE"}, "isn't in"),
    ({"instrument": "NIFTY26N26150CE"}, "strikes from the money"),
    ({"underlying": "RELIANCE"}, "wasn't in the snapshot"),
    ({"stop_loss": 101}, "isn't between"),
    ({"target": 120}, "under 1.5x"),
])
def test_each_limit_can_refuse(book, change, why):
    r = ct.check(buy(**change), ctx(), NOW, lambda k: 100.0, [])
    assert not r["ok"] and why in r["why"]


def test_liquidity_time_and_book_limits(book):
    assert "spread" in ct.check(buy(), ctx(c=chain(bid=90, ask=110)), NOW, lambda k: 100.0, [])["why"]
    assert "open interest" in ct.check(buy(underlying="ABC"), ctx(kind="stock", c=chain(oi=100), sym="ABC"), NOW, lambda k: 100.0, [])["why"]
    assert "after" in ct.check(buy(), ctx(), datetime(2026, 11, 19, 14, 35), lambda k: 100.0, [])["why"]
    held = [{"status": "open", "underlying": "NIFTY", "entry": 100, "qty": 65, "slip": 0.003, "day": DAY}]
    assert "already holds" in ct.check(buy(), ctx(), NOW, lambda k: 100.0, held)["why"]
    lost = [{"status": "closed", "underlying": "X", "net": -9000, "exit_day": DAY, "slip": 0.01}]
    assert "day limit" in ct.check(buy(), ctx(), NOW, lambda k: 100.0, lost)["why"]


def test_a_name_that_stopped_out_today_is_off_limits_until_tomorrow(book):
    stopped = {"status": "closed", "underlying": "NIFTY", "exit_reason": "stop", "net": -1500, "day": DAY, "exit_day": DAY, "slip": 0.003}
    assert "no second try" in ct.check(buy(), ctx(), NOW, lambda k: 100.0, [stopped])["why"]
    assert ct.check(buy(), ctx(), NOW, lambda k: 100.0, [dict(stopped, exit_reason="target", net=3000)])["ok"]         # a winner may be traded again
    assert ct.check(buy(), ctx(), NOW, lambda k: 100.0, [dict(stopped, day="2026-11-18", exit_day="2026-11-18")])["ok"]  # yesterday's stop doesn't count
    assert ct.check(buy(underlying="BANKNIFTY"), ctx(sym="BANKNIFTY"), NOW, lambda k: 100.0, [stopped])["ok"]                # other names are untouched


def test_schedule_every_15_minutes_once_each():
    assert ct.slots()[0] == "09:30" and ct.slots()[-1] == "14:30" and len(ct.slots()) == 21


def test_due_slot_runs_once_and_skips_late_looks(book):
    assert ct.due_slot(datetime(2026, 11, 19, 9, 31)) == "09:30"
    ct._set_state(day=DAY, done=["09:30"])
    assert ct.due_slot(datetime(2026, 11, 19, 9, 31)) is None
    assert ct.due_slot(datetime(2026, 11, 19, 9, 57)) is None          # 12 minutes after 09:45: skipped, not run late
    assert ct.due_slot(datetime(2026, 11, 19, 9, 20)) is None


def pos(**kw):
    return {"id": "p1", "underlying": "NIFTY", "instrument": "NIFTY26N26000CE", "key": "NFO:NIFTY26N26000CE", "entry": 100.3, "stop": 80.0, "target": 150.0, "qty": 130, "lots": 2,
            "slip": 0.003, "status": "open", "day": DAY, "entry_at": "10:00", "entry_bar": "10:00", "ltp": 100.0, "max_loss": 2700.0, "expiry": "2026-11-24"} | kw


def test_live_stop_closes_and_telegram_gets_the_exit(book, monkeypatch):
    ct._save_positions([pos()])
    monkeypatch.setattr(ct, "bars_of", lambda key, day: {"10:00": (100, 102, 95, 98)})
    monkeypatch.setattr(live_stops, "source", lambda keys: {"NFO:NIFTY26N26000CE": 79.0})
    ct.follow(datetime(2026, 11, 19, 10, 7, 30))
    e = ct.positions()[0]
    assert e["status"] == "closed" and e["exit_reason"] == "stop" and e["exit_at"] == "10:07" and e["net"] < 0
    assert book and book[-1][0] == "claude_trader|p1|exit" and "Stop hit" in book[-1][1]


def test_target_on_candles_and_1515_exit(book):
    e = pos()
    ct.walk(e, {"10:00": (100, 104, 99, 103), "10:05": (103, 151, 102, 149)}, DAY, "10:05")
    assert e["exit_reason"] == "target" and e["net"] > 0
    e = pos()
    ct.walk(e, {t: (100, 101, 99, 100) for t in ct.common.LABELS if t >= "10:00"}, DAY, "15:25")
    assert e["exit_reason"] == "15:15 exit" and e["exit_at"] == "15:15"


def test_a_trade_left_from_an_earlier_day_is_closed(book, monkeypatch):
    ct._save_positions([pos(day="2026-11-18")])
    monkeypatch.setattr(ct, "bars_of", lambda key, day: {})
    ct.follow(datetime(2026, 11, 19, 9, 30, 0))
    assert ct.positions()[0]["status"] == "closed"


def test_a_look_books_a_trade_and_sends_the_entry(book, monkeypatch):
    monkeypatch.setattr(ct, "snapshot", lambda now, progress=None, extra=(): {**ctx(), "india_vix": 14.0, "not_available": []})
    monkeypatch.setattr(ct, "ask", lambda c, tool, progress=None: buy())
    monkeypatch.setattr(ct, "live_price", lambda k: 100.0)
    monkeypatch.setattr(ct, "clock", lambda: NOW.replace(tzinfo=ct.common.IST))
    ct._work(NOW, "claude", "10:00")
    assert ct.positions()[0]["instrument"] == "NIFTY26N26000CE"
    assert ct.runs(DAY)[0]["paper"]["taken"]
    assert book[0][0].endswith("|open") and "BUY CALL" in book[0][1] and "Claude Trader" in book[0][1]


def test_a_skip_books_nothing_and_sends_nothing(book, monkeypatch):
    monkeypatch.setattr(ct, "snapshot", lambda now, progress=None, extra=(): {**ctx(), "india_vix": 14.0, "not_available": []})
    monkeypatch.setattr(ct, "ask", lambda c, tool, progress=None: buy(action="SKIP", thesis="choppy, no edge"))
    monkeypatch.setattr(ct, "clock", lambda: NOW.replace(tzinfo=ct.common.IST))
    ct._work(NOW, "claude", "10:00")
    assert ct.positions() == [] and book == []
    assert "stayed out" in ct.activity(DAY)[-1]["text"]


def test_walkthrough_is_read_while_claude_writes():
    buf = '{"walkthrough":[{"role":"market","verdict":"neutral","finding":"Nifty flat, VIX 13"},{"role":"candidates","verdict":"supports","finding":"HDFC'
    done, writing = ct.steps_so_far(buf)
    assert done[0]["role"] == "market" and writing == {"role": "candidates", "finding": "HDFC"}


def test_view_shape(book):
    v = ct.view(DAY)
    assert v["paper_mode"] and v["name"] == "Claude Trader" and v["schedule"]["looks"] == 21
    json.dumps(v)


def test_greeks_are_sensible():
    g = ct.greeks(25300, 25300, 4 / 365.25, 13.0, True)
    assert -40 < g["theta_day"] < -10 and 0 < g["gamma"] < 0.01 and 5 < g["vega"] < 20
    assert ct.greeks(25300, 25300, 4 / 365.25, None, True) == {}


def test_chain_rows_carry_greeks_and_spread():
    c = ct.trim_chain({**chain(), "days_to_expiry": 4, "forward": 26010, "rows": [{**r, "ce": {**r["ce"], "iv": 14.0}} for r in chain()["rows"]]})
    atm = next(r for r in c["rows"] if r["strike"] == 26000)
    assert atm["ce"]["theta_day"] < 0 and atm["ce"]["spread_pct"] == 2.0 and "theta_day" not in atm["pe"]


def test_realised_vol_and_playbook_in_the_prompt():
    assert ct.realised_vol([100 * (1.01 if i % 2 else 0.99) ** 1 for i in range(25)]) > 0
    p = ct.system_prompt()
    assert "Claude Trader playbook" in p and "Nifty weekly expires on Tuesday" in p and "theta" in p


# ---------------------------------------------------------------- early looks

def bar(start, o, h, l, c):
    return {"start": start, "o": o, "h": h, "l": l, "c": c}


def quiet_day(n=6, px=100.0):
    return [bar(ct.common.plus_minutes("09:15", 5 * i), px, px + 0.1, px - 0.1, px) for i in range(n)]


def test_index_close_through_opening_range_is_an_event():
    today = quiet_day(6)
    today[-1] = bar(today[-1]["start"], 100, 100.4, 99.9, 100.3)      # closes above the 100.1 opening-range high
    ev = ct.candle_events("NIFTY", "index", today, [])
    assert any("opening-range high" in w for _, w in ev)
    assert ct.candle_events("NIFTY", "index", quiet_day(6), []) == []


def test_big_candle_on_a_stock_is_an_event_but_noise_is_not():
    today = quiet_day(8)
    today[-1] = bar(today[-1]["start"], 100, 101.2, 99.95, 101.1)     # 1.25% range, ~6x the day's usual
    assert any("candle up" in w for _, w in ct.candle_events("ABC", "stock", today, []))
    today[-1] = bar(today[-1]["start"], 100, 100.5, 99.95, 100.4)     # bigger than usual but under 0.8% of price
    assert ct.candle_events("ABC", "stock", today, []) == []


def test_early_look_fires_once_per_event_and_respects_the_gap(book, monkeypatch):
    started = []
    monkeypatch.setattr(ct, "start", lambda now, slot, early=None, extra=(): started.append((now.strftime("%H:%M"), slot, early)))
    monkeypatch.setattr(ct, "follow", lambda now: None)
    events = [("NIFTY|opening-range high|1", "NIFTY closed above its opening-range high")]
    monkeypatch.setattr(ct, "early_events", lambda now, seen, vix: ([e for e in events if e[0] not in seen], vix))
    ct._set_state(day=DAY, done=["09:30", "09:45", "10:00"], last_look="10:00", last_kind="scheduled")
    ct.tick(datetime(2026, 11, 19, 10, 5, 30))          # 5 min after the 10:00 look: inside the gap, noted only
    assert started == []
    assert "no early look" in ct.activity(DAY)[-1]["text"]
    events[:] = [("BANKNIFTY|previous-day high|1", "BANKNIFTY closed above its previous-day high")]
    ct.tick(datetime(2026, 11, 19, 10, 10, 30))         # 10 min after: fires
    assert started[-1][1] == "early" and "BANKNIFTY" in started[-1][2][0]
    ct.tick(datetime(2026, 11, 19, 10, 10, 50))         # same candle: not checked again
    assert len(started) == 1
    ct.tick(datetime(2026, 11, 19, 10, 15, 10))         # the 10:15 slot comes 5 min after the early look: skipped
    assert len(started) == 1 and "looked early" in ct.activity(DAY)[-1]["text"]


def test_no_early_look_when_the_book_is_full(book, monkeypatch):
    started = []
    monkeypatch.setattr(ct, "start", lambda now, slot, early=None, extra=(): started.append(slot))
    monkeypatch.setattr(ct, "follow", lambda now: None)
    monkeypatch.setattr(ct, "early_events", lambda now, seen, vix: ([("X|big|10:05", "X big candle")], vix))
    ct._save_positions([pos(id=f"p{i}", underlying=f"U{i}") for i in range(ct.MAX_OPEN)])
    ct._set_state(day=DAY, done=["10:00"], last_look="10:00", last_kind="scheduled")
    ct.tick(datetime(2026, 11, 19, 10, 10, 30))
    assert started == [] and "can't take a trade" in ct.activity(DAY)[-1]["text"]


# ---------------------------------------------------------------- the wide watch: every F&O stock on a price tape

def feed(monkeypatch, path):
    """Polls a fake tape: path is [(time, {name: price})], one Kite last-price call per entry."""
    monkeypatch.setattr(ct, "fno_source", lambda: ["AAA", "BBB"])
    for hhmmss, px in path:
        h, m, sec = map(int, hhmmss.split(":"))
        prices(monkeypatch, {f"NSE:{k}": v for k, v in px.items()})
        ct.tape_poll(datetime(2026, 11, 19, h, m, sec))


def prices(monkeypatch, table):
    monkeypatch.setattr(live_stops, "source", lambda keys: {k: table[k] for k in keys if k in table})


def quiet_path(until_min=40):
    """Both stocks wiggle 0.2 a candle from 09:15 to 09:{until_min}."""
    out = []
    for m in range(15, until_min, 5):
        for sec, d in ((5, 0.0), (65, 0.1), (125, -0.1), (185, 0.05)):
            mm, ss = m + sec // 60, sec % 60
            out.append((f"09:{mm:02d}:{ss:02d}", {"AAA": 100 + d, "BBB": 200 + d}))
    return out


def test_tape_builds_five_minute_candles_from_last_prices(monkeypatch):
    ct._tape.update(day=None, bars={})
    feed(monkeypatch, [("09:15:05", {"AAA": 100.0}), ("09:16:00", {"AAA": 101.0}), ("09:18:00", {"AAA": 99.5}), ("09:20:10", {"AAA": 100.2})])
    bars = ct.tape_bars("AAA", datetime(2026, 11, 19, 9, 21))
    assert bars == [{"start": "09:15", "o": 100.0, "h": 101.0, "l": 99.5, "c": 99.5}]       # the 09:20 candle is still forming
    assert ct._tape["bars"]["AAA"]["09:20"]["o"] == 99.5                                    # a new candle opens at the last close


def test_a_big_candle_on_any_fno_stock_is_flagged_and_a_quiet_one_is_not(monkeypatch):
    ct._tape.update(day=None, bars={})
    feed(monkeypatch, quiet_path() + [("09:40:30", {"AAA": 101.0, "BBB": 200.1}), ("09:43:00", {"AAA": 102.5, "BBB": 200.0})])
    ev = ct.wide_events(datetime(2026, 11, 19, 9, 45, 5), set())
    assert [k for k, _ in ev] == ["AAA|big|09:40"] and "AAA printed" in ev[0][1]
    assert ct.wide_events(datetime(2026, 11, 19, 9, 45, 5), {"AAA|big|09:40"}) == []        # once per candle
    assert ct.wide_events(datetime(2026, 11, 19, 9, 45, 5), set(), skip={"AAA"}) == []      # names on real candles are checked there


def test_a_flagged_stock_joins_claudes_read_for_that_look(monkeypatch):
    monkeypatch.setattr(ct, "stock_source", lambda: {"up": ["UP1"], "down": ["DN1"]})
    assert ct.universe(["AAA", "UP1"]) == ["NIFTY", "BANKNIFTY", "UP1", "DN1", "AAA"]


def test_the_early_look_passes_the_flagged_stock_to_claude(book, monkeypatch):
    got = {}
    monkeypatch.setattr(ct, "start", lambda now, slot, early=None, extra=(): got.update(slot=slot, extra=list(extra)))
    monkeypatch.setattr(ct, "follow", lambda now: None)
    monkeypatch.setattr(ct, "tape_poll", lambda now: None)
    monkeypatch.setattr(ct, "early_events", lambda now, seen, vix: ([("AAA|big|10:05", "AAA printed a 4.0x candle up"), ("NIFTY|previous-day high|1", "NIFTY closed above")], vix))
    ct._set_state(day=DAY, done=["10:00"], last_look="09:55", last_kind="scheduled")
    ct.tick(datetime(2026, 11, 19, 10, 10, 30))
    assert got == {"slot": "early", "extra": ["AAA"]}


# ---------------------------------------------------------------- sectors: the board and the sector-turn early look

def sbars(path, prev=100.0):
    """5-minute candles from closes; path[0] is the 09:15 close."""
    out, o = [], prev
    for i, c in enumerate(path):
        out.append({"start": f"{9 + (15 + 5 * i) // 60:02d}:{(15 + 5 * i) % 60:02d}", "o": o, "h": max(o, c), "l": min(o, c), "c": c})
        o = c
    return out


def flat(n, px=100.0):
    return [px] * n


def test_sector_rows_rank_and_need_a_full_hour():
    data = {"NIFTY 50": (sbars(flat(13)), 100.0), "NIFTY BANK": (sbars(flat(1) + [100 + 0.1 * i for i in range(1, 13)]), 100.0), "NIFTY IT": (sbars(flat(13, 101.0)), 100.0)}
    base, rows = ct.sector_rows(data)
    assert base["last_60m_pct"] == 0.0 and [r["sector"] for r in rows] == ["Banks", "IT"]
    bank = rows[0]
    assert bank["day_pct"] == 1.2 and bank["last_60m_pct"] == 1.2 and bank["rank"] == 1 and bank["rank_60m_ago"] == 2 and bank["full_hour"]
    _, early = ct.sector_rows({k: (v[0][:8], v[1]) for k, v in data.items()})
    assert not early[0]["full_hour"]


def test_a_sector_pulling_away_from_nifty_fires_once_then_rearms():
    base = {"last_60m_pct": 0.2}
    row = {"sector": "Private banks", "index": "NIFTY PVT BANK", "day_pct": 1.8, "last_60m_pct": 1.0, "day_pct_60m_ago": 0.8, "rank": 2, "rank_60m_ago": 2, "full_hour": True}
    state, seen = {}, set()
    ev = ct.sector_events(base, [row], seen, state)
    assert [k for k, _ in ev] == ["SECTOR|NIFTY PVT BANK|up|1"] and "gained 1.00% in the last hour" in ev[0][1]
    seen.add(ev[0][0])
    assert ct.sector_events(base, [row], seen, state) == []                                    # still running: no repeat
    ct.sector_events(base, [dict(row, last_60m_pct=0.2)], seen, state)                          # cools off
    assert [k for k, _ in ct.sector_events(base, [row], seen, state)] == ["SECTOR|NIFTY PVT BANK|up|2"]


def test_moving_with_nifty_is_not_a_turn_but_crossing_red_to_green_is():
    base = {"last_60m_pct": 0.8}
    row = {"sector": "PSU banks", "index": "NIFTY PSU BANK", "day_pct": 0.9, "last_60m_pct": 1.0, "day_pct_60m_ago": 0.1, "rank": 3, "rank_60m_ago": 3, "full_hour": True}
    assert ct.sector_events(base, [row], set(), {}) == []                                       # 1.0% with Nifty up 0.8%: just the market
    ev = ct.sector_events(base, [dict(row, day_pct=0.3, day_pct_60m_ago=-0.7)], set(), {})
    assert ev and "crossing from red to green" in ev[0][1]
    assert ct.sector_events(base, [dict(row, last_60m_pct=0.5, day_pct=0.3, day_pct_60m_ago=-0.2)], set(), {}) == []   # too small
    assert ct.sector_events(base, [dict(row, full_hour=False, day_pct=0.3, day_pct_60m_ago=-0.7)], set(), {}) == []    # before 10:15


def test_a_falling_sector_fires_down():
    ev = ct.sector_events({"last_60m_pct": 0.0}, [{"sector": "IT", "index": "NIFTY IT", "day_pct": 1.3, "last_60m_pct": -0.9, "day_pct_60m_ago": 2.2, "rank": 6, "rank_60m_ago": 2, "full_hour": True}], set(), {})
    assert [k for k, _ in ev] == ["SECTOR|NIFTY IT|down|1"] and "lost 0.90%" in ev[0][1]


def test_flagged_names_take_a_sectors_leaders_and_skip_indices(monkeypatch):
    monkeypatch.setitem(ct._sector_picks, "SECTOR|NIFTY PVT BANK|up|1", ["HDFCBANK", "AXISBANK"])
    ev = [("SECTOR|NIFTY PVT BANK|up|1", "x"), ("AAA|big|10:05", "y"), ("NIFTY|previous-day high|1", "z"), ("VIX|10:05", "v"), ("AXISBANK|big|10:05", "w")]
    assert ct.flagged_names(ev) == ["AAA", "AXISBANK", "HDFCBANK"]


def test_sector_stocks_are_fno_names_in_that_sector(monkeypatch):
    monkeypatch.setattr(ct.india, "app_get", lambda path: {"groups": {"Banks": ["HDFCBANK", "SBIN", "AXISBANK", "TINY"], "PSU Banks": ["SBIN"]},
                                                            "universe": {"HDFCBANK": [100, 103], "SBIN": [100, 105], "AXISBANK": [100, 101], "TINY": [100, 110]}})
    monkeypatch.setattr(ct, "fno_source", lambda: ["HDFCBANK", "SBIN", "AXISBANK"])
    assert [r["symbol"] for r in ct._sector_stocks("Private banks", True)] == ["HDFCBANK", "AXISBANK"]
    assert [r["symbol"] for r in ct._sector_stocks("Banks", False)][0] == "AXISBANK"
    assert ct._sector_stocks(None, True) == []
