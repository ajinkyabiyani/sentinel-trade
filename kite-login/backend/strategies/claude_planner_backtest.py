"""Claude as a daily planner, on past days: one Claude call per day at 10:15, fixed rules carry out the plan on real option prices.

At 10:15 Claude reads a map of the day: Nifty and Bank Nifty (gap, opening range, the last 20 sessions), India VIX, every
sector against Nifty today and over 1, 5 and 20 sessions with today's breadth, and the F&O stocks moving most since the
open with their relative volume. It returns the regime and up to two option buys (index or stock), each with the
underlying price where the idea is wrong.

The code then, without any choice of its own: buys the at-the-money option of the nearest expiry at 10:16 on Dhan's real
1-minute premium (slippage 0.3% index / 1% stock each way, plus charges); sizes it so the loss at the underlying stop is
about RISK rupees; exits when the underlying hits the stop or 2R, or at 15:15. Capital is CAPITAL; the premium of both
trades together must fit in it. No trade in an underlying on its own expiry day.

Only days after Claude's training cutoff (June 2026) are used.

    python -m strategies.claude_planner_backtest dry 2026-07-01     (the map for one day, no Claude)
    python -m strategies.claude_planner_backtest run --workers 3
    python -m strategies.claude_planner_backtest report
"""
from __future__ import annotations

import argparse
import gzip
import csv
import json
import os
import math
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from quant import candles
from strategies import claude_trader as ct
from strategies import claude_trader_backtest as bt
from strategies import common

OUT = common.CACHE_DIR / "claude_planner_backtest"
OPT = candles.ROOT / "options_1m"
FROM, TO = os.environ.get("BACKTEST_FROM", "2026-07-01"), os.environ.get("BACKTEST_TO", "2026-11-20")
CAPITAL, RISK, MAX_TRADES, MAX_PREMIUM = 100000.0, 2500.0, 2, 60000.0
SLIP = {"index": 0.003, "stock": 0.01}
MAX_STOP = {"index": 1.2, "stock": 2.5}          # % from spot: a wider stop is replaced by the day's low/high so far
MIN_STOP = {"index": 0.15, "stock": 0.4}         # % from spot: a closer stop is widened to this (noise would take it out)
READ, ENTRY, FLAT = "10:15", "10:16", "15:15"
INDICES = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}
SECTORS = {"Private banks": "NIFTY_PVT_BANK", "PSU banks": "NIFTY_PSU_BANK", "Financial services": "NIFTY_FIN_SERVICE", "IT": "NIFTY_IT",
           "Auto": "NIFTY_AUTO", "Pharma": "NIFTY_PHARMA", "FMCG": "NIFTY_FMCG", "Metals": "NIFTY_METAL", "Energy": "NIFTY_ENERGY",
           "Oil & gas": "NIFTY_OIL_AND_GAS", "Realty": "NIFTY_REALTY", "Infrastructure": "NIFTY_INFRA", "Media": "NIFTY_MEDIA",
           "Consumer durables": "NIFTY_CONSR_DURBL", "CPSE": "NIFTY_CPSE", "Midcap select": "NIFTY_MID_SELECT"}
