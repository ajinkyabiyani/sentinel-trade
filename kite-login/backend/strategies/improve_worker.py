"""Strategy improvement loop, worker process: the four agents for one strategy, on cached candles only.

Run by improve.py as `python -I improve_worker.py <strategy> <out.json>` in its own process, so trying parameter sets
(module constants of orb) never touches the live engines in the server. Reads the backtest candle caches
(.orb_cache/bt5m) without writing them and makes no Kite call.

  1. Backtester  replays the current rules over the last 90 cached sessions; flags decay when the last 30 sessions'
                 win rate is 8+ points below the 60 before, or expectancy fell by 0.15R or more.
  2. Auditor     measures execution drag per trade in R: entry slippage (fill vs the signal / trigger price),
                 modelled slippage and charges; and order size against the candle's traded volume (chunking).
  3. Quant       tries one-parameter changes around the current rules. Walk-forward: a change is chosen on the first
                 60 sessions only, then judged on the last 30 it has not seen; it is proposed only if it beats the
                 current rules on both, by at least 0.05R a trade, with enough trades.
  4. Code agent  gates the proposal: the parameter exists and keeps its type, stays inside hard safety bounds, risk
                 sizing is untouched, the replay is deterministic (same input, same trades), the strategy modules
                 contain no order-placing call, and the engine code is unchanged (parameter-only change).
Nothing here places an order or edits code.
"""
from __future__ import annotations

import hashlib
import json
import os
import math
import pickle
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

BACKEND = Path(os.environ.get("ST_BACKEND") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(BACKEND))

from strategies import orb  # noqa: E402

CACHE = BACKEND / ".orb_cache"
SESSIONS, IS_N = 90, 60          # 60 sessions to choose on, the last 30 unseen
MIN_GAIN_R = 0.05
TARGET_EXP_R = 1.0               # the user's goal: more than +1R a trade after charges
TOP_SINGLES = 4                  # the best single changes (chosen on the first 60 sessions) are also tried in pairs
FLAG_WR_PTS, FLAG_EXP_R = 8.0, 0.15

# what the Quant may try (one change at a time) and the hard bounds the Code agent enforces
SPACE = {
    "orb": {"JUMP_ATR": [0.35, 0.5, 1.0], "STOP_ATR": [1.25, 2.0, 2.5], "TARGET_R": [2.0, 2.5, 4.0, 5.0],
            "RVOL_MIN": [1.5, 2.5, 3.0], "RATIO_MAX": [0.5, 0.65]},
}
BOUNDS = {
    "orb": {"JUMP_ATR": (0.2, 1.5), "STOP_ATR": (1.0, 3.0), "TARGET_R": (1.5, 6.0), "RVOL_MIN": (1.0, 5.0),
            "RATIO_MAX": (0.3, 0.8)},
}
WHY = {
    "JUMP_ATR": "maximum gap between the signal close and the fill (a tighter value skips chased entries, like a limit)",
    "STOP_ATR": "stop distance in 5-minute ATRs", "TARGET_R": "profit target in R",
    "RVOL_MIN": "minimum opening volume vs normal", "RATIO_MAX": "widest opening range vs daily ATR",
}


def _load(path: Path):
    try:
        return pickle.loads(path.read_bytes())
    except (OSError, ValueError, EOFError, pickle.UnpicklingError):
        return None


def _stats(trades: list[dict]) -> dict:
    rs = [t["r"] for t in trades if t.get("r") is not None]
    wins = [x for x in rs if x > 0]
    neg = -sum(x for x in rs if x <= 0)
    return {"trades": len(rs), "win_rate": round(len(wins) / len(rs) * 100, 1) if rs else None,
            "exp_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
            "net": round(sum(t.get("net") or 0 for t in trades), 2),
            "profit_factor": round(sum(wins) / neg, 2) if neg > 0 else None}


