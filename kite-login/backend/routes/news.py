"""Market news from public RSS feeds, merged newest first."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
import html as _html
import re
from urllib.parse import urlparse
import requests
import threading
import time
import xml.etree.ElementTree as ET

from fastapi import Query

from core import app, log
from strategies import news_priority


# ---------------------------------------------------------------------------
# Market news: public RSS feeds (no Kite login needed), merged newest first and cached for a minute.
# ---------------------------------------------------------------------------


NEWS_FEEDS = {
    "mint": ("Mint", "https://www.livemint.com/rss/markets"),
    # Google News feeds removed 19 Nov 2026: only Mint and NSE filings are read now
}
# Outlets you chose not to read. Their own feeds are gone above; this also drops them when Google News carries them,
# matched on the publisher's name and on the link's site.
BLOCKED_OUTLETS = re.compile(
    r"\bndtv\b|economic ?times|^et ?markets|^the hindu$|times of india|\bnews ?18\b|business standard|investing\.com"
    r"|indian express|rush ?lane|news ?on ?air|cnbc[- ]?tv ?18|ibtimes|mathrubhumi|\bwsj\b|wall street journal|cricinfo|carwale"
    r"|forex ?factory|jerusalem post|cricbuzz|india today|deccan ?herald|gaadiwaadi|bike ?advice"
    r"|business ?line|financial express|al ?jazeera|dalal street|\bdsij\b|upstox|financial times|^ft$|the guardian|guardian (world|business)"
    r"|hans india|\bet ?now\b|equitymaster|reuters"
    r"|times now|zee business|hindustan times|hdfc sky|trading ?view|fx ?empire|yahoo|\beconomist\b", re.I)
BLOCKED_SITES = ("ndtv.com", "ndtvprofit.com", "economictimes.indiatimes.com", "timesofindia.indiatimes.com", "thehindu.com",
                 "news18.com", "business-standard.com", "investing.com",
                 "indianexpress.com", "newindianexpress.com", "rushlane.com", "newsonair.gov.in", "cnbctv18.com", "ibtimes.co.in",
                 "mathrubhumi.com", "wsj.com", "espncricinfo.com", "carwale.com", "forexfactory.com", "jpost.com", "cricbuzz.com",
                 "indiatoday.in", "deccanherald.com", "gaadiwaadi.com", "bikeadvice.in",
                 "thehindubusinessline.com", "financialexpress.com", "aljazeera.com", "dsij.in", "upstox.com", "ft.com",
                 "theguardian.com", "thehansindia.com", "etnownews.com", "equitymaster.com", "reuters.com",
                 "timesnownews.com", "zeebiz.com", "hindustantimes.com", "hdfcsky.com", "tradingview.com", "fxempire.com", "yahoo.com", "economist.com")


# Only these are read (the user's choice, 19 Nov 2026): Mint, from its own feed or when Google News carries it, and NSE filings.
# Every other outlet is dropped, whoever it is.
ALLOWED_OUTLETS = re.compile(r"^(live ?)?mint$|livemint", re.I)
ALLOWED_SITES = ("livemint.com", "nseindia.com")


def allowed(item: dict) -> bool:
    if item.get("source") in ("mint", "nse"):
        return True
    if ALLOWED_OUTLETS.search((item.get("publisher") or "").strip()):
        return True
    host = urlparse(item.get("link") or "").netloc.lower()
    return any(host == s or host.endswith("." + s) for s in ALLOWED_SITES)


def blocked(item: dict) -> bool:
    if not allowed(item):
        return True
    if BLOCKED_OUTLETS.search((item.get("publisher") or "").strip()):
        return True
    host = urlparse(item.get("link") or "").netloc.lower()
    return any(host == s or host.endswith("." + s) for s in BLOCKED_SITES)
NEWS_TTL = 30                  # seconds: feeds are checked twice a minute
_news_cache: dict[str, tuple[float, list[dict]]] = {}
_news_lock = threading.Lock()
_TAG = re.compile(r"<[^>]+>")


def _news_text(s: str | None, limit: int) -> str:
    s = re.sub(r"\s+", " ", _TAG.sub(" ", _html.unescape(s or ""))).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def _fetch_feed(key: str) -> list[dict]:
    """One feed's items; a feed that fails keeps its last good copy (the others still show)."""
    label, url = NEWS_FEEDS[key]
    with _news_lock:
        hit = _news_cache.get(key)
    if hit and time.time() - hit[0] < NEWS_TTL:
        return hit[1]
    items = []
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KiteDesk/1.0"})
        r.raise_for_status()
        for it in ET.fromstring(r.content).iter("item"):
            title = _news_text(it.findtext("title"), 220)
            link = (it.findtext("link") or "").strip()
            if not title or not link.startswith(("https://", "http://")):
                continue
            src = it.find("source")
            publisher = src.text.strip() if src is not None and src.text else label
            if key in ("google", "world", "business") and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3]
            try:
                published = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published = None
            items.append({"id": link, "title": title, "link": link, "source": key, "source_label": label,
                          "publisher": publisher, "published": published,
                          "summary": "" if key in ("google", "world", "business") else _news_text(it.findtext("description"), 320)})
    except (requests.RequestException, ET.ParseError) as exc:
        log.warning("news feed %s failed: %s", key, exc)
        if hit:
            return hit[1]
    with _news_lock:
        _news_cache[key] = (time.time(), items)
    return items


@app.get("/api/news")
def market_news(source: Literal["all", "mint", "google", "world", "business", "nse"] = "all",
                limit: int = Query(60, ge=1, le=200)):
    keys = list(NEWS_FEEDS) if source == "all" else [k for k in [source] if k in NEWS_FEEDS]
    with ThreadPoolExecutor(max(1, len(keys))) as pool:
        batches = list(pool.map(_fetch_feed, keys))
    if source in ("all", "nse"):
        batches.append(_filings())
    seen, items = set(), []
    for it in sorted((i for b in batches for i in b), key=lambda i: i["published"] or "", reverse=True):
        if blocked(it):
            continue
        k = re.sub(r"\W+", "", it["title"].lower())[:80]
        if k not in seen:
            seen.add(k)
            items.append(it)
    ms = news_priority.stock_matchers()
    items = [news_priority.enrich(i, ms) for i in items[:limit]]
    return {"items": items, "sources": {**{k: v[0] for k, v in NEWS_FEEDS.items()}, "nse": "NSE filings"},
            "fetched_at": datetime.now(timezone.utc).isoformat()}




def _filings() -> list[dict]:
    """NSE's corporate announcements (material ones), checked at most once a minute. A refusal keeps the last copy."""
    try:
        from terminal.sources import nse
        res = nse("corporate-announcements?index=equities", ttl=60, key="news_announcements")
        return news_priority.filing_items(res.get("data") or [])
    except Exception as exc:  # noqa: BLE001 - the news feeds still show
        log.warning("NSE filings failed: %s", exc)
        return []


@app.post("/api/logos/request")
def request_logos(body: dict):
    """The app saw stocks without a logo: queue them for the logo fetcher (it works in the background, one at a time)."""
    from strategies import logo_fetcher
    return {"queued": logo_fetcher.request(list(body.get("symbols") or [])), **logo_fetcher.status()}