GROUP_OF = {"Private banks": "Banks", "PSU banks": "PSU Banks", "Financial services": "Financial Services", "IT": "IT", "Auto": "Auto",
            "Pharma": "Pharma", "FMCG": "FMCG", "Metals": "Metals", "Energy": "Energy", "Oil & gas": "Energy", "Realty": "Realty",
            "Infrastructure": "Infrastructure"}

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["regime", "read", "trades"],
    "properties": {
        "regime": {"type": "string", "enum": ["trend_up", "trend_down", "choppy", "reversal_up", "reversal_down"]},
        "read": {"type": "string", "description": "Three or four sentences: the market, where money is moving today and why you chose these trades (or none)"},
        "trades": {"type": "array", "maxItems": MAX_TRADES, "items": {
            "type": "object", "additionalProperties": False, "required": ["underlying", "option", "stop_level", "conviction", "why"],
            "properties": {"underlying": {"type": "string", "description": "One of map.tradable"},
                           "option": {"type": "string", "enum": ["CALL", "PUT"]},
                           "stop_level": {"type": "number", "description": "Underlying price where the idea is wrong"},
                           "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
                           "why": {"type": "string"}}}},
    },
}
SYSTEM = f"""You are Claude Trader's daily planner: an intraday option buyer on India's NSE. Once a day at {READ}, after the
first hour, you read a map of the market and decide what, if anything, to buy for the rest of the day.

How your plan is carried out (you can't change this): each trade buys the at-the-money call or put of the nearest expiry at
{ENTRY}, sized so the loss at your stop is about Rs {RISK:,.0f}. It exits when the underlying reaches your stop_level, or twice
that distance in your favour, or at {FLAT}. Capital is Rs {CAPITAL:,.0f}; at most {MAX_TRADES} trades. Costs are real: about
0.3% slippage each way on index options, 1% on stock options, plus charges, and time decay all day.

What tends to matter: money rotates between sectors and the strongest moves come where a sector moves together (breadth),
with volume, against the index. Choppy, range-bound index days punish option buyers. A trade only pays if the underlying
travels about twice your stop distance before {FLAT}. No trade is a good answer when nothing stands out: say so with an
empty trades list. Only pick underlyings in map.tradable. Answer only with the JSON object the schema asks for."""
INTRO = "Here is today's map at 10:15. Decide the plan."


# ---------------------------------------------------------------- data

_c5: dict = {}


def bars5(sym: str) -> dict:
    """{date: DataFrame of 5-minute candles} for a symbol (index names as saved: NIFTY, NIFTY_IT, ...)."""
    if sym not in _c5:
        try:
            df = candles.load(sym, 5)
            _c5[sym] = {d: x for d, x in df.groupby(df.index.date)}
        except FileNotFoundError:
            _c5[sym] = {}
    return _c5[sym]


_c1: dict = {}


def bars1(sym: str, d: date):
    if sym not in _c1:
        df = candles.load(sym, 1)
        _c1[sym] = {k: x for k, x in df.groupby(df.index.date)}
    return _c1[sym].get(d)


def days() -> list[date]:
    n = bars5("NIFTY")
    return sorted(d for d in n if FROM <= d.isoformat() <= TO and len(n[d]) >= 70)


def prev_days(sym: str, d: date, k: int) -> list[date]:
    return sorted(x for x in bars5(sym) if x < d)[-k:]


def close_at(x, t: str) -> float | None:
    seen = x[x.index.strftime("%H:%M") < t]
    return float(seen["close"].iloc[-1]) if len(seen) else None


_opt: dict = {}


def options_day(name: str, d: date) -> dict:
    """{(HH:MM, CE/PE): {strike: row}} for one underlying's nearest expiry on one day."""
    key = (name, d)
    if key in _opt:
        return _opt[key]
    path = OPT / name / f"{d:%Y-%m}.csv.gz"
    out: dict = {}
    if path.exists():
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                if r["date"] != d.isoformat() or not r["strike"] or not r["close"]:
                    continue
                out.setdefault((r["time"], r["type"]), {})[float(r["strike"])] = {k: float(r[k]) if r[k] not in ("", None) else None
                                                                               for k in ("offset", "spot", "open", "high", "low", "close", "iv")}
    _opt[key] = out
    return out


def has_options(name: str, d: date) -> bool:
    return (OPT / name / f"{d:%Y-%m}.csv.gz").exists()


# ---------------------------------------------------------------- the map

