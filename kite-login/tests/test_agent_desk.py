"""Agent Desk: the team's decisions and the paper position, with the skill scripts stood in for."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from strategies import agent_desk as ad, common, paper_desk


@pytest.fixture(autouse=True)
def _class_test_book(monkeypatch):
    """The Agent Desk itself is gone; its AgentDesk class runs the Agent Pipeline. These class tests use a test-only book."""
    monkeypatch.setitem(paper_desk.DESKS, "agents", "Agent Desk (test)")
    monkeypatch.setitem(paper_desk.CAPITALS, "agents", 500000.0)

SRC = (Path(__file__).resolve().parents[1] / "backend" / "strategies" / "agent_desk.py").read_text(encoding="utf-8")
DAY = "2026-11-05"
HIT = {"symbol": "AAA", "at": "9:35 AM", "bar_start": "09:30", "or_high": 100, "pct_over": 0.5}
CAND = {"symbol": "AAA", "at": "9:35 AM", "bar_start": "09:30", "close_time": "09:35", "entry": 100.0, "stop": 99.0, "target": 102.0, "qty": 50}


def test_paper_only_and_no_credentials():
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt|exit_order)\s*\(", SRC)
    assert "access_token" not in SRC and "api_key" not in SRC and "kite_session" not in SRC


# ---------------- the paper position ----------------
def test_stop():
    t = ad.simulate(CAND, {"09:35": (100.0, 100.5, 98.5, 99.0)}, "15:10", slip=0)
    assert t["exit_reason"] == "stop" and t["exit_ref"] == 99 and t["status"] == "closed" and t["exit_time"] == "09:40" and t["gross"] == -50


def test_target():
    t = ad.simulate(CAND, {"09:35": (100.0, 102.5, 99.5, 102.0)}, "15:10", slip=0)
    assert t["exit_reason"] == "target" and t["exit_ref"] == 102 and t["r"] == 2


def test_a_candle_touching_both_counts_as_the_stop():
    assert ad.simulate(CAND, {"09:35": (100.0, 103.0, 98.0, 100.0)}, "15:10", slip=0)["exit_reason"] == "stop"


def test_a_gap_through_the_stop_fills_at_the_open():
    assert ad.simulate(CAND, {"09:35": (97.0, 97.5, 96.5, 97.0)}, "15:10", slip=0)["exit_ref"] == 97


def test_the_trigger_candle_itself_is_not_replayed():
    t = ad.simulate(CAND, {"09:30": (100.0, 100.0, 90.0, 100.0)}, "09:35", slip=0)
    assert t["status"] == "open"


def test_out_at_1515():
    bars = {t: (100.0, 100.5, 99.5, 100.2) for t in common.LABELS}
    t = ad.simulate(CAND, bars, "15:10", slip=0)
    assert t["exit_reason"] == "15:15 exit" and t["exit_time"] == "15:15" and t["exit_ref"] == 100.2


def test_open_until_then_marked():
    t = ad.simulate(CAND, {"09:35": (100.0, 100.5, 99.5, 100.4)}, "09:35", slip=0)
    assert t["status"] == "open" and t["ltp"] == 100.4 and t["left"] == 1 and t["exit_ref"] is None


def test_slippage_and_charges_are_taken():
    t = ad.simulate(CAND, {"09:35": (100.0, 102.5, 99.5, 102.0)}, "15:10")
    assert t["entry_ref"] == pytest.approx(100.05) and t["exit_ref"] == pytest.approx(101.95) and t["net"] == pytest.approx(t["gross"] - t["costs"], abs=0.01) and t["costs"] > 0


# ---------------- the team ----------------
class Team:
    """Stands in for the skill scripts."""

    def __init__(self, hits=(HIT,), avg_r=0.2, entry="triggered", budget_rc=0, risk="PASS", exit_rc=0, rate=60.0):
        self.hits, self.avg_r, self.entry, self.budget_rc, self.risk, self.exit_rc = list(hits), avg_r, entry, budget_rc, risk, exit_rc
        self.rate = rate
        self.calls = []

    def __call__(self, agent, args, cwd):
        self.calls.append((agent, args))
        if agent == "scanner":
            return 0, json.dumps({"scanned": 50, "hits": self.hits})
        if agent == "entry":
            return 0, json.dumps({"status": self.entry, "reason": "the bar closed above", "trigger": {"time": "9:35 AM", "price": 100.0}})
        if agent == "exit":
            return self.exit_rc, "Exit plan: stop 99, target 102"
        if agent == "timemachine":
            return 0, json.dumps({"mc": {"p_target": 0.4, "p_stop": 0.3, "p_neither": 0.3, "avg_R": self.avg_r}, "history": {"triggered": 10, "target": 5, "stop": 3}, "verdict": "Leans to target",
                                  "confidence": {"level": "Medium", "success_rate_pct": self.rate, "sample": 10, "why": "10 past triggers"}})
        if agent == "budget":
            if self.budget_rc == 0:
                (Path(cwd) / "trader" / "orders.json").write_text(json.dumps({"orders": [{"qty": "50", "plan": {"stop": 99.0, "target": 102.0}}]}), encoding="utf-8")
            return self.budget_rc, "50 shares"
        if agent == "risk":
            return 0, json.dumps({"results": [{"verdict": self.risk, "reasons": ["over the limit"] if self.risk != "PASS" else [], "info": ["max loss 50"]}]})
        return 0, f"{agent} output"


BARS = {(DAY, "09:35"): (100.0, 102.5, 99.5, 102.0)}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_desk, "CACHE_DIR", tmp_path)
    paper_desk._write(ad.DESK, {"capital": 100000.0, "started": "2026-11-01"})

    def make(team):
        return ad.AgentDesk(cache_dir=tmp_path, runner=team, bars=lambda s: BARS, symbols=lambda: ["AAA", "BBB"])
    return make


def tick(d, hm="09:40"):
    d.tick(datetime(2026, 11, 5, int(hm[:2]), int(hm[3:]), 30, tzinfo=common.IST))


def agents_of(c):
    return [s["agent"] for s in c["steps"]]


def test_every_agent_agrees_and_it_trades_on_paper(desk):
    d = desk(Team())
    tick(d)
    c = d.snapshot["candidates"][0]
    assert c["decision"] == "traded" and agents_of(c) == ["scanner", "news", "analyst", "insiders", "entry", "exit", "timemachine", "budget", "risk", "broker"]
    assert paper_desk.view(ad.DESK)["closed"][0]["exit_reason"] == "target"
    data = {st["agent"]: st.get("data") for st in c["steps"]}
    assert data["scanner"]["symbol"] == c["symbol"] and data["entry"]["entry"] and data["risk"] == {"verdict": "PASS"}
    assert data["timemachine"]["avg_R"] > 0 and data["broker"]["order"] == "BUY" and data["broker"]["book"] == "paper" and data["broker"]["qty"] == c["qty"]


@pytest.mark.parametrize("kw,agent,text", [({"entry": "waiting"}, "entry", "did not confirm"), ({"exit_rc": 1}, "exit", "stop and target"), ({"rate": 51.0}, "timemachine", "not above 51%"), ({"rate": 40.0}, "timemachine", "not above 51%"), ({"budget_rc": 1}, "budget", "size it"), ({"risk": "BLOCK"}, "risk", "over the limit")])
def test_any_agent_can_stop_it(desk, kw, agent, text):
    d = desk(Team(**kw))
    tick(d)
    c = d.snapshot["candidates"][0]
    assert c["decision"] == "skipped" and text in c["why"] and c["steps"][-1]["agent"] == agent and c["steps"][-1]["state"] == "no"
    assert text in c["steps"][-1]["data"]["blocked"] and "broker" not in agents_of(c)          # the block travels as data, and nothing reaches the Broker
    assert not paper_desk.view(ad.DESK)["closed"]


def test_context_agents_do_not_decide(desk):
    team = Team()
    real = team.__call__

    class Failing(Team):
        def __call__(self, agent, args, cwd):
            return (1, "unreachable") if agent in ("news", "analyst", "insiders") else real(agent, args, cwd)
    d = desk(Failing())
    tick(d)
    assert d.snapshot["candidates"][0]["decision"] == "traded"


def test_a_candidate_is_reviewed_once(desk):
    team = Team()
    d = desk(team)
    tick(d, "09:40")
    tick(d, "09:45")
    assert sum(a == "entry" for a, _ in team.calls) == 1 and len(d.snapshot["candidates"]) == 1


def test_only_three_trades_a_day(desk):
    hits = [{**HIT, "symbol": f"S{i}"} for i in range(5)]
    d = desk(Team(hits=hits))
    tick(d)
    done = d.snapshot["candidates"]
    assert sum(c["decision"] == "traded" for c in done) == 3 and "limit of 3" in done[3]["why"]


def test_candidates_are_taken_in_trigger_order(desk):
    hits = [{**HIT, "symbol": "LATE", "bar_start": "10:00"}, {**HIT, "symbol": "EARLY", "bar_start": "09:55"}]
    d = desk(Team(hits=hits))
    tick(d, "10:05")
    assert [c["symbol"] for c in d.snapshot["candidates"]] == ["EARLY", "LATE"]


def test_each_cash_trade_gets_up_to_a_lakh_and_shrinks_when_the_book_is_used(desk, tmp_path, monkeypatch):
    hits = [{**HIT, "symbol": f"A{i}", "bar_start": "09:50"} for i in range(3)]
    seen = []

    class Watch(Team):
        def __call__(self, agent, args, cwd):
            if agent == "budget":
                seen.append(json.loads((Path(cwd) / "trader" / "config.json").read_text(encoding="utf-8"))["budget"])
                (Path(cwd) / "trader" / "orders.json").write_text(json.dumps({"orders": [{"qty": "900", "plan": {"stop": 99.0, "target": 102.0}}]}), encoding="utf-8")
                return 0, "900 shares"
            return Team.__call__(self, agent, args, cwd)
    monkeypatch.setattr(ad, "CASH_CAPITAL", 250000.0)
    d = ad.AgentDesk(cache_dir=tmp_path, runner=Watch(hits=hits), bars=lambda s: {(DAY, "09:55"): (100.0, 100.5, 99.5, 100.0)}, symbols=lambda: ["A0", "A1", "A2"])
    tick(d, "10:00")
    assert seen[0] == 100000 and seen[1] == 100000 and seen[2] < 100000


def test_one_trade_may_use_the_whole_desk(desk, tmp_path):
    d = desk(Team())
    tick(d)
    cfg = json.loads((tmp_path / "agents" / DAY / "trader" / "config.json").read_text(encoding="utf-8"))
    assert cfg["cash_buffer_pct"] == 0 and cfg["max_position_pct"] == 100 and cfg["max_order_inr"] == 100000 and cfg["max_loss_per_trade_inr"] is None and cfg["max_daily_loss_inr"] is None


def test_no_free_cash_is_reviewed_and_stays_off_the_paper_book(desk, tmp_path, monkeypatch):
    hits = [{**HIT, "symbol": "A1", "bar_start": "09:50"}, {**HIT, "symbol": "A2", "bar_start": "09:50"}]
    team = Team(hits=hits)
    monkeypatch.setattr(ad, "CASH_CAPITAL", 100000.0)

    class Big(Team):
        def __call__(self, agent, args, cwd):
            if agent == "budget":
                (Path(cwd) / "trader" / "orders.json").write_text(json.dumps({"orders": [{"qty": "1000", "plan": {"stop": 99.0, "target": 102.0}}]}), encoding="utf-8")
                return 0, "1000 shares"
            return team(agent, args, cwd)
    d = ad.AgentDesk(cache_dir=tmp_path, runner=Big(), bars=lambda s: {(DAY, "09:35"): (100.0, 100.5, 99.5, 100.0)}, symbols=lambda: ["A1", "A2"])
    tick(d, "10:00")
    c = d.snapshot["candidates"]
    assert c[0]["decision"] == "traded" and c[1]["decision"] == "skipped" and "No cash free" in c[1]["why"]
    assert c[0]["reviewed"] and c[1]["reviewed"] and "call_ready" not in c[1] and [t["symbol"] for t in d.snapshot["trades"]] == ["A1"]


def test_first_seen_after_the_close_is_not_counted(desk):
    d = desk(Team())
    tick(d, "17:30")
    assert d.snapshot["counted"] is False
    assert paper_desk.view(ad.DESK)["equity"] == 100000


def test_a_scanner_failure_is_shown_not_hidden(desk):
    class Down(Team):
        def __call__(self, agent, args, cwd):
            return 1, "Not logged in to Kite"
    d = desk(Down())
    tick(d)
    assert "Not logged in" in d.snapshot["scan_note"]


def test_restart_does_not_review_again(desk, tmp_path):
    team = Team()
    d = desk(team)
    tick(d, "09:40")
    again = desk(team)
    tick(again, "09:45")
    assert sum(a == "entry" for a, _ in team.calls) == 1


def test_the_team_list_covers_every_tool():
    assert set(ad.TOOLS) <= {a for a, _, _ in ad.TEAM}


def test_one_trade_can_use_the_whole_lakh_and_no_rupee_limit_applies(desk, tmp_path):
    d = desk(Team())
    tick(d)
    cfg = json.loads((tmp_path / "agents" / DAY / "trader" / "config.json").read_text(encoding="utf-8"))
    assert cfg["max_order_inr"] == 100000 and cfg["max_position_pct"] == 100 and cfg["max_loss_per_trade_inr"] is None and cfg["max_daily_loss_inr"] is None


def test_a_losing_day_does_not_stop_new_entries_by_rupees(desk):
    hits = [{**HIT, "symbol": "A1", "bar_start": "09:40"}, {**HIT, "symbol": "A2", "bar_start": "09:45"}]
    d = desk(Team(hits=hits))
    d.bars = lambda s: {(DAY, "09:45"): (100.0, 100.0, 50.0, 60.0)}
    tick(d, "09:50")
    assert [c["symbol"] for c in d.snapshot["candidates"]] == ["A1", "A2"] and "loss limit" not in d.snapshot["candidates"][1]["why"]


def test_options_are_refused_by_the_risk_config(desk, tmp_path):
    d = desk(Team())
    tick(d)
    cfg = json.loads((tmp_path / "agents" / DAY / "trader" / "config.json").read_text(encoding="utf-8"))
    assert cfg["allow_options"] is False and cfg["allow_margin"] is False and cfg["allow_short"] is False


def test_the_old_council_code_stays_out():
    assert "agent_options" not in SRC and "council" not in SRC and "shadow" not in SRC


def test_the_scan_list_is_the_strongest_rising_stocks():
    q = {"NSE:A": {"last_price": 110.0, "ohlc": {"open": 100.0}}, "NSE:B": {"last_price": 105.0, "ohlc": {"open": 100.0}}, "NSE:C": {"last_price": 90.0, "ohlc": {"open": 100.0}}, "NSE:D": {"last_price": None, "ohlc": {"open": 100.0}}, "NSE:E": {"last_price": 100.0, "ohlc": {"open": 100.0}}, "NSE:M&M": {"last_price": 120.0, "ohlc": {"open": 100.0}}}
    assert ad.strongest(q) == ["M&M", "A", "B"] and ad.strongest(q, 1) == ["M&M"] and ad.strongest({}) == []



# ---------------- the new rules: ₹5 lakh, two books, fresh breakouts only, confidence ----------------
OPTION_OK = {"ok": True, "plan": {"symbol": "NIFTY26N1024500CE", "entry": 100.0, "stop": 70.0, "target": 160.0, "expiry": "2026-11-10", "strike": 24500, "option_type": "CE"},
             "sizing": {"lots": 2, "qty": 150, "max_loss": 4600.0}, "notes": ["At the money fits the risk limit."],
             "odds": {"fired": 34, "sessions": 60, "avg_R_net": 0.14, "target_first": 0.41, "paths": {"p_target": 0.4, "p_stop": 0.35, "avg_R_net": 0.05}, "confidence": {"level": "High", "success_rate_pct": 62.0, "sample": 34, "why": "34 past triggers"}}}


class OptionTeam(Team):
    def __init__(self, option=OPTION_OK, **kw):
        super().__init__(**kw)
        self.option = option

    def __call__(self, agent, args, cwd):
        if agent == "options":
            self.calls.append((agent, args))
            return (0 if self.option.get("ok") else 1), json.dumps(self.option)
        return super().__call__(agent, args, cwd)


def test_the_desk_has_five_lakh_in_two_books():
    assert ad.CAPITAL == 500000 and ad.CASH_CAPITAL == 300000 and ad.PER_TRADE == 100000 and ad.OPTION_CAPITAL == 200000 and ad.OPTION_MAX_LOSS == 5000
    assert paper_desk.CAPITALS["pipeline"] == 500000


def test_a_stale_breakout_is_never_traded(desk):
    team = Team()
    d = desk(team)
    tick(d, "11:00")
    assert d.snapshot["candidates"] == [] and d.snapshot["stale_skipped"] >= 1 and not any(a == "entry" for a, _ in team.calls)


def test_skips_do_not_use_up_the_review_limit(desk, monkeypatch):
    monkeypatch.setattr(ad, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(ad, "MAX_TRADES", 1)
    hits = [{**HIT, "symbol": f"S{i}"} for i in range(4)]
    d = desk(Team(hits=hits))
    tick(d)
    c = d.snapshot["candidates"]
    assert [x["decision"] for x in c] == ["traded", "skipped", "skipped", "skipped"] and sum(x["reviewed"] for x in c) == 1


def test_an_option_buy_goes_through_the_options_agent_and_carries_its_confidence(desk, tmp_path):
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    team = OptionTeam(hits=[hit])
    d = ad.AgentDesk(cache_dir=tmp_path, runner=team, bars=lambda s: {(DAY, "09:35"): (100.0, 165.0, 95.0, 150.0)}, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    o = d.snapshot["options"][0]
    assert o["symbol"] == "NIFTY" and o["decision"] == "traded" and o["lots"] == 2 and o["odds"]["confidence"] == "High" and o["odds"]["success_rate_pct"] == 62.0
    args = next(a for n, a in team.calls if n == "options")
    assert args[args.index("--max-loss") + 1] == "5000.0" and args[args.index("--stop") + 1] == "24345.0"
    t = d.snapshot["option_trades"][0]
    assert t["key"] == "NFO:NIFTY26N1024500CE" and t["exit_reason"] == "target" and t["costs"] > 0
    assert any(x["key"] == "NFO:NIFTY26N1024500CE" for x in paper_desk.view(ad.DESK)["closed"])


def test_a_put_on_a_break_down(desk, tmp_path):
    hit = {**HIT, "symbol": "BBB", "price": 95.0, "or_high": 101.0, "or_low": 96.0, "direction": "down"}

    class Down(OptionTeam):
        def __call__(self, agent, args, cwd):
            if agent == "scanner":
                return 0, json.dumps({"scanned": 1, "hits": [hit] if "--direction" in args and "BBB" in args[args.index("--symbols") + 1] else []})
            return super().__call__(agent, args, cwd)
    team = Down(option=dict(OPTION_OK, plan=dict(OPTION_OK["plan"], symbol="BBB26NOV95PE", option_type="PE")))
    d = ad.AgentDesk(cache_dir=tmp_path, runner=team, bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": ["BBB"]})
    tick(d)
    o = d.snapshot["options"][0]
    args = next(a for n, a in team.calls if n == "options")
    assert o["direction"] == "down" and args[args.index("--direction") + 1] == "down" and float(args[args.index("--target") + 1]) < 95 and d.snapshot["candidates"] == []


def test_a_blocked_option_shows_why_and_its_confidence(desk, tmp_path):
    hit = {**HIT, "symbol": "^NSEBANK", "price": 52000.0, "or_high": 51990.0, "or_low": 51800.0}
    bad = dict(OPTION_OK, ok=False, reasons=["history averages -0.180R after charges over 34 times"], odds={"fired": 34, "confidence": {"level": "Low", "success_rate_pct": 26.5, "sample": 34, "why": "past triggers averaged -0.18R"}})
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=bad, hits=[hit]), bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    o = d.snapshot["options"][0]
    assert o["decision"] == "skipped" and "26.5% is not above 51%" in o["why"] and o["odds"]["confidence"] == "Low" and not d.snapshot["option_trades"]


# ---------------------------------------------------------------- the success rate decides (above 51%), in place of the Time Machine's other checks

def test_an_option_above_51_percent_is_taken_even_if_the_time_machine_objects(desk, tmp_path):
    """Like 16 Nov's ITC: a 54.8% success rate, blocked only by the simulated rest-of-day paths."""
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    paths = dict(OPTION_OK, ok=False, reasons=["the rest-of-session paths average -0.066R after charges"], odds=dict(OPTION_OK["odds"], confidence={"level": "Medium", "success_rate_pct": 54.8, "sample": 31, "why": "31"}))
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=paths, hits=[hit]), bars=lambda s: {(DAY, "09:35"): (100.0, 165.0, 95.0, 150.0)}, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    o = d.snapshot["options"][0]
    assert o["decision"] == "traded" and any("set aside" in s["text"] and "paths average" in s["text"] for s in o["steps"] if s["agent"] == "timemachine")


