"""The India desk: costs, rules, bar sizes, option maths, Risk's vetoes, the paper book, Telegram and the end-of-day report."""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SK = ROOT / ".claude" / "skills"
for d in ("_india", "trader-timemachine", "trader-risk", "trader-budget", "trader-broker"):
    sys.path.insert(0, str(SK / d))

import check  # noqa: E402
import fno_check  # noqa: E402
import fno_size  # noqa: E402
import india  # noqa: E402
import setups  # noqa: E402
import ticket  # noqa: E402
import timemachine as tm  # noqa: E402
from strategies import notify  # noqa: E402


def bar(slot, o, h, l, c, v=100):
    m = 555 + 5 * slot
    return {"slot": slot, "start": "%02d:%02d" % (m // 60, m % 60), "date": "2026-11-05", "o": o, "h": h, "l": l, "c": c, "v": v}


# ---------------- costs and config ----------------
def test_equity_costs_by_hand():
    # 1 lakh each way: brokerage 20+20, STT 25, exchange 6.14, SEBI 0.2, GST 18% of 46.34, stamp 3
    want = 40 + 25 + 6.14 + 0.2 + 0.18 * (40 + 6.14 + 0.2) + 3
    assert india.costs("equity_intraday", 100000, 100000) == pytest.approx(want, abs=0.01)


def test_option_costs_are_flat_brokerage_and_stt_on_sells():
    want = 40 + 0.0015 * 15000 + 0.0003553 * 25000 + 1e-6 * 25000 + 0.18 * (40 + 0.0003553 * 25000 + 1e-6 * 25000) + 0.00003 * 10000
    assert india.costs("options", 10000, 15000) == pytest.approx(want, abs=0.01)


def test_the_app_and_the_skills_agree_on_charges():
    from strategies import common
    assert common.costs(100000, 101000) == pytest.approx(india.costs("equity_intraday", 100000, 101000), abs=0.01)
    assert common.charges([(True, 10000.0), (False, 15000.0)]) == pytest.approx(india.costs("options", 10000, 15000), abs=0.01)


def test_charges_can_be_overridden():
    assert india.costs("options", 1000, 1000, charges={"options": {"stt_sell": 0.0}}) < india.costs("options", 1000, 1000)


def test_friendly_config_keys_and_an_explicit_key_wins():
    c = india.normalise_config({"account_size": 200000, "max_risk_per_trade_pct": 1, "daily_loss_limit_inr": 5000})
    assert c["budget"] == 200000 and c["max_loss_per_trade_inr"] == 2000 and c["max_daily_loss_inr"] == 5000
    assert india.normalise_config({"budget": 1000, "max_risk_per_trade_pct": 1, "max_loss_per_trade_inr": None})["max_loss_per_trade_inr"] is None


def test_config_holds_no_kite_credentials():
    text = (ROOT / "trader" / "config.json").read_text(encoding="utf-8")
    assert '"paper": true' in text and "access_token" not in text and "api_key" not in text


def test_session_phases():
    assert india.phase(datetime(2026, 11, 9, 9, 5))[0] == "pre-open"
    assert india.phase(datetime(2026, 11, 9, 9, 20))[0] == "opening"
    assert india.phase(datetime(2026, 11, 9, 12, 0))[0] == "lull"
    assert india.phase(datetime(2026, 11, 9, 15, 0))[0] == "closing"
    assert india.phase(datetime(2026, 11, 7, 10, 0))[0] == "closed"
    assert india.phase(datetime(2026, 11, 10, 10, 0))[0] == "closed"     # a holiday


# ---------------- strategies ----------------
def ctx(**kw):
    return dict(setups.context(5, 15), **kw)


def test_orb_both_ways_and_a_wick_is_not_a_trigger():
    bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 101, 99, 100), bar(2, 100, 101, 99, 100), bar(3, 100, 102, 100, 100.5), bar(4, 100.5, 102, 100, 101.5)]
    t = setups.detect("orb", bars, ctx(), "up")
    assert t["bar"] == 4 and t["entry"] == 101.5 and t["stop"] == 100
    assert setups.detect("orb", bars, ctx(), "down") is None
    down = bars[:3] + [bar(3, 100, 100, 98, 98.5)]
    assert setups.detect("orb", down, ctx(), "down")["stop"] == 100


