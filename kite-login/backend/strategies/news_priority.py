"""What in the news needs attention first: stock-specific triggers and market-moving global events.

Each headline gets the NSE stocks it names (from the Nifty 500 list), and a lane:
  stock   it names a stock and a material trigger (results, an order win, a deal, a regulator, a rating move, capital action...)
  global  a market-moving event anywhere (war, sanctions, central banks, inflation, crude, a crash, an election, a disaster)
  market  everything else
with tags saying why and a level (high for the strongest triggers, medium otherwise). NSE's own corporate filings are read too:
they are the earliest official word on a stock, usually before the news sites. Routine filings are dropped. Read only.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# (tag, pattern, high?) for stock-specific triggers
STOCK_TRIGGERS = [
    ("Results", r"\b(q[1-4]\s?(fy)?\d*\s?(results?|profit|earnings)|results?|net profit|quarterly (profit|loss)|earnings|revenue (rises|falls|jumps|drops))\b", True),
    ("Order win", r"\b(bags?|wins?|secures?|receives?|bagged|won)\b.{0,40}\b(orders?|contracts?|deal|project)\b|\border (win|worth|book)\b|\bLoA\b", True),
    ("Deal", r"\b(acquir\w*|acquisition|merger|merge|demerger|takeover|open offer|stake (sale|buy|purchase)|to buy .{0,25} stake|joint venture|JV)\b", True),
    ("Block deal", r"\b(block deals?|bulk deals?|promoters? (sell|buy|offload|pledge)\w*)\b", True),
    ("Regulator", r"\b(sebi|ed raids?|income tax|gst notice|penalty|probe|show[- ]cause|cbi|rbi (bars|curbs|penal\w*)|ban(ned)?)\b", True),
    ("Rating", r"\b(upgrades?|downgrades?|target price|price target|initiates? coverage|overweight|underweight|buy rating|sell rating)\b", False),
    ("Management", r"\b(resign\w*|steps down|quits|appoint\w*|new (ceo|md|cfo|chairman))\b", False),
    ("Capital", r"\b(buyback|dividend|bonus issue|bonus shares|stock split|split|rights issue|qip|fund ?rais\w*|ofs|offer for sale|ipo)\b", False),
    ("Pharma", r"\b(usfda|fda|warning letter|import alert|eir|observations?)\b", True),
    ("Operations", r"\b(fire|blaze|shutdown|plant (closure|shut)|strike|explosion|accident|outage|cyber ?attack|hack\w*)\b", True),
    ("Distress", r"\b(default\w*|insolvency|nclt|bankrupt\w*|fraud|forensic audit|auditor resigns?)\b", True),
    ("Big move", r"\b(surges?|soars?|plunges?|tanks?|crash\w*|zooms?|rall(y|ies)|slumps?|hits? (upper|lower) circuit|52-week (high|low))\b", False),
]
# Market-moving global events. Each needs its subject AND its context, so "price war" or "RBI approves a co-op bank's licence" stays out.
def W(p: str) -> str:
    """A pattern matched as whole words."""
    return r"\b(?:" + p + r")\b"


PLACES = W(r"russia|ukraine|israel|iran|gaza|hamas|hezbollah|china|taiwan|pakistan|india|north korea|nato|u\.?s\.?|america|middle east|red sea|yemen|houthis?|syria|lebanon|europe")
GLOBAL_RULES = [   # (tag, subject, context needed or None, words that rule it out or None, high?)
    ("War", W(r"war|missiles?|air ?strikes?|drone strikes?|invasion|invade\w*|troops|bombing|shelling|ceasefire|military (?:strike|action|escalation)"), PLACES,
     W(r"(?:price|trade|turf|talent|bidding|supermarket|streaming|culture|console|cola|format|ratings?) wars?|war (?:on|against) (?:drugs|terror|inflation|waste)|1[89]\d\d"), True),
    ("Geopolitics", W(r"sanctions?|tariffs?|trade war|embargo|retaliat\w*|export (?:ban|curbs?)"), PLACES + "|" + W(r"trump|beijing|washington|brussels|eu"), None, True),
    ("Central banks", W(r"fed|fomc|powell|federal reserve|rbi|reserve bank|ecb|bank of japan|boj|pboc|bank of england"),
     W(r"rates?|repo|policy|inflation|cut|hike|hold|pause|stance|liquidity|minutes|governor|yields?"), W(r"approv\w*|licen[cs]e|co-?operative bank|payments? (?:bank|system)"), True),
    ("Macro data", W(r"inflation|cpi|wpi|gdp|jobs report|payrolls|unemployment rate|recession|pmi|iip|trade deficit|fiscal deficit"), None, None, False),
    ("Crude", W(r"crude|brent|wti|opec\+?|oil prices?"), None, None, False),
    ("Crisis", W(r"market crash|crash(?:es|ed)?|meltdown|sell-?off|bank (?:collapse|failure|run)|bailout|sovereign default|debt crisis|circuit breaker|black (?:monday|friday)|rout"),
     W(r"markets?|stocks?|shares|sensex|nifty|dow|nasdaq|s&p|bonds?|banks?|economy|rupee|dollar"), None, True),
    ("Politics", W(r"election results?|exit polls?|union budget|budget 20\d\d|government falls|no-confidence|snap election"), None, None, True),
    ("Currency", W(r"rupee (?:hits|falls|slumps|record|weakens|strengthens)|dollar index|yuan devalu\w*|currency crisis"), None, None, False),
    ("Disaster", W(r"earthquake|tsunami|cyclone|pandemic|outbreak|lockdown"), None, None, True),
]
_GR = [(t, re.compile(a, re.I), re.compile(b, re.I) if b else None, re.compile(n, re.I) if n else None, h) for t, a, b, n, h in GLOBAL_RULES]
_S = [(t, re.compile(p, re.I), h) for t, p, h in STOCK_TRIGGERS]

# NSE filing subjects: routine ones are dropped; material ones are kept, the strongest marked high
ROUTINE = re.compile(r"newspaper|trading window|monitoring agency|certificate under|reg(ulation)?\.? ?74|loss of share|duplicate share|compliance certificate|investor complaints|"
                     r"closure of trading|book closure|shareholders? meeting|record date|transcript|recording|audio|statement of deviation|esop|esos|"
                     r"allotment of (equity|securities) under esop|copy of", re.I)
MATERIAL_HIGH = re.compile(r"financial result|outcome of board meeting|acquisition|sale or disposal|amalgamation|merger|scheme of arrangement|award|bagging|order|"
                           r"credit rating|resignation|fraud|default|insolvency|buy ?back|bonus|split|dividend|press release|litigation|"
                           r"penalty|search|raid|fund raising|qualified institutions|preferential", re.I)

_names: dict = {"day": None, "matchers": []}
_lock = threading.Lock()
SHORT_NAME = re.compile(r"\b(ltd|limited|india|industries|the|company|corporation|corp|co)\b\.?", re.I)


def matchers(rows: list[dict]) -> list[tuple[str, re.Pattern]]:
    """(symbol, pattern) for each stock: its NSE symbol (4+ letters, as written) or its short name."""
    out = []
    for r in rows:
        sym = r.get("symbol") or ""
        name = re.sub(r"\s+", " ", re.sub(r"[^\w&\s-]", " ", SHORT_NAME.sub(" ", r.get("name") or ""))).strip()
        parts = [re.escape(sym) if len(sym) >= 4 else None, re.escape(name) if len(name) >= 5 else None]
        parts = [p for p in parts if p]
        if parts:
            out.append((sym, re.compile(r"(^|[^\w])(" + "|".join(parts) + r")($|[^\w])")))
    return out


def stock_matchers() -> list[tuple[str, re.Pattern]]:
    """The Nifty 500's matchers, built once a day."""
    today = datetime.now(IST).date().isoformat()
    with _lock:
        if _names["day"] != today or not _names["matchers"]:
            try:
                from terminal.sources import nifty500
                _names.update(day=today, matchers=matchers(nifty500()))
            except Exception:  # noqa: BLE001 - no list: headlines just don't get stocks tagged
                _names.update(day=today, matchers=[])
        return _names["matchers"]