def test_an_option_at_or_below_51_percent_is_skipped_even_if_the_time_machine_passes_it(desk, tmp_path):
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    weak = dict(OPTION_OK, odds=dict(OPTION_OK["odds"], confidence={"level": "High", "success_rate_pct": 47.1, "sample": 34, "why": "34"}))
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=weak, hits=[hit]), bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    o = d.snapshot["options"][0]
    assert o["decision"] == "skipped" and "47.1% is not above 51%" in o["why"] and not d.snapshot["option_trades"]


def test_an_option_above_51_percent_still_needs_one_lot_to_fit(desk, tmp_path):
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    big = dict(OPTION_OK, ok=False, reasons=["the rest-of-session paths average -0.1R after charges", "one lot doesn't fit your budget"], sizing={"lots": 0, "qty": 0},
               odds=dict(OPTION_OK["odds"], confidence={"level": "Medium", "success_rate_pct": 60.0, "sample": 30, "why": "30"}))
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=big, hits=[hit]), bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    o = d.snapshot["options"][0]
    assert o["decision"] == "skipped" and "doesn't fit your budget" in o["why"] and "paths average" not in o["why"]


def test_a_share_trade_above_51_percent_is_taken_even_with_a_negative_simulated_average(desk):
    d = desk(Team(avg_r=-0.05, rate=58.0))
    tick(d)
    c = d.snapshot["candidates"][0]
    assert c["decision"] == "traded" and any("set aside" in s["text"] for s in c["steps"] if s["agent"] == "timemachine")


