"""News priority: stock-specific triggers, market-moving global events, and NSE filings."""
import pytest

from strategies import news_priority as npz

MS = npz.matchers([{"symbol": "INFY", "name": "Infosys Ltd."}, {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd."}, {"symbol": "TCS", "name": "Tata Consultancy Services Ltd."}])


@pytest.mark.parametrize("title, lane, tag", [
    ("Russia launches missile strikes on Kyiv as war escalates", "global", "War"),
    ("RBI keeps repo rate unchanged at 5.5%, stance neutral", "global", "Central banks"),
    ("Fed signals rate cut in December as inflation cools", "global", "Central banks"),
    ("Trump slaps 50% tariffs on India", "global", "Geopolitics"),
    ("Oil prices jump 4% after OPEC+ cut", "global", "Crude"),
    ("Sensex crashes 1,200 points as markets sell off", "global", "Crisis"),
])
def test_global_events_are_caught(title, lane, tag):
    c = npz.classify(title)
    assert c["lane"] == lane and tag in c["tags"]


@pytest.mark.parametrize("title", ["Will Woolworths ever win the supermarket war?", "Kokan Bank gets RBI Approval for Direct RTGS, NEFT Participation",
                                   "Thousands rally against the Vietnam War in 1965", "The price war in quick commerce heats up"])
def test_lookalikes_stay_out(title):
    assert npz.classify(title)["lane"] == "market"


def test_stock_triggers_need_a_named_stock():
    t = "Infosys Q2 results: net profit rises 12%, beats estimates"
    found = npz.tickers_in(t, MS)
    assert found == ["INFY"]
    c = npz.classify(t, "", found)
    assert c["lane"] == "stock" and c["level"] == "high" and "Results" in c["tags"]
    assert npz.classify("Q2 results season: what to expect", "", [])["lane"] == "market"
    assert npz.classify("HDFC Bank bags a ₹2,000 crore order", "", ["HDFCBANK"])["tags"][0] == "Order win"


def test_nse_filings_keep_material_subjects_and_drop_routine_ones():
    rows = [{"symbol": "AJAXENGG", "desc": "Copy of Newspaper Publication", "sort_date": "2026-11-15 10:30:21", "seq_id": "1"},
            {"symbol": "INFY", "desc": "Outcome of Board Meeting", "attchmntText": "Board approved a buyback of shares", "sort_date": "2026-11-15 10:31:00", "seq_id": "2", "attchmntFile": "https://x/y.pdf"},
            {"symbol": "TCS", "desc": "Investor Presentation", "sort_date": "2026-11-15 10:32:00", "seq_id": "3"}]
    items = npz.filing_items(rows)
    assert [i["tickers"][0] for i in items] == ["INFY", "TCS"]
    assert items[0]["priority"] == {"lane": "stock", "level": "high", "tags": ["Capital"]} and items[0]["published"].startswith("2026-11-15T05:01")
    assert items[1]["priority"]["level"] == "medium" and items[0]["link"] == "https://x/y.pdf"