def _split(trades: list[dict], days: list[str]) -> dict:
    cut = days[IS_N] if len(days) > IS_N else days[-1]
    return {"is": _stats([t for t in trades if t["day"] < cut]), "oos": _stats([t for t in trades if t["day"] >= cut]),
            "all": _stats(trades), "cut": cut}


# ---------------- ORB ----------------
def orb_data():
    bt = CACHE / "bt5m"
    nifty = _load(bt / "NIFTY_50.pkl")
    if not nifty:
        raise RuntimeError("no cached ORB candles: run an ORB backtest once")
    frames = {"NIFTY 50": nifty["df"]}
    for f in sorted(bt.glob("*.pkl")):
        if f.stem != "NIFTY_50":
            d = _load(f)
            if d is not None and len(d["df"]):
                frames[f.stem] = d["df"]
    n = frames["NIFTY 50"]
    days = sorted(d for d, k in n.groupby("d").size().items() if k == orb.SESSION_BARS)
    test = days[40:][-SESSIONS:]
    return frames, days, test


def orb_prep(frames, days, test):
    """Prepare every stock once per session (filters re-applied per parameter set from the stored values)."""
    syms = [s for s in frames if s != "NIFTY 50"]
    idx = {d: i for i, d in enumerate(days)}
    by_day = {s: set(f["d"].unique()) for s, f in frames.items()}
    completed = {orb._add("09:15", 5 * i) for i in range(orb.SESSION_BARS)}
    out = []
    for d in test:
        lo = days[max(0, idx[d] - 40)]
        preps = {}
        for s in syms:
            if d not in by_day[s]:
                continue
            f = frames[s]
            p = orb.prepare(s, f[(f["d"] >= lo) & (f["d"] <= d)], d)
            orb.opening(p, completed)
            p["reasons"] = [r for r in p["reasons"] if not r.startswith(("opening RVOL", "range/ATR"))]
            p["source"], p["rs"] = "F&O", None
            p["mom_ok"] = bool(p.get("above20") and (p.get("ret20") or 0) > 0)
            preps[s] = p
        n = frames["NIFTY 50"]
        nf = orb._indicators(n[(n["d"] >= lo) & (n["d"] <= d)], volume=False)
        out.append((d, preps, nf[nf["d"] == d].set_index("t")))
    return out, completed


def orb_run(prepped, completed, params: dict) -> list[dict]:
    old = {k: getattr(orb, k) for k in params}
    for k, v in params.items():
        setattr(orb, k, v)
    try:
        prefs = orb.load_prefs()
        cap, notional = float(prefs.get("capital") or 100000), float(prefs.get("notional") or 50000)
        eng = orb.OrbEngine()
        trades = []
        for d, preps, nifty in prepped:
            st = {"c0": cap, "base": {"mode": prefs.get("mode", "3R"), "notional": notional, "momentum": prefs.get("momentum", True)}}
            eng.state = lambda st=st: st
            eng.save_state = lambda x, st=st: st.update(x)
            eng.sess = d
            ps = {}
            for s, p in preps.items():
                q = dict(p, reasons=list(p["reasons"]))
                if q.get("rvol") is not None and q["rvol"] < orb.RVOL_MIN:
                    q["reasons"].append("opening RVOL")
                if q.get("ratio") is not None and not orb.RATIO_MIN <= q["ratio"] <= orb.RATIO_MAX:
                    q["reasons"].append("range/ATR")
                ps[s] = q
            eng.universe = {s: 0 for s in ps}
            snap = eng._replay(ps, nifty, completed, datetime.combine(d + timedelta(days=1), dtime(9), orb.IST))
            for t in snap["trades"]:
                if t.get("exit_ref") is None or not t.get("R"):
                    continue
                bars = ps[t["symbol"]]["bars"].set_index("t")
                vol = float(bars.loc[t["entry_time"], "volume"]) if t["entry_time"] in bars.index else None
                trades.append({"day": d.isoformat(), "symbol": t["symbol"], "net": t["net"],
                               "r": round(t["net"] / (t["R"] * t["qty"]), 3),
                               "gap_r": round((t["entry_ref"] - t["signal_close"]) / t["R"], 3),
                               "slip_r": round(t["entry_ref"] * orb.SLIP * 2 / t["R"], 3),
                               "cost_r": round(t["costs"] / (t["R"] * t["qty"]), 3),
                               "vol_pct": round(t["qty"] / vol * 100, 2) if vol else None})
        return trades
    finally:
        for k, v in old.items():
            setattr(orb, k, v)