def test_the_cash_signal_shows_its_success_rate(desk):
    class Conf(Team):
        def __call__(self, agent, args, cwd):
            if agent == "timemachine":
                return 0, json.dumps({"mc": {"p_target": 0.4, "p_stop": 0.3, "p_neither": 0.3, "avg_R": 0.2}, "history": {"triggered": 12, "target": 5, "stop": 4, "sessions": 60, "avg_R_net": 0.08},
                                      "confidence": {"level": "Medium", "success_rate_pct": 50.0, "sample": 12, "why": "12 past triggers"}})
            return super().__call__(agent, args, cwd)
    d = desk(Conf())
    tick(d)
    od = d.snapshot["candidates"][0]["odds"]
    assert od["confidence"] == "Medium" and od["success_rate_pct"] == 50.0 and od["sample"] == 12 and od["target_first_pct"] == pytest.approx(41.7)


def test_the_down_list_is_the_weakest_falling_stocks():
    q = {"NSE:A": {"last_price": 90.0, "ohlc": {"open": 100.0}}, "NSE:B": {"last_price": 95.0, "ohlc": {"open": 100.0}}, "NSE:C": {"last_price": 110.0, "ohlc": {"open": 100.0}}}
    assert ad.weakest(q) == ["A", "B"] and ad.movers(q) == {"up": ["C"], "down": ["A", "B"]}


