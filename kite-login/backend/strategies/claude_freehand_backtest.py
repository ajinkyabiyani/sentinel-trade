"""Claude trading on its own judgement, replayed on past days with real option prices. No fixed strategy and no fixed exits.

Each month is its own book: Rs 1 lakh, flat at the start, flat at the end. Through each day Claude looks at the market
(Nifty, Bank Nifty, India VIX, sectors and breadth, the moving option-tradable stocks with their candles and option
chains, its own book) and decides everything: what to buy, how many lots, when to add, when to sell, where to keep a stop
or a profit order, and when to look next (15 to 120 minutes later). Only option buying; positions may be held overnight,
but everything is sold by 15:20 on the contract's expiry day and on the month's last trading day.

The code only carries out the orders: a buy or sell fills at the next minute's open of that exact contract on Dhan's real
1-minute premium, with slippage (0.3% index, 1% stock options) and charges; a stop or profit order Claude has set fills
when a 1-minute candle trades through it. Only days after Claude's training cutoff (June 2026) are used.

    python -m strategies.claude_freehand_backtest dry 2026-07-14 10:45     (one look's snapshot, no Claude)
    python -m strategies.claude_freehand_backtest run --months 2026-07,2026-08 --workers 2
    python -m strategies.claude_freehand_backtest report
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from quant import candles
from strategies import claude_planner_backtest as pl
from strategies import claude_trader as ct
from strategies import claude_trader_backtest as bt
from strategies import common

RULES = os.environ.get("FREEHAND_RULES", "")      # "v2": the five rules from the Jul-Nov 2026 review
OUT = common.CACHE_DIR / ("claude_freehand_backtest" + (f"_{RULES}_{os.environ.get('BACKTEST_FROM', '')[:4]}" if RULES else ""))
V2_PREMIUM, V2_MAX_1LOT, V2_STOP, V2_LAST_BUY = 15000.0, 22500.0, 0.30, "13:00"
V2S_STOCK_1LOT = 35000.0     # v2s: a stock option lot (median ATM about Rs 19k) may cost up to this; it buys 1 lot
OPT = candles.ROOT / "options_1m"
CAPITAL = 100000.0
SLIP = {"index": 0.003, "stock": 0.01}
FIRST, LAST_LOOK, FLAT = "09:30", "15:15", "15:20"
INDICES = ("NIFTY", "BANKNIFTY")
STOCKS_SHOWN = 8
NEXT_LOOK = [15, 30, 60, 120]

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["read", "actions", "watch", "next_look_in_minutes"],
    "properties": {
        "read": {"type": "string", "description": "Two to four sentences: what you see and why you act (or wait)"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "instrument", "lots", "stop_premium", "target_premium", "why"],
            "properties": {
                "type": {"type": "string", "enum": ["BUY", "SELL", "SET_ORDERS"]},
                "instrument": {"type": "string", "description": "A tradingsymbol from a chain in the snapshot (BUY) or from your open positions"},
                "lots": {"type": "integer", "minimum": 0, "description": "BUY: lots to buy. SELL: lots to sell (0 = all). SET_ORDERS: ignored"},
                "stop_premium": {"type": ["number", "null"], "description": "Premium at which to sell automatically; null for none"},
                "target_premium": {"type": ["number", "null"], "description": "Premium at which to take profit automatically; null for none"},
                "why": {"type": "string"}}}},
        "watch": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                  "description": "Stocks whose option chains you want in the next snapshot (from other_tradable)"},
        "next_look_in_minutes": {"type": "integer", "enum": NEXT_LOOK},
    },
}
SYSTEM = """You are Claude, trading Indian index and stock options on your own judgement with Rs 1,00,000 of your own capital.
This is your book and your style: no strategy is given to you and no rule decides for you. You buy calls and puts (you
never write options), choose the contract, the size, when to add, when to cut, when to take profit, and when to stay out.

How orders work: a BUY or SELL fills at the next minute's open of that contract, at the real traded premium, with slippage
of about 0.3% each way on index options and 1% on stock options, plus brokerage and taxes. You may leave a stop and a
profit order on any position (stop_premium / target_premium); they fill automatically when the premium trades through
them between your looks. You choose when to look next. You can hold overnight, but everything is sold at 15:20 on the
contract's expiry day and on the last trading day of the month. Premium paid can't exceed your cash.

