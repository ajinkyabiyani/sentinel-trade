"""Expiry Range Agent for F&O stocks: the same weekly prediction as for the indices, for every stock with options.

Stock options expire monthly, so each stock's IV and straddle come from its monthly chain, scaled to the same weekly window that
ends on Nifty's weekly expiry (square root of time). OI walls are the monthly chain's. About 220 chains are read once a week in the
background at 09:30 on the first trading day of the cycle (a few minutes, paced), and settled after the expiry close.
The replay has no per-stock VIX, so a stock's implied volatility is approximated as its own 20-day realised volatility times the
day's market premium (India VIX over Nifty's 20-day realised volatility). Read only: nothing here trades.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime

from strategies import common, expiry_range as er, pipeline_swing as swing

log = logging.getLogger("kite-desk.expiry-range-stocks")
METHODS = er.METHODS
PACE = 0.8                    # seconds between chain reads: gentle enough for Kite's quote limit
RETRY_WAIT = 3.0              # seconds before asking again after a refusal
stock_source = None           # set by the route: () -> every F&O stock's name
_lock = threading.RLock()
_job: dict = {"running": False, "done": 0, "total": 0, "purpose": None, "started": None, "expiry": None}
_preview: dict = {"expiry": None, "rec": None}
_nifty: dict = {"at": 0.0, "chain": None}


def _file():
    return er.DIR / "stocks.json"


def records() -> list[dict]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    er.DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_file())


def strike_step(rows: list[dict]) -> float:
    ks = sorted({r["strike"] for r in rows or []})
    gaps = [b - a for a, b in zip(ks, ks[1:]) if b > a]
    return min(gaps) if gaps else 1.0


def nice_step(price: float) -> float:
    """A strike-like step for the replay: the largest common step under about 1% of the price."""
    return max([s for s in (0.5, 1, 2.5, 5, 10, 20, 50, 100, 250, 500) if s <= price * 0.01] or [0.5])


def _nifty_chain(chain_of=swing.fetch_chain) -> dict:
    if not _nifty["chain"] or time.time() - _nifty["at"] > 300:
        _nifty.update(at=time.time(), chain=chain_of("NIFTY", None))
    return _nifty["chain"]


# ---------------------------------------------------------------- building a week's predictions

def build(now: datetime, names: list[str], chain_of=swing.fetch_chain, pace: float = PACE) -> dict:
    nifty = chain_of("NIFTY", None)
    expiry, days = nifty["expiry"], er._expiry_days(nifty, now)
    rec = {"id": f"stocks|{expiry}", "kind": "stocks", "expiry": expiry, "made_at": now.isoformat(timespec="minutes"), "days": round(days, 2), "stocks": {}, "failed": []}
    _job.update(total=len(names), done=0)
    for sym in names:
        try:
            c = None
            for attempt in range(3):              # Kite answers "too many requests" when chains are read fast: wait and ask again
                try:
                    c = chain_of(sym, None)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    time.sleep(RETRY_WAIT * (attempt + 1) if pace else 0)
            spot = c.get("spot") or c.get("forward")
            if spot:
                own = er._expiry_days(c, now)
                rec["stocks"][sym] = {"spot": spot, "atm_iv": c.get("atm_iv"), "straddle": c.get("straddle"), "own_expiry": c.get("expiry"), "pcr": c.get("pcr"),
                                      "ranges": er.ranges(spot, c.get("atm_iv"), days, c.get("straddle"), own, c.get("support"), c.get("resistance"), strike_step(c.get("rows"))), "result": None}
            else:
                rec["failed"].append(sym)
        except Exception:  # noqa: BLE001 - one stock without a chain never stops the rest
            rec["failed"].append(sym)
        _job["done"] += 1
        if pace:
            time.sleep(pace)
    return rec


def _run(purpose: str, now: datetime, names: list[str], expiry: str) -> None:
    try:
        rec = build(now, names)
        if purpose == "record":
            with _lock:
                rows = [r for r in records() if r["expiry"] != rec["expiry"]]
                _save(rows + [rec])
        else:
            _preview.update(expiry=expiry, rec=rec)
    except Exception:  # noqa: BLE001 - shown as not built; tried again on the next pass
        log.exception("stock expiry ranges")
    finally:
        _job.update(running=False)


def start(purpose: str, now: datetime, expiry: str) -> bool:
    """Starts a background build ('record' saves it as the week's prediction, 'preview' only shows it). False if one is running."""
    with _lock:
        if _job["running"]:
            return False
        names = [n for n in (stock_source() if stock_source else []) if not n.startswith("NIFTY")]
        if not names:
            return False
        _job.update(running=True, purpose=purpose, started=now.isoformat(timespec="seconds"), expiry=expiry, done=0, total=len(names))
    threading.Thread(target=_run, args=(purpose, now, names, expiry), daemon=True, name=f"expiry-range-stocks-{purpose}").start()
    return True


# ---------------------------------------------------------------- settling

def _store_rows(sym: str) -> list[dict]:
    from terminal.market import store
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in store.candles.get(sym) or []]


