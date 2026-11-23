"""Quant Desk intraday (MIS) strategies and their walk-forward test, on the 5-minute store.

Regime: each stock's confirmed daily regime as of the previous day's close (quant.indicators), so a day's trades never use
that day's close. Every trade is long the stock in the cash market (MIS), except the down-regime rule, which stands for a put
and is an estimate from the stock's move (not promotable). All positions close by 15:15 (the 15:10 candle's close).
Fills: the trigger candle's close plus 0.05% slippage; a stop or target touched inside a later candle fills at that level
(a candle touching both counts as the stop); gaps don't occur intraday.
Book: Rs 1,00,000, Rs 5,000 risk a trade, MIS margin of 20% (5x), no new entry once the day's realised loss reaches Rs 10,000.
Charges: quant.costs.intraday (brokerage, STT, exchange, SEBI, stamp, GST).
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime

from quant import costs, data, engine, intraday_data
from quant.strategies import Series
from strategies import common

OPEN, LAST_ENTRY, FLAT = 555, 870, 910          # 09:15, 14:30, the 15:10 candle
MARGIN = 0.2
OUT = common.CACHE_DIR / "quant" / "walkforward_intraday.json"

STRATEGIES = {
    "orb_long": {"regime": "TRENDING_UP", "side": "long",
                 "thesis": "In a stock in a confirmed uptrend, buy the first 5-minute close above the opening range; target a multiple of the risk.",
                 "edge": "Opening-range breakouts in trending names catch the day's institutional flow; traders fading the open pay.",
                 "rules": "Opening range: first m minutes. Entry: first close above its high, by 14:30. Stop: range midpoint. Target: R × risk. Out at 15:15.",
                 "grid": [{"m": m, "R": r} for m in (15, 30) for r in (1.5, 2.0, 3.0)]},
    "vwap_pullback": {"regime": "TRENDING_UP", "side": "long",
                      "thesis": "In an uptrend, after 10:00, buy a candle that dips to VWAP and closes back above it.",
                      "edge": "VWAP is where intraday institutions benchmark fills; in an uptrend they defend it, and dip sellers pay.",
                      "rules": "Entry: after 10:00, the first candle whose low touches VWAP and closes above VWAP and its open, after 3 candles above VWAP. Stop: that candle's low − 0.1%. Target: R × risk. Out at 15:15.",
                      "grid": [{"R": r} for r in (1.5, 2.0, 3.0)]},
    "vwap_revert": {"regime": "FLAT", "side": "long",
                    "thesis": "In a range-bound stock, buy a stretch of d% below VWAP after 10:00 and sell back at VWAP.",
                    "edge": "Without a trend, intraday stretches from VWAP tend to snap back; impatient sellers pay.",
                    "rules": "Entry: after 10:00, first close more than d% below VWAP. Target: VWAP at entry. Stop: s × the distance to VWAP below entry. Out at 15:15.",
                    "grid": [{"d": d, "s": s} for d in (0.6, 1.0, 1.4) for s in (1.0, 1.5)]},
    "gap_fade": {"regime": "FLAT", "side": "long",
                 "thesis": "In a range-bound stock that opens g% below yesterday's close, buy at 09:20 and target the previous close.",
                 "edge": "Overnight gaps in stocks with no trend are often overreactions that fill; panic sellers at the open pay.",
                 "rules": "Entry: the first candle's close when the open is more than g% below the previous close. Target: the previous close. Stop: the first candle's low − 0.1%. Out at 15:15.",
                 "grid": [{"g": g} for g in (1.0, 1.5, 2.0)]},
    "orb_put": {"regime": "TRENDING_DOWN", "side": "short",
                "thesis": "In a stock in a confirmed downtrend, buy a put on the first 5-minute close below the opening range.",
                "edge": "Opening-range breakdowns in falling names ride the day's selling; dip buyers at the open pay.",
                "rules": "Opening range: first m minutes. Entry: first close below its low, by 14:30. Stop: range midpoint. Target: R × risk. Out at 15:15. Results use the stock's move as a stand-in for the put.",
                "grid": [{"m": m, "R": r} for m in (15, 30) for r in (1.5, 2.0, 3.0)]},
}


def _follow(bars, i, entry, stop, target, side):
    """(exit price, minute, reason) from the candle after i."""
    for b in bars[i + 1:]:
        if b[0] > FLAT:
            break
        if side == "long":
            if b[3] <= stop:
                return stop, b[0], "stop"
            if target is not None and b[2] >= target:
                return target, b[0], "target"
        else:
            if b[2] >= stop:
                return stop, b[0], "stop"
            if target is not None and b[3] <= target:
                return target, b[0], "target"
    last = next((b for b in reversed(bars) if b[0] <= FLAT), bars[-1])
    return last[4], last[0], "15:15 exit"


def _vwap(bars):
    pv = v = 0.0
    out = []
    for b in bars:
        tp = (b[2] + b[3] + b[4]) / 3
        vol = b[5] or 1
        pv, v = pv + tp * vol, v + vol
        out.append(pv / v)
    return out


def day_trade(name, p, bars, prev_close):
    """The rule's one trade on one stock on one day, or None."""
    if not bars or bars[0][0] != OPEN or len(bars) < 30:
        return None
    if name in ("orb_long", "orb_put"):
        n = p["m"] // 5
        hi, lo = max(b[2] for b in bars[:n]), min(b[3] for b in bars[:n])
        mid = (hi + lo) / 2
        for i in range(n, len(bars)):
            b = bars[i]
            if b[0] > LAST_ENTRY:
                return None
            if name == "orb_long" and b[4] > hi:
                e = b[4]
                risk = e - mid
                return (i, e, mid, e + p["R"] * risk, "long") if risk > e * 0.0005 else None
            if name == "orb_put" and b[4] < lo:
                e = b[4]
                risk = mid - e
                return (i, e, mid, e - p["R"] * risk, "short") if risk > e * 0.0005 else None
        return None
    vw = _vwap(bars)
    if name == "vwap_pullback":
        above = 0
        for i, b in enumerate(bars):
            if b[0] > LAST_ENTRY:
                return None
            if b[0] >= 600 and above >= 3 and b[3] <= vw[i] < b[4] and b[4] > b[1]:
                e, stop = b[4], b[3] * 0.999
                risk = e - stop
                return (i, e, stop, e + p["R"] * risk, "long") if risk > e * 0.0005 else None
            above = above + 1 if b[3] > vw[i] else 0
        return None
    if name == "vwap_revert":
        for i, b in enumerate(bars):
            if b[0] > LAST_ENTRY:
                return None
            if b[0] >= 600 and b[4] < vw[i] * (1 - p["d"] / 100):
                e = b[4]
                gap = vw[i] - e
                return (i, e, e - p["s"] * gap, vw[i], "long")
        return None
    if name == "gap_fade":
        b = bars[0]
        if prev_close and b[1] < prev_close * (1 - p["g"] / 100) and b[4] < prev_close:
            e, stop = b[4], b[3] * 0.999
            return (0, e, stop, prev_close, "long") if e - stop > e * 0.0005 else None
        return None
    return None


