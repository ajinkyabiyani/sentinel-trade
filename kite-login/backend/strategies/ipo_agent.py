"""IPO Agent: reads every upcoming, open and recently closed IPO and says whether it looks worth applying for, and why.

Three views of each issue, each made of plain checks that pass, warn or fail:
  Fundamentals   revenue and profit growth, profitability, ROE, ROCE, debt, P/E against its listed peers, how much of the issue is
                 existing holders selling (offer for sale), and the promoters' stake after listing. From the issue's page on
                 investorgain.com, which restates the offer document (RHP).
  Demand         the grey market premium (GMP: an unofficial, unregulated quote of where it may list) and whether it is holding up
                 or fading, and the subscription once bidding opens.
  Timing         the market it lists into: Nifty 50 against its 50-day average, India VIX, and how the last 20 listings on the same
                 board did.
Success rate: past IPOs (investorgain's GMP performance tracker, several years) grouped by board and GMP band, giving how often
an issue in that band listed above its price and the typical gain. The grouping is fixed in advance, and it is checked on the most
recent 12 months with bands built only from the years before them, so the odds are not judged on the data that made them.
Each signal given before listing is logged and scored once the issue lists. Read only: it never applies for anything.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from html import unescape

import requests

from strategies import common, paper_desk

log = logging.getLogger("kite-desk.ipo-agent")
SITE = "https://www.investorgain.com"
UA = "SentinelTrade/1.0 (personal research tool)"
DIR = paper_desk.CACHE_DIR / "ipo_agent"
LIVE_TTL = 20 * 60
DETAIL_TTL = 6 * 3600
HISTORY_TTL = 24 * 3600
FIRST_YEAR = 2021
PACE = 2.0
STATUS = {"U": "Upcoming", "O": "Open", "CT": "Closing today", "C": "Closed", "LT": "Listing today", "LP": "Listed above price",
          "LN": "Listed below price", "L": "Listed"}
ACTIVE = ("U", "O", "CT", "C", "LT")
GMP_BANDS = [(-math.inf, 0.5, "No GMP"), (0.5, 5, "0–5%"), (5, 15, "5–15%"), (15, 30, "15–30%"), (30, math.inf, "30% and above")]
SUB_BANDS = [(0, 2, "under 2x"), (2, 10, "2–10x"), (10, 50, "10–50x"), (50, math.inf, "50x and above")]
DISCLAIMER = ("Analytical signal only, not advice to apply. The grey market premium is an unofficial, unregulated quote and can change "
              "or vanish before listing. Past listing odds describe past issues, not this one. Read the offer document (RHP) before applying.")
_lock = threading.RLock()
_cache: dict = {}
_state: dict = {"running": False, "error": None, "checked": None}


# ---------------------------------------------------------------- fetching and parsing

def _get(url: str, tries: int = 2) -> str | None:
    for i in range(tries):
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA})
            if r.status_code == 200:
                return r.text
            log.warning("ipo agent %s: HTTP %s", url, r.status_code)
        except requests.RequestException as exc:
            log.warning("ipo agent %s: %s", url, exc)
        time.sleep(2 + 3 * i)
    return None


def flight(html: str) -> str:
    """The page's data, as the site's framework embeds it (a string pushed in pieces into self.__next_f)."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', html)
    out = []
    for p in parts:
        try:
            out.append(json.loads(p))
        except ValueError:
            continue
    return "".join(out)


def report_rows(html: str) -> list[dict]:
    f = flight(html)
    i = f.find('"reportTableData"')
    if i < 0:
        return []
    try:
        rows, _ = json.JSONDecoder().raw_decode(f, f.find("[", i))
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def text(h) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", str(h or "")))).strip()