def test_confidence_grades_by_sample_and_outcome():
    import sys
    sys.path.insert(0, str(ad.SKILLS / "_india"))
    import india
    assert india.confidence(4, 0.5, 0.7)["level"] == "Low" and india.confidence(12, -0.1, 0.4)["level"] == "Low"
    assert india.confidence(12, 0.05, 0.45)["level"] == "Medium" and india.confidence(40, 0.15, 0.5)["level"] == "High" and india.confidence(40, 0.05, 0.5)["level"] == "Medium"


def test_skips_past_five_are_only_counted(desk, monkeypatch):
    monkeypatch.setattr(ad, "MAX_TRADES", 1)
    d = desk(Team(hits=[{**HIT, "symbol": f"S{i}"} for i in range(9)]))
    tick(d)
    c = d.snapshot["candidates"]
    assert len(c) == 6 and d.snapshot["skipped_more"]["cash"] == 3


def test_a_stale_breakout_is_counted_once(desk):
    d = desk(Team())
    tick(d, "11:00")
    first = d.snapshot["stale_skipped"]
    tick(d, "11:05")
    assert first >= 1 and d.snapshot["stale_skipped"] == first and d.snapshot["candidates"] == []


def test_a_blocked_signal_is_not_sent_to_telegram():
    assert "BLOCKED" not in SRC