You are judged on money made each month after all costs. Think like a professional: time decay works against every
option you hold, the spread and slippage are paid on every round trip, choppy days punish buyers, and doing nothing is a
position. Answer only with the JSON object the schema asks for."""
INTRO = "Here is the market now and your book. Decide."
V2_TEXT = f"""

House rules for this book (learned from your own earlier results, enforced by the code):
- Every BUY gets the same size: about Rs {V2_PREMIUM:,.0f} of premium (whole lots; your lots number is ignored). Your biggest,
  most confident positions were your worst ones, so size never depends on conviction.
- Every position gets a stop {V2_STOP:.0%} below its entry premium. Your old stops (about 12%) were inside normal noise. You
  can raise a stop only to breakeven or above, never tighten it below entry. Profit orders are ignored: let winners run and
  SELL yourself when the idea is done.
- No new BUY at or after {V2_LAST_BUY}: your afternoon entries lost to time decay.
- Index options only (Nifty and Bank Nifty)."""
V2S_TEXT = V2_TEXT.replace("- Index options only (Nifty and Bank Nifty).", "- Nifty, Bank Nifty and stock options are all allowed. Stock options cost about 1% each way in slippage and\n  move only on real stock moves, so buy them when a stock or its sector is clearly moving. A stock option buy is 1 lot, up to Rs 35,000.")


# ---------------------------------------------------------------- option prices

_day: dict = {"key": None, "rows": {}}


def option_rows(name: str, d: date) -> dict:
    """{(HH:MM, CE/PE): {strike: (o, h, l, c, iv, oi, vol, offset)}} for name's nearest expiry on day d (cached per day)."""
    if _day["key"] != d:
        _day.update(key=d, rows={})
    rows = _day["rows"]
    if name not in rows:
        out: dict = {}
        path = OPT / name / f"{d:%Y-%m}.csv.gz"
        if path.exists():
            iso = d.isoformat()
            with gzip.open(path, "rt") as f:
                for r in csv.DictReader(f):
                    if r["date"] != iso or not r["close"] or not r["strike"]:
                        continue
                    out.setdefault((r["time"], r["type"]), {})[float(r["strike"])] = tuple(
                        float(r[k] or 0) for k in ("open", "high", "low", "close", "iv", "oi", "volume", "offset"))
        rows[name] = out
    return rows[name]


def contract_path(c: dict, d: date) -> dict:
    """{HH:MM: (o, h, l, c)} of one contract on day d."""
    return {t: v[c["strike"]][:4] for (t, leg), v in option_rows(c["name"], d).items() if leg == c["leg"] and c["strike"] in v}


def price_at(c: dict, d: date, t: str, field: int = 3) -> float | None:
    """The contract's price in the last minute before t (field 3 = close) or, with field 0, the open of minute t."""
    p = contract_path(c, d)
    if field == 0:
        later = sorted(x for x in p if x >= t)
        return p[later[0]][0] if later else None
    before = sorted(x for x in p if x < t)
    return p[before[-1]][3] if before else None


def chain(name: str, d: date, t: str, each: int) -> list[dict]:
    """ATM +/- each strikes at the last minute before t: ltp, iv, oi and volume so far, with each contract's symbol."""
    rows = option_rows(name, d)
    stamps = sorted({tm for tm, _ in rows if tm < t})
    if not stamps:
        return []
    last = stamps[-1]
    exp = bt.expiries(name, d)[0]
    out = {}
    for leg in ("CE", "PE"):
        for k, v in (rows.get((last, leg)) or {}).items():
            if abs(v[7]) > each:
                continue
            vol = sum(rows[(tm, leg)][k][6] for tm in stamps if k in rows.get((tm, leg), {}))
            sym = bt.tsym(name, exp, k, leg)
            out.setdefault(k, {"strike": k})[leg.lower()] = {"symbol": sym, "ltp": v[3], "iv": round(v[4], 1), "oi": int(v[5]), "volume": int(vol)}
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- the snapshot

def candles_today(sym: str, d: date, t: str, n: int) -> list:
    x = pl.bars5(sym).get(d)
    if x is None:
        return []
    seen = x[x.index.strftime("%H:%M") < common.plus_minutes(t, -4)]
    return [[i.strftime("%H:%M"), round(r["open"], 2), round(r["high"], 2), round(r["low"], 2), round(r["close"], 2), int(r["volume"])] for i, r in seen.tail(n).iterrows()]


