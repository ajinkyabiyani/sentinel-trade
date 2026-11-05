"""The terminal's calendar: results dates, dividends and other corporate actions, IPOs, and the India market's fixed dates.

Earnings, corporate actions, IPOs, holidays and FII/DII come from NSE. F&O expiries come from Kite's contract list. No free
source that allows automated reads publishes India's macro calendar with estimates, so the only macro events here are
the RBI policy dates below. Nothing is estimated or guessed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .sources import nse

# RBI Monetary Policy Committee, FY2026-27: the last day of each meeting, when the decision is announced.
RBI_POLICY_DATES = ["2026-04-08", "2026-06-05", "2026-08-05", "2026-10-07", "2026-12-04", "2027-02-05"]
ACTION_KINDS = (("dividend", "Dividend"), ("bonus", "Bonus"), ("split", "Split"), ("sub-division", "Split"), ("rights", "Rights"),
                ("buy back", "Buyback"), ("buyback", "Buyback"))


def _iso(text: str | None) -> str | None:
    """'06-Nov-2026' or '06-NOV-2026' to '2026-11-06'; None when it is not a date."""
    try:
        return datetime.strptime((text or "").strip()[:11], "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def _rows(res: dict) -> list:
    return res["data"] if isinstance(res["data"], list) else []


def earnings() -> tuple[list[dict], dict]:
    res = nse("event-calendar?index=equities", key="calendar_events")
    out = [{"date": d, "symbol": r["symbol"], "company": r.get("company"), "detail": r.get("bm_desc") or r.get("purpose")}
           for r in _rows(res) if "result" in (r.get("purpose") or "").lower() and (d := _iso(r.get("date")))]
    return out, res


def corporate_actions(today: date) -> tuple[list[dict], dict]:
    span = f"from_date={(today - timedelta(days=30)).strftime('%d-%m-%Y')}&to_date={(today + timedelta(days=60)).strftime('%d-%m-%Y')}"
    res = nse(f"corporates-corporateActions?index=equities&{span}", key="calendar_actions")
    out = []
    for r in _rows(res):
        d = _iso(r.get("exDate"))
        if d:
            subject = r.get("subject") or ""
            kind = next((label for needle, label in ACTION_KINDS if needle in subject.lower()), "Other")
            out.append({"date": d, "symbol": r.get("symbol"), "company": r.get("comp"), "kind": kind, "detail": subject,
                        "record_date": _iso(r.get("recDate"))})
    return out, res


def ipos() -> tuple[list[dict], dict]:
    results = [nse("ipo-current-issue", key="calendar_ipo_current"), nse("all-upcoming-issues?category=ipo", key="calendar_ipo_upcoming"),
               nse("public-past-issues", ttl=6 * 3600, key="calendar_ipo_past")]
    seen, out = set(), []
    for res in results:
        for r in _rows(res):
            start, sym = _iso(r.get("ipoStartDate") or r.get("issueStartDate")), r.get("symbol")
            if start and (sym, start) not in seen:
                seen.add((sym, start))
                out.append({"date": start, "symbol": sym, "company": r.get("companyName") or r.get("company"),
                            "end": _iso(r.get("ipoEndDate") or r.get("issueEndDate")), "price": r.get("priceRange") or r.get("issuePrice"),
                            "board": r.get("securityType") or r.get("series"), "listing": _iso(r.get("listingDate"))})
    return out, next((x for x in results if x["error"]), results[0])


def _fno_expiries(instruments) -> list[dict]:
    out = {}
    for name, label in (("NIFTY", "Nifty 50"), ("BANKNIFTY", "Bank Nifty"), ("FINNIFTY", "FinNifty"), ("MIDCPNIFTY", "Midcap Select")):
        for r in instruments("NFO"):
            if r.get("name") == name and r.get("instrument_type") in ("CE", "PE") and r.get("expiry"):
                out[(r["expiry"].isoformat(), name)] = label
    return [{"date": d, "event": f"{label} options expiry", "group": "F&O expiry", "impact": 2} for (d, _), label in sorted(out.items())]


def _holidays() -> tuple[list[dict], dict]:
    res = nse("holiday-master?type=trading", ttl=7 * 86400, key="calendar_holidays")
    rows = (res["data"] or {}).get("CM", []) if isinstance(res["data"], dict) else []
    return [{"date": d, "event": f"Market holiday: {r.get('description')}", "group": "Holiday", "impact": 3}
            for r in rows if (d := _iso(r.get("tradingDate")))], res


def _flows() -> tuple[list[dict], dict]:
    res = nse("fiidiiTradeReact", ttl=1800, key="calendar_flows")
    out = []
    for r in _rows(res):
        d = _iso(r.get("date"))
        if d:
            out.append({"date": d, "event": f"{r.get('category')} net cash flow", "group": "Flows", "impact": 1,
                        "actual": f"₹{float(r.get('netValue') or 0):,.0f} Cr"})
    return out, res


def economic(instruments) -> tuple[list[dict], list[dict]]:
    rbi = [{"date": d, "event": "RBI policy decision", "group": "RBI", "impact": 3} for d in RBI_POLICY_DATES]
    holidays, hres = _holidays()
    flows, fres = _flows()
    return rbi + _fno_expiries(instruments) + holidays + flows, [hres, fres]


def build(instruments, today: date) -> dict:
    ern, ern_res = earnings()
    act, act_res = corporate_actions(today)
    ipo, ipo_res = ipos()
    eco, eco_res = economic(instruments)
    sources = {"earnings": ern_res, "dividends": act_res, "ipo": ipo_res, "economic": eco_res[0]}
    return {"economic": eco, "earnings": ern, "dividends": act, "ipo": ipo,
            "status": {k: {"fetched_at": v["fetched_at"], "stale": v["stale"], "error": v["error"]} for k, v in sources.items()}}
