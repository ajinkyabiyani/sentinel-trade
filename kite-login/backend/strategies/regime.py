"""Adaptive market regime: every stock and index is read against its own history, then rolled into one market view.

Regimes (the user's quick-reference table). Each candle is checked against each regime's signature; the regime with
the largest share of its signature present wins (ties: trend, squeeze, choppy, range):
  Uptrend / Downtrend  ADX > 25; MAs fanned out (close, EMA20, EMA50 in order) with a real slope; RSI > 60 / < 40;
                       higher highs and higher lows / lower highs and lower lows.
  Range-bound          ADX < 20; MAs flat or entangled; RSI between 30 and 70; volatility normal for this instrument.
  Squeeze              Bollinger width and ATR low against this instrument's own year; ATR declining; inside bars or a
                       shrinking daily range.
  Choppy               ATR high against its own year; MAs flat; price repeatedly crossing the 20-day average; long
                       wicks and deep overlap between bars.
Adaptive: "high" and "low" volatility are ranked against the instrument's own last year, so a quiet large cap and a wild
small cap are judged fairly. A new regime must hold for two closes before it replaces the old one. Today's live price is
spliced in, so regimes move during the session.

Direction score -100..+100: slope of the 50-day EMA and distance from it (in ATRs), EMA20 vs EMA50, the 200-day average,
+DI vs -DI. View from the score: Bullish, Cautiously bullish, Neutral, Cautiously bearish, Bearish.

Market view: Nifty 50 35%, Nifty Bank 20%, Midcap 100 15%, Smallcap 100 15%, breadth of liquid stocks 15%, less a
penalty when India VIX is high against its own year. Describes conditions only; read-only, nothing here orders.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("kite-desk.regime")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"
MIN_BARS = 80
CONFIRM_BARS = 2
LOOKBACK = 250
ADX_TREND, ADX_RANGE = 25.0, 20.0
RSI_UP, RSI_DOWN, RSI_LO, RSI_HI = 60.0, 40.0, 30.0, 70.0
VOL_HIGH, VOL_LOW = 70.0, 30.0     # ATR rank in the instrument's own year
BB_LOW = 20.0                      # Bollinger width rank for a squeeze
FLAT_SLOPE = 0.6                   # EMA50 move over 10 days, in ATRs: below this the averages count as flat
CROSSES_CHOPPY = 4                 # closes crossing the 20-day average in the last 20 days
WICK_CHOPPY = 0.55                 # average share of the candle that is wick, last 10 days
WEIGHTS = {"NIFTY 50": 0.35, "NIFTY BANK": 0.20, "NIFTY MIDCAP 100": 0.15, "NIFTY SMLCAP 100": 0.15}
BREADTH_WEIGHT = 0.15

NOTES = {
    "Uptrend": "Directional momentum up: higher highs and higher lows, volatility moving with the trend.",
    "Downtrend": "Directional momentum down: lower highs and lower lows, volatility moving with the trend.",
    "Range-bound": "Consolidation: bouncing between support and resistance, price rejects the range extremes.",
    "Squeeze": "Low volatility compression: narrowing candles and shrinking range, which often precedes a sharp breakout.",
    "Choppy": "Volatile whipsaw: irregular swings and long wicks, stop-losses get hit on both long and short sides.",
}


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _ema(v: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for i, x in enumerate(v):
        out.append(x if i == 0 else x * k + out[-1] * (1 - k))
    return out


def _wilder(v: list[float], n: int) -> list[float]:
    out, prev = [], None
    for i, x in enumerate(v):
        if i < n:
            prev = (prev or 0.0) + x
            out.append(prev / (i + 1))
            if i == n - 1:
                prev = prev / n
        else:
            prev = (prev * (n - 1) + x) / n
            out.append(prev)
    return out


def _pct_rank(hist: list[float], x: float) -> float:
    return 100.0 * sum(1 for h in hist if h <= x) / len(hist) if hist else 50.0


def _quantile(hist: list[float], q: float) -> float:
    s = sorted(hist)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def view_of(score: float) -> tuple[str, str]:
    if score >= 40:
        return "Bullish", "up"
    if score >= 15:
        return "Cautiously bullish", "up"
    if score > -15:
        return "Neutral", "amber"
    if score > -40:
        return "Cautiously bearish", "down"
    return "Bearish", "down"


def _rsi(c: list[float], n: int = 14) -> list[float]:
    up = _wilder([0.0] + [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))], n)
    dn = _wilder([0.0] + [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))], n)
    return [100.0 if d == 0 else 100 - 100 / (1 + u / d) for u, d in zip(up, dn)]


def classify(rows: list) -> dict | None:
    """rows: daily candles oldest first, [date, open, high, low, close, ...]. None when there is too little history."""
    rows = [r for r in rows if r[1] and r[2] and r[3] and r[4]]
    if len(rows) < MIN_BARS:
        return None
    dates = [str(r[0])[:10] for r in rows]
    o, h, l, c = ([float(r[k]) for r in rows] for k in (1, 2, 3, 4))
    n = len(c)
    e20, e50 = _ema(c, 20), _ema(c, 50)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    pdm = [0.0] + [max(h[i] - h[i - 1], 0.0) if h[i] - h[i - 1] > l[i - 1] - l[i] else 0.0 for i in range(1, n)]
    mdm = [0.0] + [max(l[i - 1] - l[i], 0.0) if l[i - 1] - l[i] > h[i] - h[i - 1] else 0.0 for i in range(1, n)]
    atr, sp, sm = _wilder(tr, 14), _wilder(pdm, 14), _wilder(mdm, 14)
    pdi = [100 * p / a if a else 0.0 for p, a in zip(sp, atr)]
    mdi = [100 * m / a if a else 0.0 for m, a in zip(sm, atr)]
    dx = [100 * abs(p - m) / (p + m) if p + m else 0.0 for p, m in zip(pdi, mdi)]
    adx = _wilder(dx, 14)
    rsi = _rsi(c)
    atrp = [a / x if x else 0.0 for a, x in zip(atr, c)]
    bbw = [0.0] * n                    # Bollinger width: 4 standard deviations of 20 closes, as a share of their mean
    for i in range(19, n):
        w = c[i - 19:i + 1]
        m = sum(w) / 20
        bbw[i] = 4 * (sum((x - m) ** 2 for x in w) / 20) ** 0.5 / m if m else 0.0

    def at(i: int) -> dict:
        lo = max(30, i - LOOKBACK)
        a = atr[i] or 1e-9
        vol, bb = _pct_rank(atrp[lo:i + 1], atrp[i]), _pct_rank(bbw[lo:i + 1], bbw[i])
        sma200 = sum(c[i - 199:i + 1]) / 200 if i >= 199 else None
        slope, dist = (e50[i] - e50[i - 10]) / a, (c[i] - e50[i]) / a
        score = (35 * _clip(slope / 2.5) + 25 * _clip(dist / 3) + 15 * (1 if e20[i] > e50[i] else -1)
                 + (0 if sma200 is None else 15 * (1 if c[i] > sma200 else -1)) + 10 * _clip((pdi[i] - mdi[i]) / 25))
        hh, hl = max(h[i - 9:i + 1]) > max(h[i - 19:i - 9]), min(l[i - 9:i + 1]) > min(l[i - 19:i - 9])
        lh, ll = max(h[i - 9:i + 1]) < max(h[i - 19:i - 9]), min(l[i - 9:i + 1]) < min(l[i - 19:i - 9])
        flat = abs(slope) < FLAT_SLOPE
        entangled = abs(e20[i] - e50[i]) < a
        crosses = sum((c[k] > e20[k]) != (c[k - 1] > e20[k - 1]) for k in range(i - 19, i + 1))
        wicks = [1 - abs(c[k] - o[k]) / (h[k] - l[k]) for k in range(i - 9, i + 1) if h[k] > l[k]]
        wick = sum(wicks) / len(wicks) if wicks else 0.0
        inside = sum(h[k] <= h[k - 1] and l[k] >= l[k - 1] for k in range(i - 4, i + 1))
        declining = atr[i] < atr[i - 5]
        shrinking = sum(tr[i - 4:i + 1]) < sum(tr[i - 9:i - 4])
        sig = {
            "Uptrend": [(adx[i] > ADX_TREND, f"ADX {adx[i]:.0f} (needs above 25)"),
                        (c[i] > e20[i] > e50[i], "Close, EMA20 and EMA50 fanned out upward"),
                        (slope >= FLAT_SLOPE, f"EMA50 sloping up ({slope:+.1f} ATR in 10 days)"),
                        (rsi[i] > RSI_UP, f"RSI {rsi[i]:.0f} (needs above 60)"), (hh and hl, "Higher highs and higher lows")],
            "Downtrend": [(adx[i] > ADX_TREND, f"ADX {adx[i]:.0f} (needs above 25)"),
                          (c[i] < e20[i] < e50[i], "Close, EMA20 and EMA50 fanned out downward"),
                          (slope <= -FLAT_SLOPE, f"EMA50 sloping down ({slope:+.1f} ATR in 10 days)"),
                          (rsi[i] < RSI_DOWN, f"RSI {rsi[i]:.0f} (needs below 40)"), (lh and ll, "Lower highs and lower lows")],
            "Range-bound": [(adx[i] < ADX_RANGE, f"ADX {adx[i]:.0f} (needs below 20)"), (flat and entangled, "Moving averages flat and entangled"),
                            (RSI_LO < rsi[i] < RSI_HI, f"RSI {rsi[i]:.0f} (needs 30 to 70)"),
                            (VOL_LOW < vol < VOL_HIGH, f"Normal volatility: ATR at percentile {vol:.0f} of its year")],
            "Squeeze": [(bb <= BB_LOW, f"Bollinger width at percentile {bb:.0f} of its year (needs ≤ 20)"),
                        (vol <= VOL_LOW, f"ATR at percentile {vol:.0f} of its year (needs ≤ 30)"), (declining, "ATR declining"),
                        (inside >= 2 or shrinking, "Inside bars or a shrinking daily range")],
            "Choppy": [(vol >= VOL_HIGH, f"ATR at percentile {vol:.0f} of its year (needs ≥ 70)"), (flat, "Moving averages flat"),
                       (crosses >= CROSSES_CHOPPY, f"Crossed the 20-day average {crosses} times in 20 days (needs 4+)"),
                       (wick >= WICK_CHOPPY, f"Wicks are {wick * 100:.0f}% of the candle (needs 55%+)")],
        }
        order = ["Uptrend", "Downtrend", "Squeeze", "Choppy", "Range-bound"]    # ties go to the earlier regime
        # each regime has one defining reading it cannot win without; with none qualifying there is no clear regime
        must = {"Uptrend": sig["Uptrend"][1][0], "Downtrend": sig["Downtrend"][1][0], "Squeeze": bb <= BB_LOW,
                "Choppy": vol >= VOL_HIGH, "Range-bound": adx[i] < ADX_TREND}
        fit = {k: sum(ok for ok, _ in v) / len(v) if must[k] else 0.0 for k, v in sig.items()}
        label = max(order, key=lambda k: fit[k]) if max(fit.values()) > 0 else "Range-bound"
        return {"label": label, "score": score, "sig": sig, "fit": fit, "vol": vol, "bb": bb, "sma200": sma200}

    # walk the recent bars: the regime changes after two closes in a row where it is no longer the best match, and
    # its age is known
    start = max(60, n - 40)
    cur, since, miss = at(start)["label"], start, 0
    now = None
    for i in range(start, n):
        now = at(i)
        miss = miss + 1 if now["label"] != cur else 0
        if miss >= CONFIRM_BARS:
            cur, since, miss = now["label"], i, 0
    score = round(now["score"])
    view, tone = view_of(score)
    return {"regime": cur, "forming": now["label"] if now["label"] != cur else None, "fit": round(100 * sum(ok for ok, _ in now["sig"][cur]) / len(now["sig"][cur])),
            "score": score, "view": view, "tone": tone, "since": dates[since], "days": n - since,
            "adx": round(adx[-1], 1), "rsi": round(rsi[-1], 1), "vol_pct": round(now["vol"]), "bb_pct": round(now["bb"]),
            "atr_pct": round(atrp[-1] * 100, 2), "signs": [[bool(ok), text] for ok, text in now["sig"][cur]],
            "note": NOTES[cur], "close": round(c[-1], 2)}


def splice(rows: list, today: str, o, h, l, last) -> list:
    """Daily candles with today's candle updated from a live quote."""
    if not last:
        return rows
    if rows and str(rows[-1][0])[:10] == today:
        r = list(rows[-1])
        r[2], r[3], r[4] = max(r[2], last), min(r[3], last), last
        return [*rows[:-1], r]
    if rows and str(rows[-1][0])[:10] > today:
        return rows
    return [*rows, [today, o or last, max(h or last, last), min(l or last, last), last, 0]] if h else rows