def levels(sym: str, d: date, t: str) -> dict | None:
    x = pl.bars5(sym).get(d)
    if x is None:
        return None
    seen = x[x.index.strftime("%H:%M") < common.plus_minutes(t, -4)]
    if seen.empty:
        return None
    pds = pl.prev_days(sym, d, 10)
    if not pds:
        return None
    y = pl.bars5(sym)[pds[-1]]
    prev = float(y["close"].iloc[-1])
    now = float(seen["close"].iloc[-1])
    closes = [float(pl.bars5(sym)[p]["close"].iloc[-1]) for p in pds]
    return {"price": now, "change_pct": round((now / prev - 1) * 100, 2), "open": float(x["open"].iloc[0]), "high": float(seen["high"].max()),
            "low": float(seen["low"].min()), "prev_close": prev, "prev_day_low_high": [float(y["low"].min()), float(y["high"].max())],
            "last_10_sessions_pct": [round((b / a - 1) * 100, 2) for a, b in zip(closes, closes[1:])]}


def snapshot(d: date, t: str, book: dict, fno: list[str], groups: dict, watch: list[str]) -> dict:
    pl.READ = t                                     # the planner's helpers read the market as of this time
    moves, sec_of = pl.stock_moves(d, fno, groups)
    nifty_chg = (levels("NIFTY", d, t) or {}).get("change_pct") or 0.0
    stocks = sorted(n for n in moves if n not in INDICES and (OPT / n / f"{d:%Y-%m}.csv.gz").exists() and bt.expiries(n, d)[0] >= d)
    movers = sorted(stocks, key=lambda n: -abs(moves[n]["chg"]))[:STOCKS_SHOWN]
    shown = list(dict.fromkeys(movers + [w for w in watch if w in stocks] + [p["name"] for p in book["open"] if p["name"] not in INDICES]))
    idx = {}
    for n in INDICES:
        lv = levels(n, d, t) or {}
        idx[n] = dict(lv, candles_5m=candles_today(n, d, t, 30), days_to_expiry=(bt.expiries(n, d)[0] - d).days, option_chain=chain(n, d, t, 5))
    vix = levels("INDIAVIX", d, t) or {}
    up = sum(1 for m in moves.values() if m["chg"] > 0)
    open_rows = []
    for p in book["open"]:
        now = price_at(p, d, t)
        open_rows.append({"instrument": p["symbol"], "lots": p["lots"], "lot_size": p["lot"], "avg_price": p["avg"], "now": now,
                          "unrealised": None if now is None else round((now - p["avg"]) * p["lots"] * p["lot"], 0),
                          "stop_premium": p.get("stop"), "target_premium": p.get("target"), "bought": p["since"],
                          "expiry": p["expiry"]})
    snap = {
        "as_of": f"{d.isoformat()} {t}", "weekday": d.strftime("%A"), "minutes_to_close": ct.mins("15:30") - ct.mins(t),
        "india_vix": {"now": vix.get("price"), "change_pct": vix.get("change_pct")}, "fno_breadth": {"up": up, "down": len(moves) - up},
        "indices": idx, "sectors": pl.sector_table(d, moves, sec_of, nifty_chg),
        "stocks": [dict(levels(n, d, t) or {}, symbol=n, sector=sec_of.get(n), lot_size=book["lots"].get(n),
                        first_hour_volume_x_avg=None if moves[n]["relvol"] is None else round(moves[n]["relvol"], 1),
                        days_to_expiry=(bt.expiries(n, d)[0] - d).days, candles_5m=candles_today(n, d, t, 12), option_chain=chain(n, d, t, 3))
                   for n in shown],
        "other_tradable": [{"symbol": n, "sector": sec_of.get(n), "change_pct": round(moves[n]["chg"], 2)} for n in stocks if n not in shown],
        "book": {"capital_at_month_start": CAPITAL, "cash": round(book["cash"], 0), "open": open_rows,
                 "realised_today": round(book["today"], 0), "realised_this_month": round(book["month"], 0)},
        "lot_sizes": {n: book["lots"].get(n) for n in INDICES},
    }
    return json.loads(json.dumps(snap, default=lambda o: o.item() if hasattr(o, "item") else str(o)))   # numpy numbers -> plain


# ---------------------------------------------------------------- orders

