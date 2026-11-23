"""Second-order event screener for Indian equities: from world events to the less obvious Indian companies they may touch, with the evidence and what is missing.

It produces research hypotheses only: no price forecast, no buy or sell instruction, no position size, and it is connected to no order or paper desk.

Three ways in:
1. World events: headlines from named outlets, matched to a reviewed-by-you library of themes. A theme is active when two outlets report it.
2. Company filings: NSE order filings and press releases, read down to the PDF, linked to active themes.
3. Any sector: an order that is large against the company's own sales, whatever its industry, and clusters of orders in one industry.

The dark-horse lens looks inside an active theme's sub-sector for a company that is not one of its largest, is growing, earns a fair return, and has not already outrun the Nifty.

Version one reads no power-grid data, consensus estimates or earnings transcripts, and measures no press coverage. Text from feeds and filings is matched against patterns, never treated as instructions.

Rules enforced in code: no thesis rests on an unscored source; an edge a person has not reviewed caps confidence at Low; stale inputs cap confidence at Low; a Speculative chain is always Low and never ranks above evidence;
Medium and High wait for a person's review; every record needs an invalidator and a case for a smaller benefit; certainty language is refused. Confidence is how well supported the analysis is, not how likely a stock is to rise.
"""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import common

log = logging.getLogger("kite-desk.event-screener")
DIR = common.CACHE_DIR / "events"
LIBRARY_VERSION = "2026-11-06.2"
DISCLAIMER = ("Analytical signal only. Not a prediction or personal investment advice. It does not know your financial situation, goals or risk tolerance. "
              "Use beyond personal research needs review by a SEBI-registered research analyst or compliance function.")
CONFIDENCE_NOTE = "Confidence describes how well supported the analysis is, not how likely the stock is to rise."
LOOKBACK_DAYS = 14
NEWS_KEEP_DAYS = 30
MAX_PDFS = 80
PRICE_WINDOW_DAYS = 90
FRESH_DAYS = {"event": 30, "price": 4, "fundamentals": 130}
# Not yet calibrated on past events: the design asks for that before they are relied on, and the page says so.
THRESHOLDS = {"abnormal_large_pct": 15.0, "abnormal_very_large_pct": 25.0, "abnormal_small_pct": 5.0, "min_pe_points": 4, "order_surprise_share": 0.10,
              "theme_min_outlets": 2, "dark_max_rel_90d_pct": 15.0, "dark_min_roce": 10.0, "dark_min_growth_pct": 0.0, "dark_max_mcap_cr": 30000, "dark_per_node": 3, "order_suspect_share": 2.0, "dark_max_total": 60, "cluster_min_companies": 3}
BANNED = re.compile(r"\b(will benefit|guaranteed|sure to rise|must buy|certain to|cannot fail|risk[- ]free)\b", re.I)
ORDER_DESCS = ("Bagging/Receiving of orders/contracts", "Awarding of orders/contracts")
UPDATE_DESCS = ("Press Release", "General Updates", "Updates")
CLASS_RANK = {"Direct": 0, "Indirect": 1, "Speculative": 2}
CONF_RANK = {"High": 0, "Medium": 1, "Low": 2}
ECONOMIC = re.compile(r"shortage|\bban\b|sanction|factory|tariff|subsid|boom|disrupt|plant\b|supply|capacity|curbs?\b", re.I)

_POWER = r"heavy electrical|other electrical|electrical equipment"
_CABLES = r"cables?\b|transmission towers"
_EPC = r"civil construction|^construction$|^engineering$"
_IT = r"computers - software|software products|it enabled"
_METALS = r"^copper$|^aluminium$|^zinc$|diversified metals"
_SWITCH = r"transformer|switchgear|switchboard|busbar|\bT&D\b"
_GREEN = r"solar|renewable|\bwind\b|inverter|photovoltaic"
_RISE = r"(?:\W+(?:prices?|futures|rates?|benchmark))?\W+(?:jump|surg|spik|soar|rall|climb|rise)"
_FALL = r"(?:\W+(?:prices?|futures|rates?|benchmark))?\W+(?:slid|fall|slump|drop|tumbl|plung|sink|ease|hits? \S+ low)"
# Headlines that report a theme going the wrong way. They count against it and never make it live.
AGAINST = re.compile(r"slowdown|\bfears?\b|plunge|sell-?off|\brout\b|\btanks?\b|crash|slump|cancel|delay|revolt|scrap|bubble|panic|cut back|tumbl", re.I)
DEMAND_THEMES = {"ai_datacentre", "ai_software", "semiconductors", "clean_power", "ev", "nuclear", "rail", "infra", "space", "minerals", "grid_transmission"}


def _node(nid, name, industry, confidence="Medium", about=None, level=1):
    """about: where the industry label is too coarse, words the company's own profile must contain."""
    return {"id": nid, "level": level, "name": name, "industry": industry, "confidence": confidence, "about": about}


