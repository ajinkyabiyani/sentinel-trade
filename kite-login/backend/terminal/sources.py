"""Public data sources for what Kite does not publish: NSE filings and flows, the Nifty 500 list, Screener.in fundamentals.

Rules for every request here:
  - The client identifies itself honestly. No browser impersonation, no cookie tricks, no retry around a refusal.
  - A source that refuses (403, 429) is left alone for REFUSAL_BACKOFF; the page shows the last saved copy, marked stale.
  - Everything is cached on disk, so a page view never turns into a burst of requests.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger("kite-desk.terminal")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache" / "terminal"
HEADERS = {"User-Agent": "SentinelTrade/1.0 (personal research tool; python-requests)"}
TIMEOUT = 25
REFUSAL_BACKOFF = 3600

NSE_API = "https://www.nseindia.com/api/"
NIFTY500_CSV = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
SCREENER = "https://www.screener.in/company/{symbol}/{suffix}"

_lock = threading.Lock()
_refused_until: dict[str, float] = {}


class SourceRefused(Exception):
    """The source said no, or is being left alone after saying no."""


def _host(url: str) -> str:
    return url.split("/")[2]


def _get(url: str) -> requests.Response:
    host = _host(url)
    if time.time() < _refused_until.get(host, 0):
        raise SourceRefused(f"{host} refused earlier; leaving it alone for now")
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if r.status_code in (403, 429):
        _refused_until[host] = time.time() + REFUSAL_BACKOFF
        raise SourceRefused(f"{host} answered {r.status_code}")
    return r


def _cache_file(key: str) -> Path:
    return CACHE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', key)}.json"


def _read_cache(key: str) -> dict | None:
    try:
        return json.loads(_cache_file(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(key: str, entry: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_file(key)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    tmp.replace(path)


def cached(key: str, ttl: float, fetch) -> dict:
    """{data, fetched_at, stale, error}: fresh within `ttl` seconds, else refetched; a failure serves the last saved copy."""
    with _lock:
        entry = _read_cache(key)
    if entry and time.time() - entry["at"] < ttl:
        return {"data": entry["data"], "fetched_at": entry["fetched_at"], "stale": False, "error": None}
    try:
        data = fetch()
    except (requests.RequestException, SourceRefused, ValueError, KeyError) as exc:
        log.warning("source %s: %s", key, exc)
        if entry:
            return {"data": entry["data"], "fetched_at": entry["fetched_at"], "stale": True, "error": str(exc)}
        return {"data": None, "fetched_at": None, "stale": True, "error": str(exc)}
    now = datetime.now(IST).isoformat(timespec="seconds")
    with _lock:
        _write_cache(key, {"at": time.time(), "fetched_at": now, "data": data})
    return {"data": data, "fetched_at": now, "stale": False, "error": None}


def nse(path: str, ttl: float = 1800, key: str | None = None) -> dict:
    """A public NSE JSON feed, cached."""
    def fetch():
        r = _get(NSE_API + path)
        r.raise_for_status()
        return r.json()
    return cached(key or f"nse_{path}", ttl, fetch)


def nifty500(ttl: float = 7 * 86400) -> list[dict]:
    """The Nifty 500 list: [{symbol, name, sector}], where sector is the index provider's industry group."""
    def fetch():
        r = _get(NIFTY500_CSV)
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        return [{"symbol": x["Symbol"].strip(), "name": x["Company Name"].strip(), "sector": x["Industry"].strip()}
                for x in rows if x.get("Symbol")]
    return cached("nifty500", ttl, fetch)["data"] or []


# ---------------- Screener.in company pages ----------------
_RATIO = re.compile(r'<li class="flex flex-space-between"[^>]*>\s*<span class="name">\s*(.*?)\s*</span>\s*'
                    r'<span class="nowrap value">(.*?)</span>\s*</li>', re.S)
_RATIO_KEYS = {"Market Cap": "mcap_cr", "Current Price": "price", "Stock P/E": "pe", "Book Value": "book_value",
               "Dividend Yield": "div_yield", "ROCE": "roce", "ROE": "roe", "Face Value": "face_value"}


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _number(text: str) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", text.replace("₹", ""))
    return float(m.group().replace(",", "")) if m else None


def _table(section: str) -> dict | None:
    """The first table of a Screener section: {periods: [...], rows: {label: [numbers]}}."""
    m = re.search(r"<table[^>]*>(.*?)</table>", section, re.S)
    if not m:
        return None
    head = re.search(r"<thead>(.*?)</thead>", m.group(1), re.S)
    periods = [_text(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", head.group(1), re.S)][1:] if head else []
    rows: dict[str, list] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1).split("</thead>")[-1], re.S):
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) > 1:
            rows[cells[0].replace("+", "").strip()] = [_number(c) for c in cells[1:]]
    return {"periods": periods, "rows": rows}


def _section(html: str, name: str) -> str:
    m = re.search(rf'<section id="{name}"(.*?)</section>', html, re.S)
    return m.group(1) if m else ""


def parse_screener(html: str) -> dict:
    """Fields of a Screener company page. Raises ValueError when the page does not look like one."""
    out: dict = {}
    for label, value in _RATIO.findall(html):
        key = _RATIO_KEYS.get(_text(label))
        if key:
            out[key] = _number(_text(value))
    if out.get("mcap_cr") is None:
        raise ValueError("no market cap on the page")
    about = re.search(r'<div class="sub show-more-box about"[^>]*>(.*?)</div>', html, re.S)
    out["about"] = _text(about.group(1)) if about else None
    links = dict(re.findall(r'title="(Broad Sector|Sector|Broad Industry|Industry)"[^>]*>\s*([^<]+?)\s*<', html))
    out["sector"] = unescape(links.get("Sector") or links.get("Broad Sector") or "") or None
    out["industry"] = unescape(links.get("Industry") or links.get("Broad Industry") or "") or None
    site = re.search(r'<a href="(https?://(?!www\.(?:bse|nse)india)[^"]+)"[^>]*>\s*<i class="icon-link', html)
    out["website"] = site.group(1) if site else None
    out["quarters"] = _table(_section(html, "quarters"))
    out["shareholding"] = _table(_section(html, "shareholding"))
    return out


def screener_company(symbol: str, ttl: float = 86400) -> dict:
    """One company's fundamentals (consolidated where the company reports them), cached."""
    def fetch():
        for suffix in ("consolidated/", ""):
            r = _get(SCREENER.format(symbol=quote(symbol, safe=""), suffix=suffix))
            if r.status_code == 200:
                try:
                    return parse_screener(r.text)
                except ValueError:
                    if not suffix:   # the standalone page is the last try
                        raise
        raise ValueError(f"no Screener page for {symbol}")
    return cached(f"screener_{symbol}", ttl, fetch)
