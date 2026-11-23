"""News outlets you've chosen not to read, editable from the News page. Paper app, personal reading list: nothing here trades.

Each outlet has a name, a pattern matched against a headline's publisher (Google News names the original outlet) and the sites
its links point to. Blocking or unblocking takes effect on the next news request. The first time, the list is seeded with every
outlet blocked so far. An outlet that had its own feed (Economic Times, Business Standard, Yahoo Finance) gets that feed back
when it is unblocked. Every change is kept with its time.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from urllib.parse import urlparse

from strategies import common

FILE = common.CACHE_DIR / "news_outlets.json"
HISTORY = 200
_lock = threading.Lock()
_compiled: dict = {"stamp": None, "rx": None, "sites": ()}

# (name, publisher pattern, sites): everything blocked up to 17 Nov 2026, as the starting list
SEED = [
    ("NDTV and NDTV Profit", r"\bndtv\b", ["ndtv.com", "ndtvprofit.com"]),
    ("The Economic Times", r"economic ?times|^et ?markets", ["economictimes.indiatimes.com"]),
    ("The Hindu", r"^the hindu$", ["thehindu.com"]),
    ("The Times of India", r"times of india", ["timesofindia.indiatimes.com"]),
    ("News18", r"\bnews ?18\b", ["news18.com"]),
    ("Business Standard", r"business standard", ["business-standard.com"]),
    ("Investing.com", r"investing\.com", ["investing.com"]),
    ("The Indian Express and New Indian Express", r"indian express", ["indianexpress.com", "newindianexpress.com"]),
    ("RushLane", r"rush ?lane", ["rushlane.com"]),
    ("News On AIR", r"news ?on ?air", ["newsonair.gov.in"]),
    ("CNBC TV18", r"cnbc[- ]?tv ?18", ["cnbctv18.com"]),
    ("IBTimes", r"ibtimes", ["ibtimes.co.in"]),
    ("Mathrubhumi", r"mathrubhumi", ["mathrubhumi.com"]),
    ("The Wall Street Journal", r"\bwsj\b|wall street journal", ["wsj.com"]),
    ("ESPNcricinfo", r"cricinfo", ["espncricinfo.com"]),
    ("CarWale", r"carwale", ["carwale.com"]),
    ("Forex Factory", r"forex ?factory", ["forexfactory.com"]),
    ("The Jerusalem Post", r"jerusalem post", ["jpost.com"]),
    ("Cricbuzz", r"cricbuzz", ["cricbuzz.com"]),
    ("India Today", r"india today", ["indiatoday.in"]),
    ("Deccan Herald", r"deccan ?herald", ["deccanherald.com"]),
    ("Gaadiwaadi", r"gaadiwaadi", ["gaadiwaadi.com"]),
    ("BikeAdvice", r"bike ?advice", ["bikeadvice.in"]),
    ("BusinessLine", r"business ?line", ["thehindubusinessline.com"]),
    ("The Financial Express", r"financial express", ["financialexpress.com"]),
    ("Al Jazeera", r"al ?jazeera", ["aljazeera.com"]),
    ("Dalal Street Investment Journal", r"dalal street|\bdsij\b", ["dsij.in"]),
    ("Upstox", r"upstox", ["upstox.com"]),
    ("Financial Times", r"financial times|^ft$", ["ft.com"]),
    ("The Guardian", r"the guardian|guardian (world|business)", ["theguardian.com"]),
    ("The Hans India", r"hans india", ["thehansindia.com"]),
    ("ET Now", r"\bet ?now\b", ["etnownews.com"]),
    ("Equitymaster", r"equitymaster", ["equitymaster.com"]),
    ("Reuters", r"reuters", ["reuters.com"]),
    ("Times Now", r"times now", ["timesnownews.com"]),
    ("Zee Business", r"zee business", ["zeebiz.com"]),
    ("Hindustan Times", r"hindustan times", ["hindustantimes.com"]),
    ("HDFC Sky", r"hdfc sky", ["hdfcsky.com"]),
    ("TradingView", r"trading ?view", ["tradingview.com"]),
    ("FXEmpire", r"fx ?empire", ["fxempire.com"]),
    ("Yahoo Finance", r"yahoo", ["yahoo.com"]),
    ("The Economist", r"\beconomist\b", ["economist.com"]),
]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _seed() -> dict:
    return {"outlets": [{"id": _slug(n), "name": n, "pattern": p, "sites": s, "blocked": True, "custom": False} for n, p, s in SEED], "history": []}


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _seed()


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)
    _compiled["stamp"] = None


def _matchers():
    """The compiled pattern and sites of every blocked outlet, rebuilt only after a change."""
    try:
        stamp = FILE.stat().st_mtime
    except OSError:
        stamp = "seed"
    if _compiled["stamp"] != stamp:
        on = [o for o in _load()["outlets"] if o.get("blocked")]
        pats = [f"(?:{o['pattern']})" for o in on if o.get("pattern")]
        _compiled.update(stamp=stamp, rx=re.compile("|".join(pats), re.I) if pats else None,
                         sites=tuple(s for o in on for s in o.get("sites") or []))
    return _compiled["rx"], _compiled["sites"]


def is_blocked(item: dict) -> bool:
    rx, sites = _matchers()
    if rx and rx.search((item.get("publisher") or "").strip()):
        return True
    host = urlparse(item.get("link") or "").netloc.lower()
    return any(host == s or host.endswith("." + s) for s in sites)


def name_blocked(name: str) -> bool:
    """Whether an outlet name (as a feed labels itself) is blocked."""
    return is_blocked({"publisher": name})


def outlets() -> dict:
    data = _load()
    return {"outlets": sorted(data["outlets"], key=lambda o: o["name"].lower()), "history": data.get("history", [])[:50]}


def _log(data: dict, action: str, name: str) -> None:
    data["history"] = ([{"at": datetime.now(common.IST).isoformat(timespec="seconds"), "action": action, "outlet": name}] + data.get("history", []))[:HISTORY]


def set_blocked(outlet_id: str, blocked: bool) -> dict:
    with _lock:
        data = _load()
        o = next((x for x in data["outlets"] if x["id"] == outlet_id), None)
        if not o:
            raise ValueError("Unknown outlet.")
        if bool(o.get("blocked")) != bool(blocked):
            o["blocked"] = bool(blocked)
            _log(data, "blocked" if blocked else "unblocked", o["name"])
            _save(data)
    return outlets()


def block_name(name: str) -> dict:
    """Blocks an outlet by the name its headlines carry, adding it to the list if it's new."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or len(name) > 120:
        raise ValueError("Give the outlet's name as it appears on its headlines.")
    with _lock:
        data = _load()
        oid = _slug(name)
        o = next((x for x in data["outlets"] if x["id"] == oid or x["name"].lower() == name.lower()), None)
        if o:
            if o.get("blocked"):
                return outlets()
            o["blocked"] = True
        else:
            data["outlets"].append({"id": oid, "name": name, "pattern": r"^\s*" + re.escape(name) + r"\s*$", "sites": [], "blocked": True, "custom": True})
        _log(data, "blocked", name)
        _save(data)
    return outlets()