# Drafted by Claude from the design document and general industry knowledge, using the real industry labels of Screener and NSE. Every edge starts unreviewed and caps confidence at Low until a person approves it on the page.
# Edges are never invented at run time: an event matching nothing goes to the "no chain yet" list, and Claude's drafts (event_chains.py) join only when a person approves them.
BUILTIN_CHAINS = [
    {"id": "ai_datacentre", "name": "AI and data-centre build-out", "driver": r"data.?cent(?:re|er)s?|hyperscale|\bGPUs?\b|\bAI (?:infrastructure|capex|spending|boom|investment|chips?|slowdown|fears?|curbs?|selloff|sell-off|panic|bubble|rout)\b", "lead_months": "12 to 24",
     "counter": "Demand may be met by imports or a few large qualified suppliers, projects can wait on power and land, and AI spending itself may be cut if sentiment turns.",
     "nodes": [_node("dc_power", "Transformers, switchgear and power equipment", _POWER, about=_SWITCH), _node("dc_cables", "Power cables", _CABLES), _node("dc_backup", "Backup power and pumps", r"compressors, pumps", "Low"),
               _node("dc_fibre", "Fibre and network equipment", r"telecom - +equipment"), _node("dc_epc", "Construction and fit-out", _EPC, "Low", about=r"data cent|commercial building|industrial building")]},
    {"id": "ai_software", "name": "AI adoption in software and services", "driver": r"\bAI (?:adoption|agents?|models?|software|services|tools?)\b|generative AI|\bGenAI\b|artificial intelligence", "lead_months": "6 to 24",
     "counter": "AI may shrink billable work at Indian IT services as much as it adds demand, so the sign of this edge is itself uncertain.",
     "nodes": [_node("ai_it", "IT services and software", _IT, "Low"), _node("ai_data", "Data, analytics and platforms", r"healthcare research|exchange and data|business process", "Low")]},
    {"id": "semiconductors", "name": "Chips and electronics supply chain", "driver": r"semiconductor|chip(?:maker|s)?\b.*(?:plant|fab|shortage|curb|export|supply)|\bfab\b|electronics manufactur|\bPLI\b|\bTSMC\b|\bASML\b", "lead_months": "12 to 36",
     "counter": "Indian firms are mostly at assembly and materials stages, where margins are thin and qualification takes years.",
     "nodes": [_node("chip_ems", "Electronics manufacturing", r"consumer electronics|industrial products|other electrical", "Low", about=r"electronics manufactur|\bEMS\b|printed circuit|\bPCB|semiconductor"), _node("chip_gas", "Industrial gases", r"industrial gases", "Low"), _node("chip_chem", "Speciality chemicals", r"specialty chemicals", "Low", about=r"semiconductor|electronic")]},
    {"id": "defence", "name": "Defence spending and conflict", "driver": r"defen[cs]e (?:spending|budget|deal|procurement|export|order|contract)|\bmissiles?\b|air ?strikes?|\bNATO\b|rearm|fighter jets?|\bwarship|\bdrones?\b.*(?:attack|strike|military|army)", "lead_months": "12 to 36",
     "counter": "Procurement is slow and lumpy, order books can already be priced richly, and a ceasefire can reverse sentiment quickly.",
     "nodes": [_node("def_core", "Aerospace and defence makers", r"aerospace & defense"), _node("def_ship", "Warship and ship builders", r"ship building"), _node("def_expl", "Explosives", r"explosives", "Low"), _node("def_forge", "Castings and forgings", r"castings", "Low", about=r"defen[cs]e|aerospace")]},
    {"id": "oil_up", "name": "Oil price rising", "driver": r"\b(?:oil|crude|brent)\b" + _RISE + r"|strait of hormuz|\bOPEC\b.*\bcut", "lead_months": "1 to 12",
     "counter": "Government windfall taxes and subsidy sharing can take the upside from Indian producers, and spikes often fade.",
     "nodes": [_node("oil_ep", "Oil and gas producers", r"oil exploration"), _node("oil_store", "Oil storage and transport", r"oil storage"), _node("oil_coal", "Coal as a substitute", r"^coal$", "Low")]},
    {"id": "oil_down", "name": "Oil price falling: cost relief", "driver": r"\b(?:oil|crude|brent)\b" + _FALL, "lead_months": "1 to 6",
     "counter": "Cost relief is often passed on to customers, and a weak oil price can signal weak demand.",
     "nodes": [_node("ofall_paint", "Paints", r"^paints$"), _node("ofall_tyre", "Tyres", r"tyres"), _node("ofall_air", "Airlines", r"^airline$"), _node("ofall_omc", "Refiners and fuel retailers", r"refineries"),
               _node("ofall_carbon", "Carbon black", r"carbon black", "Low"), _node("ofall_lube", "Lubricants", r"lubricants", "Low")]},
    {"id": "shipping", "name": "Shipping routes and freight disruption", "driver": r"red sea|\bsuez\b|houthi|freight rates?|shipping (?:disruption|lane|route|crisis)|panama canal|container shortage", "lead_months": "1 to 12",
     "counter": "Freight spikes reverse when routes reopen, and exporters on the same routes are hurt.",
     "nodes": [_node("ship_own", "Ship owners", r"^shipping$"), _node("ship_port", "Ports", r"port & port"), _node("ship_log", "Logistics", r"logistics", "Low")]},
    {"id": "trade_deal", "name": "Trade deals and tariff relief", "driver": r"trade (?:deal|pact|agreement)|free trade|\bFTA\b|tariffs? (?:cut|relief|removed|lowered|reduc|exempt)", "lead_months": "6 to 24",
     "counter": "Deals can take years to apply, and rival exporters may gain the same access.",
     "nodes": [_node("td_text", "Textiles and garments", r"textile|garments"), _node("td_auto", "Auto components", r"auto components", "Low", about=r"export|overseas|global"), _node("td_shoes", "Footwear", r"footwear", "Low"), _node("td_food", "Food exports", r"other food|other agricultural", "Low", about=r"export|overseas")]},
    {"id": "china_shift", "name": "Supply moving away from China", "driver": r"china ?(?:\+|plus) ?(?:1|one)|export (?:curbs?|controls?|ban)|anti-?dumping|de-?risk\w* .*china|decoupl", "lead_months": "12 to 36",
     "counter": "Chinese producers can cut prices and undercut, and customer requalification is slow.",
     "nodes": [_node("cn_chem", "Speciality and commodity chemicals", r"specialty chemicals|commodity chemicals"), _node("cn_agro", "Agrochemicals", r"pesticides"), _node("cn_pharma", "Drug makers", r"pharmaceuticals", "Low", about=r"\bAPI\b|active pharma|intermediates"), _node("cn_ind", "Industrial products", r"industrial products|castings|abrasives", "Low", about=r"export")]},
    {"id": "minerals", "name": "Critical minerals and metal shortages", "driver": r"rare earths?|critical minerals?|lithium|cobalt|\b(?:copper|aluminium|aluminum|zinc)\b" + _RISE + r"|\b(?:copper|aluminium|aluminum) (?:shortage|deficit)", "lead_months": "3 to 24",
     "counter": "Metals are cyclical, and many users would see higher input costs, not gains.",
     "nodes": [_node("min_metal", "Copper, aluminium and zinc producers", _METALS), _node("min_ind", "Industrial minerals and traders", r"industrial minerals|trading - minerals", "Low")]},
    {"id": "clean_power", "name": "Solar, wind and storage", "driver": r"\bsolar\b|renewable|wind (?:power|farm|energy)|green hydrogen|battery storage|\bBESS\b", "lead_months": "6 to 24",
     "counter": "Module prices, auctions and policy change quickly, and a crowded field squeezes returns.",
     "nodes": [_node("cl_gen", "Power generators", r"power generation", about=_GREEN), _node("cl_eq", "Solar and wind equipment", _POWER, about=_GREEN), _node("cl_cables", "Cables", _CABLES, "Low")]},
    {"id": "nuclear", "name": "Nuclear power revival", "driver": r"nuclear (?:power|energy|reactor|plant|liability)|small modular reactor", "lead_months": "24 to 60",
     "counter": "Nuclear timelines run for many years and rules decide who may take part.",
     "nodes": [_node("nu_eq", "Heavy equipment", r"heavy electrical", "Low", about=r"nuclear"), _node("nu_ind", "Forgings and industrial products", r"castings|industrial products", "Low", about=r"nuclear"), _node("nu_gen", "Power generators", r"integrated power|power generation", "Low", about=r"nuclear")]},
    {"id": "grid_transmission", "name": "Transmission and grid build-out", "driver": r"\btransmission (?:line|project|network|tender)|sub-?stations?|\bHVDC\b|power grid|green energy corridor|grid (?:upgrade|expansion|investment|capacity)", "lead_months": "6 to 24",
     "counter": "Tender awards can slip, competition can compress margins, and input costs can absorb the benefit.",
     "nodes": [_node("grid_power", "Transformers and switchgear", _POWER, about=_SWITCH), _node("grid_cables", "Conductors, cables and towers", _CABLES), _node("grid_epc", "Grid contractors", _EPC + r"|power - transmission", about=r"transmission|sub-?station|power grid|\bT&D\b")]},
    {"id": "ev", "name": "Electric vehicles and batteries", "driver": r"electric vehicles?|\bEVs\b|\bEV (?:sales|makers?|batter\w+|charging|market|policy)|battery (?:plant|maker|gigafactory|cell)|gigafactory", "lead_months": "12 to 36",
     "counter": "EV demand has swung with subsidies, and legacy parts can lose as new ones gain.",
     "nodes": [_node("ev_parts", "Auto components", r"auto components", "Low", about=r"\bEVs?\b|electric vehicle|electrification|batter"), _node("ev_2w", "Two and three wheelers", r"2/3 wheelers", "Low", about=r"\bEVs?\b|electric"), _node("ev_graphite", "Graphite and electrodes", r"electrodes", "Low", about=r"graphite")]},
    {"id": "rail", "name": "Railways and metro spending", "driver": r"\brailways?\b|vande bharat|metro (?:rail|project|line)|bullet train|\bkavach\b", "lead_months": "6 to 24",
     "counter": "Budget allocation is not the same as spending, and tenders are price-competitive.",
     "nodes": [_node("rail_wag", "Wagon makers", r"railway wagons"), _node("rail_epc", "Rail contractors", _EPC, "Low", about=r"railway|\brail\b|metro"), _node("rail_elec", "Electrical systems", r"other electrical|heavy electrical", "Low", about=r"railway|traction|locomotive")]},
    {"id": "pharma_us", "name": "US pharma policy and drug supply", "driver": r"biosecure|pharma(?:ceutical)? tariffs?|\bCDMO\b|generic drugs?|\bGLP-1\b|weight.loss drug|drug shortage", "lead_months": "6 to 36",
     "counter": "US pricing and tariff policy can turn against Indian exporters, and FDA findings are a constant risk.",
     "nodes": [_node("ph_make", "Drug makers", r"pharmaceuticals"), _node("ph_bio", "Biotech and research services", r"biotechnology|healthcare research", "Low")]},
    {"id": "outbreak", "name": "Disease outbreak", "driver": r"\boutbreak\b|\bebola\b|pandemic|epidemic|\bbird flu\b", "lead_months": "1 to 6",
     "counter": "Most outbreaks stay local, and any demand spike usually fades fast.",
     "nodes": [_node("out_ph", "Drug makers", r"pharmaceuticals", "Low", about=r"vaccine|anti-?viral|anti-?infective"), _node("out_med", "Medical equipment and diagnostics", r"medical equipment|healthcare service", "Low")]},
    {"id": "gold", "name": "Gold rush and safe-haven demand", "driver": r"\bgold\b" + _RISE + r"|\bgold(?: prices?)? hits? |safe.haven", "lead_months": "1 to 12",
     "counter": "High gold prices can reduce jewellery volumes even as inventories gain in value.",
     "nodes": [_node("gold_jew", "Jewellers", r"gems, jewellery")]},
    {"id": "monsoon", "name": "Monsoon and farm economy", "driver": r"monsoon|el ni[nñ]o|la ni[nñ]a|\bdrought\b|\bkharif\b|\brabi\b|fertili[sz]er (?:price|shortage|subsid|suppl)", "lead_months": "3 to 12",
     "counter": "Rain arrives unevenly, and government price and subsidy decisions can decide the outcome.",
     "nodes": [_node("agri_fert", "Fertiliser makers", r"fertilizers"), _node("agri_pest", "Crop protection", r"pesticides"), _node("agri_trac", "Tractors", r"tractors", "Low"), _node("agri_sugar", "Sugar", r"^sugar$", "Low")]},
    {"id": "space", "name": "Satellites and space", "driver": r"\bsatellite (?:launch|network|internet|broadband|service|constellation)|\bISRO\b|space (?:mission|launch|policy|startup)|\bstarlink\b", "lead_months": "12 to 36",
     "counter": "Space revenue is a small part of most listed suppliers, so the effect may be mainly narrative.",
     "nodes": [_node("sp_aero", "Aerospace suppliers", r"aerospace & defense", "Low", about=r"\bspace\b|satellite|\bISRO\b"), _node("sp_tel", "Telecom equipment", r"telecom - +equipment", "Low", about=r"satellite|\bspace\b")]},
    {"id": "cyber", "name": "Cyber attacks and security spending", "driver": r"cyber.?attack|ransomware|data breach|cyber ?security", "lead_months": "3 to 18",
     "counter": "Most security spending goes to global vendors, not Indian listed companies.",
     "nodes": [_node("cy_soft", "Software products", r"software products|computers - software", "Low", about=r"cyber|security")]},
    {"id": "infra", "name": "Infrastructure spending push", "driver": r"union budget|national highways?|infrastructure (?:spending|push|plan|investment)|government capex|\bNHAI\b", "lead_months": "6 to 24",
     "counter": "Execution often lags announcements, and contractors carry working-capital risk.",
     "nodes": [_node("inf_epc", "Road and civil contractors", _EPC), _node("inf_cem", "Cement", r"cement"), _node("inf_steel", "Steel products", r"iron & steel products", "Low"), _node("inf_veh", "Construction vehicles", r"construction vehicles", "Low")]},
    {"id": "rates", "name": "Interest-rate cuts", "driver": r"(?:repo|interest) rates? cut|\b(?:RBI|Fed|Federal Reserve)\b.*\bcuts?\b|rate cuts?", "lead_months": "3 to 12",
     "counter": "Lenders can see margins narrow, and cuts are often expected well ahead of time.",
     "nodes": [_node("rt_house", "Housing finance", r"housing finance", "Low"), _node("rt_real", "Real estate", r"residential, commercial", "Low")]},
]
CHAINS = list(BUILTIN_CHAINS)
ORDER_CHAIN = {"id": "order_surprise", "name": "Large order relative to company size", "driver": "", "lead_months": "3 to 18",
               "counter": "Execution can stretch over years, the order may carry thin margins, and a single order may not repeat.", "nodes": []}