def number(s) -> float | None:
    """The first number in a cell: '₹455.49 Cr' -> 455.49, '12.79x' -> 12.79, '-14.17' -> -14.17; None for '-', '--' or ''."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text(s).replace("₹", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _iso_short(s: str) -> str | None:
    """'11-Nov-26' -> '2026-11-11'."""
    for fmt in ("%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _slug_id(path: str) -> tuple[str | None, str | None]:
    m = re.search(r"/gmp/([^/]+)/(\d+)/?", path or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def parse_live(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        slug, iid = _slug_id(r.get("~urlrewrite_folder_name", ""))
        gmp_cell = text(r.get("GMP"))
        gmp = number(re.sub(r"\(.*", "", gmp_cell))
        lo_hi = re.search(r"(-?[\d.]+)\s*↓\s*/\s*(-?[\d.]+)\s*↑", gmp_cell)
        price = number(r.get("Price (₹)")) or None
        out.append({
            "id": str(r.get("~id") or iid), "slug": slug, "name": text(r.get("~ipo_name") or r.get("Name")),
            "board": "SME" if str(r.get("~IPO_Category", "")).upper() == "SME" else "Mainboard",
            "status": r.get("~ipo_status1") or "", "status_label": STATUS.get(r.get("~ipo_status1") or "", r.get("~ipo_status1") or ""),
            "price": price, "gmp": gmp, "gmp_pct": round(gmp / price * 100, 2) if (gmp is not None and price) else None,
            "gmp_low": float(lo_hi.group(1)) if lo_hi else None, "gmp_high": float(lo_hi.group(2)) if lo_hi else None,
            "sub": number(r.get("Sub")), "size_cr": number(r.get("IPO Size")), "lot": number(r.get("Lot")), "pe": number(r.get("~P/E")),
            "open": r.get("~Srt_Open") or None, "close": r.get("~Srt_Close") or None, "allotment": r.get("~Srt_BoA_Dt") or None,
            "listing": r.get("~Str_Listing") or None, "gmp_updated": text(r.get("Updated-On")),
            "link": f"{SITE}/ipo/{slug}/{iid}/" if slug else None,
        })
    return out


def parse_history(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        price = number(r.get("Price"))
        gmp = number(r.get("GMP"))
        listed = _iso_short(r.get("Listing Dt"))
        gain = number(r.get("~str_listing_gain_in_per"))
        if not (price and listed) or gain is None:
            continue
        out.append({"id": str(r.get("~id") or ""), "name": re.sub(r"\s+SME$", "", text(r.get("IPO"))),
                    "board": "SME" if str(r.get("~IPO_Category", "")).upper() == "SME" else "Mainboard", "listed": listed, "price": price,
                    "gmp": gmp, "gmp_pct": round((gmp or 0) / price * 100, 2), "sub": number(r.get("Sub")), "size_cr": number(r.get("Size")),
                    "listing_gain": gain, "close_gain": number(r.get("~str_closing_gain_in_per"))})
    return out


def _cells(row_html: str) -> list[str]:
    return [text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.S)]


def parse_detail(html: str) -> dict:
    """Financials, KPIs, issue structure and peers from an IPO's page."""
    f = flight(html) or html
    out: dict = {"financials": None, "kpi": {}, "issue": {}, "peers": []}
    i = f.find("fin-table-new")
    if i >= 0:
        head = f[max(0, i - 1500):i]
        e = f.find("</table>", i)
        rows = [_cells(r) for r in re.findall(r"<tr>(.*?)</tr>", f[i:e], re.S)]
        if rows and len(rows[0]) > 1:
            unit = re.search(r"Amount in ([^<]{3,30})", f[i:e + 400])
            note = re.search(r"<p>([^<]*revenue[^<]*)</p>", head, re.I)
            out["financials"] = {"periods": rows[0][1:], "rows": {r[0]: [number(v) for v in r[1:]] for r in rows[1:] if r},
                                 "unit": unescape(unit.group(1)).strip() if unit else "₹ crore", "note": unescape(note.group(1)) if note else None}
    for m in re.finditer(r'"kpi-td-label","children":"([^"]+)"', f):
        seg = f[m.end():m.end() + 400]
        seg = seg.split("kpi-td-label")[0]
        vals = re.findall(r'"kpi-td-val"(?:,"colSpan":\d+)?,"children":(\[[^\]]*\]|-?[\d.]+|"[^"]*")', seg)
        out["kpi"][m.group(1)] = [number(v.replace('"₹"', "")) for v in vals]
    for label in ("Fresh Issue", "Offer for Sale", "Total Issue Size", "Price Band", "IPO Listing At", "Face Value"):
        m = re.search(r'"children":"' + re.escape(label) + r'"\}\],\["\$","td",null,\{"className":"text-val","children":(.{0,200})', f)
        if m:
            raw = m.group(1)
            html_val = re.search(r'"__html":"([^"]*)"', raw)
            val = unescape(html_val.group(1)) if html_val else text(re.sub(r'["\[\]{}$,]|null|className|text-val|children', " ", raw.split("}]")[0]))
            out["issue"][label] = val.strip()
    p = f.find("id='analysisTable'")
    if p >= 0:
        rows = [_cells(r) for r in re.findall(r"<tr>(.*?)</tr>", f[p:f.find("</table>", p)], re.S)]
        if rows:
            head = [h.lower() for h in rows[0]]
            col = lambda key: next((k for k, h in enumerate(head) if key in h), None)  # noqa: E731
            pe_c, ronw_c, pb_c = col("p/e"), col("ronw"), col("p/bv")
            for r in rows[1:]:
                if len(r) == len(head):
                    out["peers"].append({"name": r[0], "pe": number(r[pe_c]) if pe_c is not None else None,
                                         "ronw": number(r[ronw_c]) if ronw_c is not None else None, "pb": number(r[pb_c]) if pb_c is not None else None})
    return out