def test_orb_vwap_wants_volume_when_there_is_volume():
    base = [bar(i, 100, 101, 99, 100) for i in range(3)]
    thin = base + [bar(3, 100, 102, 100, 101.5, 100)]
    loud = base + [bar(3, 100, 102, 100, 101.5, 400)]
    assert setups.detect("orb_vwap", thin, ctx(), "up") is None and setups.detect("orb_vwap", loud, ctx(), "up")


def test_orb_vwap_on_an_index_without_volume_uses_price_only():
    bars = [bar(i, 100, 101, 99, 100, 0) for i in range(3)] + [bar(3, 100, 102, 100, 101.5, 0)]
    assert setups.detect("orb_vwap", bars, ctx(), "up")


def test_gap_go_and_gap_fill():
    bars = [bar(0, 102, 103, 101.5, 102.5), bar(1, 102.5, 104, 102.4, 103.5)]
    assert setups.detect("gap_go", bars, ctx(prev_close=100), "up")["stop"] == 101.5
    assert setups.detect("gap_go", bars, ctx(prev_close=101.9), "up") is None
    fill = [bar(0, 102, 102.5, 101.5, 102), bar(1, 102, 102, 101, 101.2)]
    t = setups.detect("gap_fill", fill, ctx(prev_close=100), "down")
    assert t["target"] == 100 and setups.target_of(t, "down", 2) == 100


def test_vwap_reclaim_needs_time_on_the_other_side_and_volume():
    bars = [bar(i, 100, 100.2, 99, 99.5) for i in range(7)] + [bar(7, 99.5, 101, 99.4, 100.8, 400)]
    t = setups.detect("vwap_reclaim", bars, ctx(), "up")
    assert t and t["bar"] == 7 and "reclaim" in t["why"]
    assert setups.detect("vwap_reclaim", bars[:7] + [bar(7, 99.5, 101, 99.4, 100.8, 100)], ctx(), "up") is None


def test_vwap_reclaim_survives_bars_with_no_volume_at_the_open():
    """A session's first bars can have no volume, so no VWAP. Those bars count as neither side, and the pass doesn't crash."""
    quiet = [bar(i, 100, 100.2, 99, 99.5, 0) for i in range(4)]
    bars = quiet + [bar(i, 100, 100.2, 99, 99.5) for i in range(4, 7)] + [bar(7, 99.5, 101, 99.4, 100.8, 400)]
    assert setups.detect("vwap_reclaim", quiet + [bar(4, 99.5, 101, 99.4, 100.8, 400)], ctx(), "up") is None
    t = setups.detect("vwap_reclaim", bars, ctx(), "up")
    assert t and t["bar"] == 7


def test_momentum_needs_relative_volume_against_recent_sessions():
    day = [bar(i, 100 + i * 0.3, 100.4 + i * 0.3, 99.9 + i * 0.3, 100.3 + i * 0.3, 300) for i in range(8)]
    calm = [[bar(i, 100, 100.1, 99.9, 100, 100) for i in range(8)] for _ in range(4)]
    assert setups.detect("momentum_rvol", day, setups.context(5, 15, calm[-1], calm), "up")
    busy = [[bar(i, 100, 100.1, 99.9, 100, 300) for i in range(8)] for _ in range(4)]
    assert setups.detect("momentum_rvol", day, setups.context(5, 15, busy[-1], busy), "up") is None


def test_detect_drops_a_stop_on_the_wrong_side():
    setups.REGISTRY["_bad"] = {"fn": lambda b, c, d: setups.trigger(0, 100, 101, "x")}
    try:
        assert setups.detect("_bad", [bar(0, 100, 100, 100, 100)], ctx(), "up") is None
    finally:
        del setups.REGISTRY["_bad"]


def test_replay_counts_costs_and_adverse_moves():
    days = []
    for n in range(3):
        d = [bar(i, 100, 101, 99, 100) for i in range(3)] + [bar(3, 100, 102, 100, 101.5), bar(4, 101.5, 101.6, 100.5, 101), bar(5, 101, 110, 101, 105)]
        days.append(("2026-10-0%d" % (n + 1), d))
    r = setups.replay(days, "orb", "up", 2.0, 5, 5, 15, lambda e, s: 0.1)
    assert r["triggered"] == 3 and r["target"] == 3 and r["avg_R"] == 2.0 and r["avg_R_net"] == 1.9 and r["avg_mae_R"] == pytest.approx(0.67, abs=0.01)