def market_view(indices: dict[str, dict], breadth: dict | None, vix: dict | None) -> dict:
    """indices: name -> classify() result. breadth: {bull_pct, bear_pct, total, ...}. vix: {last, pct}."""
    parts, reasons = [], []
    total_w = 0.0
    for name, w in WEIGHTS.items():
        r = indices.get(name)
        if r:
            parts.append(w * r["score"])
            total_w += w
    if breadth and breadth.get("total"):
        parts.append(BREADTH_WEIGHT * (breadth["bull_pct"] - breadth["bear_pct"]))
        total_w += BREADTH_WEIGHT
    if not total_w:
        return {"view": "–", "tone": "", "score": None, "reasons": [], "summary": "needs a Kite session"}
    score = sum(parts) / total_w
    penalty = 0.0
    if vix and vix.get("pct") is not None and vix["pct"] >= 80:
        penalty = 10.0 if vix["pct"] < 95 else 20.0
        score -= penalty
    score = round(max(-100.0, min(100.0, score)))
    view, tone = view_of(score)
    for name, label in (("NIFTY 50", "Nifty"), ("NIFTY BANK", "Bank"), ("NIFTY MIDCAP 100", "Midcap"), ("NIFTY SMLCAP 100", "Smallcap")):
        r = indices.get(name)
        if r:
            reasons.append(f"{label}: {r['regime']} ({r['score']:+d})")
    if breadth and breadth.get("total"):
        reasons.append(f"Breadth: {breadth['bull_pct']:.0f}% of {breadth['total']} liquid stocks bullish, {breadth['bear_pct']:.0f}% bearish")
    if vix and vix.get("last") is not None:
        reasons.append(f"India VIX {vix['last']:.1f}: percentile {vix['pct']:.0f} of its year" + (f" (−{penalty:.0f} points)" if penalty else ""))
    n = indices.get("NIFTY 50")
    summary = f"Nifty {n['regime'].lower()} · " if n else ""
    if breadth and breadth.get("total"):
        summary += f"{breadth['bull_pct']:.0f}% of stocks bullish"
    elif vix and vix.get("last") is not None:
        summary += f"VIX {vix['last']:.1f}"
    return {"view": view, "tone": tone, "score": score, "reasons": reasons, "summary": summary.rstrip(" ·")}


