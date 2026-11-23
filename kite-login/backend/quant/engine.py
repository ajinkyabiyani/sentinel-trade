"""Quant Desk walk-forward engine.

For each strategy: candidate trades are built once per parameter setting from the daily store. The test runs in folds:
choose the setting with the best net profit on a 9-month training window, then trade that setting, unchanged, on the next
3 months it has never seen. Windows roll forward by 3 months. Only the unseen windows count as results.

Portfolio: ₹1,00,000 paper capital and ₹5,000 risk a trade. Each trade's size is the smaller of risk / stop distance and the
cash free; signals on the same day are taken most-liquid first. Costs: full Indian charges (quant.costs) plus 0.05% slippage
a side. Benchmark: buying NIFTYBEES at the start of each unseen window and selling at its end.

A strategy is ready for champion status only if, on unseen data: net profit after costs is positive, it beats NIFTYBEES,
it has at least 30 trades, at least two thirds of its neighbouring settings are also profitable, and the drawdown stays
under 15% of capital. Short-side (put) results are estimates from the stock's move and can't be promoted until real put
prices can be replayed.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime

from quant import costs, data, indicators as ind
from quant.strategies import STRATEGIES, Series, candidates
from strategies import common

CAPITAL, RISK, MIN_POSITION = 100_000.0, 5_000.0, 5_000.0
TRAIN, TEST, WARM = 189, 63, 201
DAILY_HALT, DD_HALT, LOSS_STREAK_HALT = 10_000.0, 0.15, 3
OUT = common.CACHE_DIR / "quant" / "walkforward.json"


def load_series() -> dict[str, Series]:
    out = {}
    for p in sorted(data.DIR.glob("*.json")):
        sym = p.stem.replace("_", " ")                     # "M&M" is stored as MandM; the name is only for display
        bars = json.loads(p.read_text(encoding="utf-8"))
        if len(bars) > WARM + 20:
            out[sym] = Series(sym, bars)
    return out


def _fill(t: dict) -> tuple[float, float, int, float]:
    """(entry fill, exit fill, quantity, rupee charges) for one trade, sized for ₹5,000 risk inside the cash available."""
    long = t["side"] == "long"
    entry = t["entry"] * (1 + costs.SLIP if long else 1 - costs.SLIP)
    exit_ = t["exit"] * (1 - costs.SLIP if long else 1 + costs.SLIP)
    unit = abs(t["entry"] - t["stop"]) if t["stop"] is not None else (t.get("atr") or t["entry"] * 0.02)
    return entry, exit_, unit, 0.0


def simulate(trades: list[dict], series: dict[str, Series], start: str, end: str) -> dict:
    """Trades entered in [start, end), on ₹1 lakh, one book. -> results with each taken trade."""
    pool = sorted((t for t in trades if start <= t["entry_day"] < end), key=lambda t: (t["entry_day"], -t["turnover"]))
    cash, open_, taken = CAPITAL, [], []
    for t in pool:
        # release cash from trades closed before this entry
        still = []
        for o in open_:
            if o["exit_day"] < t["entry_day"]:          # money from a trade is free the day after it closes
                cash += o["value"] + o["pnl"]
            else:
                still.append(o)
        open_ = still
        if any(o["sym"] == t["sym"] for o in open_):
            continue
        entry, exit_, unit, _ = _fill(t)
        if unit <= 0:
            continue
        qty = int(min(RISK / unit, cash / entry))
        value = qty * entry
        if qty < 1 or value < MIN_POSITION:
            continue
        sell, buy = (exit_ * qty, value) if t["side"] == "long" else (value, exit_ * qty)
        if t["side"] == "long":
            fee = costs.delivery(value, exit_ * qty)
            gross = (exit_ - entry) * qty
        else:
            fee = 60.0                                     # estimate: two option orders' charges
            gross = (entry - exit_) * qty
        pnl = gross - fee
        rec = dict(t, qty=qty, entry_fill=round(entry, 2), exit_fill=round(exit_, 2), value=value, gross=round(gross, 2), charges=round(fee, 2), pnl=round(pnl, 2),
                   r=round(pnl / RISK, 3), exit_i_rank=0)
        cash -= value
        open_.append(rec)
        taken.append(rec)
    return metrics(taken, series, start, end)


def _equity(taken: list[dict], series: dict[str, Series], start: str, end: str) -> list[tuple[str, float]]:
    """Daily marked-to-market profit of the book."""
    days = [d for d in series["NIFTY 50"].d if start <= d < end]
    idx = {s: {d: i for i, d in enumerate(series[s].d)} for s in {t["sym"] for t in taken}}
    out = []
    for d in days:
        total = 0.0
        for t in taken:
            if t["exit_day"] <= d:
                total += t["pnl"]
            elif t["entry_day"] <= d:
                i = idx[t["sym"]].get(d)
                if i is not None:
                    px = series[t["sym"]].c[i]
                    total += ((px - t["entry_fill"]) if t["side"] == "long" else (t["entry_fill"] - px)) * t["qty"]
        out.append((d, total))
    return out


def metrics(taken: list[dict], series: dict[str, Series], start: str, end: str) -> dict:
    eq = _equity(taken, series, start, end)
    peak, mdd = CAPITAL, 0.0
    for _, v in eq:
        peak = max(peak, CAPITAL + v)
        mdd = max(mdd, peak - (CAPITAL + v))
    streak = longest = 0
    for t in sorted(taken, key=lambda x: x["exit_day"]):
        streak = streak + 1 if t["pnl"] < 0 else 0
        longest = max(longest, streak)
    months = defaultdict(float)
    for t in taken:
        months[t["exit_day"][:7]] += t["pnl"]
    by_day = defaultdict(float)
    for t in taken:
        by_day[t["exit_day"]] += t["pnl"]
    wins = [t["pnl"] for t in taken if t["pnl"] > 0]
    losses = [t["pnl"] for t in taken if t["pnl"] < 0]
    return {"start": start, "end": end, "trades": len(taken), "net": round(sum(t["pnl"] for t in taken), 2), "charges": round(sum(t["charges"] for t in taken), 2),
            "win_pct": round(100 * len(wins) / len(taken), 1) if taken else None, "avg_r": round(statistics.fmean(t["r"] for t in taken), 3) if taken else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None, "max_dd": round(mdd, 2), "max_dd_pct": round(100 * mdd / CAPITAL, 2),
            "longest_losing_streak": longest, "worst_month": min(months.items(), key=lambda x: x[1]) if months else None,
            "halt_days": sum(1 for v in by_day.values() if v <= -DAILY_HALT), "trades_list": taken}


def benchmark(series: dict[str, Series], start: str, end: str) -> float:
    b = series.get("NIFTYBEES")
    days = [i for i, d in enumerate(b.d) if start <= d < end]
    if not days:
        return 0.0
    buy, sell = b.o[days[0]] * (1 + costs.SLIP), b.c[days[-1]] * (1 - costs.SLIP)
    qty = int(CAPITAL / buy)
    return round((sell - buy) * qty - costs.delivery(buy * qty, sell * qty), 2)


def folds(days: list[str]) -> list[tuple[str, str, str]]:
    """(train start, test start, test end) by date, rolling by TEST days."""
    out, i = [], WARM
    while i + TRAIN < len(days):
        a, b = days[i], days[i + TRAIN]
        e = days[i + TRAIN + TEST] if i + TRAIN + TEST < len(days) else "9999-12-31"
        out.append((a, b, e))
        i += TEST
    return out


def run(progress=print) -> dict:
    series = load_series()
    days = series["NIFTY 50"].d
    fs = folds(days)
    universe = [s for s in series if s not in ("NIFTYBEES",)]
    result = {"built": datetime.now(common.IST).isoformat(timespec="seconds"), "data_from": days[0], "data_to": days[-1], "symbols": len(universe),
              "capital": CAPITAL, "risk": RISK, "folds": [{"train_from": a, "test_from": b, "test_to": e} for a, b, e in fs], "strategies": {}}
    bench_oos = sum(benchmark(series, b, e) for _, b, e in fs)
    result["benchmark_oos"] = bench_oos
    for name, st in STRATEGIES.items():
        progress(f"{name}: building candidates")
        cand = {json.dumps(p, sort_keys=True): [t for s in universe for t in candidates(series[s], name, p)] for p in st["grid"]}
        oos, chosen = [], []
        for a, b, e in fs:
            best = max(cand, key=lambda k: simulate(cand[k], series, a, b)["net"])
            r = simulate(cand[best], series, b, e)
            chosen.append({"test_from": b, "test_to": e, "params": json.loads(best), "net": r["net"], "trades": r["trades"], "benchmark": benchmark(series, b, e)})
            oos.extend(r["trades_list"])
        whole = {k: simulate(v, series, days[WARM], "9999-12-31") for k, v in cand.items()}
        positive = sum(1 for v in whole.values() if v["net"] > 0)
        m = metrics(oos, series, fs[0][1], "9999-12-31")
        stable = positive / len(whole)
        checks = {"profit_after_costs": m["net"] > 0, "beats_niftybees": m["net"] > bench_oos, "trades_30_plus": m["trades"] >= 30,
                  "neighbours_hold": stable >= 2 / 3, "drawdown_under_15pct": m["max_dd_pct"] < 15, "real_prices": st["side"] == "long"}
        result["strategies"][name] = {**{k: v for k, v in st.items() if k != "grid"}, "oos": {k: v for k, v in m.items() if k != "trades_list"},
                                      "folds": chosen, "grid_full_period": {k: v["net"] for k, v in whole.items()}, "neighbours_profitable": round(stable, 2),
                                      "checks": checks, "promotable": all(checks.values()), "trades": [
                                          {k: t[k] for k in ("sym", "signal_day", "entry_day", "exit_day", "side", "entry_fill", "exit_fill", "stop", "qty", "gross", "charges", "pnl", "r", "reason")} | {"params": t["params"]}
                                          for t in sorted(oos, key=lambda x: x["entry_day"])]}
        progress(f"{name}: OOS net {m['net']:+,.0f} on {m['trades']} trades, max DD {m['max_dd_pct']}%, neighbours {positive}/{len(whole)} profitable, promotable {all(checks.values())}")
    # today's regime
    nifty = series["NIFTY 50"]
    counts = defaultdict(int)
    for s in universe:
        counts[series[s].regime[-1] or "NONE"] += 1
    result["regime_today"] = {"nifty": nifty.regime[-1], "bank_nifty": series["NIFTY BANK"].regime[-1], "stocks": dict(counts), "as_of": nifty.d[-1]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return result


if __name__ == "__main__":
    r = run()
    print("\nNIFTYBEES buy-and-hold over the same unseen windows:", f"{r['benchmark_oos']:+,.0f}")
    print("Regime today:", r["regime_today"])
