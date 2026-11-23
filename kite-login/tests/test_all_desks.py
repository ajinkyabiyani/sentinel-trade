"""The combined portfolio: every system's open and closed paper trades, and today's P&L."""
from strategies import all_desks, paper_desk

TODAY = paper_desk.datetime.now(paper_desk.IST).date().isoformat()


def test_the_portfolio_carries_the_multi_agent_team(monkeypatch, tmp_path):
    monkeypatch.setattr(paper_desk, "portfolio", lambda: {"capital": 0, "desks": []})
    monkeypatch.setattr(all_desks.risk_settings, "settings", lambda: {"capital": 100000})
    monkeypatch.setattr(all_desks.team, "DIR", tmp_path)
    from strategies import claude_trader
    monkeypatch.setattr(claude_trader, "DIR", tmp_path / "claude_trader")
    (tmp_path / f"day_{TODAY}.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(all_desks.team, "positions", lambda day: [{"instrument": "PNB26NOV120CE", "key": "NFO:PNB26NOV120CE", "qty": 8000, "entry": 2.0, "stop": 1.8, "target": 2.5, "entry_at": "10:55", "status": "open", "ltp": 2.1},
                                                                  {"instrument": "Y", "key": "NFO:Y", "qty": 30, "entry": 700.0, "stop": 650.0, "target": 800.0, "entry_at": "10:10", "status": "closed", "exit": 650.0, "exit_at": "10:30", "exit_reason": "stop", "net": -1600.0, "r": -1.07}])
    p = all_desks.portfolio()
    assert [d["id"] for d in p["desks"]] == ["claude_trader", "multi_agent_team"] and p["capital"] == 300000.0
    assert p["desks"][0]["capital"] == 200000.0 and p["desks"][0]["open"] == []
    team = p["desks"][1]
    assert team["open"][0]["symbol"] == "PNB26NOV120CE" and team["open"][0]["net"] == 800.0 and team["today"] == -800.0 and team["equity"] == 99200.0
    assert team["stats"]["trades"] == 1 and team["curve"][0] == ["start", 100000.0]


def test_a_broken_book_does_not_hide_the_others(monkeypatch):
    monkeypatch.setattr(paper_desk, "portfolio", lambda: {"capital": 0.0, "desks": []})
    monkeypatch.setattr(all_desks, "team_trades", lambda: 1 / 0)
    p = all_desks.portfolio()
    by = {d["id"]: d for d in p["desks"]}
    assert "claude_trader" in by and by["multi_agent_team"]["error"]


def books(monkeypatch, desks, capital=800000.0):
    monkeypatch.setattr(all_desks, "portfolio", lambda: {"capital": capital, "desks": desks})


def test_one_position_per_underlying_across_every_system(monkeypatch):
    books(monkeypatch, [{"name": "Agent Desk", "today": 0, "open": [{"symbol": "NIFTY26N1725400CE"}, {"symbol": "ETERNAL"}]}, {"name": "Multi Agent Team", "today": 0, "open": []}, {"name": "X", "error": "broken"}])
    assert "Agent Desk already has an open paper trade on NIFTY" in all_desks.gate("NIFTY") and all_desks.gate("ETERNAL") and all_desks.gate("BANKNIFTY") is None
    assert all_desks.underlying_of("PNB26NOV120CE") == "PNB" and all_desks.underlying_of("M&M26NOV3000PE") == "M&M" and all_desks.underlying_of("RELIANCE") == "RELIANCE"


def test_an_option_is_not_blocked_by_another_desks_shares_and_shares_not_by_its_option(monkeypatch):
    books(monkeypatch, [{"name": "Agent Desk", "today": 0, "open": [{"symbol": "CDSL"}, {"symbol": "SBILIFE26NOV1800CE"}]}, {"name": "Multi Agent Cash", "today": 0, "open": [{"symbol": "ABCAPITAL"}]}])
    assert all_desks.gate("CDSL", "option") is None and all_desks.gate("ABCAPITAL", "option") is None and all_desks.gate("CDSL26NOV1460CE") is None
    assert "Agent Desk already has an open paper share trade on CDSL" in all_desks.gate("CDSL", "shares")
    assert "Multi Agent Cash already has an open paper share trade on ABCAPITAL" in all_desks.gate("ABCAPITAL", "shares")
    assert all_desks.gate("SBILIFE", "shares") is None
    assert "Agent Desk already has an open paper option trade on SBILIFE" in all_desks.gate("SBILIFE", "option") and all_desks.gate("SBILIFE26NOV1750PE")
    assert all_desks.gate("CDSL") and all_desks.gate("SBILIFE")          # no kind given: every open trade on the name counts, as before


def test_the_shared_daily_loss_limit_stops_every_new_trade(monkeypatch):
    books(monkeypatch, [{"name": "A", "today": -5000.0, "open": []}, {"name": "B", "today": -3000.0, "open": []}])
    assert "past the shared limit of ₹8,000" in all_desks.gate("NIFTY")
    books(monkeypatch, [{"name": "A", "today": -5000.0, "open": []}, {"name": "B", "today": 2000.0, "open": []}])
    assert all_desks.gate("NIFTY") is None


def test_a_gate_that_cannot_read_the_books_does_not_stop_the_desks(monkeypatch):
    monkeypatch.setattr(all_desks, "portfolio", lambda: 1 / 0)
    assert all_desks.gate("NIFTY") is None