# ---------------- stock regimes on disk (nightly from the sweep, refreshed hourly in market hours) ----------------
def _file(day: str) -> Path:
    return CACHE_DIR / f"regime_{day}.json"


def save(day: str, stocks: list[dict], partial: bool = False) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = _file(day).with_suffix(".tmp")
    tmp.write_text(json.dumps({"day": day, "built": datetime.now(IST).isoformat(timespec="seconds"), "partial": partial,
                               "stocks": stocks}), encoding="utf-8")
    tmp.replace(_file(day))
    for old in sorted(CACHE_DIR.glob("regime_*.json"))[:-5]:
        old.unlink(missing_ok=True)


def latest() -> dict | None:
    for f in reversed(sorted(CACHE_DIR.glob("regime_*.json"))):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def breadth_of(stocks: list[dict]) -> dict:
    pool = [s for s in stocks if s.get("liquid", True)]
    n = len(pool)
    if not n:
        return {"total": 0, "bull_pct": 0.0, "bear_pct": 0.0, "regimes": {}}
    regimes: dict[str, int] = {}
    for s in pool:
        regimes[s["regime"]] = regimes.get(s["regime"], 0) + 1
    bull, bear = sum(s["score"] >= 15 for s in pool), sum(s["score"] <= -15 for s in pool)
    return {"total": n, "bull_pct": round(bull / n * 100, 1), "bear_pct": round(bear / n * 100, 1), "regimes": regimes}


