"""Logos on demand: when the app shows a stock without a logo, this finds the company's own logo in the background and saves it.

For each symbol, in order:
  1. Screener.in's company page: the company's full name, its website and the logo it shows beside the name.
  2. Wikipedia: the company's article, accepted only when its infobox lists that NSE symbol, and the logo in that infobox (sharpest).
  3. The logo on the Screener page (96 px).
  4. The company's own website: a logo image in its page header, else its app icon (apple-touch-icon and the like).
Saved under frontend/public/logos and added to index.json, so it is fetched once and served by the app from then on. One symbol at
a time, politely paced; a symbol with nothing found is tried again after a week. Requests name this tool honestly. Read only.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections import deque
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

log = logging.getLogger("kite-desk.logos")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import fetch_logos as fl  # noqa: E402

PACE = 3.0                    # seconds between symbols
RETRY_DAYS = 7
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&-]{1,19}$")
SKIP = re.compile(r"^(NIFTY|SENSEX|BANKEX|INDIA VIX|_)")
_queue: deque = deque()
_queued: set = set()
_lock = threading.Lock()
_worker: dict = {"thread": None}


def _state() -> dict:
    try:
        return json.loads(fl.STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    fl.STATE.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")


def manifest() -> dict:
    try:
        return json.loads(fl.MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- sources

def screener_page(symbol: str) -> tuple[str | None, str | None, str | None]:
    """(company name, website, the logo thumbnail Screener shows beside the name) from the symbol's Screener page."""
    r = fl.get(f"https://www.screener.in/company/{quote(symbol)}/", tries=2)
    if r is None or r.status_code != 200:
        return None, None, None
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", r.text, re.S)
    inner = h1.group(1) if h1 else ""
    name = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", inner))).strip() or None
    thumb = re.search(r'<img[^>]+src="(https://cdn-media\.screener\.in/company-logos/[^"]+)"', inner)
    site = None
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*target="_blank"[^>]*>(.*?)</a>', r.text, re.S):
        u = urlparse(m.group(1))
        host = u.netloc.lower()
        if host and not any(s_ in host for s_ in fl.SKIP_HOSTS) and not u.path.lower().endswith(".pdf") and "rating" not in host and "icon-link" in m.group(2):
            site = f"{u.scheme}://{host}"
            break
    return name, site, (thumb.group(1) if thumb else None)


def safe_svg(b: bytes) -> bool:
    low = b[:200_000].lower()
    head = low.lstrip()[:400]
    return (head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in low[:3000])) and not (
        b"<script" in low or re.search(rb"\son\w+\s*=", low) or b"javascript:" in low or b"<foreignobject" in low or b"xlink:href=\"http" in low)


def fetch_any(url: str) -> tuple[bytes, str] | None:
    """A raster logo big enough to read, or a clean SVG."""
    got = fl.fetch_image(url, wide_ok=True)
    if got:
        return got
    r = fl.get(url, timeout=20, tries=1)
    if r is not None and r.status_code == 200 and 0 < len(r.content) < 400_000 and safe_svg(r.content):
        return r.content, "svg"
    return None


def header_logo_urls(site: str) -> list[str]:
    """Images in the site's page whose src, alt, class or id says logo: the company's own mark, as on its home page."""
    r = fl.get(site, timeout=15, tries=1)
    if r is None or r.status_code != 200:
        return []
    base, html = r.url, r.text[:400_000]
    out = []
    for tag in re.findall(r"<img\b[^>]*>", html, re.I):
        if not re.search(r"logo", tag, re.I) or re.search(r"(partner|client|award|footer|payment|social|app-?store|play)", tag, re.I):
            continue
        src = re.search(r'\s(?:data-src|src)=["\']([^"\']+)["\']', tag, re.I)
        if src and not src.group(1).startswith("data:"):
            out.append(urljoin(base, unescape(src.group(1))))
    return list(dict.fromkeys(out))[:4]


def find(symbol: str) -> tuple[tuple[bytes, str], str] | None:
    """(image, where it came from) for one symbol, or None."""
    name, site, thumb = screener_page(symbol)
    if name:
        hit = fl.article_by_symbol(symbol, name)
        if hit:
            got = fetch_any(hit[1])
            if got:
                return got, f"wikipedia '{hit[0]}'"
    if thumb:                                  # Screener's thumbnails are 96 px images; any raster format it serves is fine
        r = fl.get(thumb, timeout=20, tries=1)
        k = fl.kind(r.content) if r is not None and r.status_code == 200 and 0 < len(r.content) < 400_000 else None
        if k:
            return (r.content, k), "screener logo"
    if site:
        for u in header_logo_urls(site) + fl.site_icon_urls(site):
            got = fetch_any(u)
            if got:
                return got, f"site {u}"
    return None


def store(symbol: str, got: tuple[bytes, str]) -> str:
    for old in fl.OUT.glob(f"{symbol}.*"):
        old.unlink()
    name = f"{symbol}.{got[1]}"
    (fl.OUT / name).write_bytes(got[0])
    with _lock:
        m = manifest()
        m[symbol] = name
        fl.MANIFEST.write_text(json.dumps(m, indent=1, sort_keys=True), encoding="utf-8")
    return name


# ---------------------------------------------------------------- the queue

def request(symbols: list[str]) -> int:
    """Queues the symbols that have no logo and haven't been tried this week. -> how many were queued."""
    have = manifest()
    tried = _state().get("ondemand", {})
    cutoff = (date.today() - timedelta(days=RETRY_DAYS)).isoformat()
    added = 0
    with _lock:
        for s in symbols[:60]:
            s = str(s).upper().strip()
            if not SYMBOL.match(s) or SKIP.match(s) or s in have or s in _queued or tried.get(s, "") > cutoff:
                continue
            _queue.append(s)
            _queued.add(s)
            added += 1
        if added and not (_worker["thread"] and _worker["thread"].is_alive()):
            _worker["thread"] = threading.Thread(target=_work, daemon=True, name="logo-fetcher")
            _worker["thread"].start()
    return added


def _work() -> None:
    while True:
        with _lock:
            if not _queue:
                return
            sym = _queue.popleft()
        try:
            hit = find(sym)
            if hit:
                log.info("logo %s from %s", sym, hit[1])
                store(sym, hit[0])
        except Exception:  # noqa: BLE001 - one bad page never stops the queue
            log.exception("logo %s", sym)
        finally:
            with _lock:
                st = _state()
                st.setdefault("ondemand", {})[sym] = date.today().isoformat()
                _save_state(st)
                _queued.discard(sym)
        time.sleep(PACE)


def status() -> dict:
    with _lock:
        return {"waiting": list(_queue), "running": bool(_worker["thread"] and _worker["thread"].is_alive()), "logos": len(manifest())}