def test_each_call_is_published_as_soon_as_it_is_decided(desk, monkeypatch):
    books = []
    monkeypatch.setattr(paper_desk, "on_recorded", lambda: books.append(len(paper_desk.view(ad.DESK)["open"]) + len(paper_desk.view(ad.DESK)["closed"])))
    d = desk(Team(hits=[{**HIT, "symbol": "S1"}, {**HIT, "symbol": "S2"}]))
    tick(d)
    assert books[:2] == [1, 2]


def test_a_blocked_candidate_publishes_nothing(desk, monkeypatch):
    books = []
    monkeypatch.setattr(paper_desk, "on_recorded", lambda: books.append(len(paper_desk.view(ad.DESK)["open"]) + len(paper_desk.view(ad.DESK)["closed"])))
    tick(desk(Team(rate=40.0)))          # blocked: success rate not above 51%
    assert not any(books)


def test_recording_any_desk_triggers_telegram_and_a_failure_cannot_stop_it(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_desk, "CACHE_DIR", tmp_path)
    paper_desk._write("agents", {"capital": 100000.0, "started": "2026-11-01"})
    calls = []
    monkeypatch.setattr(paper_desk, "on_recorded", lambda: calls.append(1))
    paper_desk.put_session("agents", DAY, [])
    assert calls == [1]

    def boom():
        raise RuntimeError("telegram down")
    monkeypatch.setattr(paper_desk, "on_recorded", boom)
    paper_desk.put_session("agents", DAY, [])


