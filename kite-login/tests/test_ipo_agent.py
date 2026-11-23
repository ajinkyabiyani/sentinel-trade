"""IPO Agent: parsing the site's embedded data, the listing odds, the out-of-sample check, the checks and the signal rules."""
import json
from datetime import date

from strategies import ipo_agent as ia


def _page(payload: str) -> str:
    return "<html><script>self.__next_f.push([1," + json.dumps(payload) + "])</script></html>"


def test_report_rows_and_live_parse():
    rows = [{"~id": 7, "~ipo_name": "Alpha Tech", "~IPO_Category": "IPO", "~ipo_status1": "U", "Price (₹)": "200",
             "GMP": "&#8377;<b>30</b> (15.00%)<br><small><b>20 ↓ / 40 ↑</b></small>", "Sub": "-", "IPO Size": "₹500.00 Cr", "Lot": "75",
             "~P/E": "22.5", "~Srt_Open": "2026-11-20", "~Srt_Close": "2026-11-24", "~Srt_BoA_Dt": "2026-11-25", "~Str_Listing": "2026-11-27",
             "~urlrewrite_folder_name": "/gmp/alpha-tech-ipo/7/", "Updated-On": "15-Nov 9:00"}]
    html = _page('29:["$","x",null,{"resultData":{"reportTableData":' + json.dumps(rows) + "}}]")
    got = ia.parse_live(ia.report_rows(html))
    a = got[0]
    assert (a["id"], a["board"], a["status"], a["price"], a["gmp"], a["gmp_pct"]) == ("7", "Mainboard", "U", 200.0, 30.0, 15.0)
    assert (a["gmp_low"], a["gmp_high"], a["sub"], a["size_cr"], a["lot"]) == (20.0, 40.0, None, 500.0, 75.0)
    assert a["link"] == "https://www.investorgain.com/ipo/alpha-tech-ipo/7/"


def test_history_parse_skips_rows_without_a_listing():
    rows = [{"~id": 1, "IPO": "Beta SME", "~IPO_Category": "SME", "Listing Dt": "6-Nov-26", "Price": "₹100.00", "GMP": "₹25", "Sub": "80x",
             "Size": "₹40 Cr", "~str_listing_gain_in_per": "30", "~str_closing_gain_in_per": "35"},
            {"~id": 2, "IPO": "Gamma", "~IPO_Category": "IPO", "Listing Dt": "", "Price": "₹100.00", "GMP": "₹0", "~str_listing_gain_in_per": ""}]
    h = ia.parse_history(rows)
    assert len(h) == 1 and h[0]["name"] == "Beta" and h[0]["board"] == "SME" and h[0]["listed"] == "2026-11-06" and h[0]["gmp_pct"] == 25.0


def test_number():
    assert ia.number("₹1,455.49 Cr") == 1455.49 and ia.number("12.79x") == 12.79 and ia.number("-14.17") == -14.17
    assert ia.number("-") is None and ia.number("--") is None and ia.number("") is None


def _hist(n, board, gmp_pct, gain, listed="2024-01-01", sub=None):
    return [{"id": f"{board}{gmp_pct}{gain}{i}", "name": "x", "board": board, "listed": listed, "price": 100.0, "gmp": gmp_pct, "gmp_pct": gmp_pct,
             "sub": sub, "size_cr": 100, "listing_gain": gain, "close_gain": gain} for i in range(n)]


def test_bands_and_odds():
    hist = _hist(30, "Mainboard", 20, 15) + _hist(10, "Mainboard", 20, -5) + _hist(25, "Mainboard", 0, -8)
    assert ia.band(20, ia.GMP_BANDS) == "15–30%" and ia.band(0, ia.GMP_BANDS) == "No GMP" and ia.band(None, ia.GMP_BANDS) is None
    o = ia.odds(hist, "Mainboard", 22)
    assert o["n"] == 40 and o["win_pct"] == 75.0 and "15–30%" in o["basis"]
    assert ia.odds(hist, "SME", 22) is None


def test_odds_narrow_by_subscription_only_with_enough_issues():
    hist = _hist(25, "SME", 40, 50, sub=100) + _hist(10, "SME", 40, -2, sub=3)
    assert "subscription 50x and above" in ia.odds(hist, "SME", 40, 120)["basis"]
    assert "subscription" not in ia.odds(hist, "SME", 40, 3)["basis"]          # only 10 like it: falls back to the GMP band


def test_check_uses_only_earlier_issues_for_the_bands():
    old = _hist(120, "Mainboard", 20, 10, listed="2024-03-01") + _hist(60, "Mainboard", 0, -10, listed="2024-03-01")
    new = _hist(20, "Mainboard", 20, 12, listed="2026-06-01") + _hist(15, "Mainboard", 0, -6, listed="2026-06-01")
    ck = ia.check(old + new, date(2026, 11, 15))
    assert ck["train"] == 180 and ck["test"] == 35
    assert ck["brier_band"] < ck["brier_base"] and ck["high_odds_hit"] == 100.0 and ck["low_odds_hit"] == 0.0


def test_fundamentals_checks():
    d = {"financials": {"periods": ["30 Jun 2026", "31 Mar 2026", "31 Mar 2025"], "unit": "₹ Crore",
                        "rows": {"Total Income": [300, 1200, 1000], "Profit After Tax": [30, 120, 90]}},
         "kpi": {"ROE": [18.0, 20.0], "ROCE": [9.0], "Debt / Equity": [2.0], "P/E (x)": [40.0, 30.0], "Promoter Holding": [90.0, 70.0]},
         "issue": {"Fresh Issue": "₹100 Cr", "Offer for Sale": "₹300 Cr"},
         "peers": [{"name": "Self", "pe": None}, {"name": "A", "pe": 20.0}, {"name": "B", "pe": 30.0}]}
    v = {c["name"]: c["verdict"] for c in ia.fundamentals(d, None)}
    assert v == {"Revenue growth": "good", "Profit growth": "good", "Return on equity": "good", "Return on capital": "bad",
                 "Debt to equity": "bad", "Price against peers": "bad", "Offer for sale": "bad", "Promoter stake after listing": "good"}
    assert ia.score([{"verdict": "good"}, {"verdict": "warn"}, {"verdict": "bad"}]) == 50


def test_signal_rules():
    ipo = {"status": "U", "price": 100}
    assert ia.signal(ipo, {"win_pct": 90}, 60, 60)[0] == "Apply"
    assert ia.signal(ipo, {"win_pct": 80}, 40, 60)[0] == "Listing gain only"
    assert ia.signal(ipo, {"win_pct": 60}, 70, 60)[0] == "Neutral"
    assert ia.signal(ipo, {"win_pct": 40}, 50, 60)[0] == "Avoid"
    assert ia.signal({"status": "U", "price": None}, None, None, None)[0] == "Waiting"
    assert ia.signal({"status": "LP", "price": 100}, {"win_pct": 90}, 60, 60)[0] == "Listed"


def test_demand_needs_a_price():
    assert ia.demand({"price": None, "gmp": 9, "gmp_high": 9, "sub": None}, None) == []
    names = [c["name"] for c in ia.demand({"price": 100, "gmp": 20, "gmp_pct": 20, "gmp_high": 40, "sub": 30}, None)]
    assert names == ["Grey market premium", "GMP trend", "Subscription"]