# ---------------------------------------------------------------- cached reads

def _cached(key: str, ttl: float, fn):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    if val is None and hit:
        return hit[1]
    if val is not None:
        with _lock:
            _cache[key] = (time.time(), val)
    return val


def live() -> list[dict] | None:
    def fetch():
        html = _get(f"{SITE}/report/ipo-gmp-live/331/")
        rows = report_rows(html) if html else []
        return parse_live(rows) if rows else None
    return _cached("live", LIVE_TTL, fetch)


def detail(ipo: dict) -> dict | None:
    if not ipo.get("link"):
        return None
    def fetch():
        html = _get(ipo["link"])
        return parse_detail(html) if html else None
    return _cached(f"detail:{ipo['id']}", DETAIL_TTL, fetch)


def _history_file(year: int):
    return DIR / f"history_{year}.json"


def history(today: date | None = None) -> list[dict]:
    """Every listed IPO with its final GMP and listing gain since FIRST_YEAR. Past years are kept on disk; this year is refreshed daily."""
    today = today or date.today()
    out = []
    for year in range(FIRST_YEAR, today.year + 1):
        path = _history_file(year)
        current = year >= today.year - (1 if today.month == 1 else 0)
        rows = None
        try:
            if path.exists() and (not current or time.time() - path.stat().st_mtime < HISTORY_TTL):
                rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = None
        if rows is None:
            html = _get(f"{SITE}/report/ipo-gmp-performance-tracker/377/all/?year={year}")
            parsed = parse_history(report_rows(html)) if html else []
            if parsed:
                DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
                rows = parsed
            elif path.exists():
                rows = json.loads(path.read_text(encoding="utf-8"))
            time.sleep(PACE)
        out.extend(rows or [])
    seen, uniq = set(), []
    for r in sorted(out, key=lambda r: r["listed"]):
        k = (r["id"] or r["name"], r["listed"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


# ---------------------------------------------------------------- success rate

def band(value: float | None, bands) -> str | None:
    if value is None:
        return None
    return next((label for lo, hi, label in bands if lo <= value < hi), None)


def outcome(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    gains = sorted(r["listing_gain"] for r in rows)
    closes = [r["close_gain"] for r in rows if r.get("close_gain") is not None]
    q = lambda p: gains[min(len(gains) - 1, max(0, int(round(p * (len(gains) - 1)))))]  # noqa: E731
    return {"n": len(rows), "win_pct": round(100 * sum(g > 0 for g in gains) / len(gains), 1),
            "loss_pct": round(100 * sum(g < 0 for g in gains) / len(gains), 1),
            "median_gain": round(statistics.median(gains), 2), "avg_gain": round(statistics.fmean(gains), 2),
            "p10_gain": round(q(0.1), 2), "p90_gain": round(q(0.9), 2),
            "close_win_pct": round(100 * sum(c > 0 for c in closes) / len(closes), 1) if closes else None}


def table(hist: list[dict]) -> dict:
    """Listing outcomes by board and GMP band, and by board and subscription band."""
    out = {"gmp": {}, "sub": {}, "board": {}}
    for board in ("Mainboard", "SME"):
        rows = [r for r in hist if r["board"] == board]
        out["board"][board] = outcome(rows)
        out["gmp"][board] = [{"band": label, **(outcome([r for r in rows if band(r["gmp_pct"], GMP_BANDS) == label]) or {"n": 0})} for _, _, label in GMP_BANDS]
        out["sub"][board] = [{"band": label, **(outcome([r for r in rows if band(r["sub"], SUB_BANDS) == label]) or {"n": 0})} for _, _, label in SUB_BANDS]
    return out


def odds(hist: list[dict], board: str, gmp_pct: float | None, sub: float | None = None) -> dict | None:
    """How past issues like this one listed: same board and GMP band, narrowed by subscription band when there are enough of them."""
    g = band(gmp_pct, GMP_BANDS)
    if g is None:
        return None
    same = [r for r in hist if r["board"] == board and band(r["gmp_pct"], GMP_BANDS) == g]
    s = band(sub, SUB_BANDS)
    narrower = [r for r in same if band(r["sub"], SUB_BANDS) == s] if s else []
    if len(narrower) >= 20:
        return {**outcome(narrower), "basis": f"{board} issues with a GMP of {g} and subscription {s}"}
    o = outcome(same)
    return {**o, "basis": f"{board} issues with a GMP of {g}"} if o else None


def check(hist: list[dict], today: date) -> dict | None:
    """Out-of-sample check: bands built from issues before the last 12 months, applied to the last 12 months.
    Brier score of the band's listing odds against a board-only base rate (lower is better)."""
    cut = (today - timedelta(days=365)).isoformat()
    train = [r for r in hist if r["listed"] < cut]
    test = [r for r in hist if r["listed"] >= cut]
    if len(train) < 100 or len(test) < 30:
        return None
    base = {b: (outcome([r for r in train if r["board"] == b]) or {}).get("win_pct", 50) / 100 for b in ("Mainboard", "SME")}
    br_band, br_base, rows = 0.0, 0.0, []
    for r in test:
        o = odds(train, r["board"], r["gmp_pct"])
        p = (o["win_pct"] / 100) if o and o["n"] >= 10 else base[r["board"]]
        y = 1.0 if r["listing_gain"] > 0 else 0.0
        br_band += (p - y) ** 2
        br_base += (base[r["board"]] - y) ** 2
        rows.append((p, y))
    hi = [y for p, y in rows if p >= 0.75]
    lo = [y for p, y in rows if p < 0.5]
    return {"train": len(train), "test": len(test), "from": cut, "brier_band": round(br_band / len(test), 3), "brier_base": round(br_base / len(test), 3),
            "high_odds_n": len(hi), "high_odds_hit": round(100 * sum(hi) / len(hi), 1) if hi else None,
            "low_odds_n": len(lo), "low_odds_hit": round(100 * sum(lo) / len(lo), 1) if lo else None}


# ---------------------------------------------------------------- the checks

def _c(name: str, value: str, verdict: str, why: str) -> dict:
    return {"name": name, "value": value, "verdict": verdict, "why": why}


def _latest_fy(fin: dict, label_options) -> tuple[float | None, float | None, str | None]:
    """(latest full-year value, the year before, latest year's label), skipping a part-year first column."""
    if not fin:
        return None, None, None
    rows = fin["rows"]
    row = next((rows[k] for k in label_options if k in rows), None)
    if not row:
        return None, None, None
    periods = fin["periods"]
    idx = [i for i, p in enumerate(periods) if re.search(r"\bMar\b", p) and i < len(row) and row[i] is not None]
    if len(idx) < 2:
        return (row[idx[0]] if idx else None), None, (_fy(periods[idx[0]]) if idx else None)
    return row[idx[0]], row[idx[1]], _fy(periods[idx[0]])


def _fy(period: str) -> str:
    """'31 Mar 2026' -> 'FY26'."""
    m = re.search(r"(\d{4})", period or "")
    return f"FY{m.group(1)[2:]}" if m else period


def _growth(a, b) -> float | None:
    return None if a is None or not b or b <= 0 else round((a / b - 1) * 100, 1)


def fundamentals(d: dict | None, pe: float | None) -> list[dict]:
    if not d:
        return []
    out = []
    fin, kpi, issue = d.get("financials"), d.get("kpi", {}), d.get("issue", {})
    rev, rev0, fy = _latest_fy(fin, ("Revenue", "Total Income"))
    pat, pat0, _ = _latest_fy(fin, ("Profit After Tax",))
    g = _growth(rev, rev0)
    if g is not None:
        out.append(_c("Revenue growth", f"{g:+.1f}% in {fy}", "good" if g >= 15 else "warn" if g >= 0 else "bad", "15% or more is strong; a fall is a red flag"))
    if pat is not None:
        if pat <= 0:
            out.append(_c("Profit", f"Loss of ₹{abs(pat):,.0f} cr in {fy}", "bad", "a loss-making company leans entirely on future growth"))
        else:
            gp = _growth(pat, pat0)
            out.append(_c("Profit growth", f"{gp:+.1f}% in {fy}" if gp is not None else f"₹{pat:,.0f} cr in {fy}",
                          "good" if (gp or 0) >= 15 else "warn" if gp is None or gp >= 0 else "bad", "profit growing at least as fast as sales is healthy"))
    first = lambda k: next((v for v in (kpi.get(k) or []) if v is not None), None)  # noqa: E731
    roe = first("ROE") if first("ROE") is not None else first("RoNW")
    if roe is not None:
        out.append(_c("Return on equity", f"{roe:.1f}%", "good" if roe >= 15 else "warn" if roe >= 8 else "bad", "15% or more is good"))
    roce = first("ROCE")
    if roce is not None:
        out.append(_c("Return on capital", f"{roce:.1f}%", "good" if roce >= 15 else "warn" if roce >= 10 else "bad", "15% or more is good"))
    de = first("Debt / Equity")
    if de is not None:
        out.append(_c("Debt to equity", f"{de:.2f}", "good" if de <= 0.5 else "warn" if de <= 1.5 else "bad", "under 0.5 is comfortable"))
    vals = kpi.get("P/E (x)") or []
    own_pe = (vals[0] if vals and vals[0] is not None else None) or pe   # pre-issue P/E: full-year earnings, the same basis as the peers
    peer_pe = [p["pe"] for p in d.get("peers", [])[1:] if p.get("pe") and p["pe"] > 0]
    if own_pe and own_pe > 0 and len(peer_pe) >= 2:
        med = statistics.median(peer_pe)
        ratio = own_pe / med
        out.append(_c("Price against peers", f"P/E {own_pe:.1f} vs peers' {med:.1f}", "good" if ratio <= 1.0 else "warn" if ratio <= 1.3 else "bad",
                      "priced at or below listed peers leaves room; well above them needs faster growth"))
    elif own_pe is not None and own_pe <= 0:
        out.append(_c("Price against peers", "No P/E (loss-making)", "bad", "no earnings to value it on"))
    fresh, ofs = number(issue.get("Fresh Issue")), number(issue.get("Offer for Sale"))
    if fresh is not None or ofs is not None:
        tot = (fresh or 0) + (ofs or 0)
        share = (ofs or 0) / tot * 100 if tot else 0
        out.append(_c("Offer for sale", f"{share:.0f}% of the issue", "good" if share <= 30 else "warn" if share <= 70 else "bad",
                      "money from an offer for sale goes to existing holders, not the company"))
    ph = kpi.get("Promoter Holding") or []
    post = ph[1] if len(ph) > 1 and ph[1] is not None else (ph[0] if ph else None)
    if post is not None:
        out.append(_c("Promoter stake after listing", f"{post:.1f}%", "good" if post >= 50 else "warn" if post >= 35 else "bad", "50% or more keeps the owners' interests aligned"))
    return out


def demand(ipo: dict, recent_board: dict | None) -> list[dict]:
    out = []
    if not ipo.get("price"):
        return out                     # no price band yet: a premium in rupees means nothing on its own
    if ipo.get("gmp_pct") is not None:
        p = ipo["gmp_pct"]
        out.append(_c("Grey market premium", f"₹{ipo['gmp']:g} ({p:+.1f}%)", "good" if p >= 15 else "warn" if p >= 5 else "bad",
                      "an unofficial quote of where it may list"))
    hi = ipo.get("gmp_high")
    if hi and hi > 0 and ipo.get("gmp") is not None:
        held = ipo["gmp"] / hi * 100
        out.append(_c("GMP trend", f"{held:.0f}% of its ₹{hi:g} high", "good" if held >= 80 else "warn" if held >= 50 else "bad",
                      "a premium falling from its high often means fading interest"))
    if ipo.get("sub"):
        s = ipo["sub"]
        out.append(_c("Subscription", f"{s:g}x", "good" if s >= 10 else "warn" if s >= 2 else "bad", "how many times the shares on offer were bid for"))
    return out


def timing(back: dict, recent_board: dict | None) -> list[dict]:
    out = []
    if back.get("nifty_vs_50d") is not None:
        v = back["nifty_vs_50d"]
        out.append(_c("Nifty 50 trend", f"{v:+.1f}% vs 50-day", "good" if v >= 0 else "warn" if v >= -3 else "bad",
                      "issues tend to list better in a rising market"))
    if back.get("vix") is not None:
        v = back["vix"]
        out.append(_c("India VIX", f"{v:.1f}", "good" if v < 15 else "warn" if v < 20 else "bad", "calm markets suit listings"))
    if recent_board:
        w = recent_board["win_pct"]
        out.append(_c("Recent listings", f"{w:.0f}% of last {recent_board['n']} up, median {recent_board['median_gain']:+.1f}%",
                      "good" if w >= 70 else "warn" if w >= 50 else "bad", "how the IPO market on this board is treating new issues"))
    return out


def score(checks: list[dict]) -> int | None:
    if not checks:
        return None
    pts = {"good": 1.0, "warn": 0.5, "bad": 0.0}
    return round(100 * sum(pts[c["verdict"]] for c in checks) / len(checks))


def signal(ipo: dict, o: dict | None, f: int | None, t: int | None) -> tuple[str, str]:
    """(signal, one-line reason). Fixed rules, set before looking at any outcome."""
    if ipo["status"] in ("LP", "LN", "L"):
        return "Listed", "already listed"
    if not ipo.get("price"):
        return "Waiting", "no price band yet, so there is nothing to judge the premium against"
    if not o:
        return "Neutral", "too few similar past issues to give odds"
    win = o["win_pct"]
    if win >= 75 and (f is None or f >= 50) and (t is None or t >= 40):
        return "Apply", f"{win:.0f}% of similar issues listed above price, and the business and market checks hold up"
    if win >= 70 and f is not None and f < 50:
        return "Listing gain only", f"{win:.0f}% of similar issues listed above price, but the business checks are weak for holding it"
    if f is not None and f >= 65 and win < 70:
        return "Neutral", f"the business checks are sound, but only {win:.0f}% of similar issues listed above price"
    if win < 55 or (f is not None and f < 35 and win < 70):
        return "Avoid", f"only {win:.0f}% of similar issues listed above price" + (" and the business checks are weak" if f is not None and f < 35 else "")
    return "Neutral", f"{win:.0f}% of similar issues listed above price; the other checks are mixed"


# ---------------------------------------------------------------- market backdrop

def backdrop() -> dict:
    def fetch():
        from strategies import multi_agent_team as mat, pipeline_swing as swing
        out = {}
        try:
            n = swing._rows(mat.india.chart("NIFTY", "1y", "1d"))
            closes = [r["close"] for r in n]
            if len(closes) >= 50:
                out["nifty"] = closes[-1]
                out["nifty_vs_50d"] = round((closes[-1] / statistics.fmean(closes[-50:]) - 1) * 100, 2)
                out["nifty_20d"] = round((closes[-1] / closes[-21] - 1) * 100, 2)
        except Exception as exc:  # noqa: BLE001 - backdrop is optional
            log.warning("ipo agent nifty: %s", exc)
        try:
            v = swing._rows(mat.india.chart("INDIAVIX", "1mo", "1d"))
            if v:
                out["vix"] = round(v[-1]["close"], 2)
        except Exception as exc:  # noqa: BLE001
            log.warning("ipo agent vix: %s", exc)
        return out or None
    return _cached("backdrop", 3600, fetch) or {}


# ---------------------------------------------------------------- the track record

def _log_file():
    return DIR / "signals.json"


def signals_log() -> dict:
    try:
        return json.loads(_log_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_log(rows: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _log_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_log_file())


def record(analysed: list[dict], hist: list[dict], now: datetime) -> dict:
    """Keeps each issue's latest signal until it lists (the one standing when bidding closed), then adds how it listed."""
    log_ = signals_log()
    for a in analysed:
        if a["signal"] in ("Apply", "Listing gain only", "Neutral", "Avoid") and a["status"] in ACTIVE:
            prev = log_.get(a["id"], {})
            if prev.get("listing_gain") is None and not (prev.get("frozen") and a["status"] in ("C", "LT")):
                log_[a["id"]] = {"name": a["name"], "board": a["board"], "price": a["price"], "signal": a["signal"], "win_pct": (a["odds"] or {}).get("win_pct"),
                                 "gmp_pct": a["gmp_pct"], "fundamentals": a["fundamentals_score"], "at": now.isoformat(timespec="minutes"),
                                 "frozen": a["status"] in ("C", "LT"), "listing_gain": None, "close_gain": None}
    by_id = {h["id"]: h for h in hist if h["id"]}
    for iid, r in log_.items():
        h = by_id.get(iid)
        if h and r.get("listing_gain") is None:
            r.update(listing_gain=h["listing_gain"], close_gain=h["close_gain"], listed=h["listed"])
    _save_log(log_)
    return log_


def track(log_: dict) -> dict:
    done = [r for r in log_.values() if r.get("listing_gain") is not None]
    by = {}
    for s in ("Apply", "Listing gain only", "Neutral", "Avoid"):
        rows = [r for r in done if r["signal"] == s]
        by[s] = {"n": len(rows), "win_pct": round(100 * sum(r["listing_gain"] > 0 for r in rows) / len(rows), 1) if rows else None,
                 "avg_gain": round(statistics.fmean(r["listing_gain"] for r in rows), 2) if rows else None}
    return {"scored": len(done), "waiting": sum(1 for r in log_.values() if r.get("listing_gain") is None), "by_signal": by,
            "rows": sorted(done, key=lambda r: r.get("listed") or "", reverse=True)[:30]}


# ---------------------------------------------------------------- one pass

def analyse(ipo: dict, d: dict | None, hist: list[dict], back: dict, recent: dict) -> dict:
    o = odds(hist, ipo["board"], ipo["gmp_pct"], ipo["sub"]) if ipo.get("gmp_pct") is not None else None
    fc = fundamentals(d, ipo.get("pe"))
    dc = demand(ipo, recent.get(ipo["board"]))
    tc = timing(back, recent.get(ipo["board"]))
    fs, ds, ts = score(fc), score(dc), score(tc)
    sig, why = signal(ipo, o, fs, ts)
    lot_cost = ipo["price"] * ipo["lot"] if ipo.get("price") and ipo.get("lot") else None
    fin = (d or {}).get("financials")
    return {**ipo, "odds": o, "signal": sig, "reason": why, "fundamentals": fc, "demand": dc, "timing": tc,
            "fundamentals_score": fs, "demand_score": ds, "timing_score": ts, "lot_cost": lot_cost,
            "est_listing": round(ipo["price"] + ipo["gmp"], 2) if ipo.get("price") and ipo.get("gmp") is not None else None,
            "financials": fin, "issue": (d or {}).get("issue", {}), "peers": (d or {}).get("peers", [])[:6],
            "market_cap": next((v for v in ((d or {}).get("kpi", {}).get("Market Cap") or []) if v), None)}


def run(now: datetime | None = None) -> dict:
    now = now or datetime.now(common.IST)
    with _lock:
        if _state["running"]:
            return _state
        _state["running"] = True
    try:
        rows = live() or []
        hist = history(now.date())
        back = backdrop()
        recent = {b: outcome([r for r in hist if r["board"] == b][-20:]) for b in ("Mainboard", "SME")}
        analysed = []
        for ipo in rows:
            d = None
            if ipo["status"] in ACTIVE:
                d = detail(ipo)
                time.sleep(PACE)
            analysed.append(analyse(ipo, d, hist, back, recent))
        order = {"O": 0, "CT": 0, "U": 1, "C": 2, "LT": 2}
        active = sorted([a for a in analysed if a["status"] in ACTIVE], key=lambda a: (order.get(a["status"], 3), a["open"] or ""))
        by_id = {h["id"]: h for h in hist if h["id"]}
        listed = [{**a, "listing_gain": (by_id.get(a["id"]) or {}).get("listing_gain"), "close_gain": (by_id.get(a["id"]) or {}).get("close_gain"),
                   "final_gmp_pct": (by_id.get(a["id"]) or {}).get("gmp_pct", a["gmp_pct"])} for a in analysed if a["status"] not in ACTIVE]
        log_ = record(active, hist, now)
        cut = (now.date() - timedelta(days=365)).isoformat()
        scatter = [[round(r["gmp_pct"], 1), round(r["listing_gain"], 1), "S" if r["board"] == "SME" else "M"] for r in hist if r["listed"] >= cut]
        result = {"built": now.isoformat(timespec="seconds"), "active": active, "listed": listed, "table": table(hist), "check": check(hist, now.date()), "scatter": scatter,
                  "history_n": len(hist), "history_from": hist[0]["listed"] if hist else None, "recent": recent, "backdrop": back, "track": track(log_)}
        with _lock:
            _cache["result"] = (time.time(), result)
            _state.update(error=None if rows else "Could not read the live IPO list", checked=now.isoformat(timespec="seconds"))
        return result
    except Exception as exc:  # noqa: BLE001 - one bad page never stops the agent
        log.exception("ipo agent")
        with _lock:
            _state["error"] = str(exc)
        return {}
    finally:
        with _lock:
            _state["running"] = False


def view() -> dict:
    with _lock:
        hit = _cache.get("result")
        st = dict(_state)
    data = hit[1] if hit else {}
    return {**data, "running": st["running"], "error": st["error"], "checked": st["checked"], "disclaimer": DISCLAIMER,
            "source": "investorgain.com (GMP, issue details, past listings) · Kite (Nifty 50, India VIX)"}


def tick(now: datetime) -> None:
    with _lock:
        hit = _cache.get("result")
    if not hit or time.time() - hit[0] >= LIVE_TTL:
        run(now)