SOURCE_SCORE = {"nse_filing": 3, "news": 2, "screener": 2, "industry_tag": 2, "library": 1}


def _read(name: str, default):
    try:
        return json.loads((DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / (name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(DIR / name)


def refresh_chains() -> None:
    """The built-in themes plus the drafts a person has approved."""
    CHAINS[:] = BUILTIN_CHAINS + [r for r in _read("chains.json", []) if r.get("status") == "approved"]


def parse_day(text: str | None) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


# ---------------- scoring (section 1.2) ----------------
_VALUE = re.compile(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\b|lakhs?|lacs?|million|mn\b|billion|bn\b)", re.I)
_ORDER_WORDS = re.compile(r"order|contract|\bwork|award|\bLoA\b|letter of|project|tender", re.I)
_NOT_ORDER = re.compile(r"turnover|net ?worth|paid.up|share capital|revenue|market cap|penalt|order ?book|order intake|year.to.date|\bYTD\b|till date|so far|cumulative|total orders|backlog|shall mean|classif|\babove (?:rs|inr)|between (?:rs|inr)|in the range", re.I)


def _crore(num: str, unit: str) -> float | None:
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    u = unit.lower()
    return v if u.startswith("cr") else v / 100 if u.startswith(("lakh", "lac")) else v / 10 if u.startswith(("million", "mn")) else v * 100


def value_crore(text: str) -> float | None:
    """The largest rupee value named in a text, in crore. None when none is stated."""
    found = [v for v in (_crore(*m) for m in _VALUE.findall(text or "")) if v is not None]
    return max(found) if found else None


def order_value(text: str) -> float | None:
    """The first rupee value beside order wording and away from turnover, capital or order-book wording, so a company's size or running total is not read as this order."""
    text = re.sub(r"\s+", " ", text or "")                  # a PDF breaks lines mid-phrase
    for m in _VALUE.finditer(text):
        around = text[max(0, m.start() - 120):m.end() + 60]
        clause = re.split(r"(?<!Rs)\.\s", text[max(0, m.start() - 120):m.start()])[-1] + text[m.start():m.end() + 25].split(". ")[0]   # the sentence holding this value
        if _ORDER_WORDS.search(around) and not _NOT_ORDER.search(clause):
            v = _crore(m.group(1), m.group(2))
            if v is not None:
                return v
    return None


def magnitude(value: float | None, sales: float | None) -> int:
    """0 when the size is unknown. Against trailing sales where known, otherwise in absolute terms."""
    if value is None:
        return 0
    if sales and sales > 0:
        share = value / sales
        return 3 if share >= 0.10 else 2 if share >= 0.03 else 1
    return 3 if value >= 10000 else 2 if value >= 1000 else 1


def score_signal(source: str, value: float | None, sales: float | None, nodes: int, seen: bool) -> dict:
    s = {"primary": SOURCE_SCORE.get(source, 0), "magnitude": magnitude(value, sales), "breadth": min(3, nodes), "novelty": 0 if seen else 3}
    s["total"] = sum(s.values())
    return s


# ---------------- world events ----------------
def nodes_for(industry: str) -> list[tuple[dict, dict]]:
    """Library nodes whose industry pattern matches this company's. Only the library's edges count."""
    return [(ch, n) for ch in CHAINS for n in ch["nodes"] if industry and re.search(n["industry"], industry, re.I)]


def drivers_in(text: str) -> list[dict]:
    return [ch for ch in CHAINS if re.search(ch["driver"], text or "", re.I)]


def merge_news(stored: list[dict], fresh: list[dict], today: date) -> list[dict]:
    """A rolling store, because each feed shows only the last day or two. Keeps the first date a link was seen."""
    by_link = {n["link"]: n for n in stored}
    for n in fresh:
        if n.get("link") and n["link"] not in by_link and str(n["link"]).startswith(("http://", "https://")):
            by_link[n["link"]] = {**n, "date": (parse_day(n.get("date")) or today).isoformat(), "seen_at": datetime.now(common.IST).isoformat(timespec="seconds")}
    cutoff = (today - timedelta(days=NEWS_KEEP_DAYS)).isoformat()
    return sorted((n for n in by_link.values() if n["date"] >= cutoff), key=lambda n: (n["date"], n.get("seen_at") or ""), reverse=True)


def active_themes(news: list[dict], today: date) -> list[dict]:
    """Themes reported by at least two named outlets in the lookback window. One outlet is not enough to act on."""
    cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    hits, against = defaultdict(list), defaultdict(list)
    for n in news:
        if n["date"] >= cutoff:
            for ch in drivers_in(n["title"]):                       # the title only: feed summaries gave too many false matches
                (against if (ch["id"] in DEMAND_THEMES or ch.get("demand")) and AGAINST.search(n["title"]) else hits)[ch["id"]].append(n)
    out = []
    for ch in CHAINS:
        items = hits.get(ch["id"], [])
        outlets = sorted({n["source"] for n in items})
        if len(outlets) >= THRESHOLDS["theme_min_outlets"]:
            opp = against.get(ch["id"], [])
            out.append({"id": ch["id"], "name": ch["name"], "count": len(items), "outlets": outlets, "first": min(n["date"] for n in items), "latest": max(n["date"] for n in items), "headlines": items[:6], "nodes": len(ch["nodes"]),
                        "against": opp[:4], "against_count": len(opp), "contested": len(opp) >= len(items)})
    return sorted(out, key=lambda t: (-len(t["outlets"]), -t["count"]))


def unmapped(news: list[dict], today: date, limit: int = 30) -> list[dict]:
    """Economic-looking headlines that no theme covers: the queue for a person to decide whether a new chain is warranted."""
    cutoff = (today - timedelta(days=7)).isoformat()
    return [n for n in news if n["date"] >= cutoff and ECONOMIC.search(n["title"]) and not drivers_in(n["title"])][:limit]


def order_filings(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("desc") in ORDER_DESCS and r.get("symbol")]


def update_filings(rows: list[dict]) -> list[dict]:
    """Press releases and updates whose summary names a driver: capex and project announcements."""
    return [r for r in rows if r.get("desc") in UPDATE_DESCS and r.get("symbol") and drivers_in(r.get("attchmntText") or "")]


def pdf_text(url: str, seq: str, get, cached) -> str:
    """The attachment's text, kept on disk. Empty for a scanned PDF or when no reader is installed: the record then says it was not read."""
    git_copy = Path(r"C:\Program Files\Git\mingw64\bin\pdftotext.exe")
    exe = shutil.which("pdftotext") or (str(git_copy) if git_copy.exists() else None)
    if not exe or not url or not url.lower().endswith(".pdf"):
        return ""

    def fetch():
        r = get(url)
        r.raise_for_status()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            p.write_bytes(r.content)
            out = subprocess.run([exe, "-l", "6", str(p), "-"], capture_output=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.decode("utf-8", "replace")[:12000]
    return (cached(f"events_pdf_{seq}", 90 * 86400, fetch) or {}).get("data") or ""


# ---------------- is it already priced in? (section 3) ----------------
def abnormal_return(closes: list[list], bench: list[list], event: date) -> dict | None:
    """Stock against benchmark from the last close before the event to the latest close. closes: [[date, close], ...] oldest first."""
    def since(rows):
        before = [r for r in rows if str(r[0])[:10] < event.isoformat()]
        return (rows[-1][1] / before[-1][1] - 1) * 100 if before and rows else None
    a, b = since(closes), since(bench)
    return None if a is None or b is None else {"stock_pct": round(a, 2), "benchmark_pct": round(b, 2), "abnormal_pct": round(a - b, 2), "as_of": str(closes[-1][0])[:10]}


_MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}


def pe_history(quarters: dict, closes: list[list]) -> list[float]:
    """Trailing P/E at each quarter end, from reported EPS and the close on or before that date. Loss-making points are left out."""
    periods, eps = (quarters or {}).get("periods") or [], ((quarters or {}).get("rows") or {}).get("EPS in Rs") or []
    out = []
    for i in range(3, min(len(periods), len(eps))):
        window = eps[i - 3:i + 1]
        if any(x is None for x in window) or sum(window) <= 0:
            continue
        m = re.match(r"([A-Z][a-z]{2}) (\d{4})", periods[i])
        if not m:
            continue
        end = date(int(m.group(2)), _MONTHS[m.group(1)], 28).isoformat()
        px = [c[1] for c in closes if str(c[0])[:7] <= end[:7]]
        if px:
            out.append(round(px[-1] / sum(window), 2))
    return out


def priced_in(abn: dict | None, pe_now: float | None, pes: list[float], order_booked: bool) -> dict:
    """A state and its basis. Missing inputs give Unclear, which is not ranked."""
    missing = []
    if abn is None:
        missing.append("price response")
    if pe_now is None or pe_now <= 0:
        missing.append("current P/E")
    if len(pes) < THRESHOLDS["min_pe_points"]:
        missing.append("P/E history")
    basis = {"abnormal": abn, "pe_now": pe_now, "pe_median": round(statistics.median(pes), 1) if pes else None, "pe_high": max(pes) if pes else None, "pe_points": len(pes), "coverage": "not measured in this version",
             "thresholds": "not yet calibrated on past events"}
    if missing:
        return {"state": "Unclear", "basis": basis, "missing": missing}
    x, med, high = abn["abnormal_pct"], basis["pe_median"], basis["pe_high"]
    if x >= THRESHOLDS["abnormal_very_large_pct"] or (x >= THRESHOLDS["abnormal_large_pct"] and pe_now >= high):
        state = "Largely priced"
    elif x < THRESHOLDS["abnormal_small_pct"] and pe_now <= med and not order_booked:
        state = "Early"
    else:
        state = "Partially priced"
    return {"state": state, "basis": basis, "missing": []}


# ---------------- validating the chain and setting confidence ----------------
def staleness(inputs: dict[str, str | None], today: date) -> dict:
    """{name: {as_of, days, stale}}. A missing date counts as stale."""
    out = {}
    for name, when in inputs.items():
        d = parse_day(when)
        days = (today - d).days if d else None
        out[name] = {"as_of": when, "days": days, "stale": days is None or days > FRESH_DAYS.get(name, 30)}
    return out


def confidence(cls: str, links: list[dict], value: float | None, stale: dict, edge_reviewed: bool, counter_flags: list[str]) -> tuple[str, list[str]]:
    """Section 4's rules. The reasons always say what capped it."""
    reasons = []
    verified = all(x["score"] >= 2 for x in links)
    reasons.append("Every link has a source scoring 2 or higher." if verified else "At least one link is inferred, not sourced.")
    reasons.append(f"Exposure: a stated value of about ₹{value:,.0f} crore." if value else "Exposure is not quantified: no stated value could be read.")
    late = [k for k, v in stale.items() if v["stale"]]
    if late:
        reasons.append("Stale or missing: " + ", ".join(late) + ".")
    if counter_flags:
        reasons.append("Unresolved: " + "; ".join(counter_flags) + ".")
    if not edge_reviewed:
        reasons.append("The demand-chain edge has not been reviewed by a person.")
    if cls == "Speculative":
        reasons.append("A link in the chain has no evidence, so this is Speculative.")
    if cls == "Speculative" or not verified or late or not edge_reviewed or (counter_flags and not value):
        return "Low", reasons
    if cls == "Direct" and value and not counter_flags:
        return "High", reasons
    return "Medium", reasons


def rank_key(t: dict) -> tuple:
    return (t["priced_in"]["state"] == "Unclear", CLASS_RANK[t["classification"]], CONF_RANK[t["confidence"]], -t["signal_score"]["total"], t["company"]["symbol"])


def _own_words(t: dict) -> str:
    """The record's text without quoted headlines and filings, which may use any wording: only our own language is held to the rule."""
    own = {k: v for k, v in t.items() if k not in ("event", "causal_chain", "dark_horse", "facts")}     # facts holds the company's own profile text
    own["links"] = [x for x in t.get("causal_chain", []) if not x.get("quoted")]
    return json.dumps(own, ensure_ascii=False)


def check_record(t: dict) -> None:
    """A record that breaks a safeguard is refused, whatever produced it."""
    for field in ("company", "classification", "event", "causal_chain", "exposure", "priced_in", "horizon", "risks", "invalidators", "confidence", "confidence_reasons", "staleness", "disclaimer"):
        if not t.get(field):
            raise ValueError(f"record is missing {field}")
    if not any("smaller" in r.lower() for r in t["risks"]):
        raise ValueError("record has no case where the benefit is smaller than expected")
    if t["classification"] == "Speculative" and t["confidence"] != "Low":
        raise ValueError("a speculative chain cannot be above Low")
    if t["event"]["score"] <= 0:
        raise ValueError("no thesis may rest on an unscored source")
    if any(k in t for k in ("target_price", "buy", "sell", "quantity", "position_size", "entry", "exit")):
        raise ValueError("records carry no trading instruction")
    hit = BANNED.search(_own_words(t))
    if hit:
        raise ValueError(f"certainty language: {hit.group(0)}")


def build_thesis(f: dict, ch: dict, node: dict, cls: str, industry: str, text_read: bool, value: float | None, fund: dict | None, closes: list[list], bench: list[list], reviewed: set, today: date, score: dict, flags: list[str], trigger: dict | None = None) -> dict:
    """One section 4 record. f is the company's filing, or a stand-in with no desc when only its industry links it to the theme."""
    sym, ev_day = f["symbol"], parse_day(f.get("an_dt")) or today
    fund, filed = fund or {}, bool(f.get("desc"))
    name = f.get("sm_name") or sym
    surprise = ch["id"] == ORDER_CHAIN["id"]
    edge = f"{ch['id']}/{node['id']}"
    edge_ok = surprise or edge in reviewed
    if surprise:
        links = [{"link": f"The company's own filing states the order ({f.get('desc')})", "source": f.get("attchmntFile") or "NSE filing", "score": SOURCE_SCORE["nse_filing"], "tag": "High"},
                 {"link": "The order is large against the last four quarters' sales", "source": "Screener.in results", "score": SOURCE_SCORE["screener"], "tag": "High"}]
    else:
        links = [{"link": f"{ch['name']} to {node['name']}", "source": f"demand-chain library {LIBRARY_VERSION}, {'reviewed' if edge_ok else 'not yet reviewed'}", "score": 2 if edge_ok else 1, "tag": node["confidence"]},
                 {"link": f"{node['name']} to {name}", "source": f"industry classification: {industry}", "score": SOURCE_SCORE["industry_tag"], "tag": "Medium"}]
        if trigger:
            links.insert(0, {"link": f"Reported event: {trigger['title']}", "source": trigger["link"], "score": SOURCE_SCORE["news"], "tag": "Medium", "quoted": True})
        if cls == "Direct":
            links.insert(-1, {"link": f"The company's own filing names this demand ({f.get('desc')})", "source": f.get("attchmntFile") or "NSE filing", "score": SOURCE_SCORE["nse_filing"], "tag": "High"})
        else:
            links.insert(-1, {"link": "The company's order is not stated to be for this demand" if filed else "The company has not stated any exposure to this theme", "source": "inferred", "score": 0, "tag": "Low"})
    window = f"since {'the filing on' if filed else 'the event on'} {ev_day.isoformat()}"
    from_day = ev_day
    if not filed:
        from_day, window = today - timedelta(days=PRICE_WINDOW_DAYS), f"over the last {PRICE_WINDOW_DAYS} days, because a theme usually starts before today's headline"
    abn = abnormal_return(closes, bench, from_day) if closes and bench else None
    pes = pe_history(fund.get("quarters"), closes) if closes else []
    moves = price_moves(closes, bench, today)
    pi = priced_in(abn, fund.get("pe"), pes, order_booked=filed and f.get("desc") in ORDER_DESCS)
    pi["basis"]["window"] = window
    stale = staleness({"event": trigger["date"] if trigger and not filed else f.get("an_dt"), "price": abn["as_of"] if abn else None, "fundamentals": fund.get("fetched_at")}, today)
    conf, reasons = confidence(cls, links, value, stale, edge_ok, flags)
    tid = f"{sym}-{f.get('seq_id') or 'theme'}-{ch['id']}"
    if not filed:
        exposure = "No company filing links it to this theme: only its industry does, so its exposure is unverified."
    elif value:
        exposure = f"An order of about ₹{value:,.0f} crore was read from the filing by pattern matching. Check it against the document; a value spread over several years overstates the share of sales."
    elif text_read:
        exposure = "The attachment was read, and no rupee order value could be parsed from it."
    else:
        exposure = "The attachment could not be read, so the order's size and client are unverified."
    description = (trigger["title"] if trigger and not filed else (f.get("attchmntText") or f.get("desc") or ""))[:400]
    return {"id": tid, "company": {"symbol": sym, "name": name, "in_nifty500": bool(f.get("_in500")), "industry": industry}, "classification": cls, "sub_sector": node["name"], "chain": ch["id"], "theme": ch["name"], "edge": edge,
            "event": {"description": description, "date": (parse_day(trigger["date"]) if trigger and not filed else ev_day).isoformat(), "source": (trigger["link"] if trigger and not filed else f.get("attchmntFile")), "publisher": (trigger or {}).get("source") if not filed else "NSE filing", "score": score["primary"], "filing": f.get("desc")},
            "signal_score": score, "causal_chain": links,
            "exposure": {"order_value_crore": value, "attachment_read": text_read, "text": exposure, "capacity": "not assessed in this version", "pricing_position": "not assessed in this version", "pe": fund.get("pe"), "roce": fund.get("roce")},
            "priced_in": pi, "horizon": f"{ch['lead_months']} months, from typical build and order lead times; an assumption, not from this source",
            "risks": [ch["counter"], "The benefit may be smaller than expected if this is a small share of revenue or carries thin margins."] + ([] if f.get("_in500") else ["Outside the Nifty 500: thinner liquidity, weaker disclosure and greater governance risk."]),
            "invalidators": ["No related orders or management comment appear within two quarters.", "Margins fall in the next results despite the demand.", "The event reverses, or a related tender or project is delayed or cancelled."],
            "confidence": conf, "confidence_reasons": reasons, "confidence_note": CONFIDENCE_NOTE, "staleness": stale, "disclaimer": DISCLAIMER, "built": today.isoformat(),
            "review": "needed" if conf != "Low" else "not required", "moves": moves, "room": valuation_room(moves["last"] if moves else None, fund.get("pe"), pes), "theme_plain": THEME_PLAIN.get(ch["id"]) or ch.get("why") or "",
            "facts": {"mcap_cr": fund.get("mcap_cr"), "sales_growth_pct": sales_growth(fund), "profit_growth_pct": profit_growth(fund), "roce": fund.get("roce"), "about": (fund.get("about") or "")[:400]}}


# ---------------- the dark-horse lens ----------------
def sales_of(fund: dict) -> list:
    rows = ((fund.get("quarters") or {}).get("rows") or {})
    return rows.get("Sales") or rows.get("Revenue") or []


def ttm_sales(fund: dict) -> float | None:
    sales = sales_of(fund)
    return sum(x for x in sales[-4:] if x) if len(sales) >= 4 else None


def sales_growth(fund: dict) -> float | None:
    """Last four quarters' sales against the four before them, in percent."""
    sales = sales_of(fund)
    if len(sales) < 8 or any(x is None for x in sales[-8:]) or sum(sales[-8:-4]) <= 0:
        return None
    return round((sum(sales[-4:]) / sum(sales[-8:-4]) - 1) * 100, 1)


def fits(node: dict, fund: dict) -> bool:
    """Its industry matches and, where the label is too coarse, its own profile mentions the activity."""
    if not re.search(node["industry"], fund.get("industry") or "", re.I):
        return False
    return not node.get("about") or bool(re.search(node["about"], fund.get("about") or "", re.I))


def profit_growth(fund: dict) -> float | None:
    """Last four quarters' net profit against the four before them, when both are profits."""
    rows = ((fund.get("quarters") or {}).get("rows") or {}).get("Net Profit") or []
    if len(rows) < 8 or any(x is None for x in rows[-8:]) or sum(rows[-8:-4]) <= 0 or sum(rows[-4:]) <= 0:
        return None
    return round((sum(rows[-4:]) / sum(rows[-8:-4]) - 1) * 100, 1)


def peers_in(node: dict, funds: dict[str, dict]) -> list[tuple[str, dict]]:
    """Companies in this sub-sector, largest first."""
    out = [(s, f) for s, f in funds.items() if f.get("mcap_cr") and fits(node, f)]
    return sorted(out, key=lambda x: -x[1]["mcap_cr"])


def leader_count(n: int) -> int:
    """The largest names in a sub-sector, which the market already follows: two, or a quarter of a large group."""
    return max(2, math.ceil(n / 4))


def dark_note(sym: str, fund: dict, node: dict, funds: dict[str, dict]) -> dict | None:
    """Why this company is a less obvious name in its sub-sector, or None when it is one of the leaders."""
    if (fund.get("mcap_cr") or 0) > THRESHOLDS["dark_max_mcap_cr"]:
        return None
    peers = peers_in(node, funds)
    if sym not in {s for s, _ in peers}:
        return {"reason": "Outside the Nifty 500, so it is less followed", "rank": None, "peers": len(peers), "mcap_cr": fund.get("mcap_cr"), "growth_pct": sales_growth(fund)}
    rank = [s for s, _ in peers].index(sym) + 1
    if len(peers) < 3 or rank <= leader_count(len(peers)):
        return None
    return {"reason": f"Number {rank} of {len(peers)} by market value in {node['name']}", "rank": rank, "peers": len(peers), "group": node["name"], "mcap_cr": fund.get("mcap_cr"), "growth_pct": sales_growth(fund)}


def dark_horses(themes: list[dict], funds: dict[str, dict], closes, bench: list[list], today: date) -> tuple[list[dict], dict]:
    """For each active theme, the less obvious companies of each sub-sector that pass the mechanical screen. Returns candidates and how many each test removed."""
    out, dropped = [], {"leaders": 0, "too_large": 0, "weak_or_no_growth": 0, "low_return": 0, "already_moved": 0, "no_prices": 0}
    since = today - timedelta(days=PRICE_WINDOW_DAYS)
    by_id = {c["id"]: c for c in CHAINS}
    for theme in themes:
        ch = by_id[theme["id"]]
        for node in ch["nodes"]:
            peers = peers_in(node, funds)
            if len(peers) < 3:
                continue
            leaders = leader_count(len(peers))
            dropped["leaders"] += leaders
            found = []
            for rank, (sym, fund) in enumerate(peers[leaders:], leaders + 1):
                if fund["mcap_cr"] > THRESHOLDS["dark_max_mcap_cr"]:
                    dropped["too_large"] += 1
                    continue
                growth = sales_growth(fund)
                if growth is None or growth <= THRESHOLDS["dark_min_growth_pct"]:
                    dropped["weak_or_no_growth"] += 1
                    continue
                if (fund.get("roce") or 0) < THRESHOLDS["dark_min_roce"]:
                    dropped["low_return"] += 1
                    continue
                series = closes(sym)
                rel = abnormal_return(series, bench, since) if series and bench else None
                if rel is None:
                    dropped["no_prices"] += 1
                    continue
                if rel["abnormal_pct"] > THRESHOLDS["dark_max_rel_90d_pct"]:
                    dropped["already_moved"] += 1
                    continue
                found.append({"symbol": sym, "fund": fund, "closes": series, "chain": ch, "node": node, "theme": theme, "rank": rank, "peers": len(peers), "growth": growth, "rel": rel})
            out += sorted(found, key=lambda c: (c["rel"]["abnormal_pct"] - c["growth"]))[:THRESHOLDS["dark_per_node"]]
    return sorted(out, key=lambda c: c["rel"]["abnormal_pct"] - c["growth"])[:THRESHOLDS["dark_max_total"]], dropped


# ---------------- in plain words ----------------
THEME_PLAIN = {
    "ai_datacentre": "AI needs huge data centres. They use large amounts of electricity, so someone has to supply transformers, cables, backup power and buildings.",
    "ai_software": "As companies adopt AI, they may pay software and data firms to set it up. It can also shrink the work these firms bill for, so it could go either way.",
    "semiconductors": "As countries try to make more chips and electronics at home, local makers and their material suppliers could get more work.",
    "defence": "When conflict rises, governments tend to buy more weapons and equipment, and that money reaches defence makers and their suppliers.",
    "oil_up": "When oil gets dearer, companies that produce oil and gas earn more on each barrel.",
    "oil_down": "Many companies use oil as a raw material or fuel. When it gets cheaper, their costs can fall and their profit can rise if they keep their selling prices.",
    "shipping": "When sea routes are disrupted, ships take longer journeys and freight rates rise, which helps ship owners and can shift business between ports.",
    "trade_deal": "A trade deal or lower tariff makes Indian goods cheaper abroad, so exporters could sell more.",
    "china_shift": "Global buyers want suppliers other than China. Indian makers of the same products could win some of those orders.",
    "minerals": "Governments and factories are chasing scarce metals. Companies that produce or recycle them could sell more, or at better prices.",
    "clean_power": "More solar, wind and battery projects need equipment, cables and power producers.",
    "nuclear": "If nuclear power restarts in a big way, makers of heavy equipment and special parts could get orders, though this takes many years.",
    "grid_transmission": "More power has to travel longer distances, so new lines and substations are needed. That means work for tower, cable and transformer companies.",
    "ev": "Electric vehicles need different parts. Suppliers who make them could gain, while those making engine parts could lose.",
    "rail": "Government spending on trains and metros turns into orders for wagon makers, contractors and signalling suppliers.",
    "pharma_us": "Changes in US drug rules and supply can shift orders towards, or away from, Indian drug makers.",
    "outbreak": "A disease outbreak can briefly raise demand for certain medicines, tests and equipment.",
    "gold": "When gold prices rise, the gold a jeweller holds is worth more, though high prices can also put buyers off.",
    "monsoon": "Rain decides farm income. A good season can mean more spending on fertiliser, crop protection and tractors.",
    "space": "More satellites and launches mean work for specialist suppliers, though it is usually a small part of their business.",
    "cyber": "After big cyber attacks, companies tend to spend more on security software.",
    "infra": "Government spending on roads and buildings turns into work for contractors and demand for cement and steel.",
    "rates": "Lower interest rates make home loans cheaper, which can help housing lenders and property developers.",
    "order_surprise": "A company won an order that is large compared with what it sells in a year, which could lift its revenue noticeably if it is delivered profitably.",
}
EVIDENCE_PLAIN = {
    "Direct": "Stronger evidence: this comes from the company's own stock exchange filing.",
    "Indirect": "Medium evidence: the company won an order and the theme is in the news, but the filing does not say the two are connected.",
    "Speculative": "Weakest evidence: only its industry links it to this theme. The company has not said so itself, so treat this as a lead to check.",
}


def price_moves(closes: list[list], bench: list[list], today: date) -> dict | None:
    """How far the stock has moved over several periods, against Nifty, and where it sits in its one-year range. Facts only."""
    if not closes:
        return None
    last = closes[-1][1]
    periods = []
    for label, days in (("1 month", 30), ("3 months", 90), ("6 months", 180), ("1 year", 365)):
        r = abnormal_return(closes, bench, today - timedelta(days=days)) if bench else None
        if r:
            periods.append({"label": label, **{k: r[k] for k in ("stock_pct", "benchmark_pct", "abnormal_pct")}})
    year = [c[1] for c in closes if str(c[0])[:10] >= (today - timedelta(days=365)).isoformat()] or [last]
    high, low = max(year), min(year)
    return {"last": last, "as_of": str(closes[-1][0])[:10], "periods": periods, "high_1y": high, "low_1y": low, "from_high_pct": round((last / high - 1) * 100, 1), "from_low_pct": round((last / low - 1) * 100, 1)}


def valuation_room(last: float | None, pe_now: float | None, pes: list[float]) -> dict | None:
    """What the price would be at the stock's own past P/E levels, with profit unchanged. Arithmetic in both directions, not a forecast. None when the history is too short or too uneven to mean anything."""
    if not last or not pe_now or pe_now <= 0 or len(pes) < THRESHOLDS["min_pe_points"]:
        return None
    levels = {"usual": statistics.median(pes), "highest": max(pes), "lowest": min(pes)}
    if any(not 0.25 <= v / pe_now <= 4 for v in levels.values()):
        return None
    return {"pe_now": pe_now, "points": len(pes), **{k: {"pe": round(v, 1), "price": round(last * v / pe_now, 1), "pct": round((v / pe_now - 1) * 100, 1)} for k, v in levels.items()}}


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def _ahead(x: float) -> str:
    return f"{abs(x):.1f}% ahead of Nifty" if x >= 0 else f"{abs(x):.1f}% behind Nifty"


def explain(t: dict) -> dict:
    """The record in everyday language. Built only from the record's own numbers, and worded as possibilities."""
    fx, co, dh = t["facts"], t["company"], t.get("dark_horse") or {}
    surprise = t["chain"] == ORDER_CHAIN["id"]
    why = []
    if t["exposure"].get("order_value_crore") and co.get("order_share_of_sales_pct"):
        why.append(f"It reported an order of about ₹{t['exposure']['order_value_crore']:,.0f} crore, roughly {co['order_share_of_sales_pct']:.0f}% of what it sells in a year. A big order for a company this size.")
    elif t["exposure"].get("order_value_crore"):
        why.append(f"It reported an order of about ₹{t['exposure']['order_value_crore']:,.0f} crore.")
    if dh.get("rank") and dh.get("peers"):
        why.append(f"It is not one of the big names: number {dh['rank']} of {dh['peers']} by company value in {dh.get('group') or t['sub_sector']}. Big names get noticed first, while smaller ones can be noticed later. Smaller companies also tend to be riskier.")
    elif dh:
        why.append(f"{dh['reason']}. Fewer investors follow companies like this, which also makes them riskier.")
    if fx.get("mcap_cr"):
        why.append(f"The whole company is valued at about ₹{fx['mcap_cr']:,.0f} crore.")
    if fx.get("sales_growth_pct") is not None:
        why.append(f"Sales {'grew' if fx['sales_growth_pct'] >= 0 else 'fell'} {abs(fx['sales_growth_pct']):.0f}% over the last year compared with the year before.")
    if fx.get("profit_growth_pct") is not None:
        why.append(f"Profit {'grew' if fx['profit_growth_pct'] >= 0 else 'fell'} {abs(fx['profit_growth_pct']):.0f}% over the same period.")
    if fx.get("roce") is not None:
        why.append(f"For every ₹100 invested in the business, it earns about ₹{fx['roce']:.0f} a year before interest and tax (this is its ROCE). {'That is healthy.' if fx['roce'] >= 15 else 'That is modest.'}")
    mv, room = t.get("moves"), t.get("room")
    ran = "No price history is available."
    if mv and mv["periods"]:
        pick = next((p for p in mv["periods"] if p["label"] == "3 months"), mv["periods"][0])
        ran = f"Over {pick['label']} the stock moved {_pct(pick['stock_pct'])} while Nifty moved {_pct(pick['benchmark_pct'])}, so it is {_ahead(pick['abnormal_pct'])}. It is {abs(mv['from_high_pct']):.1f}% below its one-year high and {mv['from_low_pct']:.1f}% above its one-year low."
    warnings = []
    if fx.get("sales_growth_pct") is not None and fx["sales_growth_pct"] < 0:
        warnings.append("Sales are falling. A theme does not help much if the business itself is shrinking.")
    if fx.get("profit_growth_pct") is not None and fx["profit_growth_pct"] < 0:
        warnings.append("Profit is falling. A stock can look cheap simply because the market expects profit to keep falling.")
    if mv and mv["from_high_pct"] <= -20 and mv["from_low_pct"] < 10:
        warnings.append("The stock is close to its one-year low. Falling stocks can keep falling, and the market may know something this screen does not.")
    if mv and mv["periods"] and mv["periods"][-1]["abnormal_pct"] > 50:
        warnings.append(f"It has already run far ahead of Nifty over {mv['periods'][-1]['label']}, so much of the good news may be in the price.")
    if room and room["usual"]["pct"] < 0:
        warnings.append("It already costs more than usual compared with its profit, so there is no room on this measure.")
    if any("misread" in x for x in t["confidence_reasons"]):
        warnings.append("The order value looks too large and may be misread. Check the filing.")
    if t.get("contested"):
        warnings.append("Recent news on this theme points both ways.")
    if t["company"].get("in_nifty500") is False:
        warnings.append("It is outside the Nifty 500, so it may be hard to buy or sell in size, and less is known about it.")
    if room:
        u, h, lo = room["usual"], room["highest"], room["lowest"]
        room_text = f"Investors now pay ₹{room['pe_now']:.1f} for each ₹1 of yearly profit (its P/E). Its usual level over the last {room['points']} quarter-ends is ₹{u['pe']:.1f}. At that level, with profit unchanged, the price would be about ₹{u['price']:,.0f} ({_pct(u['pct'])}). "
        distorted = h["pe"] > 2 * u["pe"] or fx.get("profit_growth_pct") is None or abs(fx["profit_growth_pct"]) >= 50
        if not distorted:
            room_text += f"At its highest level it would be about ₹{h['price']:,.0f} ({_pct(h['pct'])}). "
        if lo["pct"] < 0:
            room_text += f"At its lowest level it would be about ₹{lo['price']:,.0f} ({_pct(lo['pct'])}). "
        else:
            room_text += "Its P/E today is below every level in this history, so this sum shows no fall. That does not make it safe: the price can keep falling, especially if profit does. "
        if distorted:
            room_text += "Profit has changed sharply or unevenly, so past P/E levels are a poor guide for this company. "
        room_text += "This is arithmetic on a short history, not a forecast: it does not say the price is going there."
    else:
        room_text = "It can't be estimated: the company's past profit and valuation history is too short or too uneven."
    return {"theme": t.get("theme_plain") or "", "evidence": EVIDENCE_PLAIN[t["classification"]], "why_selected": why, "warnings": warnings, "ran": ran, "room": room_text, "is_order": surprise,
            "check": ["Read the source document yourself, and confirm the numbers.", "Look at the next results: do sales and profit margins actually improve?", "Consider how much you could lose: smaller companies can fall quickly and be harder to sell."]}


def merge_repeats(theses: list[dict]) -> list[dict]:
    """One record per company and theme: the latest, counting its other filings. A second order is evidence, not a second thesis."""
    groups = defaultdict(list)
    for t in theses:
        groups[(t["company"]["symbol"], t["chain"])].append(t)
    out = []
    for items in groups.values():
        items.sort(key=lambda t: t["event"]["date"], reverse=True)
        best = items[0]
        best["company"]["related_filings"] = len(items)
        out.append(best)
    return out


# ---------------- tracking (safeguard 8) ----------------
def score_log(log_rows: list[dict], price_of, bench: list[list], today: date) -> dict:
    """Each thesis against the benchmark, by confidence and for dark horses as a group, once enough time has passed to score it."""
    groups: dict[str, list[float]] = {}
    for r in log_rows:
        day = date.fromisoformat(r["date"])
        if (today - day).days < 30:
            continue
        closes = price_of(r["symbol"])
        abn = abnormal_return(closes, bench, day) if closes and bench else None
        if abn:
            groups.setdefault(r["confidence"], []).append(abn["abnormal_pct"])
            if r.get("dark"):
                groups.setdefault("Dark horses", []).append(abn["abnormal_pct"])
    return {"logged": len(log_rows), "too_new": sum((today - date.fromisoformat(r["date"])).days < 30 for r in log_rows),
            "by_confidence": {k: {"n": len(v), "avg_abnormal_pct": round(sum(v) / len(v), 2), "beat_benchmark_pct": round(sum(x > 0 for x in v) / len(v) * 100, 1)} for k, v in groups.items()}}


# ---------------- a full pass ----------------
class Screener:
    """nse/screener/get/cached are the app's polite public-data clients; closes(symbol) gives daily [[date, close]]; world() gives {items, status} from the news feeds; funds() gives saved company data with no network call."""

    def __init__(self, nse, screener, closes, world, get, cached, universe, funds):
        self.nse, self.screener, self.closes, self.world, self.get, self.cached, self.universe, self.funds = nse, screener, closes, world, get, cached, universe, funds
        self.lock = threading.Lock()
        self.news_lock = threading.Lock()
        self.progress = {"running": False, "stage": "", "error": ""}

    def reviewed_edges(self) -> set:
        return set(_read("edges.json", []))

    def set_edge(self, edge: str, approved: bool) -> None:
        if edge not in {f"{c['id']}/{n['id']}" for c in CHAINS for n in c["nodes"]}:
            raise KeyError(edge)
        edges = self.reviewed_edges()
        edges.add(edge) if approved else edges.discard(edge)
        _write("edges.json", sorted(edges))

    def refresh_news(self, today: date | None = None) -> dict:
        """Read the feeds and recompute the themes, with no filings, company data or prices, so it can run every few minutes. Says which themes have just gone live."""
        today = today or datetime.now(common.IST).date()
        with self.news_lock:
            refresh_chains()
            world = self.world()
            old = _read("news.json", [])
            news = merge_news(old, world["items"], today)
            _write("news.json", news)
            known = {n["link"] for n in old}
            before = {t["id"] for t in _read("live.json", {}).get("themes", [])}
            themes = active_themes(news, today)
            live = {"checked": datetime.now(common.IST).isoformat(timespec="seconds"), "themes": themes, "unmapped": unmapped(news, today), "feeds": world["status"], "headlines_kept": len(news),
                    "new_headlines": sum(n["link"] not in known for n in news), "new_themes": sorted({t["id"] for t in themes} - before)}
            _write("live.json", live)
            return live

    def _fund(self, sym: str, funds: dict[str, dict]) -> dict:
        if sym in funds:
            return funds[sym]
        r = self.screener(sym)
        return dict(r.get("data") or {}, fetched_at=r.get("fetched_at"))

    def _filing_theses(self, rows, themes, funds, in500, seen, reviewed, bench, today, signals, clusters):
        theses, pdfs = [], 0
        active = {t["id"]: t for t in themes}
        for f in order_filings(rows) + update_filings(rows):
            sym = f["symbol"]
            f["_in500"] = sym in in500
            is_order = f.get("desc") in ORDER_DESCS
            fund = self._fund(sym, funds)
            industry = fund.get("industry") or f.get("smIndustry") or ""
            if is_order and industry:
                clusters[industry].add(sym)
            matches = [(c, n) for c, n in nodes_for(industry) if not n.get("about") or fits(n, {**fund, "industry": industry})]
            text = f.get("attchmntText") or ""
            pdf = ""
            if is_order and pdfs < MAX_PDFS:
                pdf = pdf_text(f.get("attchmntFile") or "", str(f.get("seq_id") or sym), self.get, self.cached)
                pdfs += 1
            full = f"{text}\n{pdf}"
            val = order_value(full) if is_order else None
            named = drivers_in(f"{text}\n{pdf[:2500]}")          # the letter itself, not boilerplate further down the attachment
            ttm = ttm_sales(fund)
            sid = f"{sym}-{f.get('seq_id')}"
            score = score_signal("nse_filing", val, ttm, max(1, len(matches)), sid in seen)
            signals.append({"kind": "filing", "text": f"{f.get('sm_name') or sym}: {text}"[:300], "date": (parse_day(f.get("an_dt")) or today).isoformat(), "source": f.get("attchmntFile"), "score": score, "symbol": sym})
            seen.add(sid)
            closes = self.closes(sym)
            day = parse_day(f.get("an_dt")) or today
            flags = []
            if any(x.get("symbol") == sym and x.get("desc") == "Outcome of Board Meeting" and abs(((parse_day(x.get("an_dt")) or today) - day).days) <= 3 for x in rows):
                flags.append("results or board news came within 3 days, so the price response may not be about this event")
            if val and ttm and val / ttm > THRESHOLDS["order_suspect_share"]:
                flags.append("the value read from the filing is over twice annual sales, so it may be misread: check the document")
            made = []
            if is_order and val and ttm and val / ttm >= THRESHOLDS["order_surprise_share"]:
                node = {"id": "order", "name": industry or "Its own business", "industry": "", "confidence": "High", "level": 1}
                t = build_thesis(f, ORDER_CHAIN, node, "Direct", industry or "not known", bool(pdf.strip()), val, fund, closes, bench, reviewed, today, score, flags)
                t["company"]["order_share_of_sales_pct"] = round(val / ttm * 100, 1)
                note = next((dark_note(sym, fund, n, funds) for _, n in matches), None) if matches else None
                if note or (fund.get("mcap_cr") or 0) < 5000:
                    t["dark_horse"] = note or {"reason": "A small company for an order of this size", "rank": None, "peers": None, "mcap_cr": fund.get("mcap_cr"), "growth_pct": sales_growth(fund)}
                made.append(t)
            done = set()
            for ch, node in matches:
                if ch["id"] in done:
                    continue
                if ch in named:
                    cls, trigger = "Direct", None
                elif ch["id"] in active and is_order:
                    cls, trigger = "Indirect", active[ch["id"]]["headlines"][0]
                else:
                    continue                                        # neither the filing nor a live theme ties it to this chain
                done.add(ch["id"])
                t = build_thesis(f, ch, node, cls, industry, bool(pdf.strip()), val, fund, closes, bench, reviewed, today, score, flags, trigger=trigger)
                note = dark_note(sym, fund, node, funds)
                if note:
                    t["dark_horse"] = note
                made.append(t)
            for t in made:
                try:
                    check_record(t)
                    theses.append(t)
                except ValueError as exc:
                    log.warning("event screener refused %s: %s", t["id"], exc)
        return theses

    def run(self, today: date | None = None) -> dict:
        if not self.lock.acquire(blocking=False):
            return _read("latest.json", {})
        try:
            self.progress = {"running": True, "stage": "Reading world news", "error": ""}
            refresh_chains()
            today = today or datetime.now(common.IST).date()
            live = self.refresh_news(today)
            news, themes = _read("news.json", []), live["themes"]
            self.progress["stage"] = "Reading NSE filings"
            start = today - timedelta(days=LOOKBACK_DAYS)
            res = self.nse(f"corporate-announcements?index=equities&from_date={start:%d-%m-%Y}&to_date={today:%d-%m-%Y}", ttl=540, key="events_filings")
            data = res.get("data")
            rows = data if isinstance(data, list) else (data or {}).get("data") or []
            if not rows and res.get("error"):
                raise RuntimeError(f"NSE filings: {res['error']}")
            funds = self.funds()
            in500 = {u["symbol"] for u in self.universe()}
            seen = set(_read("seen.json", []))
            reviewed = self.reviewed_edges()
            bench = self.closes("NIFTY 50")
            signals = [{"kind": "theme", "text": f"{t['name']}: {t['headlines'][0]['title']}"[:300], "date": t["latest"], "source": t["headlines"][0]["link"], "score": score_signal("news", None, None, t["nodes"], t["headlines"][0]["link"] in seen)} for t in themes]
            seen.update(t["headlines"][0]["link"] for t in themes)
            clusters: dict[str, set] = defaultdict(set)
            self.progress["stage"] = "Reading company filings"
            theses = self._filing_theses(rows, themes, funds, in500, seen, reviewed, bench, today, signals, clusters)
            self.progress["stage"] = "Looking for dark horses"
            have = {(t["company"]["symbol"], t["chain"]) for t in theses}
            candidates, dropped = dark_horses(themes, funds, self.closes, bench, today)
            for c in candidates:
                if (c["symbol"], c["chain"]["id"]) in have:
                    continue
                have.add((c["symbol"], c["chain"]["id"]))
                head = c["theme"]["headlines"][0]
                stand_in = {"symbol": c["symbol"], "sm_name": c["fund"].get("name") or c["symbol"], "an_dt": c["theme"]["first"], "_in500": True}
                score = score_signal("news", None, None, c["theme"]["nodes"], False)
                t = build_thesis(stand_in, c["chain"], c["node"], "Speculative", c["fund"].get("industry") or "", False, None, c["fund"], c["closes"], bench, reviewed, today, score, [], trigger=head)
                t["dark_horse"] = {"reason": f"Number {c['rank']} of {c['peers']} by market value in {c['node']['name']}", "rank": c["rank"], "peers": c["peers"], "mcap_cr": c["fund"].get("mcap_cr"), "growth_pct": c["growth"], "rel_90d_pct": c["rel"]["abnormal_pct"], "roce": c["fund"].get("roce")}
                try:
                    check_record(t)
                    theses.append(t)
                except ValueError as exc:
                    log.warning("event screener refused %s: %s", t["id"], exc)
            kept = []
            for t in merge_repeats(theses):
                t["plain"] = explain(t)
                try:
                    check_record(t)
                    kept.append(t)
                except ValueError as exc:
                    log.warning("event screener refused %s: %s", t["id"], exc)
            theses = kept
            by_theme = {x["id"]: x for x in themes}
            for t in theses:
                opp = by_theme.get(t["chain"], {})
                if opp.get("against_count"):
                    t["risks"].insert(0, f"{opp['against_count']} recent reports point the other way, such as: {opp['against'][0]['title']}")
                    t["contested"] = bool(opp.get("contested"))
            theses.sort(key=rank_key)
            _write("seen.json", sorted(seen)[-5000:])
            logged = _read("log.json", [])
            known = {r["id"] for r in logged}
            logged += [{"id": t["id"], "date": t["built"] if t["classification"] == "Speculative" else t["event"]["date"], "symbol": t["company"]["symbol"], "state": t["priced_in"]["state"], "confidence": t["confidence"], "classification": t["classification"], "dark": bool(t.get("dark_horse")), "logged": today.isoformat()} for t in theses if t["id"] not in known]
            _write("log.json", logged)
            chained = {re.sub(r"\s+", " ", i).strip().lower() for i in clusters if nodes_for(i)}
            grouped = [{"industry": k, "companies": sorted(v), "in_library": re.sub(r"\s+", " ", k).strip().lower() in chained} for k, v in clusters.items() if len(v) >= THRESHOLDS["cluster_min_companies"]]
            out = {"built": datetime.now(common.IST).isoformat(timespec="seconds"), "version": LIBRARY_VERSION, "filings_read": len(rows), "headlines_kept": len(news), "feeds": live["feeds"], "themes": themes, "theses": theses, "dark_screen": {"candidates": len(candidates), "removed": dropped},
                   "clusters": sorted(grouped, key=lambda g: -len(g["companies"])), "unmapped": unmapped(news, today), "signals": sorted(signals, key=lambda s: -s["score"]["total"])[:60],
                   "tracking": score_log(logged, self.closes, bench, today), "filings_status": {"fetched_at": res.get("fetched_at"), "stale": res.get("stale"), "error": res.get("error")}}
            _write("latest.json", out)
            self.progress.update(stage="Finished")
            return out
        except Exception as exc:  # noqa: BLE001 - shown on the page
            log.exception("event screener failed")
            self.progress.update(error=str(exc), stage="Failed")
            return _read("latest.json", {})
        finally:
            self.progress["running"] = False
            self.lock.release()