def _kind(name: str) -> str:
    return "index" if name in INDICES else "stock"


def resolve(symbol: str, snap: dict) -> dict | None:
    """The contract behind a symbol shown in a chain."""
    for n, ix in snap["indices"].items():
        for r in ix["option_chain"]:
            for leg in ("ce", "pe"):
                if (r.get(leg) or {}).get("symbol") == symbol:
                    return {"name": n, "strike": r["strike"], "leg": leg.upper(), "symbol": symbol}
    for s in snap["stocks"]:
        for r in s.get("option_chain") or []:
            for leg in ("ce", "pe"):
                if (r.get(leg) or {}).get("symbol") == symbol:
                    return {"name": s["symbol"], "strike": r["strike"], "leg": leg.upper(), "symbol": symbol}
    return None


def sell(book: dict, p: dict, lots: int, px: float, d: date, t: str, why: str) -> dict:
    slip = SLIP[_kind(p["name"])]
    qty = lots * p["lot"]
    out = px * (1 - slip)
    cost = common.charges([(True, p["avg"] * qty), (False, out * qty)])
    net = (out - p["avg"]) * qty - cost
    book["cash"] += out * qty
    book["today"] += net
    book["month"] += net
    p["lots"] -= lots
    rec = {"instrument": p["symbol"], "bought": p["since"], "sold": f"{d.isoformat()} {t}", "lots": lots, "buy": round(p["avg"], 2),
           "sell": round(out, 2), "costs": round(cost, 2), "net": round(net, 2), "why": why}
    book["closed"].append(rec)
    if p["lots"] <= 0:
        book["open"].remove(p)
    return rec


