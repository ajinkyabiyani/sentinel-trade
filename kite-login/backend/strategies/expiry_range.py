"""Expiry Range Agent: each week it predicts where Nifty and Bank Nifty will close on Nifty's weekly expiry, then checks itself.

Once per weekly cycle (the first look after 09:30 on a trading day after the previous expiry) it reads the live option chain and
records three ranges for the close on the coming Nifty expiry day:
  IV range        spot +/- one standard deviation from the ATM implied volatility over the time left (about 68% inside in theory)
  Straddle range  spot +/- the ATM straddle price: the market's own price for the move
  OI walls        the biggest put open interest below spot to the biggest call open interest above it
Bank Nifty has only a monthly expiry, so its IV and straddle are scaled to the same weekly window (square root of time).
After 15:30 on expiry day each range is settled: a win when the close is inside it. Whether price went outside during the week is
recorded too. A one-year replay on daily candles (India VIX as the volatility, since old option chains aren't stored) gives a win
rate from day one; it's kept apart from live results. Read only: nothing here trades.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date, datetime, timedelta

from strategies import common, multi_agent_team as mat, paper_desk, pipeline_swing as swing

india = mat.india
log = logging.getLogger("kite-desk.expiry-range")
DIR = paper_desk.CACHE_DIR / "expiry_range"
INDICES = {"NIFTY": {"label": "Nifty 50", "step": 50, "chart": "NIFTY"}, "BANKNIFTY": {"label": "Bank Nifty", "step": 100, "chart": "BANKNIFTY"}}
METHODS = {"iv": "IV range (1σ)", "straddle": "Straddle range", "oi": "OI walls"}
PREDICT_AT, SETTLE_AT = "09:30", "15:31"
STRADDLE_SIGMA = 0.798         # an ATM straddle is worth about 0.8 standard deviations of the move to expiry
WEEKLY_SWITCH = date(2025, 9, 1)   # Nifty's weekly expiry moved from Thursday to Tuesday
_lock = threading.RLock()


# ---------------------------------------------------------------- records

def _file():
    return DIR / "predictions.json"


def records() -> list[dict]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_file())


# ---------------------------------------------------------------- the ranges

def snap(lo: float, hi: float, step: int) -> tuple[float, float]:
    """Outward to whole strikes."""
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def ranges(spot: float, iv_pct: float | None, days: float, straddle: float | None, straddle_days: float | None, support, resistance, step: int) -> dict:
    """The three ranges for a close `days` calendar days ahead. straddle_days: the days left on the contract the straddle belongs to."""
    out = {}
    if iv_pct and days > 0:
        half = spot * iv_pct / 100 * math.sqrt(days / 365)
        out["iv"] = dict(zip(("lo", "hi"), snap(spot - half, spot + half, step)))
    if straddle and straddle_days and days > 0:
        half = straddle * math.sqrt(min(days, straddle_days) / straddle_days)
        out["straddle"] = dict(zip(("lo", "hi"), snap(spot - half, spot + half, step)))
    if support and resistance and support < spot < resistance:
        out["oi"] = {"lo": float(support), "hi": float(resistance)}
    return out


def judge(rng: dict, close: float, hi: float, lo: float) -> dict:
    return {"won": rng["lo"] <= close <= rng["hi"], "stayed_inside": rng["lo"] <= lo and hi <= rng["hi"],
            "miss_by": 0.0 if rng["lo"] <= close <= rng["hi"] else round(min(abs(close - rng["lo"]), abs(close - rng["hi"])), 2)}


# ---------------------------------------------------------------- live cycle

def _expiry_days(chain: dict, now: datetime) -> float:
    """Calendar days from now to the 15:30 close on the chain's expiry."""
    end = datetime.fromisoformat(f"{chain['expiry']}T15:30").replace(tzinfo=common.IST)
    return max((end - now).total_seconds() / 86400, 0.01)


def predict(now: datetime, chain_of=swing.fetch_chain) -> dict | None:
    """This cycle's predictions, if not made yet. The cycle is Nifty's nearest weekly expiry."""
    nifty = chain_of("NIFTY", None)
    with _lock:
        if any(r["expiry"] == nifty["expiry"] and r["kind"] == "live" for r in records()):
            return None
    rec = build(now, chain_of, nifty)
    with _lock:
        _save(records() + [rec])
    return rec