def tickers_in(text: str, ms: list | None = None, limit: int = 3) -> list[str]:
    found = []
    for sym, rx in (ms if ms is not None else stock_matchers()):
        if rx.search(text):
            found.append(sym)
            if len(found) == limit:
                break
    return found


def classify(title: str, summary: str = "", tickers: list[str] | None = None) -> dict:
    """{"lane", "level", "tags"} for one headline."""
    text = f"{title} {summary or ''}"
    tickers = tickers or []
    s_hits = [(t, h) for t, rx, h in _S if rx.search(text)]
    g_hits = [(t, h) for t, rx, ctx, neg, h in _GR if rx.search(text) and (ctx is None or ctx.search(text)) and not (neg and neg.search(text))]
    if tickers and s_hits:
        return {"lane": "stock", "level": "high" if any(h for _, h in s_hits) else "medium", "tags": [t for t, _ in s_hits][:3]}
    if g_hits:
        return {"lane": "global", "level": "high" if any(h for _, h in g_hits) else "medium", "tags": [t for t, _ in g_hits][:3]}
    return {"lane": "market", "level": "low", "tags": []}


def enrich(item: dict, ms: list | None = None) -> dict:
    """A news item with the stocks it names and its priority."""
    t = item.get("tickers") or tickers_in(f"{item.get('title', '')}", ms)
    return {**item, "tickers": t, "priority": item.get("priority") or classify(item.get("title", ""), item.get("summary", ""), t)}


def filing_items(rows: list[dict]) -> list[dict]:
    """NSE corporate announcements as news items: material ones only, newest first."""
    out = []
    for a in rows or []:
        subject, sym = (a.get("desc") or "").strip(), (a.get("symbol") or "").strip()
        if not subject or not sym or ROUTINE.search(subject):
            continue
        try:
            when = datetime.strptime(a.get("sort_date") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).astimezone(timezone.utc).isoformat()
        except ValueError:
            when = None
        text = re.sub(r"\s+", " ", a.get("attchmntText") or "").strip()
        tags = ([t for t, rx, _ in _S if rx.search(f"{subject} {text}")][:2] or [subject[:28]])
        out.append({"id": f"nse:{a.get('seq_id') or sym + subject + str(when)}", "title": f"{sym}: {subject}", "link": a.get("attchmntFile") or "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                    "source": "nse", "source_label": "NSE filing", "publisher": "NSE filing", "published": when, "summary": text[:320],
                    "tickers": [sym], "priority": {"lane": "stock", "level": "high" if MATERIAL_HIGH.search(subject) else "medium", "tags": tags}})
    return out