def _daily(sym: str, expiry: str) -> list[dict]:
    rows = _store_rows(sym)
    if rows and rows[-1]["date"] >= expiry:
        return rows
    return swing._rows(er.india.chart(sym, "1mo", "1d"))       # the stored candles end before the expiry: ask Kite


def settle(now: datetime, daily=_daily) -> int:
    today, done = now.date().isoformat(), 0
    with _lock:
        rows = records()
        for r in rows:
            if r["expiry"] > today or (r["expiry"] == today and now.strftime("%H:%M") < er.SETTLE_AT):
                continue
            for sym, x in r["stocks"].items():
                if x.get("result"):
                    continue
                try:
                    candles = [c for c in daily(sym, r["expiry"]) if r["made_at"][:10] <= c["date"] <= r["expiry"]]
                except Exception:  # noqa: BLE001 - tried again on the next pass
                    continue
                last = next((c for c in candles if c["date"] == r["expiry"]), None)
                if not last:
                    continue
                hi, lo = max(c["high"] for c in candles), min(c["low"] for c in candles)
                x["result"] = {"close": last["close"], "high": hi, "low": lo, "by": {m: er.judge(rg, last["close"], hi, lo) for m, rg in x["ranges"].items()}}
                done += 1
        if done:
            _save(rows)
    return done


def tick(now: datetime) -> None:
    if not er.india.trading_day(now.date()):
        return
    t = now.strftime("%H:%M")
    if t >= er.SETTLE_AT:
        settle(now)
    if er.PREDICT_AT <= t <= "15:15" and not _job["running"]:
        expiry = _nifty_chain()["expiry"]
        if not any(r["expiry"] == expiry for r in records()):
            start("record", now, expiry)


# ---------------------------------------------------------------- replay

def replay(stocks: dict[str, list[dict]], nifty: list[dict], vix: list[dict]) -> dict[str, list[dict]]:
    """{symbol: [weekly results]}, each {"expiry", "spot", "iv", "ranges", "result"}: IV is the stock's 20-day realised vol times the market premium."""
    nb = {c["date"]: c for c in nifty}
    vx = {c["date"]: c["close"] for c in vix}
    days = sorted(set(nb) & set(vx))
    exp = er._expiry_dates(days)
    n_rv = {d: er._realised([nb[x]["close"] for x in days if x <= d]) for d in exp}
    out = {}
    for sym, rows in stocks.items():
        by = {c["date"]: c for c in rows}
        sdays = sorted(by)
        weeks = []
        for start, end in zip(exp, exp[1:]):
            if start not in by or end not in by or not n_rv.get(start):
                continue
            rv = er._realised([by[d]["close"] for d in sdays if d <= start])
            if not rv:
                continue
            iv = rv * vx[start] / n_rv[start]
            spot, cal = by[start]["close"], (date.fromisoformat(end) - date.fromisoformat(start)).days
            half, step = spot * iv / 100 * math.sqrt(cal / 365), nice_step(spot)
            rg = {"iv": dict(zip(("lo", "hi"), er.snap(spot - half, spot + half, step))),
                  "straddle": dict(zip(("lo", "hi"), er.snap(spot - er.STRADDLE_SIGMA * half, spot + er.STRADDLE_SIGMA * half, step)))}
            window = [by[d] for d in sdays if start < d <= end]
            close, hi, lo = by[end]["close"], max(c["high"] for c in window), min(c["low"] for c in window)
            weeks.append({"expiry": end, "spot": spot, "iv": round(iv, 1), "ranges": rg, "result": {"close": close, "by": {m: er.judge(x, close, hi, lo) for m, x in rg.items()}}})
        if weeks:
            out[sym] = weeks
    return out