def test_a_trade_carries_its_confidence_to_the_paper_book(desk):
    class Conf(Team):
        def __call__(self, agent, args, cwd):
            if agent == "timemachine":
                return 0, json.dumps({"mc": {"p_target": 0.4, "p_stop": 0.3, "p_neither": 0.3, "avg_R": 0.2}, "history": {"triggered": 12, "target": 5, "stop": 4, "sessions": 60, "avg_R_net": 0.08}, "confidence": {"level": "Medium", "success_rate_pct": 55.0, "sample": 12, "why": "12"}})
            return super().__call__(agent, args, cwd)
    d = desk(Conf())
    tick(d)
    t = paper_desk.view(ad.DESK)["closed"][0]
    assert t["confidence"] == "Medium" and t["success_rate_pct"] == 55.0 and t["book"] == "cash" and t["setup"]


def test_a_scan_that_finds_nothing_is_not_a_failure(desk):
    class Empty(Team):
        def __call__(self, agent, args, cwd):
            if agent == "scanner":
                return 1, json.dumps({"created_by": "trader-scanner", "scanned": 2, "hits": []})
            return super().__call__(agent, args, cwd)
    d = desk(Empty())
    tick(d)
    assert not d.snapshot["scan_note"] and d.snapshot["last_bar"] == "09:35" and d.snapshot["candidates"] == []


