"""Claude Skill Desk: the skill pack is there, Claude is locked down, every code check, sizing, following trades, and no order path."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from strategies import all_desks, eod_report as eod, skill_desk as sd

SRC = (Path(__file__).resolve().parents[1] / "backend" / "strategies" / "skill_desk.py").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=sd.common.IST)
DAY = "2026-11-10"


def chain(dte=7, spread=(99.0, 101.0), oi=100000):
    rows = [{"strike": s, "ce": {"tradingsymbol": f"NIFTY26N{s}CE", "ltp": 100, "bid": spread[0], "ask": spread[1], "oi": oi, "delta": 0.5, "iv": 13},
             "pe": {"tradingsymbol": f"NIFTY26N{s}PE", "ltp": 100, "bid": spread[0], "ask": spread[1], "oi": oi, "delta": -0.5, "iv": 14}} for s in range(25700, 26350, 50)]
    return {"atm": 26000, "expiry": "2026-11-17", "days_to_expiry": dte, "lot_size": 65, "rows": rows}


def ctx(kind="index", c=None, vix=15.0, sym="NIFTY"):
    return {"underlying": sym, "instrument_type": kind, "india_vix": vix, "option_chain": chain() if c is None else c}


def buy(**kw):
    return {"action": "BUY", "vehicle": "option", "horizon": "intraday", "instrument": "NIFTY26N26000CE", "entry": 100, "stop_loss": 80, "target": 150, "quantity": 650, "confidence": 58,
            "skills_used": ["regime-detection"], "thesis": "t", "risks": [], "invalidation": "i"} | kw


@pytest.fixture
def book(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "DIR", tmp_path)
    monkeypatch.setattr(sd.notify, "send_once", lambda *a, **k: True)
    sd._running.update(underlying=None, since=None)
    return sd


# ---------------------------------------------------------------- the skill pack and Claude

def test_the_skill_pack_is_complete_and_keeps_the_source_licence():
    refs = {p.name for p in (sd.SKILL_DIR / "references").glob("*.md")}
    assert {"desk-playbook.md", "market-structure-india.md", "strategies.md", "options-volatility.md", "risk.md", "backtesting.md", "execution.md", "regime-signals.md",
            "portfolio.md", "macro-india.md", "data-sources.md", "compliance-sebi.md", "roles.md", "workflows.md"} <= refs
    assert (sd.SKILL_DIR / "LICENSE-everything-claude-trading.txt").read_text(encoding="utf-8").startswith("MIT License")
    head = (sd.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert "name: india-quant-desk" in head
    assert "desk-playbook" in sd.system_prompt() or "Claude Skill Desk" in sd.system_prompt()


WALK = [{"role": "regime", "verdict": "supports", "finding": "VIX 13.8, \"normal\" {range}."}, {"role": "technical", "verdict": "supports", "finding": "Above VWAP 25,960."}]


class FakeTool:
    """Stands in for the Claude tool's stream: thinking, then the answer in small pieces, then the result."""
    def __init__(self, answer, pieces=7, stderr=b"", result=True):
        text = json.dumps(answer)
        step = max(1, len(text) // pieces)
        events = [{"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "thinking"}}}]
        events += [{"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": text[i:i + step]}}} for i in range(0, len(text), step)]
        if result:
            events.append({"type": "result", "subtype": "success", "is_error": False, "structured_output": answer})
        self.stdout = [json.dumps(e).encode() + b"\n" for e in events]
        self.stderr = type("E", (), {"read": lambda s: stderr})()
        self.stdin = type("I", (), {"write": lambda s, b: setattr(self, "sent", b), "close": lambda s: None})()
        self.returncode = 0

    def wait(self):
        return 0

    def kill(self):
        pass


def test_claude_runs_with_no_tools_no_settings_in_an_empty_folder_and_no_api_key(monkeypatch):
    seen, steps = {}, []

    def popen(args, **kw):
        seen.update(args=args, env=kw["env"], files=list(Path(kw["cwd"]).iterdir()))
        seen["tool"] = FakeTool(buy(walkthrough=WALK))
        return seen["tool"]
    monkeypatch.setattr(sd.subprocess, "Popen", popen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    out = sd.ask_claude({"x": 1}, "claude", lambda stage, done, writing: steps.append((stage, len(done), writing and writing["role"])))
    assert out["action"] == "BUY"
    a = seen["args"]
    assert a[a.index("--tools") + 1] == "" and a[a.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in a and "--disable-slash-commands" in a
    assert "ANTHROPIC_API_KEY" not in seen["env"] and seen["files"] == [] and b'"x": 1' in seen["tool"].sent
    assert steps[0][0] == "thinking" and any(s[1] == 2 for s in steps)        # the roles arrived one by one while Claude wrote


def test_the_walkthrough_is_read_while_claude_is_still_writing_it():
    text = json.dumps({"walkthrough": WALK, "action": "BUY"})
    cut = text.index("25,9") + 4
    done, writing = sd.walkthrough_so_far(text[:cut])
    assert done == WALK[:1] and writing == {"role": "technical", "finding": "Above VWAP 25,9"}
    assert sd.walkthrough_so_far(text) == (WALK, None)


def test_a_tool_failure_never_echoes_what_the_tool_said(monkeypatch):
    monkeypatch.setattr(sd.subprocess, "Popen", lambda *a, **k: FakeTool({}, stderr=b"token sk-abc expired, please login", result=False))
    with pytest.raises(ValueError) as e:
        sd.ask_claude({}, "claude")
    assert "sk-abc" not in str(e.value) and "signed in" in str(e.value)


# ---------------------------------------------------------------- the checks

def test_a_good_intraday_option_buy_is_sized_to_the_rupee_cap_in_whole_lots(book):
    r = book.check(buy(), ctx(), NOW, lambda k: 100.0, rows=[])
    assert r["ok"], r["why"]
    e = r["position"]
    assert e["qty"] % 65 == 0 and e["max_loss"] <= book.MAX_RISK and e["entry"] == 100.3 and e["product"] == "MIS"
    assert e["qty"] < 650                                   # Claude asked for 10 lots; the cap cut it


@pytest.mark.parametrize("change, why", [
    ({"action": "SKIP"}, "skipped"),
    ({"instrument": "NIFTY26N99999CE"}, "isn't in the option chain"),
    ({"instrument": "NIFTY26N26300CE"}, "strikes from the money"),
    ({"stop_loss": 101}, "isn't between"),
    ({"target": 120}, "under 1.5x"),
    ({"stop_loss": 40, "target": 400}, "lose about"),
    ({"vehicle": "shares", "instrument": "NIFTY"}, "an index can't be bought as shares"),
])
def test_each_rule_can_stop_a_buy(book, change, why):
    r = book.check(buy(**change), ctx(), NOW, lambda k: 100.0, rows=[])
    assert not r["ok"] and why in r["why"]


def test_liquidity_vix_expiry_and_time_rules(book):
    assert "spread" in book.check(buy(), ctx(c=chain(spread=(90, 110))), NOW, lambda k: 100.0, rows=[])["why"]
    assert "India VIX" in book.check(buy(), ctx(vix=11.0), NOW, lambda k: 100.0, rows=[])["why"]
    assert "21+ days" in book.check(buy(horizon="swing"), ctx(c=chain(dte=10)), NOW, lambda k: 100.0, rows=[])["why"]
    assert "after 14:30" in book.check(buy(), ctx(), NOW.replace(hour=14, minute=45), lambda k: 100.0, rows=[])["why"]
    stock = ctx(kind="stock", c=chain(oi=65 * 10), sym="NIFTY")
    assert "open interest" in book.check(buy(), stock, NOW, lambda k: 100.0, rows=[])["why"]


def test_shares_are_long_only_capped_by_value_and_use_delivery_charges_for_swing(book):
    d = buy(vehicle="shares", horizon="swing", instrument="RELIANCE", stop_loss=1490, target=1530, quantity=500)
    r = book.check(d, ctx(kind="stock", sym="RELIANCE"), NOW, lambda k: 1500.0, rows=[])
    assert r["ok"], r["why"]
    e = r["position"]
    assert e["product"] == "CNC" and e["entry"] * e["qty"] <= book.MAX_CASH_POSITION and e["max_loss"] <= book.MAX_RISK
    assert book.fees(e, 100000, 100000) > book.fees({**e, "horizon": "intraday"}, 100000, 100000)     # STT on delivery is far higher
    assert "must be RELIANCE" in book.check(buy(vehicle="shares", instrument="TCS"), ctx(kind="stock", sym="RELIANCE"), NOW, lambda k: 1500.0, rows=[])["why"]


def test_the_book_limits_open_trades_one_per_name_and_the_day_loss(book):
    held = [{"id": str(i), "status": "open", "underlying": f"S{i}", "entry": 10, "qty": 1, "ltp": 10, "vehicle": "shares", "horizon": "intraday", "day": DAY} for i in range(3)]
    assert "already open" in book.check(buy(), ctx(), NOW, lambda k: 100.0, rows=held)["why"]
    same = [{**held[0], "underlying": "NIFTY"}]
    assert "already has an open trade on NIFTY" in book.check(buy(), ctx(), NOW, lambda k: 100.0, rows=same)["why"]
    lost = [{"id": "x", "status": "closed", "underlying": "TCS", "net": -10500, "exit_day": DAY, "day": DAY, "vehicle": "shares", "horizon": "intraday"}]
    assert "day limit" in book.check(buy(), ctx(), NOW, lambda k: 100.0, rows=lost)["why"]


# ---------------------------------------------------------------- following

def pos(**kw):
    return {"id": "p", "instrument": "NIFTY26N26000CE", "underlying": "NIFTY", "vehicle": "option", "horizon": "intraday", "entry": 100.0, "stop": 80.0, "target": 150.0, "qty": 65, "slip": 0.003,
            "status": "open", "day": DAY, "entry_bar": "10:00", "sessions": 0, "ltp": 100.0} | kw


def test_intraday_trades_hit_stop_or_target_or_close_at_1515():
    e = pos()
    sd.walk(e, {"10:00": (100, 110, 95, 105), "10:05": (105, 152, 104, 150)}, DAY, "10:05")
    assert e["status"] == "closed" and e["exit_reason"] == "target" and e["net"] > 0
    e = pos()
    sd.walk(e, {"10:00": (100, 101, 79, 80)}, DAY, "10:00")
    assert e["exit_reason"] == "stop" and e["net"] < 0
    e = pos()
    sd.walk(e, {t: (100, 101, 99, 100) for t in sd.common.LABELS if t >= "10:00"}, DAY, "15:25")
    assert e["exit_reason"] == "15:15 exit"


def test_swing_trades_carry_over_gap_through_stops_and_close_on_the_session_or_expiry_rule():
    e = pos(horizon="swing", day="2026-11-09")
    sd.walk(e, {t: (100, 101, 99, 100) for t in sd.common.LABELS}, DAY, "15:25")
    assert e["status"] == "open"                           # no 15:15 exit for swing
    gap = pos(horizon="swing", day="2026-11-09")
    sd.walk(gap, {"09:15": (70, 72, 68, 71)}, DAY, "09:15")
    assert gap["exit_reason"] == "stop" and gap["exit"] < 70   # filled at the open, below the stop
    old = pos(horizon="swing", sessions=sd.HOLD_OPTION - 1, expiry="2026-12-29")
    sd.end_of_day(old, DAY)
    assert old["exit_reason"] == f"{sd.HOLD_OPTION} sessions"
    near = pos(horizon="swing", expiry="2026-11-16")
    sd.end_of_day(near, DAY)
    assert "days to expiry" in near["exit_reason"]
    once = pos(horizon="swing", expiry="2026-12-29")
    sd.end_of_day(once, DAY)
    sd.end_of_day(once, DAY)
    assert once["sessions"] == 1


# ---------------------------------------------------------------- a run end to end

def test_a_run_records_claudes_answer_the_checks_and_a_paper_trade(book, monkeypatch):
    monkeypatch.setattr(book, "snapshot", lambda t, now: {**ctx(), "spot": 26000, "vwap": 25990, "option_chain": chain()})
    monkeypatch.setattr(book, "ask_claude", lambda c, tool, progress=None: buy(walkthrough=WALK))
    monkeypatch.setattr(book, "_live_price", lambda k: 100.0)
    monkeypatch.setattr(book, "clock", lambda: NOW)
    book._work({"underlying": "NIFTY", "kind": "index", "horizon": "intraday", "setup": "s", "direction": "up", "at": "10:00", "why": "w"}, NOW, "claude")
    r = book.runs(DAY)[0]
    assert r["decision"]["action"] == "BUY" and r["checks"] and r["paper"] == {"taken": True}
    assert book.positions()[0]["id"] == r["id"] and book.positions()[0]["status"] == "open"
    v = book.view(DAY)
    assert v["paper_mode"] and v["skill"]["name"] == "india-quant-desk" and v["runs"][0]["id"] == r["id"]
    run = v["live"]["run"]
    assert run["stage"] == "done" and run["roles"] == WALK and run["paper"] == {"taken": True} and len(v["live"]["roles"]) == 8


def test_the_scanner_shows_each_name_as_it_is_checked(book, monkeypatch):
    monkeypatch.setattr(book, "universe", lambda: [("NIFTY", None), ("TCS", "up"), ("XYZ", "up")])
    monkeypatch.setattr(book.setup_scan, "regime", lambda d: {})
    states = []

    def find(sym, *a, **k):
        states.append([n["state"] for n in book._live["scan"]["names"]])
        if sym == "XYZ":
            raise book.india.IndiaError("no candles")
        return [{"key": f"k{sym}", "direction": "up", "setup": "momentum_rvol", "technical": {"confirmed": sym == "TCS"}, "at": "10:00", "why": "w"}]
    monkeypatch.setattr(book.setup_scan, "find", find)
    found, _ = book.intraday_setups(NOW)
    assert states[0] == ["scanning", "queued", "queued"] and states[1][:2] == ["near", "scanning"]
    sweep = book._live["scan"]
    assert [n["state"] for n in sweep["names"]] == ["near", "setup_up", "nodata"] and sweep["state"] == "done" and [f["underlying"] for f in found] == ["TCS"]


def test_it_is_an_independent_book_in_the_portfolio_and_the_eod_report(book):
    book._save_positions([pos(status="closed", exit_day=DAY, exit=150, exit_at="10:05", exit_reason="target", net=3000, r=1.5, key="NFO:X", entry_at="10:00")])
    assert "skill_desk" in all_desks.INDEPENDENT
    t = book.trades()[0]
    assert t["session"] == DAY and t["net"] == 3000
    rows = eod._skill_desk_rows(DAY)
    assert rows[0]["desk"] == "Claude Skill Desk" and rows[0]["done"] and eod.CODES["Claude Skill Desk"] == "CSD"


def test_no_order_path_exists():
    for word in ("place_order", "kite.", "KiteConnect", "/api/orders", "modify_order"):
        assert word not in SRC
    assert '"SELL"' not in SRC
