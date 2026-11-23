"""The event screener's safeguards, scoring, classification and priced-in logic. No network."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from strategies import event_screener as es

SRC = (Path(__file__).resolve().parents[1] / "backend" / "strategies" / "event_screener.py").read_text(encoding="utf-8")
TODAY = date(2026, 11, 6)
CLOSES = [[f"{y}-{m:02d}-15", 100.0] for y in (2024, 2025) for m in range(1, 13)] + [[f"2026-10-{d:02d}", 100.0 + d] for d in range(1, 32)] + [["2026-11-05", 140.0]]
BENCH = [[f"2026-10-{d:02d}", 1000.0] for d in range(1, 32)] + [["2026-11-05", 1010.0]]
QUARTERS = {"periods": [f"{m} {y}" for y in (2024, 2025) for m in ("Mar", "Jun", "Sep", "Dec")], "rows": {"EPS in Rs": [1.0] * 8, "Sales": [100.0] * 8}}
FUND = {"pe": 25.0, "quarters": QUARTERS, "industry": "Cables - Electricals", "fetched_at": "2026-11-01T10:00:00+05:30"}
FILING = {"symbol": "ABC", "sm_name": "ABC Cables Ltd", "desc": es.ORDER_DESCS[0], "attchmntText": "ABC has informed the Exchange about bagging of orders", "an_dt": "20-Oct-2026 10:00:00", "seq_id": "1", "attchmntFile": "https://x/a.pdf", "smIndustry": "Cables - Electricals", "_in500": True}
CH = es.CHAINS[0]
NODE = next(n for n in CH["nodes"] if n["id"] == "dc_cables")
SCORE = {"primary": 3, "magnitude": 2, "breadth": 1, "novelty": 3, "total": 9}


def thesis(cls="Direct", value=500.0, reviewed=("ai_datacentre/dc_cables",), flags=(), filing=FILING, closes=CLOSES, fund=FUND, policy_title=None):
    trigger = {"title": policy_title, "link": "https://n/1", "date": "2026-11-05", "source": "BBC Business"} if policy_title else None
    return es.build_thesis(filing, CH, NODE, cls, "Cables - Electricals", True, value, fund, closes, BENCH, set(reviewed), TODAY, SCORE, list(flags), trigger=trigger)


def test_no_trading_or_credentials():
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt)\s*\(", SRC)
    assert "access_token" not in SRC and "api_key" not in SRC


# ---------------- values and scores ----------------
@pytest.mark.parametrize("text,want", [("order worth Rs. 1,250 crore", 1250), ("INR 500 Crores", 500), ("₹ 50 lakh", 0.5), ("Rs 200 million", 20), ("Rs. 2 billion", 200), ("no value here", None), ("Rs 10 crore and Rs 90 crore", 90)])
def test_value_crore(text, want):
    assert es.value_crore(text) == want


@pytest.mark.parametrize("value,sales,want", [(None, 1000, 0), (150, 1000, 3), (50, 1000, 2), (5, 1000, 1), (12000, None, 3), (1500, None, 2), (50, None, 1)])
def test_magnitude(value, sales, want):
    assert es.magnitude(value, sales) == want


def test_score_axes():
    s = es.score_signal("nse_filing", 150, 1000, 5, seen=False)
    assert s == {"primary": 3, "magnitude": 3, "breadth": 3, "novelty": 3, "total": 12}
    assert es.score_signal("unknown", None, None, 0, seen=True)["total"] == 0


# ---------------- the library ----------------
def test_every_industry_pattern_can_match_a_real_label():
    labels = "2/3 Wheelers|Aerospace & Defense|Airline|Auto Components & Equipments|Biotechnology|Cables - Electricals|Carbon Black|Castings & Forgings|Cement & Cement Products|Civil Construction|Coal|Commodity Chemicals|Compressors, Pumps & Diesel Engines|Computers - Software & Consulting|Construction|Construction Vehicles|Consumer Electronics|Copper|Electrodes & Refractories|Engineering|Explosives|Fertilizers|Footwear|Garments & Apparels|Gems, Jewellery And Watches|Healthcare Service Provider|Heavy Electrical Equipment|Housing Finance Company|Industrial Gases|Industrial Minerals|Industrial Products|Integrated Power Utilities|Logistics Solution Provider|Lubricants|Medical Equipment & Supplies|Oil Exploration & Production|Oil Storage & Transportation|Other Electrical Equipment|Other Food Products|Other Textile Products|Paints|Pesticides & Agrochemicals|Pharmaceuticals|Port & Port services|Power - Transmission|Power Generation|Railway Wagons|Refineries & Marketing|Residential, Commercial Projects|Ship Building & Allied Services|Shipping|Software Products|Specialty Chemicals|Sugar|Telecom -  Equipment & Accessories|Tractors|Transmission Towers|Tyres & Rubber Products|Healthcare Research, Analytics & Technology|IT Enabled Services|Iron & Steel Products|Trading - Minerals|Zinc|Aluminium|Airline".split("|")
    for c in es.CHAINS:
        for n in c["nodes"]:
            assert any(re.search(n["industry"], x, re.I) for x in labels), n["id"]


def test_every_edge_has_what_the_design_requires():
    assert len(es.CHAINS) >= 20
    for c in es.CHAINS:
        assert c["counter"] and c["lead_months"] and c["driver"]
        for n in c["nodes"]:
            assert n["confidence"] in ("Low", "Medium", "High") and n["industry"] and n["level"] in (1, 2, 3)


def test_nodes_come_only_from_the_library():
    assert {"dc_cables", "grid_cables"} <= {n["id"] for _, n in es.nodes_for("Cables - Electricals")}
    assert es.nodes_for("Private Sector Bank") == [] and es.nodes_for("") == []


def test_drivers_are_recognised():
    assert "ai_datacentre" in [c["id"] for c in es.drivers_in("order for a hyperscale data centre")]
    assert "grid_transmission" in [c["id"] for c in es.drivers_in("400 kV substation package")]
    assert es.drivers_in("a new shampoo") == []


def test_filing_filters():
    rows = [FILING, {**FILING, "desc": "Action(s) taken or orders passed"}, {"symbol": "D", "desc": "Press Release", "attchmntText": "new data centre plant"}, {"symbol": "E", "desc": "Press Release", "attchmntText": "dividend"}]
    assert len(es.order_filings(rows)) == 1 and [r["symbol"] for r in es.update_filings(rows)] == ["D"]


# ---------------- priced in ----------------
def test_abnormal_return_is_against_the_benchmark():
    a = es.abnormal_return(CLOSES, BENCH, date(2026, 10, 20))
    assert a["stock_pct"] == pytest.approx((140 / 119 - 1) * 100, abs=0.01) and a["benchmark_pct"] == 1 and a["abnormal_pct"] == pytest.approx(a["stock_pct"] - 1, abs=0.01) and a["as_of"] == "2026-11-05"


def test_no_price_before_the_event_gives_nothing():
    assert es.abnormal_return(CLOSES, BENCH, date(2026, 9, 1)) is None


def test_pe_history_skips_losses():
    closes = [[f"{y}-{m:02d}-15", 100.0] for y in (2024, 2025) for m in range(1, 13)]
    assert es.pe_history(QUARTERS, closes) == [25.0] * 5
    losing = {"periods": QUARTERS["periods"], "rows": {"EPS in Rs": [-1.0] * 8}}
    assert es.pe_history(losing, closes) == []


ABN = {"abnormal_pct": 2.0, "stock_pct": 3.0, "benchmark_pct": 1.0, "as_of": "2026-11-05"}
PES = [20.0, 22.0, 24.0, 26.0, 40.0]


@pytest.mark.parametrize("abn,pe,order,state", [(ABN, 20.0, False, "Early"), (ABN, 20.0, True, "Partially priced"), (ABN, 30.0, False, "Partially priced"), ({**ABN, "abnormal_pct": 16.0}, 41.0, True, "Largely priced"), ({**ABN, "abnormal_pct": 26.0}, 20.0, True, "Largely priced"), ({**ABN, "abnormal_pct": 16.0}, 25.0, True, "Partially priced")])
def test_priced_in_states(abn, pe, order, state):
    assert es.priced_in(abn, pe, PES, order)["state"] == state


@pytest.mark.parametrize("abn,pe,pes,word", [(None, 20.0, PES, "price response"), (ABN, None, PES, "current P/E"), (ABN, -3.0, PES, "current P/E"), (ABN, 20.0, [20.0], "P/E history")])
def test_missing_data_is_unclear_and_says_what_is_missing(abn, pe, pes, word):
    r = es.priced_in(abn, pe, pes, True)
    assert r["state"] == "Unclear" and word in r["missing"]


# ---------------- confidence ----------------
def test_direct_with_value_fresh_and_reviewed_can_reach_high():
    t = thesis()
    assert t["classification"] == "Direct" and t["confidence"] == "High" and t["review"] == "needed"


def test_an_unreviewed_edge_caps_at_low():
    t = thesis(reviewed=())
    assert t["confidence"] == "Low" and any("not been reviewed" in r for r in t["confidence_reasons"])


def test_speculative_is_always_low():
    t = thesis(cls="Speculative")
    assert t["confidence"] == "Low" and t["review"] == "not required" and any(x["score"] == 0 for x in t["causal_chain"])


def test_stale_data_caps_at_low():
    t = thesis(fund={**FUND, "fetched_at": "2026-01-01T10:00:00+05:30"})
    assert t["confidence"] == "Low" and t["staleness"]["fundamentals"]["stale"]


def test_no_prices_means_unclear_and_low():
    t = thesis(closes=[])
    assert t["priced_in"]["state"] == "Unclear" and t["confidence"] == "Low" and t["staleness"]["price"]["stale"]


def test_no_stated_value_is_not_high():
    t = thesis(value=None)
    assert t["confidence"] != "High" and "not quantified" in " ".join(t["confidence_reasons"])


def test_unresolved_conflict_is_not_high():
    assert thesis(flags=["results came within 3 days"])["confidence"] == "Medium"


def test_confidence_note_says_what_it_is_not():
    assert "not how likely the stock is to rise" in thesis()["confidence_note"]


# ---------------- safeguards on the record ----------------
def test_a_good_record_passes():
    es.check_record(thesis())


@pytest.mark.parametrize("field", ["invalidators", "disclaimer", "staleness", "confidence_reasons", "risks"])
def test_required_fields_are_enforced(field):
    t = thesis()
    t[field] = []
    with pytest.raises(ValueError):
        es.check_record(t)


@pytest.mark.parametrize("phrase", ["will benefit", "guaranteed", "sure to rise", "must buy"])
def test_certainty_language_is_refused(phrase):
    t = thesis()
    t["horizon"] = f"this stock {phrase}"
    with pytest.raises(ValueError, match="certainty"):
        es.check_record(t)


@pytest.mark.parametrize("field", ["target_price", "position_size", "quantity", "entry", "exit"])
def test_no_trading_instruction_can_appear(field):
    t = thesis()
    t[field] = 100
    with pytest.raises(ValueError):
        es.check_record(t)


def test_a_case_of_smaller_benefit_is_required():
    t = thesis()
    t["risks"] = ["Something else entirely."]
    with pytest.raises(ValueError, match="smaller"):
        es.check_record(t)


def test_speculative_cannot_claim_more_than_low():
    t = thesis(cls="Speculative")
    t["confidence"] = "High"
    with pytest.raises(ValueError):
        es.check_record(t)


def test_an_unscored_source_cannot_carry_a_thesis():
    t = thesis()
    t["event"]["score"] = 0
    with pytest.raises(ValueError):
        es.check_record(t)


def test_ranking_evidence_comes_first_and_unclear_last():
    a, b, c, d = thesis(), thesis(cls="Indirect"), thesis(cls="Speculative"), thesis(closes=[])
    order = sorted([c, d, b, a], key=es.rank_key)
    assert order[-1] is d and order[0] is a and order.index(b) < order.index(c)


def test_small_caps_are_flagged():
    assert any("Outside the Nifty 500" in r for r in thesis(filing={**FILING, "_in500": False})["risks"])


# ---------------- world events ----------------
NEWS = [{"title": "Hyperscale data centre boom in Asia", "link": "https://a/1", "date": "2026-11-05", "source": "BBC Business", "summary": ""},
        {"title": "New data centres planned", "link": "https://b/2", "date": "2026-11-04", "source": "Mint Companies", "summary": ""}]


def test_one_outlet_does_not_make_a_theme_live():
    assert es.active_themes(NEWS[:1], TODAY) == []
    assert [t["id"] for t in es.active_themes(NEWS, TODAY)] == ["ai_datacentre"]


def test_old_news_does_not_keep_a_theme_live():
    old = [{**n, "date": "2026-10-01"} for n in NEWS]
    assert es.active_themes(old, TODAY) == []


def test_news_store_keeps_first_sighting_refuses_odd_links_and_expires():
    stored = es.merge_news([], NEWS[:1], TODAY)
    again = es.merge_news(stored, [{**NEWS[0], "date": "2026-11-06"}, {"title": "x", "link": "javascript:alert(1)", "source": "y"}], TODAY)
    assert len(again) == 1 and again[0]["date"] == "2026-11-05"
    assert es.merge_news(stored, [], date(2027, 1, 1)) == []


@pytest.mark.parametrize("text,theme", [("Oil prices surge after strike", "oil_up"), ("Indian rupee strengthens as oil slides", "oil_down"), ("Gold hits record high", "gold"), ("Red Sea attacks lift freight rates", "shipping"), ("India and UK sign trade deal", "trade_deal"),
                                        ("China tightens rare earth curbs", "minerals"), ("Nuclear power plant approved", "nuclear"), ("Ebola outbreak spreads", "outbreak"), ("Generative AI reshapes work", "ai_software"), ("Monsoon forecast cut", "monsoon")])
def test_world_events_reach_many_themes(text, theme):
    assert theme in [c["id"] for c in es.drivers_in(text)]


@pytest.mark.parametrize("text", ["Oil slides on demand fears", "Oil prices hit two-month low after vote", "Markets rise after crude tumbles 9%"])
def test_oil_falling_is_not_read_as_oil_rising(text):
    ids = [c["id"] for c in es.drivers_in(text)]
    assert "oil_up" not in ids and "oil_down" in ids


@pytest.mark.parametrize("text,theme", [("Adani Ports to acquire Evos for 2.56 billion EV", "ev"), ("Repeat gold borrowing rises among customers", "gold"), ("Construction near missile bases, satellite images suggest", "space")])
def test_look_alike_headlines_do_not_match(text, theme):
    assert theme not in [c["id"] for c in es.drivers_in(text)]


def test_bad_news_counts_against_a_theme_and_cannot_make_it_live():
    bad = [{"title": "Data centre boom fears as AI selloff deepens", "link": f"https://x/{i}", "date": "2026-11-05", "source": s, "summary": ""} for i, s in enumerate(("A", "B", "C"))]
    assert es.active_themes(bad, TODAY) == []
    t = es.active_themes(NEWS + bad, TODAY)[0]
    assert t["against_count"] == 3 and t["contested"] and t["count"] == 2


def test_company_gossip_is_not_ai_demand_and_ai_fear_counts_against():
    assert "ai_datacentre" not in [c["id"] for c in es.drivers_in("Product chief joins OpenAI after quitting")]
    bad = [{"title": "Global tech stocks plunge over fears of AI curbs", "link": "https://x/1", "date": "2026-11-05", "source": "A", "summary": ""}]
    assert es.active_themes(NEWS + bad, TODAY)[0]["against_count"] == 1


def test_summary_text_does_not_make_a_theme():
    news = [{**n, "title": "A quiet day", "summary": "data centre"} for n in NEWS]
    assert es.active_themes(news, TODAY) == []


def test_an_event_with_no_theme_goes_to_a_person():
    news = [{"title": "Global sugar shortage worsens", "link": "https://c", "date": "2026-11-05", "source": "BBC Business", "summary": ""}, {"title": "A royal visit", "link": "https://d", "date": "2026-11-05", "source": "BBC World", "summary": ""}]
    assert [n["link"] for n in es.unmapped(news, TODAY)] == ["https://c"]


@pytest.mark.parametrize("text,want", [("Order worth Rs. 500 crore received", 500), ("Our turnover was Rs. 9,000 crore", None), ("no value", None), ("wins orders of Rs. 2,872 crores from PGCIL. Year to date order intake stands at Rs. 13,500 crores", 2872), ("Order book of Rs 9,000 crore. New order of Rs 50 crore received", 50), ("WABAG secures a Mega order. Note: Mega shall\nmean a Domestic Order inflow value above INR 1,000 Crores.", None)])
def test_order_value_ignores_company_size(text, want):
    assert es.order_value(text) == want


# ---------------- the dark-horse lens ----------------
def fund(mcap, sales=(100.0,) * 4 + (130.0,) * 4, roce=20.0, industry="Cables - Electricals"):
    return {"mcap_cr": mcap, "industry": industry, "roce": roce, "pe": 25.0, "fetched_at": "2026-11-01", "quarters": {"periods": QUARTERS["periods"], "rows": {"Sales": list(sales), "EPS in Rs": [1.0] * 8}}}


LONG_BENCH = [["2026-07-01", 1000.0], ["2026-11-05", 1000.0]]
FLAT = [["2026-07-01", 100.0], ["2026-11-05", 100.0]]
THEME = {"id": "ai_datacentre", "nodes": 5, "first": "2026-11-04", "headlines": NEWS}


def lens(funds, closes=lambda s: FLAT):
    return es.dark_horses([THEME], funds, closes, LONG_BENCH, TODAY)


def test_leaders_are_left_out():
    funds = {s: fund(m) for s, m in (("BIG", 90000), ("NEXT", 50000), ("MID", 9000), ("SMALL", 1000))}
    found, dropped = lens(funds)
    assert {c["symbol"] for c in found} == {"MID", "SMALL"} and dropped["leaders"] >= 2


def test_shrinking_sales_low_returns_and_runaway_prices_are_left_out():
    funds = {"A": fund(90000), "B": fund(80000), "SHRINK": fund(5000, sales=(130.0,) * 4 + (100.0,) * 4), "LOWROCE": fund(4000, roce=4.0), "RAN": fund(3000), "OK": fund(2000)}
    ran = [["2026-07-01", 100.0], ["2026-11-05", 160.0]]
    found, dropped = lens(funds, closes=lambda s: ran if s == "RAN" else FLAT)
    assert [c["symbol"] for c in found] == ["OK"] and dropped["weak_or_no_growth"] == 1 and dropped["low_return"] == 1 and dropped["already_moved"] == 1


def test_large_companies_are_not_dark_horses():
    funds = {s: fund(m) for s, m in (("A", 300000), ("B", 200000), ("C", 150000), ("D", 2000))}
    found, dropped = lens(funds)
    assert [c["symbol"] for c in found] == ["D"] and dropped["too_large"] == 1
    assert es.dark_note("C", funds["C"], NODE, funds) is None


def test_a_coarse_label_needs_the_company_profile_to_agree():
    node = next(n for n in CH["nodes"] if n["id"] == "dc_power")
    assert es.fits(node, {"industry": "Heavy Electrical Equipment", "about": "Makes transformers"})
    assert not es.fits(node, {"industry": "Heavy Electrical Equipment", "about": "Makes solar modules"})
    assert not es.fits(node, {"industry": "Heavy Electrical Equipment"})


def test_repeat_filings_merge_into_one_record():
    a, b = thesis(), thesis()
    b["event"]["date"] = "2026-10-01"
    b["id"] = "older"
    out = es.merge_repeats([b, a])
    assert len(out) == 1 and out[0]["company"]["related_filings"] == 2 and out[0]["id"] != "older"


def test_a_sub_sector_too_small_to_have_leaders_is_skipped():
    assert lens({"A": fund(100), "B": fund(50)})[0] == []


def test_dark_note_says_why_and_skips_leaders():
    funds = {s: fund(m) for s, m in (("BIG", 900), ("NEXT", 500), ("MID", 90), ("SMALL", 10))}
    assert es.dark_note("BIG", funds["BIG"], NODE, funds) is None
    assert "Number 4 of 4" in es.dark_note("SMALL", funds["SMALL"], NODE, funds)["reason"]
    assert "Outside the Nifty 500" in es.dark_note("XYZ", fund(5), NODE, funds)["reason"]


# ---------------- a full pass ----------------
@pytest.fixture
def pass_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "DIR", tmp_path)
    return tmp_path


def make(rows, news=(), pdf="", fund=FUND, funds=None):
    class R:
        content = b"%PDF"

        @staticmethod
        def raise_for_status():
            return None
    return es.Screener(nse=lambda *a, **k: {"data": {"data": rows}, "error": None, "fetched_at": "2026-11-06T10:00:00", "stale": False}, screener=lambda s: {"data": fund, "fetched_at": fund["fetched_at"]},
                       closes=lambda s: BENCH if s == "NIFTY 50" else CLOSES, world=lambda: {"items": list(news), "status": {}}, get=lambda u: R, cached=lambda k, t, f: {"data": pdf}, universe=lambda: [{"symbol": "ABC"}], funds=lambda: funds or {})


def test_a_filing_naming_the_driver_is_direct(pass_dir, monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda n: "pdftotext")
    out = make([FILING], pdf="Order of Rs. 500 crore for a data centre project").run(TODAY)
    t = next(x for x in out["theses"] if x["chain"] == "ai_datacentre")
    assert t["classification"] == "Direct" and t["exposure"]["order_value_crore"] == 500 and t["confidence"] == "Low"


def test_an_order_tied_to_nothing_makes_no_thesis(pass_dir):
    assert make([FILING]).run(TODAY)["theses"] == []


def test_a_live_theme_makes_an_order_indirect(pass_dir):
    out = make([FILING], news=NEWS).run(TODAY)
    t = next(x for x in out["theses"] if x["chain"] == "ai_datacentre")
    assert t["classification"] == "Indirect" and t["causal_chain"][0]["source"] == "https://a/1" and t["causal_chain"][0]["quoted"] and any(x["score"] == 0 for x in t["causal_chain"]) and t["confidence"] == "Low"


def test_a_large_order_counts_in_any_sector(pass_dir, monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda n: "pdftotext")
    bank = {**FUND, "industry": "Sugar", "mcap_cr": 900}
    out = make([{**FILING, "smIndustry": None}], pdf="Order worth Rs. 200 crore received", fund=bank).run(TODAY)
    t = next(x for x in out["theses"] if x["chain"] == "order_surprise")
    assert t["classification"] == "Direct" and t["company"]["order_share_of_sales_pct"] == 50 and t["dark_horse"] and t["review"] == "needed"


def test_a_small_order_is_not_a_surprise(pass_dir, monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda n: "pdftotext")
    out = make([{**FILING, "smIndustry": None}], pdf="Order worth Rs. 5 crore received", fund={**FUND, "industry": "Sugar"}).run(TODAY)
    assert not [t for t in out["theses"] if t["chain"] == "order_surprise"]


def test_the_lens_gives_speculative_low_records_that_say_exposure_is_unverified(pass_dir):
    funds = {s: fund(m) for s, m in (("BIG", 90000), ("NEXT", 50000), ("MID", 9000), ("SMALL", 1000))}
    s = make([], news=NEWS, funds=funds)
    s.nse = lambda *a, **k: {"data": {"data": [{"symbol": "Z", "desc": "Appointment"}]}, "error": None}
    s.closes = lambda sym: [["2026-07-01", 100.0], ["2026-11-05", 100.0]]
    out = s.run(TODAY)
    picks = [t for t in out["theses"] if t.get("dark_horse")]
    assert {t["company"]["symbol"] for t in picks} == {"MID", "SMALL"}
    for t in picks:
        assert t["classification"] == "Speculative" and t["confidence"] == "Low" and "unverified" in t["exposure"]["text"] and t["causal_chain"][0]["quoted"] and "90 days" in t["priced_in"]["basis"]["window"]


def test_a_headline_with_certainty_words_does_not_block_a_record():
    t = thesis(policy_title="Analyst says this will benefit everyone")
    es.check_record(t)


def test_order_clusters_are_found_in_any_industry(pass_dir):
    rows = [{**FILING, "symbol": s, "seq_id": s, "smIndustry": None} for s in ("A", "B", "C")]
    out = make(rows, fund={**FUND, "industry": "Sugar"}).run(TODAY)
    assert out["clusters"][0]["industry"] == "Sugar" and out["clusters"][0]["companies"] == ["A", "B", "C"] and out["clusters"][0]["in_library"]


def test_an_implausible_order_value_is_flagged_and_not_high(pass_dir, monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda n: "pdftotext")
    out = make([{**FILING, "smIndustry": None}], pdf="Order worth Rs. 2,000 crore received", fund={**FUND, "industry": "Sugar"}).run(TODAY)
    t = next(x for x in out["theses"] if x["chain"] == "order_surprise")
    assert t["confidence"] == "Medium" and any("misread" in r for r in t["confidence_reasons"])


def test_every_thesis_is_logged_once(pass_dir):
    s = make([FILING], news=NEWS)
    s.run(TODAY)
    s.run(TODAY)
    rows = json.loads((pass_dir / "log.json").read_text(encoding="utf-8"))
    assert rows and len(rows) == len({r["id"] for r in rows})


def test_edge_review_lifts_the_cap(pass_dir, monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda n: "pdftotext")
    s = make([FILING], pdf="Order of Rs. 50 crore for a data centre")
    assert next(t for t in s.run(TODAY)["theses"] if t["chain"] == "ai_datacentre")["confidence"] == "Low"
    s.set_edge("ai_datacentre/dc_cables", True)
    assert next(t for t in s.run(TODAY)["theses"] if t["chain"] == "ai_datacentre")["confidence"] == "High"


def test_an_unknown_edge_is_refused(pass_dir):
    with pytest.raises(KeyError):
        make([]).set_edge("made/up", True)


def test_a_source_failure_is_reported(pass_dir):
    s = make([])
    s.nse = lambda *a, **k: {"data": None, "error": "HTTP 403"}
    s.run(TODAY)
    assert "403" in s.progress["error"]


def test_scoring_waits_30_days():
    log = [{"id": "a", "date": "2026-10-30", "symbol": "ABC", "confidence": "Low"}, {"id": "b", "date": "2026-09-01", "symbol": "ABC", "confidence": "Low"}]
    closes = [["2026-08-31", 100.0], ["2026-11-05", 120.0]]
    bench = [["2026-08-31", 100.0], ["2026-11-05", 110.0]]
    t = es.score_log(log, lambda s: closes, bench, TODAY)
    assert t["too_new"] == 1 and t["by_confidence"]["Low"] == {"n": 1, "avg_abnormal_pct": 10.0, "beat_benchmark_pct": 100.0}


# ---------------- live themes ----------------
def test_a_news_check_updates_themes_without_touching_theses_or_filings(pass_dir):
    s = make([], news=NEWS)
    s.nse = s.screener = s.closes = s.funds = None                  # would fail if the fast check used them
    live = s.refresh_news(TODAY)
    assert [t["id"] for t in live["themes"]] == ["ai_datacentre"] and live["new_themes"] == ["ai_datacentre"] and live["new_headlines"] == 2
    assert not (pass_dir / "latest.json").exists() and (pass_dir / "live.json").exists()


def test_a_theme_is_new_only_once(pass_dir):
    s = make([], news=NEWS)
    s.refresh_news(TODAY)
    live = s.refresh_news(TODAY)
    assert live["new_themes"] == [] and live["new_headlines"] == 0 and live["themes"]


def test_a_full_pass_uses_the_same_news_check(pass_dir):
    out = make([FILING], news=NEWS).run(TODAY)
    assert out["themes"][0]["id"] == "ai_datacentre" and (pass_dir / "live.json").exists()


def test_the_newest_headline_comes_first_within_a_day():
    a = {**NEWS[0], "date": "2026-11-05", "seen_at": "2026-11-05T09:00:00+05:30"}
    b = {**NEWS[1], "date": "2026-11-05", "seen_at": "2026-11-05T15:00:00+05:30"}
    assert [n["link"] for n in es.merge_news([a, b], [], TODAY)] == [b["link"], a["link"]]


# ---------------- in plain words ----------------
LONG = [[f"{y}-{m:02d}-15", 100.0] for y in (2024, 2025) for m in range(1, 13)] + [["2026-08-01", 100.0], ["2026-11-05", 120.0]]
LBENCH = [["2024-01-15", 100.0], ["2026-08-01", 100.0], ["2026-11-05", 110.0]]


def test_price_moves_are_measured_against_nifty():
    m = es.price_moves(LONG, LBENCH, TODAY)
    three = next(p for p in m["periods"] if p["label"] == "3 months")
    assert three == {"label": "3 months", "stock_pct": 20.0, "benchmark_pct": 10.0, "abnormal_pct": 10.0}
    assert m["from_high_pct"] == 0.0 and m["from_low_pct"] == 20.0 and es.price_moves([], LBENCH, TODAY) is None


def test_room_shows_both_directions_and_only_arithmetic():
    r = es.valuation_room(100.0, 20.0, [10.0, 20.0, 30.0, 40.0])
    assert r["usual"] == {"pe": 25.0, "price": 125.0, "pct": 25.0} and r["highest"]["pct"] == 100.0 and r["lowest"]["pct"] == -50.0


@pytest.mark.parametrize("last,pe,pes", [(100.0, None, [20.0] * 5), (100.0, -5.0, [20.0] * 5), (100.0, 20.0, [20.0]), (None, 20.0, [20.0] * 5), (100.0, 1.0, [20.0] * 5)])
def test_room_is_withheld_when_it_would_mean_nothing(last, pe, pes):
    assert es.valuation_room(last, pe, pes) is None


def explained(**kw):
    t = thesis(closes=LONG, **kw)
    t["company"]["order_share_of_sales_pct"] = 50.0
    t["dark_horse"] = {"reason": "x", "rank": 6, "peers": 16, "mcap_cr": 900, "growth_pct": 30.0}
    t["plain"] = es.explain(t)
    return t


def test_the_explanation_says_why_in_everyday_words():
    p = explained()["plain"]
    words = " ".join(p["why_selected"])
    assert "number 6 of 16" in words and "₹500 crore" in words and "50% of what it sells" in words and "Big names get noticed first" in words and "riskier" in words
    assert "not a forecast" in p["room"] and "Nifty" in p["ran"] and p["theme"]


def test_the_explanation_passes_the_language_rules_and_gives_no_instruction():
    t = explained()
    es.check_record(t)
    text = json.dumps(t["plain"], ensure_ascii=False).lower()
    assert "will rise" not in text and "will run" not in text and "target price" not in text and "you should buy" not in text


def test_evidence_strength_is_plain_about_a_weak_link():
    assert "Weakest" in explained(cls="Speculative")["plain"]["evidence"] and "own stock exchange filing" in explained()["plain"]["evidence"]


def test_a_stock_with_no_history_says_so():
    t = thesis(closes=[])
    t["plain"] = es.explain(t)
    assert "No price history" in t["plain"]["ran"] and "can't be estimated" in t["plain"]["room"]


def test_every_builtin_theme_has_a_plain_explanation():
    assert all(c["id"] in es.THEME_PLAIN for c in es.BUILTIN_CHAINS)
    assert not [v for v in es.THEME_PLAIN.values() if es.BANNED.search(v)]


def test_a_pass_explains_every_record(pass_dir):
    out = make([FILING], news=NEWS).run(TODAY)
    assert out["theses"] and all(t["plain"]["why_selected"] is not None and t["plain"]["ran"] for t in out["theses"])


def plain_with(room=None, profit=10.0, sales=10.0, moves=None, **company):
    t = thesis(closes=LONG)
    t["room"], t["moves"] = room, moves if moves is not None else t["moves"]
    t["facts"].update(profit_growth_pct=profit, sales_growth_pct=sales)
    t["company"].update(company)
    return es.explain(t)


def test_falling_sales_and_profit_are_warned_about():
    w = " ".join(plain_with(profit=-38.0, sales=-2.0)["warnings"])
    assert "Sales are falling" in w and "Profit is falling" in w


def test_a_stock_near_its_low_is_warned_about():
    mv = {"last": 100.0, "as_of": "2026-11-05", "periods": [{"label": "1 year", "stock_pct": -50.0, "benchmark_pct": 1.0, "abnormal_pct": -51.0}], "high_1y": 250.0, "low_1y": 98.0, "from_high_pct": -60.0, "from_low_pct": 2.0}
    assert "close to its one-year low" in " ".join(plain_with(moves=mv)["warnings"])


def test_a_lowest_pe_above_today_is_not_shown_as_no_risk():
    room = es.valuation_room(100.0, 10.0, [12.0, 14.0, 16.0, 18.0])
    text = plain_with(room=room)["room"]
    assert "does not make it safe" in text and "At its lowest level" not in text


def test_a_distorted_history_hides_the_highest_figure():
    room = es.valuation_room(100.0, 13.0, [14.0, 16.0, 19.0, 35.0, 18.0])
    text = plain_with(room=room, profit=120.0)["room"]
    assert "At its highest level" not in text and "poor guide" in text


def test_a_normal_history_shows_both_directions():
    room = es.valuation_room(100.0, 20.0, [15.0, 18.0, 22.0, 25.0])
    text = plain_with(room=room)["room"]
    assert "At its highest level" in text and "At its lowest level" in text and "not a forecast" in text


def test_an_expensive_stock_says_there_is_no_room():
    room = es.valuation_room(100.0, 30.0, [15.0, 18.0, 22.0, 25.0])
    assert "no room" in " ".join(plain_with(room=room)["warnings"])

