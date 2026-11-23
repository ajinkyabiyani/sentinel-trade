"""The event screener's data, loop and routes. Reads public sources and Kite prices; places nothing."""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from core import ALGO_DIR, IST, _kite_call, app, kite_client, log, store
from routes.market import _instrument_list, _near_future
from routes.skills_data import _historical, token_of
from strategies import event_chains as ec
from strategies import event_screener as es
from terminal import sources
from terminal.market import store as terminal_store

FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC Technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Guardian World": "https://www.theguardian.com/world/rss",
    "Guardian Business": "https://www.theguardian.com/uk/business/rss",
    "NYT World": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "NYT Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Economic Times Industry": "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
    "The Hindu Business": "https://www.thehindu.com/business/feeder/default.rss",
    "Mint Companies": "https://www.livemint.com/rss/companies",
}
COMMODITIES = (("MCX", "COPPER", "Copper"), ("MCX", "ALUMINIUM", "Aluminium"))
HISTORY_DAYS = 1500
LOOP_SECONDS = 600
NEWS_SECONDS = 180            # publishers update their feeds every few minutes, so checking faster adds load, not news
MIN_TRIGGER_SECONDS = 300     # the shortest gap between full passes set off by a new theme
_daily: dict[str, tuple[float, list]] = {}


def _session_kite() -> KiteConnect | None:
    d = store.load()
    if not d:
        return None
    kite = KiteConnect(api_key=d["api_key"])
    kite.set_access_token(d["access_token"])
    return kite


def daily_closes(symbol: str) -> list[list]:
    """[[YYYY-MM-DD, close], ...] from the terminal's saved Kite candles, else from Kite and kept for six hours. Empty without a login, or for an unknown symbol."""
    saved = terminal_store.candles.get(symbol)
    if saved:
        return [[str(c[0])[:10], c[4]] for c in saved]
    hit = _daily.get(symbol)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    kite = _session_kite()
    if not kite:
        return []
    token = token_of(_instrument_list(kite, "NSE"), symbol)
    if not token:
        return []
    today = datetime.now(IST).date()
    try:
        rows = [[str(c["date"])[:10], c["close"]] for c in _historical(kite, token, today - timedelta(days=HISTORY_DAYS), today, "1d")]
    except HTTPException as exc:
        log.warning("event screener candles %s: %s", symbol, exc.detail)
        return []
    _daily[symbol] = (time.time(), rows)
    return rows


def _parse_feed(text: str, source: str) -> list[dict]:
    """Title, link, date and short summary of each item. Used only for pattern matching and display."""
    def clean(raw: str) -> str:
        raw = re.sub(r"<!\[CDATA\[|\]\]>", "", raw)
        return re.sub(r"\s+", " ", sources.unescape(re.sub(r"<[^>]+>", " ", sources.unescape(raw)))).strip()
    out = []
    for it in re.findall(r"<item[ >].*?</item>", text, re.S):
        title = re.search(r"<title[^>]*>(.*?)</title>", it, re.S)
        link = re.search(r"<link[^>]*>(.*?)</link>", it, re.S)
        when = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        desc = re.search(r"<description[^>]*>(.*?)</description>", it, re.S)
        if title and link:
            out.append({"title": clean(title.group(1))[:240], "link": clean(link.group(1)), "date": when.group(1).strip() if when else None, "summary": clean(desc.group(1))[:400] if desc else "", "source": source})
    return out


def world_news() -> dict:
    """Headlines from named outlets' public feeds. A feed that refuses is reported on the page and its last saved copy is used."""
    items, status = [], {}
    for name, url in FEEDS.items():
        def fetch(url=url, name=name):
            r = sources._get(url)
            r.raise_for_status()
            return _parse_feed(r.content.decode("utf-8", "replace"), name)
        res = sources.cached(f"events_feed_{name}", NEWS_SECONDS - 10, fetch)
        items += res.get("data") or []
        status[name] = {"count": len(res.get("data") or []), "fetched_at": res.get("fetched_at"), "error": res.get("error")}
    return {"items": items, "status": status}


def saved_funds() -> dict[str, dict]:
    """Saved Screener data for the Nifty 500, with no network call: the terminal keeps it refreshed."""
    def none():
        raise ValueError("not saved yet")
    out = {}
    for u in sources.nifty500():
        res = sources.cached(f"screener_{u['symbol']}", 10 ** 10, none)
        if res.get("data"):
            out[u["symbol"]] = {**res["data"], "name": u.get("name"), "fetched_at": res.get("fetched_at")}
    return out


screener = es.Screener(nse=sources.nse, screener=sources.screener_company, closes=daily_closes, world=world_news, get=sources._get, cached=sources.cached, universe=sources.nifty500, funds=saved_funds)


