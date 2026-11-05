"""Market terminal: calculations against hand-worked values, page parsing, and the rules for public sources."""
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from terminal import calendar_data, market, sources

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _rows(closes, start=date(2026, 1, 1)):
    return [[(start + timedelta(days=i)).isoformat(), c, c + 1, c - 1, c, 100 + i] for i, c in enumerate(closes)]


# ---------------- rules ----------------
@pytest.mark.parametrize("module", ["sources", "market", "calendar_data", "routes"])
def test_terminal_never_places_orders(module):
    src = (BACKEND / "terminal" / f"{module}.py").read_text(encoding="utf-8")
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt)\s*\(", src)


def test_public_sources_identify_honestly():
    src = (BACKEND / "terminal" / "sources.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]
    assert "Mozilla" not in code and "Referer" not in code and "Cookie" not in code
    assert "Mozilla" not in sources.HEADERS["User-Agent"]


def test_refusal_is_respected_and_serves_last_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "CACHE_DIR", tmp_path)
    assert sources.cached("k", 3600, lambda: {"v": 1})["data"] == {"v": 1}

    def refuse():
        raise sources.SourceRefused("example.com answered 403")

    res = sources.cached("k", 0, refuse)
    assert res["data"] == {"v": 1} and res["stale"] is True and "403" in res["error"]
    empty = sources.cached("other", 0, refuse)
    assert empty["data"] is None and empty["stale"] is True


def test_refused_host_is_left_alone(monkeypatch):
    calls = []

    class Resp:
        status_code = 403

    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: calls.append(a) or Resp())
    monkeypatch.setattr(sources, "_refused_until", {})
    for _ in range(2):
        with pytest.raises(sources.SourceRefused):
            sources._get("https://example.test/a")
    assert len(calls) == 1


# ---------------- calculations ----------------
def test_wilder_atr_hand_worked():
    rows = [["d", 10, 12, 9, 10, 1]] * 3    # true range 3 each day
    assert market.wilder_atr(rows, n=2) == pytest.approx(3.0)
    assert market.wilder_atr(rows[:2], n=2) is None


def test_rsi_extremes_and_midpoint():
    up = market.wilder_rsi_state(list(range(1, 30)))
    assert market.rsi_with_last(up, 29, 30) == 100.0
    down = market.wilder_rsi_state(list(range(30, 1, -1)))
    assert market.rsi_with_last(down, 2, 1) == pytest.approx(0.0)
    assert market.rsi_with_last(None, 1, 2) is None
    assert market.rsi_with_last((1.0, 1.0), 100, 100) == pytest.approx(100 - 100 / (1 + (13 / 14) / (13 / 14)))


def test_beta_of_a_stock_moving_twice_the_market():
    closes = [100.0]
    for i in range(120):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    bench = {r[0]: r[4] for r in _rows(closes)}
    stock = [100.0]
    for a, b in zip(closes, closes[1:]):
        stock.append(stock[-1] * (1 + 2 * (b / a - 1)))
    assert market.beta_of(_rows(stock), bench) == pytest.approx(2.0)
    assert market.beta_of(_rows(stock)[:30], bench) is None


def test_period_starts_end_of_month_and_year():
    s = market.period_starts(date(2026, 3, 31))
    assert s["1M"] == date(2026, 2, 28) and s["6M"] == date(2025, 9, 30) and s["1Y"] == date(2025, 3, 31)
    assert s["YTD"] == date(2025, 12, 31) and s["1W"] == date(2026, 3, 24)
    assert market.period_starts(date(2026, 1, 15))["1M"] == date(2025, 12, 15)


def test_close_on_or_before_skips_holidays():
    rows = [["2026-01-02", 0, 0, 0, 10, 0], ["2026-01-05", 0, 0, 0, 11, 0]]
    assert market.close_on_or_before(rows, date(2026, 1, 4)) == 10
    assert market.close_on_or_before(rows, date(2026, 1, 5)) == 11
    assert market.close_on_or_before(rows, date(2026, 1, 1)) is None


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 11, 5, 8, 0), 1.0), (datetime(2026, 11, 5, 16, 0), 1.0), (datetime(2026, 11, 7, 11, 0), 1.0),
    (datetime(2026, 11, 5, 9, 16), 5 / 375), (datetime(2026, 11, 5, 12, 22), 187 / 375)])
def test_session_fraction(when, expected):
    assert market.session_fraction(when) == pytest.approx(expected)


def test_stock_row_values():
    rows = _rows([100.0] * 260, start=date(2025, 3, 1))
    q = {"last_price": 110.0, "ohlc": {"open": 102.0, "high": 111.0, "low": 101.0, "close": 100.0}, "volume": 1000, "average_price": 105.0}
    now = datetime(2026, 1, 5, 16, 0)
    static = market.static_metrics(rows, None)
    r = market.stock_row({"symbol": "X"}, static, rows, q, now, market.period_starts(now.date()), {"mcap_cr": 1000.0, "price": 100.0})
    assert r["chg"] == 10.0 and r["gap"] == 2.0 and r["mcap_cr"] == 1100
    assert r["value_cr"] == pytest.approx(1000 * 105 / 1e7, abs=0.01)
    assert r["ret"]["1W"] == 10.0
    assert r["hi52"] == 111.0 and r["from_hi52"] == pytest.approx(-0.9, abs=0.01)
    assert market.stock_row({"symbol": "X"}, static, rows, {"last_price": None}, now, {}, None) is None


def test_mood_average_and_bands():
    rows = [{"chg": 1.0, "vs_sma50": 1.0, "from_hi52": -1.0, "from_lo52": 50.0} for _ in range(3)] + [{"chg": -1.0, "vs_sma50": -1.0, "from_hi52": -30.0, "from_lo52": 2.0}]
    m = market.mood(rows, None, [], None, [])
    assert [p["score"] for p in m["parts"]] == [75, 75, 75]
    assert m["score"] == 75 and m["label"] == "Greed"
    assert market.mood([], None, [], None, []) is None


# ---------------- parsing ----------------
PAGE = """
<li class="flex flex-space-between" data-source="default"><span class="name">Market Cap</span>
<span class="nowrap value">₹ <span class="number">17,76,551</span> Cr.</span></li>
<li class="flex flex-space-between"><span class="name">Stock P/E</span><span class="nowrap value"><span class="number">21.6</span></span></li>
<div class="sub show-more-box about">A <b>large</b> company.</div>
<a title="Sector" href="/x">Oil, Gas &amp; Fuels</a>
<section id="shareholding"><table><thead><tr><th></th><th>Jun 2026</th><th>Sep 2026</th></tr></thead>
<tbody><tr><td>Promoters&nbsp;+</td><td>50.48%</td><td>50.44%</td></tr></tbody></table></section>
"""


def test_parse_screener():
    d = sources.parse_screener(PAGE)
    assert d["mcap_cr"] == 1776551 and d["pe"] == 21.6 and d["about"] == "A large company."
    assert d["sector"] == "Oil, Gas & Fuels"
    assert d["shareholding"]["periods"] == ["Jun 2026", "Sep 2026"]
    assert d["shareholding"]["rows"]["Promoters"] == [50.48, 50.44]
    with pytest.raises(ValueError):
        sources.parse_screener("<html>not a company</html>")


def test_calendar_dates():
    assert calendar_data._iso("06-Nov-2026") == "2026-11-06"
    assert calendar_data._iso("06-NOV-2026") == "2026-11-06"
    assert calendar_data._iso("-") is None and calendar_data._iso(None) is None