def build(now: datetime, chain_of=swing.fetch_chain, nifty: dict | None = None) -> dict:
    """The three ranges for each index from the live chains, without recording them."""
    nifty = nifty or chain_of("NIFTY", None)
    expiry = nifty["expiry"]
    days = _expiry_days(nifty, now)
    rec = {"id": f"live|{expiry}", "kind": "live", "expiry": expiry, "made_at": now.isoformat(timespec="minutes"), "days": round(days, 2), "indices": {}}
    for sym, meta in INDICES.items():
        c = nifty if sym == "NIFTY" else chain_of(sym, None)
        own = days if sym == "NIFTY" else _expiry_days(c, now)
        spot = c.get("spot") or c.get("forward")
        rec["indices"][sym] = {"spot": spot, "atm_iv": c.get("atm_iv"), "straddle": c.get("straddle"), "own_expiry": c.get("expiry"), "pcr": c.get("pcr"), "max_pain": c.get("max_pain"),
                               "ranges": ranges(spot, c.get("atm_iv"), days, c.get("straddle"), own, c.get("support"), c.get("resistance"), meta["step"]), "result": None}
    return rec


def _daily(sym: str) -> list[dict]:
    return swing._rows(india.chart(INDICES[sym]["chart"], "1mo", "1d"))


def settle(now: datetime, daily=_daily) -> int:
    """Settles every live cycle whose expiry close has happened. -> how many indices were settled."""
    today = now.date().isoformat()
    done = 0
    with _lock:
        rows = records()
        for r in rows:
            if r["kind"] != "live" or r["expiry"] > today or (r["expiry"] == today and now.strftime("%H:%M") < SETTLE_AT):
                continue
            for sym, x in r["indices"].items():
                if x.get("result"):
                    continue
                candles = [c for c in daily(sym) if r["made_at"][:10] <= c["date"] <= r["expiry"]]
                last = next((c for c in candles if c["date"] == r["expiry"]), None)
                if not last:
                    continue
                hi, lo = max(c["high"] for c in candles), min(c["low"] for c in candles)
                x["result"] = {"close": last["close"], "high": hi, "low": lo, "by": {m: judge(rg, last["close"], hi, lo) for m, rg in x["ranges"].items()}}
                done += 1
        if done:
            _save(rows)
    return done


def tick(now: datetime) -> None:
    if not india.trading_day(now.date()):
        return
    t = now.strftime("%H:%M")
    if t >= SETTLE_AT:
        settle(now)
    if PREDICT_AT <= t <= "15:15":
        predict(now)


# ---------------------------------------------------------------- one-year replay

def _expiry_dates(days: list[str]) -> list[str]:
    """The weekly expiry trading days in the history: Thursday before the switch, Tuesday after, moved earlier over holidays."""
    have = set(days)
    out = []
    d = date.fromisoformat(days[0])
    end = date.fromisoformat(days[-1])
    while d <= end:
        weekday = 3 if d < WEEKLY_SWITCH else 1
        if d.weekday() == weekday:
            x = d
            while x.isoformat() not in have and x > d - timedelta(days=4):
                x -= timedelta(days=1)
            if x.isoformat() in have and (not out or out[-1] != x.isoformat()):
                out.append(x.isoformat())
        d += timedelta(days=1)
    return out


def _realised(closes: list[float], n: int = 20) -> float | None:
    if len(closes) <= n:
        return None
    r = [math.log(b / a) for a, b in zip(closes[-n - 1:], closes[-n:])]
    m = sum(r) / n
    return math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1)) * math.sqrt(252) * 100