def candidates(name, p, store, daily):
    """Every day's trade for every stock, before the book's limits."""
    st = STRATEGIES[name]
    out = []
    for sym, days in store.items():
        if sym.startswith("NIFTY"):
            continue                              # indices can't be bought in the cash market
        s = daily.get(sym)
        if not s:
            continue
        di = {d: i for i, d in enumerate(s.d)}
        for d, bars in days.items():
            i = di.get(d)
            if not i or s.regime[i - 1] != st["regime"]:
                continue
            got = day_trade(name, p, bars, s.c[i - 1])
            if not got:
                continue
            k, e, stop, target, side = got
            x, at, why = _follow(bars, k, e, stop, target, side)
            out.append({"sym": sym, "day": d, "entry_min": bars[k][0], "exit_min": at, "entry": e, "stop": stop, "target": target, "exit": x, "side": side,
                        "reason": why, "params": p, "turnover": bars[k][4] * sum(b[5] for b in bars[:k + 1])})
    return out


def simulate(trades, start, end):
    """One book, day by day: margin, Rs 5,000 risk sizing and the Rs 10,000 daily stop."""
    by_day = defaultdict(list)
    for t in trades:
        if start <= t["day"] < end:
            by_day[t["day"]].append(t)
    taken = []
    for d in sorted(by_day):
        free, open_, day_pnl = engine.CAPITAL, [], 0.0
        for t in sorted(by_day[d], key=lambda t: (t["entry_min"], -t["turnover"])):
            for o in [o for o in open_ if o["exit_min"] <= t["entry_min"]]:
                free += o["margin"]
                day_pnl += o["pnl"]
                open_.remove(o)
            if day_pnl <= -engine.DAILY_HALT:
                break
            long = t["side"] == "long"
            entry = t["entry"] * (1 + costs.SLIP if long else 1 - costs.SLIP)
            exit_ = t["exit"] * (1 - costs.SLIP if long else 1 + costs.SLIP)
            unit = abs(t["entry"] - t["stop"])
            qty = int(min(engine.RISK / unit, free / (entry * MARGIN))) if unit > 0 else 0
            if qty < 1 or qty * entry < engine.MIN_POSITION:
                continue
            gross = ((exit_ - entry) if long else (entry - exit_)) * qty
            fee = costs.intraday(entry * qty, exit_ * qty) if long else 60.0
            rec = dict(t, qty=qty, entry_fill=round(entry, 2), exit_fill=round(exit_, 2), gross=round(gross, 2), charges=round(fee, 2), pnl=round(gross - fee, 2),
                       r=round((gross - fee) / engine.RISK, 3), margin=qty * entry * MARGIN, exit_day=d, entry_day=d)
            free -= rec["margin"]
            open_.append(rec)
            taken.append(rec)
    return summary(taken)