# ---------------- bar sizes ----------------
@pytest.mark.parametrize("iv,bars,step", [("5m", 72, 5), ("10m", 36, 10), ("15m", 24, 15)])
def test_time_machine_bar_sizes(iv, bars, step):
    tm.set_bar(iv)
    assert tm.SESSION_BARS == bars and tm.BAR_MIN == step
    tm.set_bar("5m")


def test_time_machine_refuses_other_bar_sizes():
    with pytest.raises(tm.TMError):
        tm.set_bar("3m")
    tm.set_bar("5m")


def test_ten_minute_bars_sit_on_the_0915_grid():
    tm.set_bar("10m")
    open_ts = int(datetime(2026, 11, 5, 3, 45).timestamp()) if False else 1793936700   # 2026-11-05 09:15 IST
    res = {"meta": {}, "timestamp": [open_ts, open_ts + 600, open_ts + 300], "indicators": {"quote": [{k: [100, 100, 100] for k in ("open", "high", "low", "close", "volume")}]}}
    _, bars, dropped = tm.parse_bars(res, tm.utc_of(open_ts + 86400))
    tm.set_bar("5m")
    assert [b["slot"] for b in bars] == [0, 1] and dropped["off_grid"] == 1


def test_a_future_volume_overlay_changes_only_index_bars():
    bs = [{"date": "2026-11-05", "slot": 0, "v": 0}]
    india.apply_volumes(bs, {("2026-11-05", 0): 500})
    assert bs[0]["v"] == 500


# ---------------- option maths ----------------
def test_iv_solve_inverts_the_price():
    p = india.b76(25000, 25000, 5 / 365.25, 0.15, True)
    assert india.solve_iv(p, 25000, 25000, 5 / 365.25, True) == pytest.approx(0.15, abs=1e-4)
    assert india.solve_iv(5, 25500, 25000, 0.01, True) is None        # below intrinsic


def test_a_credit_spread_is_valued_as_what_it_costs_to_buy_back():
    import option_tm
    plan = {"instrument": "spread", "credit": True, "legs": [{"side": "buy", "strike": 24500, "option_type": "PE", "iv": 20}, {"side": "sell", "strike": 24800, "option_type": "PE", "iv": 20}]}
    v = option_tm.value(option_tm.legs_of(plan), 25000, 5 / 365.25, 1.0, 0.0)
    assert v > 0
    assert option_tm._outcome(True, 200, 50, 210, 100, 150, 100) == "stop" and option_tm._outcome(True, 200, 50, 150, 40, 100, 100) == "target"


# ---------------- budget ----------------
PLAN = {"instrument": "option", "symbol": "NIFTY26N1725000CE", "strategy": "atm_buy", "underlying": "NIFTY", "exchange": "NFO", "expiry": "2099-11-17", "lot_size": 65, "entry": 100.0, "stop": 70.0, "target": 160.0, "risk_per_share": 30.0, "side": "buy", "date": "2026-11-09", "reward_risk": 2.0, "flat_by": "15:15"}


def test_lots_fit_the_loss_with_charges():
    s = fno_size.size(PLAN, {"max_loss_per_trade_inr": 4000, "cash_buffer_pct": 0}, 100000, None, 6500)
    assert s["lots"] == 1 and s["qty"] == 65 and s["max_loss"] == pytest.approx(1950 + s["costs"], abs=0.01)
    assert fno_size.size(PLAN, {"max_loss_per_trade_inr": 1000, "cash_buffer_pct": 0}, 100000, None, 6500)["refusal"]


def test_money_limits_lots():
    assert fno_size.size(PLAN, {"max_loss_per_trade_inr": 100000, "cash_buffer_pct": 0}, 13000, None, 6500)["lots"] == 2


def test_a_spread_orders_the_bought_leg_first():
    plan = dict(PLAN, instrument="spread", legs=[{"symbol": "NIFTY26N1725000CE", "side": "buy", "premium": 100, "strike": 25000, "option_type": "CE"}, {"symbol": "NIFTY26N1725200CE", "side": "sell", "premium": 40, "strike": 25200, "option_type": "CE"}], entry=60, stop=30, target=120, risk_per_share=30, worst_per_unit=60)
    s = fno_size.size(plan, {"max_loss_per_trade_inr": 5000, "cash_buffer_pct": 0}, 100000, None, 10000)
    orders = fno_size.orders_for(plan, s, "15:15")
    assert [o["side"] for o in orders] == ["buy", "sell"] and orders[1]["plan"]["max_loss"] == 0.0