def act(book: dict, decision: dict, snap: dict, d: date, t: str) -> list[dict]:
    fill_t = common.plus_minutes(t, 1)
    done = []
    for a in decision.get("actions") or []:
        typ, sym = a.get("type"), a.get("instrument")
        held = next((p for p in book["open"] if p["symbol"] == sym), None)
        if typ == "BUY":
            c = resolve(sym, snap) or (dict(held) if held else None)
            lots = int(a.get("lots") or 0)
            lot = book["lots"].get(c["name"]) if c else None
            if not c or lots < 1 or not lot:
                done.append({"action": a, "ok": False, "why": "unknown contract, no lots or no lot size"})
                continue
            if RULES in ("v2", "v2s") and (t >= V2_LAST_BUY or (RULES == "v2" and c["name"] not in INDICES)):
                done.append({"action": a, "ok": False, "why": ("house rule: no buy at or after " + V2_LAST_BUY) if t >= V2_LAST_BUY else "house rule: index options only"})
                continue
            if bt.expiries(c["name"], d)[0] == d and t >= "15:00":
                done.append({"action": a, "ok": False, "why": "expiring contract after 15:00"})
                continue
            px = price_at(c, d, fill_t, field=0)
            if not px:
                done.append({"action": a, "ok": False, "why": "no price to fill at"})
                continue
            fill = px * (1 + SLIP[_kind(c["name"])])
            if RULES in ("v2", "v2s"):
                cap = V2S_STOCK_1LOT if (RULES == "v2s" and c["name"] not in INDICES) else V2_MAX_1LOT
                if fill * lot > cap:
                    done.append({"action": a, "ok": False, "why": f"one lot costs Rs {fill * lot:,.0f}, over the Rs {cap:,.0f} limit"})
                    continue
                lots = max(1, int(V2_PREMIUM // (fill * lot)))
            lots = min(lots, int(book["cash"] // (fill * lot)))
            if lots < 1:
                done.append({"action": a, "ok": False, "why": "not enough cash"})
                continue
            book["cash"] -= fill * lots * lot
            if held:
                held["avg"] = (held["avg"] * held["lots"] + fill * lots) / (held["lots"] + lots)
                held["lots"] += lots
                p = held
            else:
                p = dict(c, lots=lots, lot=lot, avg=fill, since=f"{d.isoformat()} {fill_t}", expiry=bt.expiries(c["name"], d)[0].isoformat())
                book["open"].append(p)
            p["stop"], p["target"] = a.get("stop_premium"), a.get("target_premium")
            if RULES in ("v2", "v2s"):
                p["stop"], p["target"] = round(p["avg"] * (1 - V2_STOP), 2), None
            done.append({"action": a, "ok": True, "fill": round(fill, 2), "lots": lots})
        elif typ == "SELL" and held:
            px = price_at(held, d, fill_t, field=0) or price_at(held, d, fill_t)
            if not px:
                done.append({"action": a, "ok": False, "why": "no price to fill at"})
                continue
            lots = held["lots"] if not a.get("lots") else min(int(a["lots"]), held["lots"])
            done.append({"action": a, "ok": True, "closed": sell(book, held, lots, px, d, fill_t, "Claude sold: " + str(a.get("why") or "")[:200])})
        elif typ == "SET_ORDERS" and held:
            if RULES in ("v2", "v2s"):
                new = a.get("stop_premium")
                if new is not None and new >= held["avg"] and new > (held.get("stop") or 0):
                    held["stop"] = new                      # only raised, and only to breakeven or above
                done.append({"action": a, "ok": True, "stop": held["stop"]})
                continue
            held["stop"], held["target"] = a.get("stop_premium"), a.get("target_premium")
            done.append({"action": a, "ok": True})
        else:
            done.append({"action": a, "ok": False, "why": "no such position"})
    return done


def walk(book: dict, d: date, frm: str, until: str) -> list[dict]:
    """Stop and profit orders between looks, minute by minute on each contract's own candles."""
    hits = []
    for p in list(book["open"]):
        if p.get("stop") is None and p.get("target") is None:
            continue
        path = contract_path(p, d)
        for m in sorted(path):
            if m < frm or m >= until or p not in book["open"]:
                continue
            o, h, lo, c = path[m]
            if p.get("stop") is not None and lo <= p["stop"]:
                hits.append(sell(book, p, p["lots"], min(o, p["stop"]), d, m, "stop order"))
                break
            if p.get("target") is not None and h >= p["target"]:
                hits.append(sell(book, p, p["lots"], max(o, p["target"]), d, m, "profit order"))
                break
    return hits


def force_flat(book: dict, d: date, month_end: bool) -> list[dict]:
    out = []
    for p in list(book["open"]):
        if month_end or p["expiry"] == d.isoformat():
            px = price_at(p, d, FLAT, field=0) or price_at(p, d, FLAT)
            if px is not None:
                out.append(sell(book, p, p["lots"], px, d, FLAT, "month end" if month_end else "expiry day"))
    return out


# ---------------------------------------------------------------- a month

def _context():
    fno, groups, lots = pl._context()
    return fno, groups, lots


def run_month(ym: str, tool: str | None, say=print) -> dict:
    path = OUT / "months" / f"{ym}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    fno, groups, lots = _context()
    days = [d for d in pl.days() if d.isoformat().startswith(ym)]
    if rec is None:
        rec = {"month": ym, "book": {"cash": CAPITAL, "open": [], "closed": [], "today": 0.0, "month": 0.0, "lots": lots},
               "looks": [], "daily": {}, "at": None, "watch": [], "done": False}
    if rec["done"]:
        return rec
    book = rec["book"]
    ct.SCHEMA, ct.INTRO = SCHEMA, INTRO
    ct.system_prompt = lambda: SYSTEM + ((V2_TEXT if RULES == "v2" else V2S_TEXT if RULES == "v2s" else ""))

    def save():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    for i, d in enumerate(days):
        if rec["at"] and rec["at"][:10] > d.isoformat():
            continue
        if not (rec["at"] and rec["at"][:10] == d.isoformat()):
            book["today"] = 0.0
            rec["at"] = f"{d.isoformat()} 09:15"
        t = rec["at"][11:]
        nxt = FIRST if t < FIRST else t
        while nxt <= LAST_LOOK:
            walk(book, d, t, nxt)
            snap = snapshot(d, nxt, book, fno, groups, rec["watch"])
            ct._usage.clear()
            try:
                dec = ct.ask(snap, tool)
            except ValueError as exc:
                if "usage limit" in str(exc):
                    save()
                    raise
                time.sleep(5)
                dec = ct.ask(snap, tool)
            res = act(book, dec, snap, d, nxt)
            rec["watch"] = list(dec.get("watch") or [])[:5]
            rec["looks"].append({"at": f"{d.isoformat()} {nxt}", "read": dec.get("read"), "results": res, "usage": dict(ct._usage),
                                 "next": dec.get("next_look_in_minutes"), "cash": round(book["cash"], 0), "open": len(book["open"])})
            t = common.plus_minutes(nxt, 1)
            step = int(dec.get("next_look_in_minutes") or 15)
            nxt = common.plus_minutes(nxt, max(15, step))
            rec["at"] = f"{d.isoformat()} {t}"
            save()
            bought = [r for r in res if r.get("ok") and r["action"]["type"] == "BUY"]
            sold = [r for r in res if r.get("ok") and r["action"]["type"] == "SELL"]
            if bought or sold:
                say(f"{d} {rec['looks'][-1]['at'][11:]} " + "; ".join(f"{r['action']['type']} {r['action']['instrument']} x{r.get('lots') or r['action']['lots']}" for r in bought + sold)
                    + f" | month {book['month']:+,.0f}")
        walk(book, d, t, FLAT)
        force_flat(book, d, month_end=(i == len(days) - 1))
        rec["daily"][d.isoformat()] = round(book["today"], 2)
        rec["at"] = f"{d.isoformat()} 23:59"
        save()
        say(f"{d} day {book['today']:+,.0f} | month {book['month']:+,.0f} | open {len(book['open'])}")
    rec["done"] = True
    save()
    return rec


def report() -> dict:
    out = {}
    for p in sorted((OUT / "months").glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        cl = r["book"]["closed"]
        out[r["month"]] = {"done": r["done"], "days": len(r["daily"]), "looks": len(r["looks"]), "trades": len(cl),
                           "wins": sum(1 for x in cl if x["net"] > 0), "net": round(r["book"]["month"], 2),
                           "costs": round(sum(x["costs"] for x in cl), 2), "green_days": sum(1 for v in r["daily"].values() if v > 0),
                           "red_days": sum(1 for v in r["daily"].values() if v < 0),
                           "usd": round(sum((lk.get("usage") or {}).get("cost_usd") or 0 for lk in r["looks"]), 2)}
    (OUT / "summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dry", "run", "worker", "report"])
    ap.add_argument("day", nargs="?")
    ap.add_argument("at", nargs="?", default="10:45")
    ap.add_argument("--months", default="2026-07,2026-08,2026-09,2026-10,2026-11")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    if a.cmd == "dry":
        fno, groups, lots = _context()
        d = date.fromisoformat(a.day)
        book = {"cash": CAPITAL, "open": [], "closed": [], "today": 0.0, "month": 0.0, "lots": lots}
        s = snapshot(d, a.at, book, fno, groups, [])
        print("chars", len(json.dumps(s)), "| stocks shown", [x["symbol"] for x in s["stocks"]])
        print("NIFTY", {k: s["indices"]["NIFTY"][k] for k in ("price", "change_pct", "days_to_expiry")}, "chain rows", len(s["indices"]["NIFTY"]["option_chain"]))
        sym = s["indices"]["NIFTY"]["option_chain"][5]["ce"]["symbol"]
        res = act(book, {"actions": [{"type": "BUY", "instrument": sym, "lots": 2, "stop_premium": None, "target_premium": None, "why": "test"}]}, s, d, a.at)
        print(res, "cash", round(book["cash"]))
        p = book["open"][0]
        p["stop"] = round(p["avg"] * 0.8, 2)
        print("walk:", walk(book, d, common.plus_minutes(a.at, 1), "15:20"), "| open", len(book["open"]))
        print("forced:", force_flat(book, d, True), "| month", round(book["month"]))
    elif a.cmd == "worker":
        tool = ct.claude_tool()
        for ym in a.months.split(","):
            run_month(ym, tool, lambda x: print(x, flush=True))
    elif a.cmd == "run":
        OUT.mkdir(parents=True, exist_ok=True)
        months = a.months.split(",")
        procs = []
        for i in range(a.workers):
            ch = months[i::a.workers]
            if ch:
                log = open(OUT / f"worker{i}.log", "a", encoding="utf-8")
                procs.append(subprocess.Popen([sys.executable, "-m", "strategies.claude_freehand_backtest", "worker", "--months", ",".join(ch)],
                                              cwd=str(Path(__file__).resolve().parents[1]), stdout=log, stderr=subprocess.STDOUT))
        for p in procs:
            p.wait()
        print(json.dumps(report(), indent=1))
    else:
        print(json.dumps(report(), indent=1))


if __name__ == "__main__":
    main()