def index_read(name: str, d: date) -> dict:
    x = bars5(name)[d]
    pd_ = prev_days(name, d, 21)
    closes = [float(bars5(name)[p]["close"].iloc[-1]) for p in pd_]
    prev = closes[-1]
    seen = x[x.index.strftime("%H:%M") < READ]
    now = float(seen["close"].iloc[-1])
    ex = bt.expiries(name, d)
    return {"prev_close": prev, "open": float(x["open"].iloc[0]), "gap_pct": round((float(x["open"].iloc[0]) / prev - 1) * 100, 2),
            "now": now, "change_pct": round((now / prev - 1) * 100, 2), "high_so_far": float(seen["high"].max()), "low_so_far": float(seen["low"].min()),
            "opening_range_15m": [float(x.iloc[:3]["low"].min()), float(x.iloc[:3]["high"].max())],
            "prev_day_high_low": [float(bars5(name)[pd_[-1]]["low"].min()), float(bars5(name)[pd_[-1]]["high"].max())],
            "last_20_sessions_pct": [round((b / a - 1) * 100, 2) for a, b in zip(closes, closes[1:])],
            "high_low_20d": [round(min(float(bars5(name)[p]["low"].min()) for p in pd_[-20:]), 2), round(max(float(bars5(name)[p]["high"].max()) for p in pd_[-20:]), 2)],
            "candles_5m_today": [[t.strftime("%H:%M"), round(r["open"], 2), round(r["high"], 2), round(r["low"], 2), round(r["close"], 2)] for t, r in seen.iterrows()],
            "days_to_expiry": (ex[0] - d).days, "expiry_today": ex[0] == d}


def stock_moves(d: date, fno: list[str], groups: dict) -> tuple[dict, dict]:
    """({stock: {chg, gap, relvol}}, {stock: sector group}) at READ for every F&O stock with candles."""
    sec_of = {}
    for g, names in groups.items():
        for n in names:
            sec_of.setdefault(n, g)
    out = {}
    for n in fno:
        b = bars5(n)
        x = b.get(d)
        if x is None or len(x) < 12:
            continue
        pds = prev_days(n, d, 10)
        if not pds:
            continue
        prev = float(b[pds[-1]]["close"].iloc[-1])
        now = close_at(x, READ)
        vol_now = float(x[x.index.strftime("%H:%M") < READ]["volume"].sum())
        vol_hist = [float(b[p][b[p].index.strftime("%H:%M") < READ]["volume"].sum()) for p in pds]
        avg = sum(vol_hist) / len(vol_hist) if vol_hist else 0
        seen = x[x.index.strftime("%H:%M") < READ]
        y = b[pds[-1]]
        out[n] = {"chg": (now / prev - 1) * 100, "gap": (float(x["open"].iloc[0]) / prev - 1) * 100, "relvol": vol_now / avg if avg else None,
                  "levels": {"price": round(now, 2), "prev_close": round(prev, 2), "high_so_far": round(float(seen["high"].max()), 2),
                             "low_so_far": round(float(seen["low"].min()), 2),
                             "opening_range_15m": [round(float(x.iloc[:3]["low"].min()), 2), round(float(x.iloc[:3]["high"].max()), 2)],
                             "prev_day_low_high": [round(float(y["low"].min()), 2), round(float(y["high"].max()), 2)]}}
    return out, sec_of


def sector_table(d: date, moves: dict, sec_of: dict, nifty_chg: float) -> list[dict]:
    nif = bars5("NIFTY")
    pds = prev_days("NIFTY", d, 21)

    def ret(sym, a, b):
        s = bars5(sym)
        try:
            return float(s[b]["close"].iloc[-1]) / float(s[a]["close"].iloc[-1]) - 1
        except KeyError:
            return None
    rows = []
    for label, sym in SECTORS.items():
        x = bars5(sym).get(d)
        if x is None or not pds:
            continue
        prev = float(bars5(sym)[pds[-1]]["close"].iloc[-1]) if pds[-1] in bars5(sym) else None
        now = close_at(x, READ)
        if not prev or not now:
            continue
        row = {"sector": label, "today_vs_nifty_pct": round((now / prev - 1) * 100 - nifty_chg, 2)}
        for k, n in (("1d", 1), ("5d", 5), ("20d", 20)):
            if len(pds) > n:
                a, b = ret(sym, pds[-1 - n], pds[-1]), float(nif[pds[-1]]["close"].iloc[-1]) / float(nif[pds[-1 - n]]["close"].iloc[-1]) - 1
                row[f"{k}_vs_nifty_pct"] = None if a is None else round((a - b) * 100, 2)
        g = GROUP_OF.get(label)
        members = [m for m, s in sec_of.items() if s == g and m in moves] if g else []
        if label == "Private banks":
            psu = {m for m, s in sec_of.items() if s == "PSU Banks"}
            members = [m for m in members if m not in psu]
        if members:
            row["breadth_beating_nifty"] = round(sum(moves[m]["chg"] > nifty_chg for m in members) / len(members), 2)
            row["stocks"] = len(members)
        rows.append(row)
    rows.sort(key=lambda r: -r["today_vs_nifty_pct"])
    return rows


