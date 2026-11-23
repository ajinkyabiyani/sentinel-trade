"""One-time (re-runnable) download of company logos into frontend/public/logos, so the app never fetches a logo at runtime.

Sources, best first, for every Nifty 500 / index-constituent symbol:
  1. Wikidata: the official logo (P154) of the company listed on NSE under that ticker, or carrying that ISIN;
  2. Wikipedia: the company's article found by its exact name, then that article's Wikidata logo, or its infobox logo;
  3. the company's own website (read from its Screener.in page): the largest icon it declares.
Logos come from Wikimedia Commons as 256px-wide PNG renders. Indices use the NSE and BSE logos.
Writes logos/index.json {SYMBOL: file}. Anything not found keeps the app's letter tile.
Every request names this tool honestly, is paced, and honours Retry-After.

Usage: python tools/fetch_logos.py [--refresh] [--no-sites]
"""
from __future__ import annotations

import csv
import io
import json
import re
import struct
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "public" / "logos"
STATE = ROOT / "tools" / ".logo_cache.json"          # what each source said, so a re-run asks nobody twice
MANIFEST = OUT / "index.json"
H = {"User-Agent": "SentinelTrade/1.0 (personal research tool; python-requests)"}
NSE_Q = "Q638740"
# Fixed Wikipedia articles whose infobox logo stands for a symbol outside the Nifty 500, or for a group of instruments:
# _NSEIX for every NIFTY index (NSE Indices owns them), _MCX for MCX commodities, JPL for Jio Platforms.
FIXED = {"_NSEIX": "NSE Indices", "_MCX": "Multi Commodity Exchange", "JPL": "Jio Platforms"}
INDEX_NAMES = {"_NSE": "National Stock Exchange of India", "_BSE": "Bombay Stock Exchange"}
SKIP_HOSTS = ("bseindia.com", "nseindia.com", "screener.in", ".gov.in", ".nic.in", "sebi.", "mca.", "rbi.org", "bseindia", "wikipedia.","google.", "youtube.", "linkedin.", "twitter.", "x.com", "facebook.")
STOP = {"ltd", "limited", "the", "of", "and", "co", "company", "corporation", "corp", "india", "inc"}
MIN_PX = 48


def say(*a):
    print(*a, flush=True)


def get(url, *, params=None, timeout=30, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "0") or 0) or 30 * (i + 1)
            say(f"  rate limited by {urlparse(url).netloc}; waiting {wait}s")
            time.sleep(min(wait, 300))
            continue
        return r
    return None


# ---------------------------------------------------------------- the universe

def universe() -> dict[str, dict]:
    """{symbol: {name, isin}} from the Nifty 500 list (with ISIN) and the index-constituent file."""
    out = {}
    r = get("https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv")
    if r is not None and r.status_code == 200:
        for row in csv.DictReader(io.StringIO(r.text)):
            out[row["Symbol"].strip()] = {"name": row["Company Name"].strip(), "isin": row["ISIN Code"].strip()}
    cached = ROOT / "backend" / ".orb_cache" / "terminal" / "nifty500.json"
    if cached.exists():
        for x in json.loads(cached.read_text(encoding="utf-8"))["data"]:
            out.setdefault(x["symbol"], {"name": x["name"], "isin": ""})
    ic = Path(r"C:\Algo\reports\index_constituents.csv")
    if ic.exists():
        for row in csv.DictReader(ic.open(encoding="utf-8")):
            if row.get("symbol"):
                out.setdefault(row["symbol"], {"name": row.get("name") or row["symbol"], "isin": row.get("isin") or ""})
    return out


# ---------------------------------------------------------------- 1. Wikidata by ticker / ISIN

def sparql(q):
    r = get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"}, timeout=120)
    return r.json()["results"]["bindings"] if r is not None and r.status_code == 200 else []


def commons_file(uri: str) -> str:
    return unquote(uri.rsplit("/", 1)[1])