def replay(nifty: list[dict], bank: list[dict], vix: list[dict]) -> list[dict]:
    """Every past weekly cycle: from one expiry's close to the next, IV from India VIX (Bank Nifty's scaled by its realised vol against Nifty's)."""
    by = {s: {c["date"]: c for c in rows} for s, rows in (("NIFTY", nifty), ("BANKNIFTY", bank))}
    vx = {c["date"]: c["close"] for c in vix}
    days = sorted(set(by["NIFTY"]) & set(by["BANKNIFTY"]) & set(vx))
    exp = _expiry_dates(days)
    out = []
    for start, end in zip(exp, exp[1:]):
        window = [d for d in days if start < d <= end]
        if not window:
            continue
        cal = (date.fromisoformat(end) - date.fromisoformat(start)).days
        rec = {"id": f"replay|{end}", "kind": "replay", "expiry": end, "made_at": f"{start}T15:30", "days": cal, "indices": {}}
        for sym, meta in INDICES.items():
            spot = by[sym][start]["close"]
            iv = vx[start]
            if sym == "BANKNIFTY":
                hist = [d for d in days if d <= start]
                rn = _realised([by["NIFTY"][d]["close"] for d in hist])
                rb = _realised([by["BANKNIFTY"][d]["close"] for d in hist])
                iv = iv * (rb / rn) if rn and rb else iv
            sigma_half = spot * iv / 100 * math.sqrt(cal / 365)
            rg = {"iv": dict(zip(("lo", "hi"), snap(spot - sigma_half, spot + sigma_half, meta["step"]))),
                  "straddle": dict(zip(("lo", "hi"), snap(spot - STRADDLE_SIGMA * sigma_half, spot + STRADDLE_SIGMA * sigma_half, meta["step"])))}
            close = by[sym][end]["close"]
            hi, lo = max(by[sym][d]["high"] for d in window), min(by[sym][d]["low"] for d in window)
            rec["indices"][sym] = {"spot": spot, "atm_iv": round(iv, 2), "ranges": rg, "result": {"close": close, "high": hi, "low": lo, "by": {m: judge(x, close, hi, lo) for m, x in rg.items()}}}
        out.append(rec)
    return out


_replay_cache: dict = {}


def replay_live(today: str) -> list[dict]:
    """The replay on Kite's last year of daily candles, worked out once a day."""
    if _replay_cache.get("day") != today:
        rows = lambda s: swing._rows(india.chart(s, "1y", "1d"))  # noqa: E731
        _replay_cache.update(day=today, rows=replay(rows("NIFTY"), rows("BANKNIFTY"), rows("INDIAVIX")))
    return _replay_cache["rows"]


# ---------------------------------------------------------------- stats and the page

def stats(rows: list[dict]) -> list[dict]:
    """Win rate per index and method over settled cycles."""
    out = []
    for sym, meta in INDICES.items():
        for m, label in METHODS.items():
            got = [r["indices"][sym] for r in rows if sym in r["indices"] and (r["indices"][sym].get("result") or {}).get("by", {}).get(m)]
            if not got:
                continue
            res = [x["result"]["by"][m] for x in got]
            widths = [(x["ranges"][m]["hi"] - x["ranges"][m]["lo"]) / x["spot"] * 100 for x in got]
            won = sum(1 for x in res if x["won"])
            out.append({"index": sym, "label": meta["label"], "method": m, "method_label": label, "weeks": len(res), "won": won, "win_rate": round(100 * won / len(res), 1),
                        "stayed_inside": round(100 * sum(1 for x in res if x["stayed_inside"]) / len(res), 1), "avg_width_pct": round(sum(widths) / len(widths), 2),
                        "avg_miss": round(sum(x["miss_by"] for x in res if not x["won"]) / max(1, len(res) - won), 1)})
    return out


def view() -> dict:
    now = datetime.now(common.IST)
    live = sorted((r for r in records() if r["kind"] == "live"), key=lambda r: r["expiry"], reverse=True)
    try:
        past = replay_live(now.date().isoformat())
        replay_error = None
    except Exception as exc:  # noqa: BLE001 - the page still shows the live record
        log.exception("expiry range replay")
        past, replay_error = [], str(exc)
    current = next((r for r in live if not all((x.get("result") for x in r["indices"].values()))), None)
    preview = None
    if not current:
        try:
            preview = build(now)                       # what the agent would predict now; recorded only at its next scheduled run
        except Exception:  # noqa: BLE001 - no chain, no preview
            log.warning("expiry range preview failed")
    return {"now": now.isoformat(timespec="seconds"), "methods": METHODS, "indices": {k: v["label"] for k, v in INDICES.items()}, "current": current, "preview": preview,
            "live": live, "live_stats": stats(live), "replay": past[::-1], "replay_stats": stats(past), "replay_error": replay_error,
            "schedule": {"predict_at": PREDICT_AT, "settle_at": SETTLE_AT}}