# ---------------- risk ----------------
NOW = tm.parse_now("2026-11-09 10:30")
CFG = {"budget": 100000, "allow_options": True, "max_loss_per_trade_inr": 3000, "max_daily_loss_inr": 9000, "cash_buffer_pct": 0}


def option_order(**kw):
    s = fno_size.size(PLAN, {"max_loss_per_trade_inr": 4000, "cash_buffer_pct": 0}, 100000, None, 6500)
    o = fno_size.orders_for(PLAN, s, "15:15")[0]
    o.update(kw)
    return o


def verdict(orders, cfg=CFG, now=NOW):
    return check.check(orders, cfg, None, {}, None, None, now, {})


def test_an_option_passes_with_options_on():
    r = verdict([option_order()])
    assert r["pass"] == 1, r["results"][0]["reasons"]
    assert any("lose all" in w for w in r["results"][0]["warnings"])


def test_options_are_refused_when_off():
    r = verdict([option_order()], {**CFG, "allow_options": False})
    assert r["block"] == 1 and "options are off" in r["results"][0]["reasons"][0]


def test_agent_desk_config_still_refuses_options(tmp_path):
    cfg = {"budget": 100000, "cash_buffer_pct": 0, "max_position_pct": 100, "max_order_inr": 100000, "max_loss_per_trade_inr": None, "max_daily_loss_inr": None, "allow_options": False, "allow_margin": False, "allow_short": False}
    assert verdict([option_order()], cfg)["block"] == 1


def test_a_contract_with_no_details_is_blocked_even_with_a_long_name():
    for sym in ("NIFTY26N1725000CE", "BANKNIFTY26NOV52000PE", "RELIANCE26NOVFUT"):
        r = verdict([{"symbol": sym, "side": "buy", "qty": "65", "type": "market"}])
        assert r["block"] == 1 and "F&O contract" in r["results"][0]["reasons"][0]
    assert not fno_check.is_fno_symbol("RELIANCE") and not fno_check.is_fno_symbol("M&M")


def test_a_naked_option_sale_is_blocked():
    r = verdict([option_order(side="sell", order_class=None)])
    assert r["block"] == 1 and "naked" in r["results"][0]["reasons"][0]


def test_a_sold_leg_needs_a_matching_bought_leg_first():
    plan = dict(PLAN, instrument="spread", legs=[{"symbol": "NIFTY26N1725000CE", "side": "buy", "premium": 100, "strike": 25000, "option_type": "CE"}, {"symbol": "NIFTY26N1725200CE", "side": "sell", "premium": 40, "strike": 25200, "option_type": "CE"}], entry=60, stop=30, target=120, risk_per_share=30, worst_per_unit=60)
    s = fno_size.size(plan, {"max_loss_per_trade_inr": 3000, "cash_buffer_pct": 0}, 100000, None, 10000)
    orders = fno_size.orders_for(plan, s, "15:15")
    assert verdict(orders)["pass"] == 2
    alone = verdict(orders[1:])
    assert alone["block"] == 1 and "naked" in alone["results"][0]["reasons"][0]
    assert "before" in verdict(orders[::-1])["results"][0]["reasons"][0]


def test_lots_loss_and_late_buying_are_blocked():
    assert "whole number of lots" in verdict([option_order(qty="60")])["results"][0]["reasons"][0]
    assert any("max loss" in x for x in verdict([option_order()], {**CFG, "max_loss_per_trade_inr": 500})["results"][0]["reasons"])
    late = verdict([option_order()], {**CFG, "no_option_buys_after": "14:30"}, tm.parse_now("2026-11-09 14:45"))
    assert any("no option buying" in x for x in late["results"][0]["reasons"])


def test_two_entries_count_against_the_daily_limit():
    r = verdict([option_order(), option_order(symbol="NIFTY26N1725100CE")], {**CFG, "max_daily_loss_inr": 3000})
    assert r["pass"] == 1 and any("daily" in x for x in r["results"][1]["reasons"])


def test_the_pipeline_gate_blocks_a_skipped_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trader").mkdir()
    (tmp_path / "trader" / "plan.json").write_text(json.dumps(dict(PLAN, date="2026-11-09")), encoding="utf-8")
    r = verdict([option_order()], {**CFG, "enforce_pipeline": True})
    reasons = " ".join(r["results"][0]["reasons"])
    assert r["block"] == 1 and all(w in reasons for w in ("scanner", "news", "entry", "time machine"))