def to_rows(kite_rows: list[dict]) -> list[list]:
    return [[r["date"], r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0] for r in kite_rows]


class Refresher:
    """Regimes for the liquid stocks from their daily candles (about 6-10 minutes at a gentle pace)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0, "error": ""}

    def run(self, kite, symbols: list[dict], day: str, pace: float = 0.5) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.progress = {"running": True, "done": 0, "total": len(symbols), "error": ""}
            end = datetime.fromisoformat(day).date()
            out = []
            for i, s in enumerate(symbols):
                rows = []
                for attempt in range(4):
                    try:
                        rows = kite.historical_data(s["token"], end - timedelta(days=380), end, "day")
                        break
                    except Exception as exc:  # noqa: BLE001 - rate limit / network
                        if "Too many" in str(exc) and attempt < 3:
                            time.sleep(1.5)
                            continue
                        if type(exc).__name__ == "TokenException":
                            raise
                        break
                time.sleep(pace)
                r = classify(to_rows(rows))
                if r:
                    out.append({"symbol": s["symbol"], "name": s.get("name"), "liquid": True, "rs": s.get("rs"), **slim(r)})
                self.progress["done"] = i + 1
            if out:
                save(day, out, partial=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("regime refresh failed")
            self.progress["error"] = str(exc)
        finally:
            self.progress["running"] = False
            self.lock.release()


def slim(r: dict) -> dict:
    return {k: r[k] for k in ("regime", "forming", "fit", "score", "view", "tone", "days", "since", "adx", "rsi", "vol_pct", "close")}


refresher = Refresher()
