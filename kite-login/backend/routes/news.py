"""Market news from public RSS feeds, merged newest first."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal
import html as _html
import re
import requests
import threading
import time
import xml.etree.ElementTree as ET

from fastapi import Query

from core import app, log


# ---------------------------------------------------------------------------
# Market news: public RSS feeds (no Kite login needed), merged newest first and cached for a minute.
# ---------------------------------------------------------------------------


NEWS_FEEDS = {
    "yahoo": ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5ENSEI,%5EBSESN&region=IN&lang=en-IN"),
    "et": ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    "mint": ("Mint", "https://www.livemint.com/rss/markets"),
    "bs": ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    "google": ("Google News", "https://news.google.com/rss/search?q=sensex+OR+nifty+stock+market&hl=en-IN&gl=IN&ceid=IN:en"),
}
NEWS_TTL = 60
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
            if key == "google" and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3]
            try:
                published = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published = None
            items.append({"id": link, "title": title, "link": link, "source": key, "source_label": label,
                          "publisher": publisher, "published": published,
                          "summary": "" if key == "google" else _news_text(it.findtext("description"), 320)})
    except (requests.RequestException, ET.ParseError) as exc:
        log.warning("news feed %s failed: %s", key, exc)
        if hit:
            return hit[1]
    with _news_lock:
        _news_cache[key] = (time.time(), items)
    return items


@app.get("/api/news")
def market_news(source: Literal["all", "yahoo", "et", "mint", "bs", "google"] = "all",
                limit: int = Query(60, ge=1, le=200)):
    keys = list(NEWS_FEEDS) if source == "all" else [source]
    with ThreadPoolExecutor(len(keys)) as pool:
        batches = list(pool.map(_fetch_feed, keys))
    seen, items = set(), []
    for it in sorted((i for b in batches for i in b), key=lambda i: i["published"] or "", reverse=True):
        k = re.sub(r"\W+", "", it["title"].lower())[:80]
        if k not in seen:
            seen.add(k)
            items.append(it)
    return {"items": items[:limit], "sources": {k: v[0] for k, v in NEWS_FEEDS.items()},
            "fetched_at": datetime.now(timezone.utc).isoformat()}