def wikidata_bulk() -> tuple[dict, dict]:
    by_ticker, by_isin = {}, {}
    for b in sparql(f"SELECT ?t ?logo WHERE {{ ?i p:P414 ?s . ?s ps:P414 wd:{NSE_Q} ; pq:P249 ?t . ?i wdt:P154 ?logo }}"):
        by_ticker[b["t"]["value"].upper()] = commons_file(b["logo"]["value"])
    for b in sparql('SELECT ?isin ?logo WHERE { ?i wdt:P946 ?isin . FILTER(STRSTARTS(?isin, "INE")) ?i wdt:P154 ?logo }'):
        by_isin[b["isin"]["value"]] = commons_file(b["logo"]["value"])
    return by_ticker, by_isin


# ---------------------------------------------------------------- 2. Wikipedia by exact name

def words(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("&", " and "))
    return {w for w in s.split() if w not in STOP}


def wiki_title(name: str) -> str | None:
    """The article whose title holds every word of the company's name (and little else), or None."""
    plain = re.sub(r"\s+(Ltd\.?|Limited)$", "", name).strip()
    r = get("https://en.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": plain, "srlimit": 5, "format": "json"})
    if r is None or r.status_code != 200:
        return None
    want = words(plain)
    for h in r.json().get("query", {}).get("search", []):
        got = words(re.sub(r"\s*\(.*\)$", "", h["title"]))
        if want and want <= got and len(got - want) <= 1:
            return h["title"]
    return None


def wiki_logos(titles: list[str]) -> dict[str, str]:
    """{title: commons file} for the articles whose Wikidata item has a logo."""
    out = {}
    for k in range(0, len(titles), 50):
        chunk = titles[k:k + 50]
        r = get("https://en.wikipedia.org/w/api.php", params={"action": "query", "titles": "|".join(chunk), "prop": "pageprops", "ppprop": "wikibase_item", "redirects": 1, "format": "json"})
        if r is None or r.status_code != 200:
            continue
        j = r.json()["query"]
        back = {x["to"]: x["from"] for x in j.get("redirects", []) + j.get("normalized", [])}
        qid = {back.get(p["title"], p["title"]): p.get("pageprops", {}).get("wikibase_item") for p in j["pages"].values()}
        ids = [q for q in qid.values() if q]
        if not ids:
            continue
        e = get("https://www.wikidata.org/w/api.php", params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims", "format": "json"})
        ents = e.json().get("entities", {}) if e is not None and e.status_code == 200 else {}
        for t, q in qid.items():
            c = ents.get(q, {}).get("claims", {}).get("P154")
            if c:
                out[t] = c[-1]["mainsnak"]["datavalue"]["value"]
        time.sleep(1)
    return out


def infobox_logo(title: str) -> str | None:
    """The image in the article infobox's logo cell (class "infobox-image ... logo"), at the thumbnail size the article uses, or None."""
    r = get("https://en.wikipedia.org/w/api.php", params={"action": "parse", "page": title, "prop": "text", "section": 0, "redirects": 1, "format": "json"})
    if r is None or r.status_code != 200:
        return None
    html = r.json().get("parse", {}).get("text", {}).get("*", "")
    m = re.search(r'class="infobox-image[^"]*logo[^"]*".{0,1500}?<img[^>]+src="([^"]+)"', html, re.S)
    if not m:
        return None
    src = unescape(m.group(1))
    return "https:" + src if src.startswith("//") else src


def article_by_symbol(symbol: str, name: str) -> tuple[str, str] | None:
    """(title, logo url) of the Wikipedia article whose infobox lists 'NSE: SYMBOL', searched by name; None if no hit proves it."""
    plain = re.sub(r"\s+(Ltd\.?|Limited)$", "", name).strip()
    r = get("https://en.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": plain, "srlimit": 3, "format": "json"})
    if r is None or r.status_code != 200:
        return None
    for h in r.json().get("query", {}).get("search", []):
        p = get("https://en.wikipedia.org/w/api.php", params={"action": "parse", "page": h["title"], "prop": "text", "section": 0, "redirects": 1, "format": "json"})
        time.sleep(1)
        html = p.json().get("parse", {}).get("text", {}).get("*", "") if p is not None and p.status_code == 200 else ""
        # the {{NSE|SYMBOL}} template: "NSE:" then the symbol as its own link or text node
        sym = "(?:&|&amp;)".join(re.escape(part) for part in symbol.split("&"))
        if not re.search(r"NSE(?:</a>)?\s*:(?:&#160;|\s|<[^>]+>)*" + sym + r"\s*<", html):
            continue
        m = re.search(r'class="infobox-image[^"]*logo[^"]*".{0,1500}?<img[^>]+src="([^"]+)"', html, re.S)
        if m:
            src = unescape(m.group(1))
            return h["title"], ("https:" + src if src.startswith("//") else src)
        return None
    return None