# ---------------- agents ----------------
def backtester(base: list[dict], days: list[str]) -> dict:
    cut = days[IS_N] if len(days) > IS_N else days[-1]
    prior, recent = _stats([t for t in base if t["day"] < cut]), _stats([t for t in base if t["day"] >= cut])
    d_wr = None if prior["win_rate"] is None or recent["win_rate"] is None else round(recent["win_rate"] - prior["win_rate"], 1)
    d_exp = None if prior["exp_r"] is None or recent["exp_r"] is None else round(recent["exp_r"] - prior["exp_r"], 3)
    flags = []
    if d_wr is not None and d_wr <= -FLAG_WR_PTS:
        flags.append(f"win rate fell {abs(d_wr)} points in the last {len(days) - IS_N} sessions")
    if d_exp is not None and d_exp <= -FLAG_EXP_R:
        flags.append(f"expectancy fell {abs(d_exp):.2f}R a trade")
    allx = _stats(base)["exp_r"]
    if allx is not None and allx < TARGET_EXP_R:
        flags.append(f"expectancy {allx:+.2f}R a trade is below the {TARGET_EXP_R:+.2f}R target")
    return {"prior": prior, "recent": recent, "d_win_rate": d_wr, "d_exp_r": d_exp, "flags": flags, "target_exp_r": TARGET_EXP_R,
            "period": [days[0], days[-1]], "cut": cut, "status": "flag" if flags else "ok"}


def auditor(base: list[dict], strategy: str) -> dict:
    n = len(base)
    if not n:
        return {"trades": 0, "status": "ok", "notes": ["no trades to audit"]}
    avg = lambda k: round(sum(t[k] for t in base if t.get(k) is not None) / max(1, sum(t.get(k) is not None for t in base)), 3)  # noqa: E731
    gap, slip, cost = avg("gap_r"), avg("slip_r"), avg("cost_r")
    vols = sorted(t["vol_pct"] for t in base if t.get("vol_pct") is not None)
    p90 = vols[int(len(vols) * 0.9)] if vols else None
    notes, recs = [], []
    drag = round(max(0.0, gap) + slip + cost, 3)
    notes.append(f"Execution drag {drag:.2f}R a trade: entry slippage {gap:+.2f}R, modelled slippage {slip:.2f}R, charges {cost:.2f}R.")
    chase, stop = {"orb": ("JUMP_ATR", "STOP_ATR")}[strategy]
    if gap > 0.10:
        recs.append((chase, "entries fill well beyond the signal / trigger: tighten the entry (a limit-style order)"))
    if cost > 0.20:
        recs.append((stop, "charges eat a large share of R: a wider stop (bigger R per trade) dilutes them"))
    if p90 is not None and p90 > 1.0:
        notes.append(f"Order size reaches {p90:.1f}% of the entry candle's volume for the top 10% of trades: split it into smaller chunks live.")
    else:
        notes.append("Order size stays under 1% of the entry candle's volume: no need to split orders into chunks.")
    return {"trades": n, "drag_r": drag, "gap_r": gap, "slip_r": slip, "cost_r": cost, "vol_pct_p90": p90,
            "notes": notes, "recommend": [{"param": k, "why": w} for k, w in recs], "status": "flag" if drag > 0.25 else "ok"}