def test_the_pipeline_gate_passes_when_every_step_ran(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = tmp_path / "trader"
    (t / "state").mkdir(parents=True)
    day = "2026-11-09"
    (t / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    (t / "watchlist.json").write_text(json.dumps({"created_at": day, "hits": [{"symbol": "NIFTY"}]}), encoding="utf-8")
    (t / "state" / "news.json").write_text(json.dumps({"checked": {"NIFTY": day}}), encoding="utf-8")
    (t / "entry.json").write_text(json.dumps({"symbol": "NIFTY", "status": "triggered", "date": day}), encoding="utf-8")
    (t / "timemachine.json").write_text(json.dumps({"plan": PLAN, "mc": {"avg_R_net": 0.2}}), encoding="utf-8")
    assert india.pipeline_gaps(PLAN, day) == []
    assert verdict([option_order()], {**CFG, "enforce_pipeline": True, "require_positive_timemachine": True})["pass"] == 1
    (t / "timemachine.json").write_text(json.dumps({"plan": PLAN, "mc": {"avg_R_net": -0.2}}), encoding="utf-8")
    assert any("not above zero" in x for x in verdict([option_order()], {**CFG, "enforce_pipeline": True, "require_positive_timemachine": True})["results"][0]["reasons"])


def test_stock_max_loss_now_includes_charges():
    order = {"symbol": "RELIANCE", "side": "buy", "qty": "100", "type": "market", "time_in_force": "day", "order_class": "bracket", "stop_loss_stop_price": "990.00", "take_profit_limit_price": "1020.00", "plan": {"entry": 1000, "stop": 990, "target": 1020}}
    r = verdict([order], {**CFG, "budget": 200000, "max_loss_per_trade_inr": 1001, "max_order_inr": 200000})
    assert r["block"] == 1 and "charges" in " ".join(r["results"][0]["reasons"])


# ---------------- broker ----------------
@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = tmp_path / "trader"
    t.mkdir()
    (t / "orders.json").write_text(json.dumps({"orders": [option_order()]}), encoding="utf-8")
    return t


def run_ticket(*args):
    return ticket.main([*args, "--now", "2026-11-09 10:30"])


def ready(t, paper=True):
    (t / "config.json").write_text(json.dumps({**CFG, "paper": paper}), encoding="utf-8")
    assert check.main(["--now", "2026-11-09 10:30", "--orders", "trader/orders.json", "--config", "trader/config.json", "--out", "trader/state/risk.json"]) == 0


def test_a_live_config_needs_the_exact_phrase_and_still_sends_nothing(desk, capsys):
    ready(desk, paper=False)
    assert run_ticket("approve", "--index", "1", "--reply", "yes") == 1
    assert "YES EXECUTE LIVE" in capsys.readouterr().out
    assert run_ticket("approve", "--index", "1", "--reply", "yes execute live") == 1
    assert run_ticket("approve", "--index", "1", "--reply", "YES EXECUTE LIVE") == 0
    assert "Trade page" in capsys.readouterr().out
    assert run_ticket("paper", "--index", "1") == 1


def test_the_paper_book_after_charges(desk, capsys):
    ready(desk)
    assert run_ticket("paper", "--index", "1") == 1                # no yes yet
    assert run_ticket("approve", "--index", "1", "--reply", "yes") == 0
    assert run_ticket("paper", "--index", "1") == 0
    book = json.loads((desk / "state" / "paper.json").read_text(encoding="utf-8"))
    assert book["open"][0]["entry"] == 100.05 and book["open"][0]["qty"] == 65
    assert ticket.main(["paper-close", "--symbol", "NIFTY26N1725000CE", "--price", "160", "--now", "2026-11-09 11:00"]) == 0
    c = json.loads((desk / "state" / "paper.json").read_text(encoding="utf-8"))["closed"][0]
    assert c["gross"] == pytest.approx((160 - 100.05) * 65, abs=0.01) and c["net"] == pytest.approx(c["gross"] - c["costs"], abs=0.01) and c["costs"] > 40
    capsys.readouterr()
    assert ticket.main(["pnl", "--all"]) == 0
    assert "after charges" in capsys.readouterr().out


def test_no_skill_or_desk_file_can_place_an_order():
    for p in [*SK.glob("_india/*.py"), *SK.glob("trader-*/*.py"), *(ROOT / "kite-login/backend").glob("*/*.py")]:
        assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt|exit_order)\s*\(", p.read_text(encoding="utf-8")), p


# ---------------- telegram ----------------
def test_nothing_is_sent_during_tests(monkeypatch):
    monkeypatch.setattr(notify, "configured", lambda: True)
    assert notify.send("hello", force=True) is False


def test_the_token_is_found_among_other_words_and_never_reported(monkeypatch):
    token = "123456789:" + "A" * 35
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "my bot " + token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert notify._token() == token and notify.configured()
    assert token not in json.dumps(notify.status())


def test_a_failed_send_never_keeps_the_token(monkeypatch):
    token = "123456789:" + "A" * 35
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)

    def boom(url, **kw):
        raise notify.requests.ConnectionError(f"cannot reach {url}")
    monkeypatch.setattr(notify.requests, "post", boom)
    notify._deliver("x")
    assert token not in json.dumps(notify.status()) and "ConnectionError" in notify.status()["last_error"]


def test_calls_are_sent_once_each():
    t = {"symbol": "RELIANCE", "side": "LONG", "qty": 10, "entry": 100, "stop": 99, "target": 102, "entry_time": "10:00", "status": "open", "net": 0}
    first = notify.diff_calls("agents", "Agent Desk", "2026-11-09", [t], set())
    assert len(first) == 1 and "NEW CALL" in first[0][1]
    assert notify.diff_calls("agents", "Agent Desk", "2026-11-09", [t], {first[0][0]}) == []
    closed = dict(t, status="closed", exit=102, exit_reason="target", exit_time="11:00", net=15.0, r=1.5)
    assert notify.diff_calls("agents", "Agent Desk", "2026-11-09", [closed], {first[0][0]}) == []          # no exit message
    assert "NEW CALL" in notify.diff_calls("agents", "Agent Desk", "2026-11-09", [closed], set())[0][1]    # a call first seen closed is still announced


def test_watch_covers_every_desk(monkeypatch, tmp_path):
    monkeypatch.setattr(notify, "STATE", tmp_path / "t.json")
    monkeypatch.setattr(notify, "configured", lambda: True)
    monkeypatch.setattr(notify, "send", lambda text, force=False: sent.append(text) or True)
    sent = []
    today = datetime.now(notify.IST).date().isoformat()
    t = {"symbol": "X", "entry_time": "10:00", "status": "open", "net": 0, "session": today}
    monkeypatch.setattr(notify.paper_desk, "view", lambda k: {"open": [dict(t, symbol=k)], "closed": []})
    assert notify.watch_once() == len(notify.paper_desk.DESKS)
    assert notify.watch_once() == 0 and len(sent) == len(notify.paper_desk.DESKS)


def test_slippage_comes_off_the_odds():
    import odds
    hist = {"trades": [{"date": "d", "R_net": 1.0}, {"date": "e", "R_net": -0.05}], "win_rate": 0.5, "avg_R_net": 0.475, "profit_factor": 20.0}
    out = odds._after_slippage(hist, 0.1)
    assert out["win_rate"] == 0.5 and out["avg_R_net"] == pytest.approx(0.375) and [t["R_net"] for t in out["trades"]] == [0.9, -0.15]
    assert odds._after_slippage(hist, 0.0) == hist


# ---------------- the Telegram channel ----------------
def _capture_posts(monkeypatch):
    token = "123456789:" + "A" * 35
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    posts = []

    class Ok:
        ok = True
        status_code = 200
    monkeypatch.setattr(notify.requests, "post", lambda url, json=None, timeout=None: posts.append(json) or Ok())
    return posts


def test_a_call_goes_to_the_chat_and_the_channel(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")
    notify._deliver(notify.signal_text({"desk": "ORB", "instrument": "AAA"}))
    assert [p["chat_id"] for p in posts] == ["42", "-100123"] and posts[0]["text"] == posts[1]["text"] and notify.status()["channel"] is True


def test_other_messages_never_reach_the_channel(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")
    notify._deliver("✅ Sentinel Trade: Telegram is connected.")
    assert [p["chat_id"] for p in posts] == ["42"]


def test_no_channel_set_means_the_chat_only(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    monkeypatch.setattr(notify, "SECRETS", notify.Path("no-such-file"))
    notify._deliver(notify.signal_text({"desk": "ORB", "instrument": "AAA"}))
    assert [p["chat_id"] for p in posts] == ["42"] and notify.status()["channel"] is False


def test_the_status_never_carries_the_channel_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100987654321")
    assert "987654321" not in json.dumps(notify.status())


# ---------------- one Telegram message ----------------
def test_the_message_is_exactly_this_card():
    text = notify.signal_text({"desk": "Agent Desk · Options", "instrument": "NIFTY26N1724900CE", "setup": "15-minute opening-range breakout", "time": "09:35", "date": "2026-11-09", "entry": 278.75, "stop": 266.2, "target": 303.8, "qty": 325, "lots": 5, "max_loss": 4340.74, "confidence": "High", "success_rate_pct": 50.0, "sample": 34})
    assert text == """🟢 NEW CALL · 09 Nov 2026 · 09:35

NIFTY 24900 CE · Exp 17 Nov
BUY CALL · 5 lots (325 qty)
--------------------------------
Entry                    ₹278.75
Stop                     ₹266.20
Target                   ₹303.80
--------------------------------
Max loss               ₹4,340.74
Reward : Risk               2.0R
Confidence            High · 50%
--------------------------------
Agent Desk Options · 15-min ORB"""


def test_a_put_a_monthly_contract_and_an_unrated_cash_call():
    assert "BUY PUT" in notify.signal_text({"instrument": "NIFTY26N1724900PE"})
    assert "GLENMARK 2700 CE · Exp Nov" in notify.signal_text({"instrument": "GLENMARK26NOV2700CE"}) and "GLENMARK 2700 CE · Exp 24 Nov" in notify.signal_text({"instrument": "GLENMARK26NOV2700CE", "expiry": "2026-11-24"})
    t = notify.signal_text({"desk": "ORB", "instrument": "RELIANCE", "side": "LONG", "entry": 100, "stop": 99, "target": 102, "qty": 50})
    assert "BUY · 50 qty" in t and re.search(r"Max loss\s+₹50.00", t) and re.search(r"Confidence\s+Not rated", t) and t.endswith("ORB")


def test_a_missing_target_shows_a_dash():
    t = notify.signal_text({"desk": "Nifty ORB Options", "instrument": "NIFTY26N0924950PE", "entry": 146.84, "stop": 117.24, "qty": 130})
    assert re.search(r"Target\s+–", t) and re.search(r"Reward : Risk\s+–", t)


def test_unknown_fields_never_reach_a_message():
    assert "SECRET" not in notify.signal_text({"instrument": "AAA", "token": "SECRET", "chat_id": "SECRET"})


# ---------------- the end-of-day report ----------------
def _won(desk="Agent Pipeline · Swing", net=500.0):
    return {"desk": desk, "symbol": "AAA", "entry": 100.0, "exit": 110.0, "at": "12:00", "reason": "target", "net": net, "r": 1.5, "done": True}


def test_the_report_is_the_table():
    from strategies import eod_report as eod
    row = lambda desk, sym, net, r: dict(_won(desk, net), symbol=sym, r=r)  # noqa: E731
    text = eod.report_text([row("Agent Pipeline · Cash", "DLF", -280.60, -0.24), row("Multi Agent Team", "BANKNIFTY26NOV59000PE", 2183.68, 0.16), row("Agent Pipeline · Options", "NIFTY26N0924950PE", -3933.53, -1.01), row("Agent Pipeline · Swing", "GLENMARK", 982.16, 1.13), row("Agent Pipeline · Cash", "LAURUSLABS", 875.46, 0.59), row("Multi Agent Team", "GLENMARK", 377.79, 0.24)], "2026-11-09")
    assert text == ["""📊 END OF DAY · 09 Nov 2026

Net +₹204.96 | 4W 2L | 67%

Stock/Option         Net ₹     R
--------------------------------
MAT BN 59000PE   +2,183.68 +0.16
PS GLENMARK        +982.16 +1.13
PC LAURUSLABS      +875.46 +0.59
MAT GLENMARK       +377.79 +0.24
PC DLF             −280.60 −0.24
PO N 24950PE     −3,933.53 −1.01
--------------------------------
TOTAL              +204.96

MAT = Multi Agent Team
PC = Agent Pipeline Cash
PO = Agent Pipeline Options
PS = Agent Pipeline Swing"""]


def test_option_names_fit_the_table_and_an_open_call_shows():
    from strategies import eod_report as eod
    for desk, sym, want in (("Agent Pipeline · Options", "ADANIENSOL26NOV1420CE", "PO ADANIE 1420CE"), ("Multi Agent Team", "BANKNIFTY26NOV59000PE", "MAT BN 59000PE"), ("Multi Agent Team", "NIFTY26N1724900CE", "MAT N 24900CE")):
        assert eod._label({"desk": desk, "symbol": sym}) == want
    open_row = {"desk": "Agent Pipeline · Swing", "symbol": "BBB", "entry": 50.0, "exit": None, "at": None, "reason": None, "net": None, "r": None, "done": False}
    text = eod.report_text([_won(), open_row], "2026-11-09")[0]
    assert "| 1W 0L | 100% | 1 open" in text and "PS BBB" in text and text.index("PS AAA") < text.index("PS BBB")


def test_a_quiet_day_says_so():
    from strategies import eod_report as eod
    assert eod.report_text([], "2026-11-09") == ["📊 END OF DAY · 09 Nov 2026\n\nNo calls today."]


def test_a_long_report_is_split_under_the_limit_and_loses_nothing():
    from strategies import eod_report as eod
    pages = eod.report_text([_won() for _ in range(300)], "2026-11-09")
    assert len(pages) > 1 and all(p.startswith("📊 END OF DAY") and len(p) <= notify.MAX_LEN for p in pages) and sum(p.count("PS AAA") for p in pages) == 300 and "TOTAL" in pages[-1] and "TOTAL" not in pages[0]


def test_reports_and_calls_are_sent_as_fixed_width_text(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    monkeypatch.setattr(notify, "SECRETS", notify.Path("no-such-file"))
    notify._deliver("📊 END OF DAY · a<b")
    assert posts[0]["parse_mode"] == "HTML" and posts[0]["text"] == "<pre>📊 END OF DAY · a&lt;b</pre>"
    notify._deliver(notify.signal_text({"desk": "ORB", "instrument": "AAA"}))
    assert posts[1]["parse_mode"] == "HTML" and posts[1]["text"].startswith("<pre>🟢 NEW CALL")
    notify._deliver("plain words")
    assert "parse_mode" not in posts[2]


def test_a_sample_goes_to_the_chat_only(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")
    notify._deliver("🧪 SAMPLE: not a real call")
    assert [p["chat_id"] for p in posts] == ["42"] and posts[0]["parse_mode"] == "HTML"


def test_the_report_goes_out_once_after_the_close_on_a_trading_day(monkeypatch):
    from strategies import eod_report as eod
    sent, have = [], set()
    monkeypatch.setattr(eod.notify, "enabled", lambda: True)
    monkeypatch.setattr(eod.notify, "configured", lambda: True)
    monkeypatch.setattr(eod.notify, "was_sent", lambda mid, now=None: mid in have)
    monkeypatch.setattr(eod.notify, "send_once", lambda mid, text, now=None: have.add(mid) or sent.append(text) or True)
    monkeypatch.setattr(eod, "collect", lambda day: [_won()])
    eod.tick(datetime(2026, 11, 9, 15, 39, tzinfo=notify.IST))
    assert sent == []
    eod.tick(datetime(2026, 11, 9, 15, 40, tzinfo=notify.IST))
    eod.tick(datetime(2026, 11, 9, 15, 41, tzinfo=notify.IST))
    assert len(sent) == 1
    eod.tick(datetime(2026, 11, 8, 16, 0, tzinfo=notify.IST))
    assert len(sent) == 1


def test_the_report_goes_to_the_channel_too(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHANNEL_ID", "-100123")
    notify._deliver("📊 END OF DAY · 09 Nov 2026")
    assert [p["chat_id"] for p in posts] == ["42", "-100123"]


def test_a_spread_call_shows_both_legs_and_its_limits():
    legs = [{"strike": 24550.0, "sign": 1, "contract": "NIFTY26N0924550PE"}, {"strike": 24800.0, "sign": -1, "contract": "NIFTY26N0924800PE"}]
    text = notify.open_text("Nifty Expiry Spread", {"symbol": "NIFTY 24800/24550 PE", "side": "SHORT", "legs": legs, "lots": 1, "entry": 36.5, "stop": 250.0, "target": 0.0, "qty": 65, "entry_time": "09:15"}, "2026-11-09")
    assert text == """🟢 NEW CALL · 09 Nov 2026 · 09:15

NIFTY PUT SPREAD · Exp 09 Nov
SELL 24800 PE
BUY  24550 PE
1 lot (65 qty)
--------------------------------
Net credit                ₹36.50
Exit                 15:15 close
--------------------------------
Max profit             ₹2,372.50
Max loss              ₹13,877.50
Confidence             Not rated
--------------------------------
Nifty Expiry Spread"""
    assert "BUY" in notify.open_text("ORB", {"symbol": "AAA", "side": "LONG"})