_replay_cache: dict = {}


def replay_live(today: str) -> dict[str, list[dict]]:
    if _replay_cache.get("day") != today:
        from terminal.market import store
        conv = lambda rows: [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]} for r in rows or []]  # noqa: E731
        names = [n for n in (stock_source() if stock_source else []) if store.candles.get(n)]
        _replay_cache.update(day=today, data=replay({n: conv(store.candles[n]) for n in names}, conv(store.candles.get("NIFTY 50")), conv(store.candles.get("INDIA VIX"))))
    return _replay_cache["data"]


# ---------------------------------------------------------------- stats and the page

def _agg(results: list[tuple[dict, dict]]) -> list[dict]:
    """[(ranges, result_by)] -> per-method totals across every stock-week."""
    out = []
    for m, label in METHODS.items():
        res = [(rg[m], by[m]) for rg, by in results if m in by]
        if not res:
            continue
        won = sum(1 for _, x in res if x["won"])
        out.append({"method": m, "method_label": label, "weeks": len(res), "won": won, "win_rate": round(100 * won / len(res), 1),
                    "stayed_inside": round(100 * sum(1 for _, x in res if x["stayed_inside"]) / len(res), 1)})
    return out


def view() -> dict:
    now = datetime.now(common.IST)
    live = sorted(records(), key=lambda r: r["expiry"], reverse=True)
    try:
        past = replay_live(now.date().isoformat())
    except Exception:  # noqa: BLE001
        log.exception("stock replay")
        past = {}
    per = {}
    for sym, weeks in past.items():
        n = len(weeks)
        w = lambda m: round(100 * sum(1 for x in weeks if x["result"]["by"][m]["won"]) / n, 1)  # noqa: E731
        per[sym] = {"weeks": n, "iv_win": w("iv"), "straddle_win": w("straddle"), "avg_width_pct": round(sum((x["ranges"]["iv"]["hi"] - x["ranges"]["iv"]["lo"]) / x["spot"] * 100 for x in weeks) / n, 2)}
    current = next((r for r in live if any(not x.get("result") for x in r["stocks"].values())), None)
    expiry = None
    if not current:
        try:
            expiry = _nifty_chain()["expiry"]
            if _preview["expiry"] != expiry and not _job["running"]:
                start("preview", now, expiry)
        except Exception:  # noqa: BLE001 - no chain, no preview
            log.warning("stock preview not started")
    week = current or (_preview["rec"] if _preview["expiry"] == expiry else None)
    rows = []
    for sym, x in sorted((week or {}).get("stocks", {}).items()):
        rows.append({"symbol": sym, **{k: x.get(k) for k in ("spot", "atm_iv", "straddle", "own_expiry", "pcr", "ranges", "result")}, "replay": per.get(sym)})
    live_res = [(x["ranges"], x["result"]["by"]) for r in live for x in r["stocks"].values() if x.get("result")]
    past_res = [(w["ranges"], w["result"]["by"]) for weeks in past.values() for w in weeks]
    return {"week": None if not week else {k: week[k] for k in ("expiry", "made_at", "days")} | {"recorded": bool(current), "failed": week.get("failed", [])},
            "rows": rows, "job": dict(_job), "live_stats": _agg(live_res), "replay_stats": _agg(past_res), "replay_stocks": len(past),
            "replay_weeks": max((len(w) for w in past.values()), default=0), "live_weeks": len(live)}