# ---------------- passed every check, but no money for it ----------------
def _capture(monkeypatch):
    from strategies import notify
    sent = []
    monkeypatch.setattr(notify, "send_once", lambda mid, text, now=None: sent.append((mid, text)) or True)
    return sent


def test_an_option_over_the_loss_limit_is_not_sent_or_booked(desk, tmp_path, monkeypatch):
    """Telegram only carries trades the paper book takes: a setup that fails only on size goes nowhere."""
    sent = _capture(monkeypatch)
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    res = dict(OPTION_OK, ok=False, capital_only=True, reasons=["one lot doesn't fit your max loss: ₹5,000"], plan=dict(OPTION_OK["plan"], lot_size=65), sizing={"lots": 0, "qty": 0})
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=res, hits=[hit]), bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    assert sent == []
    assert all(c["decision"] == "skipped" for c in d.snapshot["options"])
    book = paper_desk.view(ad.DESK)
    assert not d.snapshot["option_trades"] and not [t for t in book["open"] + book["closed"] if str(t["key"]).startswith("NFO:")]


def test_an_option_blocked_on_its_odds_is_not_sent(desk, tmp_path, monkeypatch):
    sent = _capture(monkeypatch)
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    res = dict(OPTION_OK, ok=False, capital_only=False, reasons=["history averages -0.2R", "one lot doesn't fit"], plan=dict(OPTION_OK["plan"], lot_size=65), sizing={"lots": 0, "qty": 0})
    d = ad.AgentDesk(cache_dir=tmp_path, runner=OptionTeam(option=res, hits=[hit]), bars=lambda s: BARS, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    assert sent == []


def test_a_cash_signal_with_no_money_free_is_not_sent_and_stays_off_the_book(desk, tmp_path, monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(ad, "CASH_CAPITAL", 5000.0)
    hits = [{**HIT, "symbol": "A1", "bar_start": "09:50"}, {**HIT, "symbol": "A2", "bar_start": "09:50"}]
    d = ad.AgentDesk(cache_dir=tmp_path, runner=Team(hits=hits), bars=lambda s: {(DAY, "09:55"): (100.0, 100.5, 99.5, 100.0)}, symbols=lambda: {"up": ["A1", "A2"], "down": []})
    tick(d, "10:00")
    c = {x["symbol"]: x for x in d.snapshot["candidates"]}
    assert c["A1"]["decision"] == "traded" and c["A2"]["decision"] == "skipped" and "wasn't taken" in c["A2"]["why"]
    assert not [x for x in sent if "A2" in x[0]]
    assert [t["symbol"] for t in d.snapshot["trades"]] == ["A1"]


# ---------------- the Agent Pipeline team: its own book, shares and options bought intraday ----------------
def pipeline_team(tmp_path, monkeypatch, team=None):
    monkeypatch.setattr(paper_desk, "CACHE_DIR", tmp_path)
    paper_desk._write("pipeline", {"capital": 500000.0, "started": "2026-11-01"})
    return ad.AgentDesk(cache_dir=tmp_path, runner=team or Team(), bars=lambda s: BARS, symbols=lambda: ["AAA", "BBB"], desk="pipeline", name="Agent Pipeline", books=("cash", "options"))


def test_a_pipeline_trade_is_labelled_as_its_own_in_telegram_and_the_report():
    from strategies import eod_report, notify
    text = notify.open_text("Agent Pipeline", {"book": "options", "symbol": "AAA26NOV100CE", "side": "LONG", "entry": 10.0, "stop": 8.0, "target": 14.0, "qty": 100, "lots": 1})
    assert "Agent Pipeline Options" in text
    assert eod_report.CODES["Agent Pipeline · Cash"] == "PC" and eod_report.CODES["Agent Pipeline · Options"] == "PO"


# ---------------------------------------------------------------- high-confidence option setups over the usual loss limit

class LossLimitTeam(OptionTeam):
    """The Options agent blocks one lot at the usual limit and passes it at the higher one."""
    def __init__(self, rate, **kw):
        super().__init__(**kw)
        self.rate = rate

    def __call__(self, agent, args, cwd):
        if agent == "options":
            self.calls.append((agent, args))
            odds = dict(OPTION_OK["odds"], confidence={"level": "Medium", "success_rate_pct": self.rate, "sample": 34, "why": "34 past triggers"})
            if float(args[args.index("--max-loss") + 1]) < 7289:
                res = dict(OPTION_OK, ok=False, capital_only=True, reasons=["one lot doesn't fit your max loss: ₹5,000 max loss / ₹7289 a lot at the stop with charges"],
                           sizing={"lots": 0, "qty": 0}, odds=odds)
            else:
                res = dict(OPTION_OK, sizing={"lots": 1, "qty": 75, "max_loss": 7289.0}, odds=odds)
            return (0 if res["ok"] else 1), json.dumps(res)
        return Team.__call__(self, agent, args, cwd)


def _hc_desk(tmp_path, rate):
    hit = {**HIT, "symbol": "^NSEI", "price": 24400.0, "or_high": 24390.0, "or_low": 24300.0}
    team = LossLimitTeam(rate, hits=[hit])
    d = ad.AgentDesk(cache_dir=tmp_path, runner=team, bars=lambda s: {(DAY, "09:35"): (100.0, 165.0, 95.0, 150.0)}, symbols=lambda: {"up": ["AAA"], "down": []})
    tick(d)
    return d, team


def test_a_high_confidence_setup_over_the_usual_limit_is_taken_up_to_the_higher_limit(desk, tmp_path):
    d, team = _hc_desk(tmp_path, 62.0)
    o = d.snapshot["options"][0]
    caps = [a[a.index("--max-loss") + 1] for n, a in team.calls if n == "options"]
    assert caps == ["5000.0", "8000.0"]
    assert o["decision"] == "traded" and o["high_confidence"] and o["lots"] == 1 and o["max_loss"] == 7289.0
    assert any("success rate of 62%" in s["text"] for s in o["steps"] if s["agent"] == "risk")


def test_exactly_fifty_percent_is_not_enough(desk, tmp_path):
    d, team = _hc_desk(tmp_path, 50.0)
    o = d.snapshot["options"][0]
    assert o["decision"] == "skipped" and "doesn't fit your max loss" in o["why"]
    assert len([1 for n, _ in team.calls if n == "options"]) == 1 and not d.snapshot["option_trades"]


def test_the_higher_limit_can_be_turned_off(desk, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "HIGH_CONF_MAX_LOSS", ad.OPTION_MAX_LOSS)
    d, team = _hc_desk(tmp_path, 80.0)
    assert d.snapshot["options"][0]["decision"] == "skipped" and len([1 for n, _ in team.calls if n == "options"]) == 1
