"""Outlets the user chose not to read never reach the news feed, by publisher name or by the link's site."""
import pytest

from routes.news import NEWS_FEEDS, blocked


@pytest.mark.parametrize("publisher, link", [
    ("NDTV Profit", "https://news.google.com/x"),
    ("NDTV", "https://news.google.com/x"),
    ("The Economic Times", "https://news.google.com/x"),
    ("ETMarkets.com", "https://news.google.com/x"),
    ("The Hindu", "https://news.google.com/x"),
    ("The Times of India", "https://news.google.com/x"),
    ("News18", "https://news.google.com/x"),
    ("Business Standard", "https://news.google.com/x"),
    ("Investing.com India", "https://news.google.com/x"),
    ("Yahoo Finance", "https://economictimes.indiatimes.com/markets/a.cms"),
    ("Yahoo Finance", "https://in.investing.com/news/a"),
    ("Yahoo Finance", "https://www.ndtvprofit.com/a"),
])
def test_blocked(publisher, link):
    assert blocked({"publisher": publisher, "link": link})


@pytest.mark.parametrize("publisher", ["The Indian Express", "The New Indian Express", "RushLane", "News On AIR", "CNBC TV18", "CNBC-TV18",
                                       "IBTimes India", "Mathrubhumi English", "WSJ", "The Wall Street Journal", "ESPNcricinfo", "Cricbuzz",
                                       "CarWale", "Forex Factory", "The Jerusalem Post", "India Today", "Deccan Herald",
                                       "Gaadiwaadi.com", "BikeAdvice", "BusinessLine", "The Hindu BusinessLine", "The Financial Express",
                                       "Al Jazeera", "Dalal Street Investment Journal", "Upstox", "Financial Times", "The Guardian",
                                       "The Hans India", "ET Now", "Equitymaster", "Reuters",
                                       "Times Now", "Zee Business", "Hindustan Times", "HDFC Sky", "TradingView", "FXEmpire", "Yahoo Finance", "The Economist"])
def test_blocked_second_list(publisher):
    assert blocked({"publisher": publisher, "link": "https://news.google.com/x"})


@pytest.mark.parametrize("item", [
    {"publisher": "Mint", "link": "https://news.google.com/x"},
    {"publisher": "Livemint", "link": "https://news.google.com/x"},
    {"source": "mint", "publisher": "Mint", "link": "https://www.livemint.com/market/a.html"},
    {"source": "nse", "publisher": "NSE filing", "link": "https://nsearchives.nseindia.com/corporate/a.pdf"},
])
def test_kept(item):
    assert not blocked(item)


@pytest.mark.parametrize("publisher", ["Moneycontrol.com", "The Times of Israel", "Business Today", "Bloomberg", "BBC", "Some New Outlet"])
def test_everyone_else_is_blocked(publisher):
    """Only Mint and NSE filings are read (19 Nov 2026): any other outlet is dropped, even one never listed."""
    assert blocked({"publisher": publisher, "link": "https://news.google.com/x"})


def test_only_mint_feed():
    assert list(NEWS_FEEDS) == ["mint"]


def test_their_feeds_are_gone():
    urls = " ".join(u for _, u in NEWS_FEEDS.values())
    assert "economictimes" not in urls and "business-standard" not in urls