def build_map(d: date, fno: list[str], groups: dict) -> dict:
    idx = {n: index_read(n, d) for n in INDICES}
    moves, sec_of = stock_moves(d, fno, groups)
    nifty_chg = idx["NIFTY"]["change_pct"]
    tradable = [n for n in INDICES if has_options(n, d) and not idx[n]["expiry_today"]]
    tradable += sorted(n for n in moves if n not in INDICES and has_options(n, d) and bt.expiries(n, d)[0] != d)

    def row(n):
        m = moves[n]
        return {"symbol": n, "sector": sec_of.get(n), "change_pct": round(m["chg"], 2), "gap_pct": round(m["gap"], 2),
                "first_hour_volume_x_avg": None if m["relvol"] is None else round(m["relvol"], 2), "tradable": n in tradable, **m["levels"]}
    ranked = sorted(moves, key=lambda n: -moves[n]["chg"])
    vix = bars5("INDIAVIX").get(d)
    vprev = prev_days("INDIAVIX", d, 1)
    v_now = close_at(vix, READ) if vix is not None else None
    v_prev = float(bars5("INDIAVIX")[vprev[-1]]["close"].iloc[-1]) if vprev else None
    up = sum(1 for m in moves.values() if m["chg"] > 0)
    return {"date": d.isoformat(), "weekday": d.strftime("%A"), "time": READ,
            "india_vix": {"now": v_now, "change_pct": round((v_now / v_prev - 1) * 100, 2) if v_now and v_prev else None},
            "breadth_fno": {"up": up, "down": len(moves) - up},
            "indices": idx, "sectors": sector_table(d, moves, sec_of, nifty_chg),
            "top_gainers": [row(n) for n in ranked[:12]], "top_losers": [row(n) for n in ranked[::-1][:12]],
            "most_active_vs_usual": [row(n) for n in sorted((n for n in moves if moves[n]["relvol"]), key=lambda n: -moves[n]["relvol"])[:10]],
            "tradable": tradable,
            "tradable_stocks": [{"symbol": n, "sector": sec_of.get(n), "change_pct": round(moves[n]["chg"], 2),
                                 "vol_x": None if moves[n]["relvol"] is None else round(moves[n]["relvol"], 1),
                                 **{k: moves[n]["levels"][k] for k in ("price", "high_so_far", "low_so_far", "opening_range_15m")}}
                                for n in tradable if n in moves],
            "rules": {"capital": CAPITAL, "risk_per_trade": RISK, "max_trades": MAX_TRADES, "entry": ENTRY, "exit": f"stop, 2R or {FLAT}"}}


# ---------------------------------------------------------------- carrying out the plan

def _bs_delta(spot: float, k: float, t: float, iv: float | None, call: bool) -> float:
    if not iv or iv <= 0:
        return 0.5 if call else -0.5
    v = iv / 100 if iv > 1 else iv
    d1 = (math.log(spot / k) + 0.5 * v * v * t) / (v * math.sqrt(t))
    n = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return n if call else n - 1


