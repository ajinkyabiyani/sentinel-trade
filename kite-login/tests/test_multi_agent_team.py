"""Multi Agent Team: automatic scans on indices and F&O stocks, a run, what's sent, the paper book, and that no order path exists."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from strategies import eod_report as eod, multi_agent_team as team

SRC = (Path(__file__).resolve().parents[1] / "backend" / "strategies" / "multi_agent_team.py").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=team.common.IST)
DAY = "2026-11-10"
BUY = {"action": "BUY", "instrument": "NIFTY26N1724900CE", "quantity": 65, "order_type": "MARKET", "limit_price": 0, "stop_loss": 80.0, "target": 140.0, "rationale": "aligned", "entry": 100.0, "max_loss": 1300.0, "lots": 1, "expiry": "2026-11-17"}
OUT = {"technical_report": {"trend_bias": "BULLISH"}, "risk_review": {"approved": True}, "trade_payload": BUY, "usage": [{"node": "technical", "input_tokens": 1000, "output_tokens": 500}]}


def chain():
    return {"atm": 100, "expiry": "e", "days_to_expiry": 3, "lot_size": 65, "atm_iv": 12, "pcr": 1, "support": 90, "resistance": 110, "rows": [{"strike": s, "ce": {"tradingsymbol": f"C{s}", "ltp": 1, "secret": "x"}, "pe": {"tradingsymbol": f"P{s}", "ltp": 1}} for s in range(80, 121)]}


def price(p):
    return lambda key, *a: {"meta": {"regularMarketPrice": p}}


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setattr(team, "DIR", tmp_path)
    monkeypatch.setattr(team, "claude_tool", lambda: "k")
    monkeypatch.setattr(team.risk_settings, "settings", lambda: {"capital": 100000, "max_risk_inr": 2000, "daily_loss_inr": 1500})
    monkeypatch.setattr(team, "PYTHON", Path(__file__))
    monkeypatch.setattr(team.india, "trading_day", lambda d: True)
    monkeypatch.setattr(team.india, "chart", price(100.0))
    monkeypatch.setattr(team, "snapshot", lambda u, now: {"spot": 1, "vwap": 1, "india_vix": 15, "opening_range": {}, "option_chain": chain(), "limits": {"max_risk_inr": 2000}})
    monkeypatch.setattr(team, "follow_positions", lambda now: None)
    monkeypatch.setattr(team.notify, "send_once", lambda *a, **k: True)
    team._running.update(underlying=None, since=None)
    team._scan.update(bar=None)
    return team


def test_there_is_no_order_path():
    assert not re.search(r"place_order|modify_order|cancel_order|place_gtt|orders/place|openalgo", SRC, re.I)
    routes = (Path(team.__file__).resolve().parents[1] / "routes" / "multi_agent_team.py").read_text(encoding="utf-8")
    assert "place_order" not in routes and "api_key" not in routes and "@app.post" not in routes
    for name in ("agents/intraday.py", "intraday_cli.py"):
        assert not re.search(r"place_order|orders/place|openalgo", (team.TA_DIR / "tradingagents" / name).read_text(encoding="utf-8"), re.I)


def test_the_chain_is_trimmed_to_the_strikes_near_the_money():
    t = team.trim_chain(chain())
    assert [r["strike"] for r in t["rows"]] == list(range(94, 107)) and "secret" not in json.dumps(t) and t["lot_size"] == 65


# ---------------- scanning

def test_the_universe_is_both_indices_and_the_top_fno_movers_each_way(monkeypatch):
    monkeypatch.setattr(team, "stock_source", lambda: {"up": [f"U{i}" for i in range(15)], "down": ["D1"]})
    u = team.universe()
    assert u[:2] == [("NIFTY", None), ("BANKNIFTY", None)] and len(u) == 2 + team.STOCKS_EACH_WAY + 1 and ("D1", "down") in u
    monkeypatch.setattr(team, "stock_source", lambda: 1 / 0)
    assert team.universe() == [("NIFTY", None), ("BANKNIFTY", None)]


def test_a_stock_setup_must_go_the_way_the_stock_is_moving_and_pass_the_chart_check(ready, monkeypatch):
    monkeypatch.setattr(team, "stock_source", lambda: {"up": ["RELIANCE"], "down": []})
    monkeypatch.setattr(team.setup_scan, "regime", lambda d: {"day_type": "Mild Trend", "bias": "Neutral"})

    def find(sym, *a):
        if sym != "RELIANCE":
            return [{"key": sym + "x", "technical": {"confirmed": False}, "setup": "vwap_reclaim", "direction": "up", "at": "10:00", "why": "w"}]
        return [{"key": "r-up", "technical": {"confirmed": True}, "setup": "vwap_reclaim", "direction": "up", "at": "10:00", "why": "w"},
                {"key": "r-down", "technical": {"confirmed": True}, "setup": "vwap_reclaim", "direction": "down", "at": "10:00", "why": "w"}]
    monkeypatch.setattr(team.setup_scan, "find", find)
    found, counts = team.find_triggers(NOW)
    assert [(f["underlying"], f["direction"], f["kind"]) for f in found] == [("RELIANCE", "up", "stock")]
    assert counts == {"scanned": 3, "fired": 3, "failed_chart": 2, "no_data": 0} and len(team._seen(DAY)) == 4


def test_a_setup_found_is_saved_so_a_restart_does_not_lose_it(ready, monkeypatch):
    started = []
    monkeypatch.setattr(team, "start", lambda u, now=None, trigger=None: started.append(u))
    monkeypatch.setattr(team.common, "completed_bar", lambda now: "09:55")
    trig = {"underlying": "NIFTY", "kind": "index", "setup": "ORB", "direction": "up", "at": "10:00", "why": "x"}
    monkeypatch.setattr(team, "find_triggers", lambda now: ([trig, dict(trig, underlying="RELIANCE", kind="stock")], {"scanned": 22, "fired": 2, "failed_chart": 0, "no_data": 0}))
    team._running.update(underlying="BANKNIFTY")          # busy: both wait
    team.tick(NOW)
    assert [t["underlying"] for t in team.queue(DAY)] == ["NIFTY", "RELIANCE"] and started == []
    team._running.update(underlying=None)
    team._scan.update(bar=None)                             # as after a restart
    monkeypatch.setattr(team, "find_triggers", lambda now: ([], {"scanned": 22, "fired": 0, "failed_chart": 0, "no_data": 0}))
    team.tick(NOW.replace(minute=1))
    team.tick(NOW.replace(minute=2))
    assert started == ["NIFTY", "RELIANCE"] and team.queue(DAY) == []
    kinds = [a["kind"] for a in team.activity(DAY)]
    assert kinds.count("scan") == 1 and kinds.count("setup") == 2 and kinds.count("run") == 2      # the 09:55 candle isn't scanned again after the restart


def test_a_stale_setup_the_daily_cap_and_a_failed_start_are_shown_not_lost(ready, monkeypatch):
    monkeypatch.setattr(team.common, "completed_bar", lambda now: None)
    team._write("queue", DAY, [{"underlying": "NIFTY", "setup": "ORB", "direction": "up", "at": "09:40"}])
    team.tick(NOW)
    assert "no longer fresh" in team.activity(DAY)[-1]["text"]
    for i in range(team.MAX_AUTO_RUNS):
        team._save(DAY, {"id": str(i), "trigger": {"setup": "x"}})
    team._write("queue", DAY, [{"underlying": "NIFTY", "setup": "ORB", "direction": "up", "at": "09:55"}])
    team.tick(NOW)
    assert "runs for today" in team.activity(DAY)[-1]["text"]


def test_nothing_scans_before_0935_or_on_a_holiday(ready, monkeypatch):
    calls = []
    monkeypatch.setattr(team, "find_triggers", lambda now: calls.append(now) or ([], {}))
    team.tick(NOW.replace(hour=9, minute=34))
    monkeypatch.setattr(team.india, "trading_day", lambda d: False)
    team.tick(NOW)
    assert calls == []


# ---------------- one run

def test_a_buy_is_saved_sent_once_and_taken_as_a_paper_trade(ready, monkeypatch):
    sent = []
    monkeypatch.setattr(team, "run_graph", lambda c, k: dict(OUT))
    monkeypatch.setattr(team.notify, "send_once", lambda mid, text, now=None: sent.append((mid, text)) or True)
    team._work("NIFTY", NOW, "k", {"setup": "ORB"})
    r = team.runs(DAY)[0]
    assert r["trade_payload"]["action"] == "BUY" and r["paper"] == {"taken": True}
    e = r["position"]
    assert e["entry"] == 100.3 and e["qty"] == 65 and e["status"] == "open" and e["key"] == "NFO:NIFTY26N1724900CE" and e["kind"] == "index"
    assert len(sent) == 1 and "Multi Agent Team" in sent[0][1] and "NIFTY 24900 CE · Exp 17 Nov" in sent[0][1]
    assert team._running["underlying"] is None and "Paper trade taken" in team.activity(DAY)[-1]["text"]


def test_a_hold_or_a_failure_sends_nothing_and_takes_nothing(ready, monkeypatch):
    sent = []
    monkeypatch.setattr(team.notify, "send_once", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(team, "run_graph", lambda c, k: {**OUT, "trade_payload": {"action": "HOLD", "rationale": "no"}})
    team._work("NIFTY", NOW, "k")
    monkeypatch.setattr(team, "run_graph", lambda c, k: {"error": "Your Claude usage limit has been reached."})
    team._work("NIFTY", NOW, "k")
    rows = team.runs(DAY)
    assert sent == [] and rows[1]["error"] and len(rows) == 2 and team.positions(DAY) == []


def test_a_price_past_the_stop_while_the_team_worked_is_not_taken(ready, monkeypatch):
    monkeypatch.setattr(team, "run_graph", lambda c, k: dict(OUT))
    monkeypatch.setattr(team.india, "chart", price(79.0))
    team._work("NIFTY", NOW, "k")
    r = team.runs(DAY)[0]
    assert r["paper"]["taken"] is False and "past the stop or target" in r["paper"]["why"] and "position" not in r


def test_the_open_limit_and_the_daily_loss_limit_stop_new_paper_trades(ready, monkeypatch):
    monkeypatch.setattr(team, "run_graph", lambda c, k: dict(OUT))
    for i in range(team.MAX_OPEN):
        team._save(DAY, {"id": f"o{i}", "position": {"status": "open", "entry": 1, "qty": 1, "ltp": 1, "kind": "index"}})
    team._work("NIFTY", NOW, "k")
    assert "already open" in team.runs(DAY)[-1]["paper"]["why"]
    team._write("day", DAY, [{"id": "c", "position": {"status": "closed", "net": -1600.0}}])
    team._work("NIFTY", NOW, "k")
    assert "loss limit" in team.runs(DAY)[-1]["paper"]["why"]


@pytest.mark.parametrize("change,text", [({"underlying": "RELIANCE"}, "NIFTY or BANKNIFTY"), ({"now": datetime(2026, 11, 10, 9, 20, tzinfo=team.common.IST)}, "from 09:30"), ({"now": datetime(2026, 11, 10, 14, 31, tzinfo=team.common.IST)}, "from 09:30")])
def test_runs_are_refused_outside_the_rules(ready, change, text):
    args = {"underlying": "NIFTY", "now": NOW, **change}
    with pytest.raises(ValueError, match=text):
        team.start(args["underlying"], args["now"])


def test_a_stock_trigger_can_start_a_run(ready, monkeypatch):
    monkeypatch.setattr(team.threading, "Thread", lambda **k: type("T", (), {"start": lambda self: None})())
    team.start("RELIANCE", NOW, {"setup": "ORB"})
    assert team._running["underlying"] == "RELIANCE"


def test_no_sign_in_and_no_settings_are_explained(ready, monkeypatch):
    monkeypatch.setattr(team, "claude_tool", lambda: None)
    with pytest.raises(ValueError, match="isn't signed in"):
        team.start("NIFTY", NOW)
    monkeypatch.setattr(team.risk_settings, "settings", lambda: None)
    with pytest.raises(ValueError, match="paper capital"):
        team.start("NIFTY", NOW)


def test_a_second_run_waits_for_the_first(ready):
    team._running.update(underlying="NIFTY", since="x")
    with pytest.raises(ValueError, match="still going"):
        team.start("BANKNIFTY", NOW)
    team._running.update(underlying=None, since=None)


def test_the_team_uses_the_pro_login_and_passes_no_api_key(monkeypatch):
    seen = {}

    class Done:
        stdout = b'{"trade_payload": {"action": "HOLD"}}'

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    monkeypatch.setattr(team.subprocess, "run", lambda args, **k: seen.update(k) or Done())
    assert team.run_graph({}, "tool.exe")["trade_payload"]["action"] == "HOLD"
    assert seen["env"]["CLAUDE_PRO_TOOL"] == "tool.exe" and "ANTHROPIC_API_KEY" not in seen["env"]


def test_the_card_carries_the_agents_confidence_and_says_it_is_their_view(ready, monkeypatch):
    sent = []
    monkeypatch.setattr(team, "run_graph", lambda c, k: {**OUT, "trade_payload": {**BUY, "confidence": 72}})
    monkeypatch.setattr(team.notify, "send_once", lambda mid, text, now=None: sent.append(text) or True)
    team._work("NIFTY", NOW, "k")
    assert re.search(r"Confidence\s+72% · AI view", sent[0])


# ---------------- the paper book

def pos(**k):
    return {"id": "p", "instrument": "X", "key": "NFO:X", "kind": "index", "qty": 10, "entry": 100.0, "stop": 90.0, "target": 120.0, "entry_at": "10:02", "entry_bar": "10:05", "status": "open", "ltp": 100.0, **k}


@pytest.mark.parametrize("bars,reason", [({"10:00": (80, 80, 80, 80), "10:05": (100, 105, 95, 102), "10:10": (101, 103, 89, 90)}, "stop"),
                                         ({"10:05": (100, 121, 99, 118)}, "target"),
                                         ({"10:05": (100, 105, 95, 101), "15:10": (104, 106, 103, 105)}, "15:15 exit")])
def test_a_paper_trade_exits_at_its_stop_target_or_1515_and_ignores_candles_before_entry(bars, reason):
    e = pos()
    team.walk(e, bars, "15:10")
    assert e["status"] == "closed" and e["exit_reason"] == reason and e["net"] is not None and e["costs"] > 0


def test_a_stock_option_pays_more_slippage_than_an_index_option():
    a, b = pos(), pos(kind="stock")
    for e in (a, b):
        team._close(e, 110.0, "x", "11:00")
    assert a["exit"] == round(110 * 0.997, 2) and b["exit"] == round(110 * 0.99, 2) and b["net"] < a["net"]


REAL_FOLLOW = team.follow_positions       # the fixture swaps it for a no-op


def test_open_trades_are_followed_and_the_view_shows_the_book(ready, monkeypatch):
    team._save(DAY, {"id": "a", "position": pos()})
    monkeypatch.setattr(team, "bars_of", lambda key, day: {"10:05": (100, 121, 99, 118)})
    monkeypatch.setattr(team.common, "completed_bar", lambda now: "10:05")
    REAL_FOLLOW(NOW.replace(minute=11))
    v = team.view(DAY)
    assert v["book"]["closed"] == 1 and v["book"]["won"] == 1 and v["book"]["realized"] > 0 and v["activity"][0]["kind"] == "exit"


def test_the_view_never_carries_the_tool_path(ready, monkeypatch):
    monkeypatch.setattr(team, "claude_tool", lambda: "C:/SECRET/claude.exe")
    v = team.view(DAY)
    assert "SECRET" not in json.dumps(v) and v["signed_in"] is True and v["paper_mode"] is True and v["auto"]["stocks_each_way"] == team.STOCKS_EACH_WAY


def test_a_team_call_is_in_the_end_of_day_report(ready, monkeypatch):
    team._save(DAY, {"id": "a", "at": "10:02", "underlying": "NIFTY", "trade_payload": BUY})
    team._save(DAY, {"id": "b", "at": "10:30", "underlying": "NIFTY", "trade_payload": {"action": "HOLD"}})
    monkeypatch.setattr(team, "bars_of", lambda key, day: {"10:05": (100, 141, 99, 140)})
    rows = eod._team_rows(DAY)
    assert len(rows) == 1 and rows[0]["desk"] == "Multi Agent Team" and rows[0]["reason"] == "target" and rows[0]["done"]
    assert "MAT = Multi Agent Team" in eod.report_text(rows, DAY)[0]


def test_the_shared_risk_gate_stops_a_paper_trade_but_not_the_call(ready, monkeypatch):
    sent = []
    monkeypatch.setattr(team, "run_graph", lambda c, k: dict(OUT))
    asked = []
    monkeypatch.setattr(team, "risk_gate", lambda u, kind: asked.append(kind) or "Agent Desk already has an open paper trade on " + u)
    monkeypatch.setattr(team.notify, "send_once", lambda mid, text, now=None: sent.append(text) or True)
    team._work("NIFTY", NOW, "k")
    r = team.runs(DAY)[0]
    assert r["paper"] == {"taken": False, "why": "Agent Desk already has an open paper trade on NIFTY."} and "position" not in r and len(sent) == 1
    assert asked == ["option"]          # the options team asks about option trades only


def test_confidence_is_the_agents_view_until_enough_trades_have_closed(ready):
    assert team.confidence_text(None) is None and team.confidence_text(58) == "58% · AI view"
    rows = [{"id": str(i), "position": pos(id=str(i), status="closed", net=100.0 if i % 4 == 0 else -50.0, confidence=55)} for i in range(52)]
    team._write("day", DAY, rows)
    c = team.calibration()
    band = next(b for b in c["bands"] if b["band"].startswith("50"))
    assert c["ready"] and band["trades"] == 52 and band["won"] == 13 and band["win_rate"] == 25.0
    assert team.confidence_text(58) == "25% · measured" and team.confidence_text(75) == "75% · AI view"
    assert team.view(DAY)["calibration"]["trades"] == 52


def test_the_team_has_its_own_three_thousand_rupee_risk_limit():
    assert team.MAX_RISK_INR == 3000.0 and '"max_risk_inr": MAX_RISK_INR' in SRC


def test_the_view_shows_the_run_in_progress_with_what_each_agent_has_said(ready):
    assert team.view(DAY)["live"] is None
    team._running.update(underlying="NIFTY", since="2026-11-10T10:00:00+05:30", trigger={"setup": "ORB", "direction": "up"})
    assert team.view(DAY)["live"]["stage"] == "snapshot"
    team._live_file().write_text(json.dumps({"stage": "risk", "technical_report": {"trend_bias": "BULLISH"}}), encoding="utf-8")
    v = team.view(DAY)["live"]
    assert v["stage"] == "risk" and v["technical_report"]["trend_bias"] == "BULLISH" and v["underlying"] == "NIFTY" and v["trigger"]["setup"] == "ORB"
    team._running.update(underlying=None, since=None, trigger=None)


def test_the_progress_file_is_cleared_when_a_run_ends(ready, monkeypatch):
    monkeypatch.setattr(team, "run_graph", lambda c, k: team._live_file().write_text("{}", encoding="utf-8") and dict(OUT))
    team._work("NIFTY", NOW, "k")
    assert not team._live_file().exists() and team._running["trigger"] is None


def test_the_gemini_key_reaches_the_graph_only_from_the_secrets(monkeypatch):
    from strategies import multi_agent_team as mat
    seen = {}

    def run(args, input, cwd, env, capture_output, timeout, creationflags):
        seen.update(env)
        return type("P", (), {"stdout": b'{"trade_payload": {"action": "HOLD"}}'})()
    monkeypatch.setattr(mat.subprocess, "run", run)
    monkeypatch.setattr(mat.notify, "_secret", lambda name: "gem-test" if name == "GEMINI_API_KEY" else "")
    assert mat.run_graph({}, "claude") == {"trade_payload": {"action": "HOLD"}} and seen["GEMINI_API_KEY"] == "gem-test"
    seen.clear()
    monkeypatch.setattr(mat.notify, "_secret", lambda name: "")
    mat.run_graph({}, "claude")
    assert "GEMINI_API_KEY" not in seen


def test_a_restart_does_not_rescan_the_last_candle(tmp_path, monkeypatch):
    """The last scanned candle is read back from the day's log, so a restart doesn't scan it a second time."""
    from strategies import multi_agent_team as m
    for mod, line in ((m, "Scanned 22 charts after the 10:20 candle: 0 setups fired, 0 passed the chart check."),):
        monkeypatch.setattr(mod, "activity", lambda day, line=line: [{"at": "10:25", "kind": "scan", "text": line}, {"at": "10:26", "kind": "setup", "text": "X: setup, up, at 10:20."}])
        assert mod.last_scanned("2026-11-16") == "10:20"
        monkeypatch.setattr(mod, "activity", lambda day: [{"at": "14:41", "kind": "scan", "text": "Swing scan: 0 name(s) with the daily and weekly trend in line."}])
        assert mod.last_scanned("2026-11-16") is None