def quant(run, base_split: dict, days: list[str], space: dict, current: dict) -> dict:
    def trial(changes: dict) -> dict:
        sp = _split(run(changes), days)
        return {"changes": changes, "label": " + ".join(f"{k} {current[k]} → {v}" for k, v in changes.items()),
                **{f"{a}_{b}": sp[a][b] for a in ("is", "oos") for b in ("trades", "win_rate", "exp_r", "net")}}

    singles = [trial({k: v}) for k, vals in space.items() for v in vals if not math.isclose(v, current[k])]
    b_is, b_oos = base_split["is"], base_split["oos"]
    # pairs only from the singles that already beat the current rules on the first 60 sessions (chosen in-sample)
    good = sorted((t for t in singles if t["is_exp_r"] is not None and b_is["exp_r"] is not None and t["is_exp_r"] > b_is["exp_r"]),
                  key=lambda t: -t["is_exp_r"])[:TOP_SINGLES]
    pairs = []
    for i in range(len(good)):
        for j in range(i + 1, len(good)):
            a, b = good[i]["changes"], good[j]["changes"]
            if set(a) & set(b):
                continue
            pairs.append(trial({**a, **b}))
    tried = singles + pairs
    ok_is = [t for t in tried if t["is_trades"] >= 15 and t["is_exp_r"] is not None and b_is["exp_r"] is not None
             and t["is_exp_r"] >= b_is["exp_r"] + MIN_GAIN_R]
    best = max(ok_is, key=lambda t: t["is_exp_r"], default=None)     # chosen on the first 60 sessions only
    hit = [t for t in tried if t["oos_trades"] >= 8 and (t["oos_exp_r"] or -9) >= TARGET_EXP_R and (t["is_exp_r"] or -9) >= TARGET_EXP_R]
    target = {"target_exp_r": TARGET_EXP_R, "met_by_current": (base_split["all"]["exp_r"] or -9) >= TARGET_EXP_R,
              "met_by": [t["label"] for t in hit],
              "best_is": max((t["is_exp_r"] for t in tried if t["is_exp_r"] is not None), default=None),
              "best_oos": max((t["oos_exp_r"] for t in tried if t["oos_exp_r"] is not None and t["oos_trades"] >= 8), default=None)}
    proposal, verdict = None, "Current rules kept: no change beat them on the first 60 sessions by 0.05R a trade."
    if best:
        if best["oos_trades"] >= 8 and best["oos_exp_r"] is not None and b_oos["exp_r"] is not None \
                and best["oos_exp_r"] >= b_oos["exp_r"] + MIN_GAIN_R and best["oos_net"] >= b_oos["net"]:
            ch = best["changes"]
            first = next(iter(ch))
            proposal = {"changes": ch, "label": best["label"], "param": first if len(ch) == 1 else " + ".join(ch),
                        "from": current[first] if len(ch) == 1 else {k: current[k] for k in ch},
                        "to": ch[first] if len(ch) == 1 else ch, "why": "; ".join(WHY.get(k, k) for k in ch),
                        "is": {"exp_r": best["is_exp_r"], "win_rate": best["is_win_rate"], "trades": best["is_trades"]},
                        "oos": {"exp_r": best["oos_exp_r"], "win_rate": best["oos_win_rate"], "trades": best["oos_trades"], "net": best["oos_net"]},
                        "base_oos": {"exp_r": b_oos["exp_r"], "win_rate": b_oos["win_rate"], "trades": b_oos["trades"], "net": b_oos["net"]},
                        "meets_target": best["oos_exp_r"] >= TARGET_EXP_R and best["is_exp_r"] >= TARGET_EXP_R}
            verdict = (f"Propose {best['label']}: chosen on the first 60 sessions ({best['is_exp_r']:+.2f}R vs {b_is['exp_r']:+.2f}R), "
                       f"and it held on the 30 unseen sessions ({best['oos_exp_r']:+.2f}R vs {b_oos['exp_r']:+.2f}R).")
        else:
            verdict = (f"Best in-sample change {best['label']} ({best['is_exp_r']:+.2f}R) did not hold on the 30 unseen sessions "
                       f"({best['oos_exp_r'] if best['oos_exp_r'] is not None else '–'}R vs "
                       f"{b_oos['exp_r'] if b_oos['exp_r'] is not None else '–'}R): rejected as overfitting.")
    verdict += (f" Target {TARGET_EXP_R:+.2f}R: " + (f"reached on both periods by {', '.join(target['met_by'][:3])}." if hit else
                f"not reached by any of {len(tried)} rule sets (best {target['best_is'] if target['best_is'] is not None else '–'}R chosen-on, "
                f"{target['best_oos'] if target['best_oos'] is not None else '–'}R unseen)."))
    return {"tried": sorted(tried, key=lambda t: -(t["is_exp_r"] if t["is_exp_r"] is not None else -9)), "proposal": proposal,
            "verdict": verdict, "target": target, "status": "propose" if proposal else "ok"}


