"""The local Kite data route for the trader skills: read only, the right shape, and no credential in any response."""
import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest
from core import IST
from routes import skills_data as sd

SRC = (Path(__file__).resolve().parents[1] / "backend" / "routes" / "skills_data.py").read_text(encoding="utf-8")
TOKEN = "secret-token-value"


class FakeKite:
    access_token = TOKEN

    def __init__(self):
        self.history = 0
        self.orders = []

    def quote(self, *keys):
        return {k: {"last_price": 1317.4, "volume": 900, "last_trade_time": datetime(2026, 11, 6, 15, 9, 7), "ohlc": {"open": 1295.0, "high": 1320.0, "low": 1290.0, "close": 1313.4}} for k in keys}

    def historical_data(self, token, a, b, interval):
        self.history += 1
        rows = []
        for day in (5, 6):
            for m in (15, 20):
                rows.append({"date": datetime(2026, 11, day, 9, m, tzinfo=IST), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10})
        return rows

    def place_order(self, **kw):
        self.orders.append(kw)
        raise AssertionError("the data route tried to place an order")


ROWS = [{"tradingsymbol": "RELIANCE", "instrument_token": 1, "instrument_type": "EQ", "segment": "NSE"}, {"tradingsymbol": "M&M", "instrument_token": 2, "instrument_type": "EQ", "segment": "NSE"},
        {"tradingsymbol": "NIFTY 50", "instrument_token": 3, "instrument_type": "EQ", "segment": "INDICES"}]


@pytest.fixture
def kite(monkeypatch):
    k = FakeKite()
    monkeypatch.setattr(sd, "_nse_rows", lambda _k: ROWS)
    monkeypatch.setattr(sd, "PACE", 0)
    monkeypatch.setattr(sd.terminal_store, "candles", {})
    sd._cache.clear()
    return k


class Client:
    """Calls the route functions directly."""

    def __init__(self, kite):
        self.kite = kite

    def chart(self, symbol, range="1d", interval="5m"):  # noqa: A002
        return sd.skills_chart(symbol, range=range, interval=interval, kite=self.kite)

    def spark(self, symbols, range="1d", interval="5m"):  # noqa: A002
        return sd.skills_spark(symbols=symbols, range=range, interval=interval, kite=self.kite)


@pytest.fixture
def client(kite):
    return Client(kite)


def test_read_only_and_no_credentials_in_the_code():
    assert not re.search(r"\.(place_order|modify_order|cancel_order|place_gtt|exit_order)\s*\(", SRC)
    assert "access_token" not in SRC and "api_key" not in SRC


@pytest.mark.parametrize("given,want", [("RELIANCE.NS", "RELIANCE"), ("reliance", "RELIANCE"), ("M&M.NS", "M&M"), ("^NSEI", "NIFTY 50")])
def test_plain_symbol(given, want):
    assert sd.plain_symbol(given) == want


def test_epoch_treats_a_naive_time_as_ist():
    assert sd.epoch(datetime(2026, 11, 6, 9, 15)) == int(datetime(2026, 11, 6, 9, 15, tzinfo=IST).timestamp())
    assert sd.epoch(date(2026, 11, 6)) == int(datetime(2026, 11, 6, tzinfo=IST).timestamp())
    assert sd.epoch("2026-11-06") == sd.epoch(date(2026, 11, 6))


def test_chart_has_the_shape_the_skills_read(client, kite):
    d = client.chart("RELIANCE.NS", "5d", "5m")
    r = d["chart"]["result"][0]
    m = r["meta"]
    assert m["symbol"] == "RELIANCE.NS" and m["regularMarketPrice"] == 1317.4 and m["previousClose"] == 1313.4 and m["regularMarketDayHigh"] == 1320 and m["source"] == "kite"
    assert m["regularMarketTime"] == sd.epoch(datetime(2026, 11, 6, 15, 9, 7))
    assert len(r["timestamp"]) == 4 and r["indicators"]["quote"][0]["close"] == [100.5] * 4
    assert TOKEN not in json.dumps(d)


def test_1d_keeps_only_the_latest_session(client):
    r = client.chart("RELIANCE", "1d", "5m")["chart"]["result"][0]
    assert len(r["timestamp"]) == 2 and datetime.fromtimestamp(r["timestamp"][0], IST).day == 6


def test_a_price_check_makes_no_historical_request(client, kite):
    r = client.chart("RELIANCE", "1d", "1d")["chart"]["result"][0]
    assert kite.history == 0 and r["meta"]["regularMarketPrice"] == 1317.4 and r["indicators"]["quote"][0]["open"] == [1295.0]


def test_candles_are_cached_but_the_quote_is_fresh(client, kite):
    client.chart("RELIANCE", "5d")
    client.chart("RELIANCE", "5d")
    assert kite.history == 1


def test_an_unknown_symbol_is_a_404_the_skills_can_read(client):
    res = client.chart("NOPE")
    assert res.status_code == 404 and "not an NSE symbol" in json.loads(res.body)["chart"]["error"]["description"]


def test_spark_returns_known_symbols_only(client):
    d = client.spark("RELIANCE.NS,M&M.NS,NOPE.NS", "3mo", "1d")["spark"]
    assert [x["symbol"] for x in d["result"]] == ["RELIANCE.NS", "M&M.NS"] and d["result"][0]["response"][0]["meta"]["symbol"] == "RELIANCE.NS"


def test_spark_limits_its_size(client):
    with pytest.raises(sd.HTTPException):
        client.spark(",".join(f"S{i}" for i in range(26)))


def test_daily_uses_the_apps_stored_candles(client, kite, monkeypatch):
    monkeypatch.setattr(sd.terminal_store, "candles", {"RELIANCE": [[datetime.now(IST).date().isoformat(), 1.0, 2.0, 0.5, 1.5, 7]]})
    r = client.chart("RELIANCE", "3mo", "1d")["chart"]["result"][0]
    assert kite.history == 0 and r["indicators"]["quote"][0]["close"] == [1.5]