# ---------------------------------------------------------------- 3. the company's own site

def site_of(symbol: str) -> str | None:
    r = get(f"https://www.screener.in/company/{quote(symbol)}/", tries=2)
    if r is None or r.status_code != 200:
        return None
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*target="_blank"', r.text):
        u = urlparse(m.group(1))
        host = u.netloc.lower()
        if host and not any(s in host for s in SKIP_HOSTS) and not u.path.lower().endswith(".pdf"):
            return f"{u.scheme}://{host}"
    return None


def site_icon_urls(site: str) -> list[str]:
    r = get(site, timeout=15, tries=1)
    base, html = (r.url, r.text[:300_000]) if r is not None and r.status_code == 200 else (site, "")
    scored = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel = re.search(r'rel=["\']([^"\']+)', tag, re.I)
        href = re.search(r'href=["\']([^"\']+)', tag, re.I)
        if not rel or not href or "icon" not in rel.group(1).lower() or "mask" in rel.group(1).lower():
            continue
        size = re.search(r'sizes=["\'](\d+)x', tag, re.I)
        px = int(size.group(1)) if size else (180 if "apple" in rel.group(1).lower() else 32)
        scored.append((px, urljoin(base, unescape(href.group(1)))))
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    return list(dict.fromkeys([u for _, u in sorted(scored, reverse=True)] + [root + "/apple-touch-icon.png"]))[:6]


# ---------------------------------------------------------------- image checks

def size_px(b: bytes) -> int:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return min(struct.unpack(">II", b[16:24]))
    if b[:4] == b"\0\0\1\0":
        n = struct.unpack("<H", b[4:6])[0]
        return max((min(b[6 + 16 * i] or 256, b[7 + 16 * i] or 256) for i in range(n)), default=0)
    if b[:2] == b"\xff\xd8":
        i = 2
        while i < len(b) - 9 and b[i] == 0xFF:
            mk, ln = b[i + 1], struct.unpack(">H", b[i + 2:i + 4])[0]
            if 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC):
                return min(struct.unpack(">HH", b[i + 5:i + 9]))
            i += 2 + ln
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP" and b[12:16] == b"VP8X":
        return min(int.from_bytes(b[24:27], "little"), int.from_bytes(b[27:30], "little")) + 1
    return 0


def long_px(b: bytes) -> int:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return max(struct.unpack(">II", b[16:24]))
    return size_px(b)


def kind(b: bytes) -> str | None:
    """File extension for a raster image we can trust; None for anything else (SVGs are never kept as-is)."""
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if b[:4] == b"\0\0\1\0": return "ico"
    if b[:2] == b"\xff\xd8": return "jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    return None


def fetch_image(url: str, wide_ok: bool = False) -> tuple[bytes, str] | None:
    """A raster image at least MIN_PX on its short side, or (wide_ok: a wordmark) on its long side."""
    r = get(url, timeout=20, tries=2)
    if r is None or r.status_code != 200 or not (0 < len(r.content) < 800_000):
        return None
    k = kind(r.content)
    return (r.content, k) if k and (long_px(r.content) if wide_ok else size_px(r.content)) >= MIN_PX else None