def anthropic_key() -> str | None:
    """From the environment or C:\\Algo\\Anthropic_key.txt. It goes to Anthropic's API only: never to the browser or the log."""
    found = os.environ.get("ANTHROPIC_API_KEY")
    if found:
        return found.strip()
    try:
        return (ALGO_DIR / "Anthropic_key.txt").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


drafter = ec.Drafter(funds=saved_funds, key=anthropic_key)
es.refresh_chains()


def _loop() -> None:
    time.sleep(120)
    while True:
        try:
            now = datetime.now(IST)
            if store.load() and "08:00" <= now.strftime("%H:%M") <= "20:00":
                screener.run()
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("event screener loop failed")
        time.sleep(LOOP_SECONDS)


def _news_loop() -> None:
    """Live themes, at any hour and without a Kite login, since the news is public. A theme that has just gone live rebuilds the theses at once."""
    time.sleep(20)
    last_pass = 0.0
    while True:
        try:
            live = screener.refresh_news()
            if live["new_themes"] and time.time() - last_pass >= MIN_TRIGGER_SECONDS and not screener.progress.get("running"):
                last_pass = time.time()
                threading.Thread(target=screener.run, daemon=True, name="event-screener-new-theme").start()
        except Exception:  # noqa: BLE001 - a failed check must not stop the next one
            log.exception("event screener news check failed")
        time.sleep(NEWS_SECONDS)


@app.on_event("startup")
def _start_events() -> None:
    threading.Thread(target=_loop, daemon=True, name="event-screener").start()
    threading.Thread(target=_news_loop, daemon=True, name="event-news").start()


@app.get("/api/events")
def events():
    live = es._read("live.json", {})                                # newer than the last full pass: the themes shown are always the latest
    latest = {**es._read("latest.json", {}), **live}
    reviews = es._read("reviews.json", {})
    return {"latest": latest, "reviews": reviews, "progress": screener.progress, "edges": sorted(screener.reviewed_edges()), "chains": es.CHAINS, "thresholds": es.THRESHOLDS, "disclaimer": es.DISCLAIMER, "confidence_note": es.CONFIDENCE_NOTE, "version": es.LIBRARY_VERSION, "drafter": drafter.state(), "drafts": ec.drafts()}


@app.post("/api/events/run")
def events_run():
    if screener.progress.get("running"):
        raise HTTPException(409, "The screener is already running")
    threading.Thread(target=screener.run, daemon=True, name="event-screener-run").start()
    return {"started": True}


@app.post("/api/events/drafts/run")
def events_drafts_run():
    """A paid call to Claude, only ever started by this button on the page."""
    if drafter.progress.get("running"):
        raise HTTPException(409, "Claude is already reading")
    threading.Thread(target=drafter.run, daemon=True, name="event-drafter").start()
    return {"started": True}


class DraftBody(BaseModel):
    id: str = Field(min_length=3, max_length=40)
    decision: str = Field(pattern="^(approve|reject|reopen)$")


@app.post("/api/events/drafts/decide")
def events_drafts_decide(body: DraftBody):
    try:
        return {"drafts": ec.decide(body.id, body.decision)}
    except KeyError:
        raise HTTPException(404, "No such draft") from None


class EdgeBody(BaseModel):
    edge: str = Field(min_length=3, max_length=80)
    approved: bool


@app.post("/api/events/edge")
def events_edge(body: EdgeBody):
    try:
        screener.set_edge(body.edge, body.approved)
    except KeyError:
        raise HTTPException(404, "No such edge") from None
    return {"edges": sorted(screener.reviewed_edges())}


class ReviewBody(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    decision: str = Field(pattern="^(approved|rejected)$")


@app.post("/api/events/review")
def events_review(body: ReviewBody):
    reviews = es._read("reviews.json", {})
    reviews[body.id] = {"decision": body.decision, "at": datetime.now(IST).isoformat(timespec="seconds")}
    es._write("reviews.json", reviews)
    return {"reviews": reviews}


@app.get("/api/events/commodities")
def events_commodities(kite: KiteConnect = Depends(kite_client)):
    """Context for input costs, from Kite's near-month futures. It changes no thesis in this version."""
    out = []
    for exch, name, label in COMMODITIES:
        inst = _near_future(kite, exch, name)
        if not inst:
            continue
        key = f"{exch}:{inst['tradingsymbol']}"
        q = (_kite_call(kite.quote, key) or {}).get(key) or {}
        prev = (q.get("ohlc") or {}).get("close")
        last = q.get("last_price")
        out.append({"label": label, "contract": inst["tradingsymbol"], "last": last, "change_pct": round((last / prev - 1) * 100, 2) if last and prev else None})
    return out