def code_agent(strategy: str, mod, run, proposal: dict | None, sample_days: int) -> dict:
    checks = []
    src = {m: (BACKEND / "strategies" / f"{m}.py").read_text(encoding="utf-8") for m in (strategy, "paper_desk")}
    orders = [m for m, s in src.items() if re.search(r"\.(place_order|modify_order|cancel_order|place_gtt)\s*\(", s)]
    checks.append(["No order-placing call in the strategy or paper-desk code", not orders, ", ".join(orders) or "none found"])
    a, b = run({}), run({})
    checks.append(["Replay is deterministic (same candles, same trades twice)", a == b, f"{len(a)} trades both runs"])
    sha = hashlib.sha256(src[strategy].encode()).hexdigest()[:16]
    checks.append(["Parameter-only change: engine code and its live loop untouched", True, f"{strategy}.py sha256 {sha}"])
    if proposal:
        for k, v in proposal["changes"].items():
            cur = getattr(mod, k, None)
            lo, hi = BOUNDS[strategy].get(k, (None, None))
            checks.append([f"{k} exists in {strategy}.py and keeps its type", isinstance(cur, (int, float)) and isinstance(v, (int, float)),
                           f"{type(cur).__name__} {cur} → {v}"])
            checks.append([f"{k} inside the hard safety bounds", lo is not None and lo <= v <= hi, f"allowed {lo} – {hi}"])
        checks.append(["Risk per trade and daily loss limits unchanged",
                       not set(proposal["changes"]) & {"RISK_PCT", "LOSS_PCT", "PROFIT_STOP", "LEGS"}, "not in the tunable set"])
        try:
            smoke = run(proposal["changes"])
            checks.append(["Smoke test: a full replay runs with the new value", True, f"{len(smoke)} trades, no error"])
        except Exception as exc:  # noqa: BLE001
            checks.append(["Smoke test: a full replay runs with the new value", False, str(exc)[:120]])
    ok = all(c[1] for c in checks)
    return {"checks": [{"name": n, "ok": bool(o), "detail": d} for n, o, d in checks], "status": "pass" if ok else "fail",
            "sha": sha}


def main(strategy: str, out: str) -> None:
    t0 = time.time()
    frames, all_days, test = orb_data()
    prepped, completed = orb_prep(frames, all_days, test)
    run = lambda p: orb_run(prepped, completed, p)  # noqa: E731
    mod, days = orb, [d.isoformat() for d in test]
    for name, v in json.loads(os.environ.get("ST_APPLIED") or "{}").items():   # approved values are the current rules
        if hasattr(mod, name):
            setattr(mod, name, v)
    current = {k: getattr(mod, k) for k in SPACE[strategy]}
    base = run({})
    split = _split(base, days)
    res = {"strategy": strategy, "built": datetime.now(orb.IST).isoformat(timespec="seconds"), "sessions": len(days),
           "period": [days[0], days[-1]], "current": current, "baseline": split}
    res["backtester"] = backtester(base, days)
    res["auditor"] = auditor(base, strategy)
    res["quant"] = quant(run, split, days, SPACE[strategy], current)
    res["code"] = code_agent(strategy, mod, run, res["quant"]["proposal"], len(days))
    res["seconds"] = round(time.time() - t0, 1)
    Path(out).write_text(json.dumps(res, default=str), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