def commons_png(file: str) -> tuple[bytes, str] | None:
    return fetch_image(f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(file)}?width=256", wide_ok=True)


# ---------------------------------------------------------------- run

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    refresh = "--refresh" in sys.argv
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"title": {}, "site": {}}
    manifest = {} if refresh or not MANIFEST.exists() else json.loads(MANIFEST.read_text(encoding="utf-8"))
    save = lambda: (STATE.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8"),  # noqa: E731
                    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8"))

    uni = universe()
    todo = {s: v for s, v in uni.items() if s not in manifest}
    say(f"{len(uni)} symbols, {len(todo)} without a logo yet")

    def store(sym, got, src):
        for old in OUT.glob(f"{sym}.*"):
            old.unlink()
        name = f"{sym}.{got[1]}"
        (OUT / name).write_bytes(got[0])
        manifest[sym] = name
        save()
        say(f"  {sym}: {src}")

    # 1. Wikidata, in two queries
    by_ticker, by_isin = wikidata_bulk()
    say(f"wikidata: {len(by_ticker)} tickers, {len(by_isin)} ISINs with a logo")
    for s, v in list(todo.items()):
        f = by_ticker.get(s) or by_isin.get(v["isin"])
        if f and (got := commons_png(f)):
            store(s, got, f"wikidata {f}")
            todo.pop(s)
        time.sleep(0.5)

    # 2. Wikipedia by name (plus the two exchanges for the indices)
    names = {**{s: v["name"] for s, v in todo.items()}, **{k: n for k, n in INDEX_NAMES.items() if k not in manifest}}
    for s, n in names.items():
        if s not in st["title"]:
            st["title"][s] = wiki_title(n)
            save()
            time.sleep(1.2)
    titles = sorted({t for s, t in st["title"].items() if t and s in names})
    logos = wiki_logos(titles)
    say(f"wikipedia: {len(titles)} articles matched, {len(logos)} with a logo")
    for s in names:
        t = st["title"].get(s)
        if t and t in logos and (got := commons_png(logos[t])):
            store(s, got, f"wikipedia '{t}' -> {logos[t]}")
            todo.pop(s, None)
        time.sleep(0.5)

    # 2b. the logo cell of the article's infobox (Wikipedia keeps most Indian logos there, not on Commons)
    for s in names:
        t = st["title"].get(s)
        if s in manifest or not t:
            continue
        u = infobox_logo(t)
        if u and (got := fetch_image(u, wide_ok=True)):
            store(s, got, f"wikipedia infobox '{t}'")
            todo.pop(s, None)
        time.sleep(1)

    # 2b'. the fixed articles
    for s, t in FIXED.items():
        if s not in manifest and (u := infobox_logo(t)) and (got := fetch_image(u, wide_ok=True)):
            store(s, got, f"wikipedia infobox '{t}'")

    # 2c. no article by exact name: an article found by search counts only if its infobox lists "NSE: SYMBOL"
    for s in list(todo):
        if s in manifest or st["title"].get(s) or st.setdefault("proved", {}).get(s) == "none":
            continue
        hit = article_by_symbol(s, todo[s]["name"])
        st["proved"][s] = hit[0] if hit else "none"
        save()
        if hit and (got := fetch_image(hit[1], wide_ok=True)):
            store(s, got, f"wikipedia infobox '{hit[0]}' (lists NSE: {s})")
            todo.pop(s, None)

    # 3. the company's own site (skipped with --no-sites)
    for s in ([] if "--no-sites" in sys.argv else list(todo)):
        if s not in st["site"]:
            st["site"][s] = site_of(s)
            save()
            time.sleep(1.5)
        site = st["site"][s]
        for u in site_icon_urls(site) if site else []:
            if got := fetch_image(u):
                store(s, got, f"site {u}")
                todo.pop(s)
                break

    save()
    say(f"done: {len(manifest)} logos; {len(todo)} keep the letter tile: {', '.join(sorted(todo))}")


if __name__ == "__main__":
    main()