def summary(taken):
    by_day = defaultdict(float)
    for t in taken:
        by_day[t["day"]] += t["pnl"]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for d in sorted(by_day):
        eq += by_day[d]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    streak = longest = 0
    for t in sorted(taken, key=lambda x: (x["day"], x["exit_min"])):
        streak = streak + 1 if t["pnl"] < 0 else 0
        longest = max(longest, streak)
    months = defaultdict(float)
    for d, v in by_day.items():
        months[d[:7]] += v
    wins = [t["pnl"] for t in taken if t["pnl"] > 0]
    losses = [t["pnl"] for t in taken if t["pnl"] < 0]
    g = sum(t["gross"] for t in taken)
    c = sum(t["charges"] for t in taken)
    return {"trades": len(taken), "net": round(g - c, 2), "gross": round(g, 2), "charges": round(c, 2),
            "win_pct": round(100 * len(wins) / len(taken), 1) if taken else None, "avg_r": round(statistics.fmean(t["r"] for t in taken), 3) if taken else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None, "max_dd": round(mdd, 2), "max_dd_pct": round(100 * mdd / engine.CAPITAL, 2),
            "longest_losing_streak": longest, "worst_month": min(months.items(), key=lambda x: x[1]) if months else None,
            "halt_days": sum(1 for v in by_day.values() if v <= -engine.DAILY_HALT), "trades_list": taken}


def run(progress=print):
    daily = engine.load_series()
    import gzip
    store = {}
    for p in intraday_data.DIR.glob("*.json.gz"):
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            store[p.name[:-8].replace("_", " ")] = json.load(fh)      # same names as the daily store ("M&M" is MandM)
    days = sorted({d for v in store.values() for d in v})
    nifty_days = [d for d in daily["NIFTY 50"].d if d >= days[0]]
    fs = engine.folds(daily["NIFTY 50"].d)
    fs = [f for f in fs if f[1] >= days[0]]
    result = {"built": datetime.now(common.IST).isoformat(timespec="seconds"), "from": days[0], "to": days[-1], "symbols": len([s for s in store if not s.startswith("NIFTY")]),
              "folds": [{"train_from": a, "test_from": b, "test_to": e} for a, b, e in fs], "strategies": {}}
    bench = sum(engine.benchmark(daily, b, e) for _, b, e in fs)
    result["benchmark_oos"] = bench
    for name, st in STRATEGIES.items():
        progress(f"{name}: building candidates")
        cand = {json.dumps(p, sort_keys=True): candidates(name, p, store, daily) for p in st["grid"]}
        oos, chosen = [], []
        for a, b, e in fs:
            best = max(cand, key=lambda k: simulate(cand[k], a, b)["net"])
            r = simulate(cand[best], b, e)
            chosen.append({"test_from": b, "test_to": e, "params": json.loads(best), "net": r["net"], "trades": r["trades"], "benchmark": engine.benchmark(daily, b, e)})
            oos.extend(r["trades_list"])
        whole = {k: simulate(v, fs[0][0], "9999-12-31")["net"] for k, v in cand.items()}
        stable = sum(1 for v in whole.values() if v > 0) / len(whole)
        m = summary(oos)
        checks = {"profit_after_costs": m["net"] > 0, "beats_niftybees": m["net"] > bench, "trades_30_plus": m["trades"] >= 30,
                  "neighbours_hold": stable >= 2 / 3, "drawdown_under_15pct": m["max_dd_pct"] < 15, "real_prices": st["side"] == "long"}
        result["strategies"][name] = {**{k: v for k, v in st.items() if k != "grid"}, "oos": {k: v for k, v in m.items() if k != "trades_list"}, "folds": chosen,
                                      "grid_full_period": whole, "neighbours_profitable": round(stable, 2), "checks": checks, "promotable": all(checks.values()),
                                      "trades": [{k: t[k] for k in ("sym", "day", "entry_min", "exit_min", "side", "entry_fill", "exit_fill", "stop", "target", "qty", "gross", "charges", "pnl", "r", "reason")} | {"params": t["params"]}
                                                 for t in sorted(oos, key=lambda x: (x["day"], x["entry_min"]))]}
        progress(f"{name}: OOS net {m['net']:+,.0f} (before charges {m['gross']:+,.0f}) on {m['trades']} trades, DD {m['max_dd_pct']}%, neighbours {stable:.0%}, promotable {all(checks.values())}")
    OUT.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return result


if __name__ == "__main__":
    r = run()
    print("NIFTYBEES over the same unseen windows:", f"{r['benchmark_oos']:+,.0f}")