def carry_out(tr: dict, d: date, lots: dict, used_premium: float) -> dict:
    name, call = tr["underlying"], tr["option"] == "CALL"
    kind = "index" if name in INDICES else "stock"
    out = {"underlying": name, "option": tr["option"], "conviction": tr.get("conviction"), "asked_stop": tr.get("stop_level")}
    und = bars1(name, d)
    if und is None:
        return dict(out, taken=False, why="no 1-minute candles")
    seen = und[und.index.strftime("%H:%M") < ENTRY]
    after = und[und.index.strftime("%H:%M") >= ENTRY]
    if after.empty:
        return dict(out, taken=False, why="no candles after entry")
    spot = float(after["open"].iloc[0])
    stop = float(tr.get("stop_level") or 0)
    ok = (stop < spot) if call else (stop > spot)
    if not ok or abs(spot - stop) / spot * 100 > MAX_STOP[kind]:
        stop = float(seen["low"].min()) if call else float(seen["high"].max())
        out["stop_note"] = "replaced by the day's " + ("low" if call else "high") + " so far"
        if abs(spot - stop) / spot * 100 > MAX_STOP[kind] or abs(spot - stop) <= 0:
            return dict(out, taken=False, why=f"stop too far ({abs(spot - stop) / spot * 100:.1f}%)")
    floor_pts = spot * MIN_STOP[kind] / 100
    if abs(spot - stop) < floor_pts:
        stop = spot - floor_pts if call else spot + floor_pts
        out["stop_note"] = (out.get("stop_note", "") + "; widened to the minimum").lstrip("; ")
    risk_pts = abs(spot - stop)
    target = spot + 2 * risk_pts if call else spot - 2 * risk_pts
    leg = "CE" if call else "PE"
    chain = options_day(name, d)
    first = chain.get((ENTRY, leg)) or {}
    atm = next((k for k, r in first.items() if r["offset"] == 0), None)
    if atm is None:
        return dict(out, taken=False, why="no option price at entry")
    path = {t: rows[atm] for (t, lg), rows in chain.items() if lg == leg and atm in rows}
    e_row = path[ENTRY]
    slip = SLIP[kind]
    entry = round(e_row["open"] * (1 + slip), 2)
    lot = int(lots.get(name) or 0)
    if not lot:
        return dict(out, taken=False, why="no lot size")
    dte = max((bt.expiries(name, d)[0] - d).days, 0.5)
    delta = abs(_bs_delta(spot, atm, dte / 365.25, e_row.get("iv"), call))
    per_unit = delta * risk_pts + entry * slip * 2
    n_lots = int(RISK // (per_unit * lot))
    if n_lots < 1:
        if per_unit * lot > RISK * 1.4:
            return dict(out, taken=False, why=f"one lot risks about Rs {per_unit * lot:,.0f}")
        n_lots = 1
    n_lots = min(n_lots, int(MAX_PREMIUM // (entry * lot)), int((CAPITAL - used_premium) // (entry * lot)))
    if n_lots < 1:
        return dict(out, taken=False, why="premium doesn't fit the capital")
    qty = n_lots * lot
    exit_t, reason = None, FLAT
    for t, b in after.iterrows():
        hm = t.strftime("%H:%M")
        if hm >= FLAT:
            exit_t = hm
            break
        if (call and b["low"] <= stop) or (not call and b["high"] >= stop):
            exit_t, reason = hm, "stop"
            break
        if (call and b["high"] >= target) or (not call and b["low"] <= target):
            exit_t, reason = hm, "target"
            break
    known = sorted(t for t in path if t <= (exit_t or FLAT))
    if not known:
        return dict(out, taken=False, why="no option price at exit")
    x_row = path[known[-1]]
    px = x_row["open"] if reason == FLAT and known[-1] == exit_t else x_row["close"]
    sell = round(px * (1 - slip), 2)
    costs = common.charges([(True, entry * qty), (False, sell * qty)])
    net = round((sell - entry) * qty - costs, 2)
    return dict(out, taken=True, strike=atm, spot=spot, stop=round(stop, 2), target=round(target, 2), entry=entry, exit=sell, qty=qty, lots=n_lots,
                exit_at=exit_t, reason=reason, premium=round(entry * qty, 2), costs=round(costs, 2), net=net, r=round(net / RISK, 2))


# ---------------------------------------------------------------- one day

def _context() -> tuple[list[str], dict, dict]:
    meta = json.loads((bt.OUT / "meta.json").read_text(encoding="utf-8"))
    lots = json.loads((bt.KITE / "lots.json").read_text(encoding="utf-8"))
    return meta["fno"], meta["groups"], lots


def run_day(d: date, tool: str | None, say=print) -> dict:
    path = OUT / "days" / f"{d.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    fno, groups, lots = _context()
    m = build_map(d, fno, groups)
    ct.SCHEMA, ct.INTRO = SCHEMA, INTRO
    ct.system_prompt = lambda: SYSTEM
    ct._usage.clear()
    try:
        plan = ct.ask(m, tool)
    except ValueError as exc:
        if "usage limit" in str(exc):
            raise
        time.sleep(5)
        plan = ct.ask(m, tool)
    rec = {"day": d.isoformat(), "plan": plan, "usage": dict(ct._usage), "map_chars": len(json.dumps(m)), "trades": []}
    used = 0.0
    for tr in (plan.get("trades") or [])[:MAX_TRADES]:
        if tr.get("underlying") not in m["tradable"]:
            rec["trades"].append({"underlying": tr.get("underlying"), "taken": False, "why": "not tradable today"})
            continue
        res = carry_out(tr, d, lots, used)
        if res.get("taken"):
            used += res["premium"]
        rec["trades"].append(res)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    net = sum(t.get("net") or 0 for t in rec["trades"])
    say(f"{d} {plan.get('regime')} trades {sum(1 for t in rec['trades'] if t.get('taken'))} net {net:+,.0f} | "
        + "; ".join(f"{t['underlying']} {t.get('option', '')} {t.get('reason') or t.get('why')}" for t in rec["trades"]))
    return rec


def report() -> dict:
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((OUT / "days").glob("*.json"))]
    months: dict = {}
    for r in recs:
        mo = months.setdefault(r["day"][:7], {"days": 0, "trades": 0, "wins": 0, "net": 0.0, "no_trade_days": 0, "usd": 0.0})
        mo["days"] += 1
        taken = [t for t in r["trades"] if t.get("taken")]
        mo["trades"] += len(taken)
        mo["wins"] += sum(1 for t in taken if t["net"] > 0)
        mo["net"] = round(mo["net"] + sum(t["net"] for t in taken), 2)
        mo["no_trade_days"] += not taken
        mo["usd"] = round(mo["usd"] + ((r.get("usage") or {}).get("cost_usd") or 0), 2)
    allt = [t for r in recs for t in r["trades"] if t.get("taken")]
    out = {"months": months, "days": len(recs), "trades": len(allt), "wins": sum(1 for t in allt if t["net"] > 0),
           "net": round(sum(t["net"] for t in allt), 2), "index_net": round(sum(t["net"] for t in allt if t["underlying"] in INDICES), 2),
           "stock_net": round(sum(t["net"] for t in allt if t["underlying"] not in INDICES), 2),
           "exits": {k: sum(1 for t in allt if t["reason"] == k) for k in ("stop", "target", FLAT)}}
    (OUT / "summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dry", "run", "worker", "report"])
    ap.add_argument("day", nargs="?")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--days", default="")
    ap.add_argument("--upto", default="", help="only days up to this date (when later option data isn't downloaded yet)")
    a = ap.parse_args()
    if a.cmd == "dry":
        fno, groups, lots = _context()
        d = date.fromisoformat(a.day)
        m = build_map(d, fno, groups)
        print(json.dumps({k: m[k] for k in ("india_vix", "breadth_fno", "sectors", "tradable")}, indent=0)[:3000])
        print("map chars", len(json.dumps(m)))
        for tr in ({"underlying": "NIFTY", "option": "CALL", "stop_level": 0}, {"underlying": m["tradable"][-1], "option": "PUT", "stop_level": 0}):
            print(carry_out(tr, d, lots, 0))
    elif a.cmd == "worker":
        tool = ct.claude_tool()
        for s in a.days.split(","):
            run_day(date.fromisoformat(s), tool, lambda x: print(x, flush=True))
    elif a.cmd == "run":
        todo = [d.isoformat() for d in days() if not (OUT / "days" / f"{d.isoformat()}.json").exists() and (not a.upto or d.isoformat() <= a.upto)]
        OUT.mkdir(parents=True, exist_ok=True)
        procs = []
        for i in range(a.workers):
            ch = todo[i::a.workers]
            if ch:
                log = open(OUT / f"worker{i}.log", "a", encoding="utf-8")
                procs.append(subprocess.Popen([sys.executable, "-m", "strategies.claude_planner_backtest", "worker", "--days", ",".join(ch)],
                                              cwd=str(Path(__file__).resolve().parents[1]), stdout=log, stderr=subprocess.STDOUT))
        for p in procs:
            p.wait()
        print(json.dumps(report(), indent=1))
    else:
        print(json.dumps(report(), indent=1))


if __name__ == "__main__":
    main()
