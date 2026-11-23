"""Claude Trader on past days: every scheduled look rebuilt from history, decided by Claude exactly as live, booked on real
expired-option premiums from Dhan.

What is the same as live: the system prompt and playbook, the schema, the model and effort, the snapshot's shape (candles,
VWAP, opening range, yesterday's levels, ATR and realised vol, India VIX, the F&O movers, the sector board, option chains
with IV, OI and Greeks), every limit in check(), slippage, charges, the stop/target walk on 5-minute candles and 15:15.

What is not: no headlines and no futures flow board (neither exists for past days), no early looks (scheduled looks only),
bid and ask are modelled from the last price (Dhan keeps no quotes), today's lot sizes and today's F&O list are used, and
stops are checked on 5-minute candles only. Only days after Claude's training cutoff (June 2026) are used, so Claude can't
remember what happened next.

Run:  python -m strategies.claude_trader_backtest prefetch        (Kite candles for the window, once)
      python -m strategies.claude_trader_backtest run --workers 3
      python -m strategies.claude_trader_backtest report
"""
from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from strategies import claude_trader as ct
from strategies import common, dhan_data

india = ct.india
setups = ct.setups

OUT = common.CACHE_DIR / "claude_trader_backtest"
KITE = OUT / "kite"
RUN = OUT                                           # where a run's days and summary go: OUT for the first run, OUT/<variant> for others
INDEX_ONLY = False

# Variants: rule changes tried on new days only, never on the days that suggested them. Live Claude Trader is not touched.
V2_DAYS, V2_SEED = 60, 2026112102
V2_LAST_ENTRY = "13:30"
V2_RULES = """

Rules for this desk (in addition to everything above):
- Index options only: Nifty and Bank Nifty. The F&O movers and the sector board are context for the index read; you can't
  buy stock options.
- No new trade after 13:30. Afternoon time decay works hardest against a bought option.
- Put the stop where the index proves the trade wrong, not at a fixed % of the premium: just beyond a level from the
  snapshot (the opening range, yesterday's high or low, today's high or low, or the last clear 5-minute swing). Convert it to
  a premium level with the option's delta: stop premium = entry - |delta| x (points from the spot to that level), less a
  little for time decay. Say which level the stop sits behind in "invalidation". If that stop is too far for the risk
  limit or the reward to the next level isn't at least 1.5x, SKIP."""


def use_variant(name: str | None) -> None:
    """Point this process at a variant's folder and rules. Only the backtest process changes; the live desk never imports this."""
    global RUN, INDEX_ONLY, DAYS, SEED
    if not name:
        return
    if name != "v2":
        raise SystemExit(f"unknown variant {name}")
    RUN, INDEX_ONLY, DAYS, SEED = OUT / name, True, V2_DAYS, V2_SEED
    ct.LAST_RUN = V2_LAST_ENTRY                     # slots() and check() both read it
    ct.SYSTEM = ct.SYSTEM + V2_RULES
WINDOW = (date(2026, 7, 1), date(2026, 11, 19))     # after Claude's training cutoff
DAYS, SEED = 20, 20261120
INDEX_KITE = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}
VIX_KITE = "INDIA VIX"
SPREAD = {"index": 0.6, "stock": 1.5}               # assumed bid-ask, % of the price: Dhan keeps no quotes
CHAIN_STRIKES = {"index": 5, "stock": 3}            # Dhan keeps ATM+/-10 for indices near expiry, +/-3 for stocks
TRACK_STRIKES = {"index": 10, "stock": 3}
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def hm_minus(t: str, m: int) -> str:
    return common.plus_minutes(t, -m)


# ---------------------------------------------------------------- Kite history (cached)

def _kite():
    from strategies import fno_flow
    k = fno_flow._kite()
    if not k:
        raise RuntimeError("Kite isn't logged in")
    return k


def _tokens(k) -> dict[str, int]:
    from routes.market import _instrument_list
    return {r["tradingsymbol"]: r["instrument_token"] for r in _instrument_list(k, "NSE") if r.get("segment") in ("NSE", "INDICES")}


def lot_sizes(k) -> dict[str, int]:
    from routes.market import _instrument_list
    out = {}
    for r in sorted((r for r in _instrument_list(k, "NFO") if r.get("instrument_type") == "FUT"), key=lambda r: r["expiry"]):
        out.setdefault(r["name"], int(r["lot_size"]))
    return out


def _kite_rows(k, token: int, frm: date, to: date, interval: str) -> list[dict]:
    for attempt in range(4):
        try:
            return k.historical_data(token, frm, to, interval)
        except Exception:  # noqa: BLE001 - rate limit or a blip
            time.sleep(1 + attempt)
    return []


def prefetch(names: list[str]) -> None:
    """5-minute and daily candles for every name over the window, one file each."""
    k = _kite()
    tok = _tokens(k)
    KITE.mkdir(parents=True, exist_ok=True)
    (KITE / "lots.json").write_text(json.dumps(lot_sizes(k)), encoding="utf-8")
    start = WINDOW[0] - timedelta(days=10)
    for i, n in enumerate(names):
        path = KITE / f"{n.replace(' ', '_')}.json"
        if path.exists() or n not in tok:
            continue
        bars: dict[str, list] = {}
        a = start
        while a <= WINDOW[1]:
            b = min(a + timedelta(days=90), WINDOW[1])
            for r in _kite_rows(k, tok[n], a, b, "5minute"):
                t = r["date"].strftime("%H:%M")
                if "09:15" <= t <= "15:25":
                    bars.setdefault(r["date"].date().isoformat(), []).append({"start": t, "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "v": r["volume"]})
            time.sleep(0.35)
            a = b + timedelta(days=1)
        daily = [{"date": r["date"].date().isoformat(), "h": r["high"], "l": r["low"], "c": r["close"]}
                 for r in _kite_rows(k, tok[n], WINDOW[0] - timedelta(days=150), WINDOW[1], "day")]
        time.sleep(0.35)
        path.write_text(json.dumps({"bars": bars, "daily": daily}), encoding="utf-8")
        if i % 25 == 0:
            print(f"prefetch {i}/{len(names)} {n}", flush=True)


_mem: dict = {}


def hist(name: str) -> dict:
    if name not in _mem:
        path = KITE / f"{name.replace(' ', '_')}.json"
        _mem[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bars": {}, "daily": []}
    return _mem[name]


def closed(name: str, day: str, t: str) -> list[dict]:
    """The name's 5-minute candles that had closed by t."""
    return [b for b in hist(name)["bars"].get(day) or [] if common.plus_minutes(b["start"], 5) <= t]


def prev_day(name: str, day: str) -> str | None:
    ds = [d for d in hist(name)["bars"] if d < day]
    return max(ds) if ds else None


# ---------------------------------------------------------------- expiries and contracts

_open_days: set | None = None


def _is_trading(d: date) -> bool:
    """2026 from NSE's holiday list; other years from the days Nifty actually traded in the downloaded candles."""
    global _open_days
    if d.year == 2026:
        return india.trading_day(d)
    if _open_days is None:
        try:
            from quant import candles
            df = candles.load("NIFTY", 5)
            _open_days = set(df.index.date)
        except Exception:  # noqa: BLE001 - no candles: weekdays only
            _open_days = set()
    if not _open_days or not min(_open_days) <= d <= max(_open_days):
        return d.weekday() < 5
    return d in _open_days


def _back_to_trading(d: date) -> date:
    while not _is_trading(d):
        d -= timedelta(days=1)
    return d


def _expiry_weekday(d: date) -> int:
    """NSE moved Nifty, Bank Nifty and stock expiries from Thursday to Tuesday on 1 Sep 2025."""
    return 3 if d < date(2025, 9, 1) else 1


def _monthly(y: int, m: int) -> date:
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    while last.weekday() != _expiry_weekday(last):
        last -= timedelta(days=1)
    return _back_to_trading(last)


def expiries(sym: str, day: date) -> list[date]:
    """Upcoming expiries on or after day: Nifty weekly (Tuesday), everything else monthly (last Tuesday)."""
    out = set()
    if sym == "NIFTY":
        d = day - timedelta(days=7)
        while len(out) < 6:
            if d.weekday() == _expiry_weekday(d):
                e = _back_to_trading(d)
                if e >= day:
                    out.add(e)
            d += timedelta(days=1)
    for i in range(4):
        y, m = day.year + (day.month - 1 + i) // 12, (day.month - 1 + i) % 12 + 1
        e = _monthly(y, m)
        if e >= day and sym != "NIFTY":
            out.add(e)
        if sym == "NIFTY" and e >= day:
            out.add(e)
    return sorted(out)


def tsym(sym: str, exp: date, strike: float, leg: str) -> str:
    k = f"{strike:g}"
    if exp == _monthly(exp.year, exp.month):
        return f"{sym}{exp.year % 100}{MON[exp.month - 1]}{k}{leg}"
    return f"{sym}{exp.year % 100}{'123456789OND'[exp.month - 1]}{exp.day:02d}{k}{leg}"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def delta(spot: float, k: float, t: float, iv: float | None, call: bool) -> float | None:
    if not iv or iv <= 0 or t <= 0:
        return None
    v = iv / 100
    d1 = (math.log(spot / k) + 0.5 * v * v * t) / (v * math.sqrt(t))
    return round(_ncdf(d1) if call else _ncdf(d1) - 1, 3)


class Day:
    """One past session: the snapshot Claude reads at any time, and the premium of any contract it bought."""

    def __init__(self, day: str, fno: list[str], lots: dict, groups: dict):
        self.day, self.d = day, date.fromisoformat(day)
        self.fno, self.lots, self.groups = fno, lots, groups
        self.contracts: dict[str, dict] = {}
        self._bars: dict[str, dict] = {}

    # ---- the option chain from Dhan
    def code(self, sym: str, t: str) -> tuple[int, date]:
        ex = expiries(sym, self.d)
        if ex[0] == self.d and t >= ct.LATE_EXPIRY:
            return 2, ex[1]
        return 1, ex[0]

    def chain(self, sym: str, t: str) -> dict | None:
        kind = "index" if sym in ct.INDICES else "stock"
        code, exp = self.code(sym, t)
        n = CHAIN_STRIKES[kind]
        by_strike: dict[float, dict] = {}
        spot = atm = None
        t_years = max((exp - self.d).days / 365.25, 0.5 / 365.25)
        for k in range(-n, n + 1):
            for leg in ("CE", "PE"):
                s = dhan_data.rolling(sym, self.d, k, leg == "CE", code)
                before = [lb for lb in s if lb < t]                     # 1-minute candles closed by t
                c = s[max(before)] if before else None
                if not c or not c.get("c") or not c.get("strike"):
                    continue
                label = max(before)
                strike = float(c["strike"])
                spot = c.get("spot") or spot
                if k == 0:
                    atm = strike
                vol = sum((x.get("v") or 0) for lb, x in s.items() if lb <= label and x.get("strike") == strike)
                ltp = float(c["c"])
                half = ltp * SPREAD[kind] / 200
                sym_ = tsym(sym, exp, strike, leg)
                self.contracts[sym_] = {"sym": sym, "code": code, "call": leg == "CE", "strike": strike, "kind": kind}
                by_strike.setdefault(strike, {"strike": strike})[leg.lower()] = {
                    "tradingsymbol": sym_, "ltp": ltp, "bid": round(max(ltp - half, 0.05), 2), "ask": round(ltp + half, 2), "oi": c.get("oi"), "volume": vol,
                    "iv": round(c["iv"], 2) if c.get("iv") else None, "delta": delta(float(spot or strike), strike, t_years, c.get("iv"), leg == "CE")}
        if not by_strike or atm is None:
            return None
        rows = [by_strike[s] for s in sorted(by_strike)]
        a = by_strike[atm]
        ivs = [x["iv"] for x in (a.get("ce"), a.get("pe")) if x and x.get("iv")]
        straddle = (a.get("ce") or {}).get("ltp", 0) + (a.get("pe") or {}).get("ltp", 0) if a.get("ce") and a.get("pe") else None
        coi = sum((r.get("ce") or {}).get("oi") or 0 for r in rows)
        poi = sum((r.get("pe") or {}).get("oi") or 0 for r in rows)
        sup = max(rows, key=lambda r: (r.get("pe") or {}).get("oi") or 0)["strike"]
        res = max(rows, key=lambda r: (r.get("ce") or {}).get("oi") or 0)["strike"]
        return {"expiry": exp.isoformat(), "days_to_expiry": (exp - self.d).days, "lot_size": self.lots.get(sym), "atm": atm,
                "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None, "pcr": round(poi / coi, 2) if coi else None,
                "support": sup, "resistance": res, "max_pain": None, "straddle": round(straddle, 2) if straddle else None,
                "implied_move_pct": round(straddle / spot * 100, 2) if straddle and spot else None, "forward": spot, "spot": spot, "rows": rows}

    # ---- a held contract's candles
    def contract_bars(self, tsym_: str) -> dict:
        """The contract's own 1-minute candles {HH:MM: (o, h, l, c)}: at each minute, the offset whose strike is this contract's."""
        if tsym_ in self._bars:
            return self._bars[tsym_]
        c = self.contracts[tsym_]
        out, step = {}, None
        atm_s = dhan_data.rolling(c["sym"], self.d, 0, c["call"], c["code"])
        near = dhan_data.rolling(c["sym"], self.d, 1, c["call"], c["code"])
        for lb in atm_s:
            if lb in near and near[lb].get("strike") and atm_s[lb].get("strike"):
                step = abs(near[lb]["strike"] - atm_s[lb]["strike"]) or step
                if step:
                    break
        if not step:
            return out
        lim = TRACK_STRIKES[c["kind"]]
        for lb in sorted(atm_s):
            if not "09:15" <= lb < ct.FLAT_AT:
                continue
            a = atm_s.get(lb)
            if not a or not a.get("strike"):
                continue
            guess = round((c["strike"] - a["strike"]) / step)
            for k in (guess, guess - 1, guess + 1):
                if abs(k) > lim:
                    continue
                x = dhan_data.rolling(c["sym"], self.d, k, c["call"], c["code"]).get(lb)
                if x and x.get("strike") == c["strike"] and x.get("c"):
                    out[lb] = (x["o"], x["h"], x["l"], x["c"])
                    break
        self._bars[tsym_] = out
        return out

    def premium(self, tsym_: str, t: str) -> float:
        bars = self.contract_bars(tsym_)
        got = [bars[lb] for lb in sorted(bars) if lb < t]
        if not got:
            raise ValueError(f"no premium for {tsym_} at {t}")
        return float(got[-1][3])

    # ---- the snapshot
    def kname(self, sym: str) -> str:
        return INDEX_KITE.get(sym, sym)

    def movers(self, t: str) -> dict:
        rows = []
        for n in self.fno:
            today = closed(n, self.day, t)
            p = prev_day(n, self.day)
            if not today or not p:
                continue
            pc = hist(n)["bars"][p][-1]["c"]
            rows.append((today[-1]["c"] / pc - 1, n))
        rows.sort(reverse=True)
        return {"up": [n for x, n in rows if x > 0][:10], "down": [n for x, n in sorted(rows) if x < 0][:10], "pct": {n: round(x * 100, 2) for x, n in rows}}

    def underlying(self, sym: str, t: str) -> dict:
        kn = self.kname(sym)
        today = closed(kn, self.day, t)
        if len(today) < 3:
            raise ValueError("too few candles today")
        p = prev_day(kn, self.day)
        y = hist(kn)["bars"][p] if p else []
        vw = setups.vwap(today)[-1]
        chain = self.chain(sym, t)
        spot = today[-1]["c"]
        daily = [d for d in hist(kn)["daily"] if d["date"] < self.day]
        rv = ct.realised_vol([d["c"] for d in daily[-21:]])
        trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(daily[-15:], daily[-14:])]
        atr = sum(trs) / len(trs) if len(trs) >= 10 else None
        iv = (chain or {}).get("atm_iv")
        left = max(0, ct.mins("15:30") - ct.mins(t))
        vol = {"atm_iv": iv, "realised_vol_20d": rv, "iv_to_rv": round(iv / rv, 2) if iv and rv else None,
               "atr_14d": None if atr is None else round(atr, 2),
               "today_range_vs_atr": round((max(b["h"] for b in today) - min(b["l"] for b in today)) / atr, 2) if atr else None,
               "expected_move_rest_of_day": round(spot * iv / 100 * math.sqrt(left / 375 / 252), 2) if iv and left else None,
               "implied_move_to_expiry_pct": (chain or {}).get("implied_move_pct"),
               "days_to_expiry": (chain or {}).get("days_to_expiry"), "is_expiry_day": bool(chain and chain.get("expiry") == self.day)}
        return {"underlying": sym, "kind": "index" if sym in ct.INDICES else "stock", "spot": spot, "volatility": vol,
                "change_pct_vs_yesterday": round((today[-1]["c"] / y[-1]["c"] - 1) * 100, 2) if y else None,
                "today_open": today[0]["o"], "today_high": max(b["h"] for b in today), "today_low": min(b["l"] for b in today),
                "opening_range_15m": {"high": max(b["h"] for b in today[:3]), "low": min(b["l"] for b in today[:3])},
                "vwap": None if vw is None else round(vw, 2), "futures_flow": None,
                "yesterday": {"high": max(b["h"] for b in y), "low": min(b["l"] for b in y), "close": y[-1]["c"]} if y else None,
                "candles_5m_today": [[b["start"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in today[-30:]],
                "option_chain": ct.trim_chain(chain, CHAIN_STRIKES["index" if sym in ct.INDICES else "stock"]) if chain else None}

    def sectors(self, t: str, pct: dict) -> dict:
        data = {}
        for name in [ct.SECTOR_BASE] + [n for n, _, _ in ct.SECTORS]:
            today, p = closed(name, self.day, t), prev_day(name, self.day)
            if today and p:
                data[name] = (today, hist(name)["bars"][p][-1]["c"])
        base, rows = ct.sector_rows(data)
        if not rows:
            return {"error": "no sector data"}
        group = {n: g for n, _, g in ct.SECTORS}

        def stocks(g, best):
            if not g:
                return []
            names = set(self.groups.get("Banks") or []) - set(self.groups.get("PSU Banks") or []) if g == "Private banks" else set(self.groups.get(g) or [])
            xs = [{"symbol": n, "day_pct": pct[n]} for n in names if n in pct]
            xs.sort(key=lambda r: -r["day_pct"] if best else r["day_pct"])
            return xs[:ct.SECTOR_LEADERS]
        return {"nifty": base, "sectors": rows,
                "strongest_fno_stocks": {r["sector"]: stocks(group[r["index"]], True) for r in rows[:3]},
                "weakest_fno_stocks": {r["sector"]: stocks(group[r["index"]], False) for r in rows[-3:]},
                "note": "Day % is from yesterday's close. A sector whose rank rose a lot in the last hour is where money is moving now; one far ahead of Nifty all day may already be stretched."}

    def snapshot(self, t: str, rows: list[dict]) -> dict:
        m = self.movers(t)
        uni = list(ct.INDICES)
        for s in [] if INDEX_ONLY else m["up"][:ct.MOVERS_EACH_WAY] + m["down"][:ct.MOVERS_EACH_WAY]:
            if s not in uni:
                uni.append(s)
        markets, skipped = [], []
        for sym in uni:
            try:
                markets.append(self.underlying(sym, t))
            except (ValueError, KeyError, dhan_data.DhanError) as exc:
                if isinstance(exc, dhan_data.DhanError) and "token" in str(exc):
                    raise
                skipped.append(f"{sym}: {exc}")
        vt, vp = closed(VIX_KITE, self.day, t), prev_day(VIX_KITE, self.day)
        vix = vt[-1]["c"] if vt else None
        vprev = hist(VIX_KITE)["bars"][vp][-1]["c"] if vp else None
        now = datetime.combine(self.d, datetime.strptime(t, "%H:%M").time())
        return {
            "as_of": f"{self.day} {t}", "session_phase": india.phase(now)[1], "minutes_to_close": max(0, ct.mins("15:30") - ct.mins(t)),
            "india_vix": vix, "india_vix_change_pct": round((vix / vprev - 1) * 100, 2) if vix and vprev else None,
            "top_fno_movers": {"up": m["up"], "down": m["down"]}, "underlyings": markets, "not_available": skipped, "headlines": [],
            "sectors": self.sectors(t, m["pct"]), "flow": {"error": "futures flow not available"},
            "book": {"open": [{k: e.get(k) for k in ("instrument", "underlying", "entry", "stop", "target", "ltp", "qty", "entry_at")} for e in rows if e["status"] == "open"],
                     "today_pnl": ct.day_pnl(self.day, rows), "trades_today": len(rows), "stopped_out_today": ct.stopped_today(self.day, rows)},
            "limits": {"capital": ct.CAPITAL, "max_loss_at_stop_inr": ct.MAX_RISK, "max_open": ct.MAX_OPEN, "daily_loss_inr": ct.DAY_LOSS, "max_premium_inr": ct.MAX_PREMIUM,
                       "no_reentry_after_stop": "an underlying in book.stopped_out_today can't be bought again today", "min_reward_risk": ct.MIN_RR,
                       "max_strikes_from_atm": ct.MAX_STRIKES_AWAY, "max_spread_pct": ct.MAX_SPREAD, "last_entry": ct.LAST_RUN, "flat_by": ct.FLAT_AT},
        }

    # ---- following the book
    def advance(self, rows: list[dict], until: str) -> None:
        """Walk every open trade through its 1-minute candles that closed before until: the stop first, then the target
        (live checks the price every 15 seconds). At FLAT_AT anything still open goes out at the last price."""
        for e in rows:
            if e["status"] != "open":
                continue
            bars = self.contract_bars(e["instrument"])
            for m in sorted(bars):
                if m < e["look_at"] or m < e.get("walked", "") or m >= until or m >= ct.FLAT_AT:
                    continue
                o, h, lo, c = bars[m]
                e["walked"], e["ltp"] = common.plus_minutes(m, 1), c
                if lo <= e["stop"]:
                    ct.close(e, min(o, e["stop"]), "stop", self.day, e["walked"])
                    break
                if h >= e["target"]:
                    ct.close(e, max(o, e["target"]), "target", self.day, e["walked"])
                    break
            if e["status"] == "open" and until >= ct.FLAT_AT:
                ct.close(e, e.get("ltp") or e["entry"], "15:15 exit", self.day, ct.FLAT_AT)


# ---------------------------------------------------------------- one day, many days

def _all_days() -> list[str]:
    nifty = hist(INDEX_KITE["NIFTY"])["bars"]
    return [d for d in sorted(nifty) if WINDOW[0].isoformat() <= d <= WINDOW[1].isoformat() and india.trading_day(date.fromisoformat(d)) and len(nifty[d]) >= 70]


def first_run_days() -> list[str]:
    days = _all_days()
    return sorted(random.Random(20261120).sample(days, min(20, len(days))))


def pick_days() -> list[str]:
    """The first run's 20 days, or for a variant a fresh random draw from the days the first run never saw."""
    if RUN == OUT:
        return first_run_days()
    seen = set(first_run_days())
    pool = [d for d in _all_days() if d not in seen]
    return sorted(random.Random(SEED).sample(pool, min(DAYS, len(pool))))


def _context() -> tuple[list[str], dict, dict]:
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    return meta["fno"], json.loads((KITE / "lots.json").read_text(encoding="utf-8")), meta["groups"]


def run_day(day: str, tool: str, say=print) -> dict:
    path = RUN / "days" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"day": day, "looks": [], "trades": [], "done": False}
    if rec.get("done"):
        return rec
    fno, lots, groups = _context()
    D = Day(day, fno, lots, groups)
    rows: list[dict] = rec["trades"]
    for e in rows:                      # resumed: the contracts it holds need to be known again
        D.contracts[e["instrument"]] = e["contract"]
    done = {lk["at"] for lk in rec["looks"]}
    for t in ct.slots():
        if t in done:
            continue
        D.advance(rows, t)
        look = {"at": t}
        try:
            ctx = D.snapshot(t, rows)
            look["read"] = [u["underlying"] for u in ctx["underlyings"]]
            ct._usage.clear()
            try:
                d = ct.ask(ctx, tool)
            except ValueError as exc:
                if "usage limit" in str(exc):
                    raise
                time.sleep(5)
                d = ct.ask(ctx, tool)
            look["usage"] = dict(ct._usage)
            look["decision"] = {k: d.get(k) for k in ("action", "underlying", "instrument", "entry", "stop_loss", "target", "quantity", "confidence", "thesis", "invalidation")}
            now = datetime.combine(D.d, datetime.strptime(t, "%H:%M").time(), tzinfo=common.IST)
            res = ct.check(d, ctx, now, lambda key: D.premium(key.split(":", 1)[1], t), rows)
            look["checks"] = res["checks"]
            look["taken"] = res["ok"]
            if res["ok"]:
                e = dict(res["position"], id=f"{day}|{t}", look_at=t, contract=D.contracts[res["position"]["instrument"]])
                rows.append(e)
            else:
                look["why"] = res["why"]
        except dhan_data.DhanError:
            raise
        except ValueError as exc:
            if "usage limit" in str(exc):
                raise                       # stop the day here; a rerun resumes from this look
            look["error"] = f"ValueError: {exc}"
        except Exception as exc:  # noqa: BLE001 - one bad look is recorded and the day goes on
            look["error"] = f"{type(exc).__name__}: {exc}"
        rec["looks"].append(look)
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        d = look.get("decision") or {}
        say(f"{day} {t} {d.get('action') or 'ERR'} {d.get('instrument') or ''} {'TAKEN' if look.get('taken') else look.get('why') or look.get('error') or ''}"[:200])
    D.advance(rows, ct.FLAT_AT)
    rec["done"] = True
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    say(f"{day} done: {len(rows)} trades, net {sum(e.get('net') or 0 for e in rows):+,.0f}")
    return rec


def prefetch_dhan(days: list[str]) -> None:
    """The index option chains every look of a variant will read, fetched now while the Dhan token is valid."""
    for i, day in enumerate(days):
        d = date.fromisoformat(day)
        for sym in ct.INDICES:
            ex = expiries(sym, d)
            codes = [1, 2] if ex[0] == d else [1]           # expiry day: the next expiry after 13:00
            for code in codes:
                for k in range(-TRACK_STRIKES["index"], TRACK_STRIKES["index"] + 1):
                    for call in (True, False):
                        dhan_data.rolling(sym, d, k, call, code)
        print(f"dhan {i + 1}/{len(days)} {day}", flush=True)


def rewalk() -> list[dict]:
    """Re-follow a finished run's trades on 1-minute, strike-filtered candles: the same decisions, levels and size, the fill
    re-read at the look time. Claude is not asked again. Book effects (a slot freed earlier, no re-entry) are not replayed."""
    fno, lots, groups = _context()
    out = []
    for p in sorted((RUN / "days").glob("*.json")):
        r = json.loads(p.read_text(encoding="utf-8"))
        D = Day(r["day"], fno, lots, groups)
        for old in r["trades"]:
            D.contracts[old["instrument"]] = old["contract"]
            e = {k: old[k] for k in ("id", "day", "underlying", "instrument", "stop", "target", "qty", "slip", "look_at", "entry_at", "option_type", "contract")}
            e["status"] = "open"
            try:
                last = D.premium(e["instrument"], e["look_at"])
            except ValueError:
                out.append(dict(e, status="no data", was=old.get("net")))
                continue
            if not e["stop"] < last < e["target"]:
                out.append(dict(e, status="not taken", why=f"premium {last} outside stop/target", was=old.get("net")))
                continue
            e["entry"] = round(last * (1 + e["slip"]), 2)
            D.advance([e], ct.FLAT_AT)
            e["was"] = old.get("net")
            out.append(e)
    (RUN / "rewalk.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def report() -> dict:
    days = sorted((RUN / "days").glob("*.json"))
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in days]
    trades = [e for r in recs for e in r["trades"] if e["status"] == "closed"]
    looks = [lk for r in recs for lk in r["looks"]]
    wins = [e for e in trades if (e.get("net") or 0) > 0]
    net = sum(e["net"] for e in trades)
    gross_win, gross_loss = sum(e["net"] for e in wins), -sum(e["net"] for e in trades if e["net"] <= 0)
    per_day = {r["day"]: round(sum(e.get("net") or 0 for e in r["trades"]), 2) for r in recs}
    eq, peak, dd = 0.0, 0.0, 0.0
    for d in sorted(per_day):
        eq += per_day[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    out = {"days": len(recs), "days_done": sum(1 for r in recs if r.get("done")), "looks": len(looks),
           "buys_asked": sum(1 for lk in looks if (lk.get("decision") or {}).get("action") == "BUY"), "errors": sum(1 for lk in looks if lk.get("error")),
           "trades": len(trades), "wins": len(wins), "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
           "net": round(net, 2), "costs": round(sum(e.get("costs") or 0 for e in trades), 2), "avg_r": round(sum(e.get("r") or 0 for e in trades) / len(trades), 2) if trades else None,
           "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None, "max_drawdown": round(dd, 2),
           "green_days": sum(1 for v in per_day.values() if v > 0), "red_days": sum(1 for v in per_day.values() if v < 0), "per_day": per_day,
           "exits": {k: sum(1 for e in trades if e["exit_reason"] == k) for k in ("stop", "target", "15:15 exit")},
           "index_vs_stock": {k: round(sum(e["net"] for e in trades if (e["underlying"] in ct.INDICES) == (k == "index")), 2) for k in ("index", "stock")},
           "usage_usd": round(sum((lk.get("usage") or {}).get("cost_usd") or 0 for lk in looks), 2)}
    (RUN / "summary.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prefetch", "run", "worker", "report", "days", "dhan", "rewalk"])
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--days", default="")
    ap.add_argument("--variant", default="")
    a = ap.parse_args()
    use_variant(a.variant or None)
    if a.cmd == "prefetch":
        from routes import breakout
        fno = breakout.fno_names()
        groups = india.app_get("/api/market/movers").get("groups") or {}
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "meta.json").write_text(json.dumps({"fno": fno, "groups": groups}), encoding="utf-8")
        prefetch(list(INDEX_KITE.values()) + [VIX_KITE, ct.SECTOR_BASE] + [n for n, _, _ in ct.SECTORS] + fno)
        print("days:", pick_days())
    elif a.cmd == "days":
        print(pick_days())
    elif a.cmd == "dhan":
        prefetch_dhan(pick_days())
    elif a.cmd == "rewalk":
        for e in rewalk():
            print(e["day"], e["instrument"], e["status"], e.get("exit_reason"), e.get("net"), "was", e.get("was"), flush=True)
    elif a.cmd == "worker":
        tool = ct.claude_tool()
        for d in a.days.split(","):
            run_day(d, tool, lambda s: print(s, flush=True))
    elif a.cmd == "run":
        days = [d for d in pick_days() if not (RUN / "days" / f"{d}.json").exists() or not json.loads((RUN / "days" / f"{d}.json").read_text(encoding="utf-8")).get("done")]
        RUN.mkdir(parents=True, exist_ok=True)
        chunks = [days[i::a.workers] for i in range(a.workers)]
        procs = []
        for i, ch in enumerate(c for c in chunks if c):
            log = open(RUN / f"worker{i}.log", "a", encoding="utf-8")
            procs.append(subprocess.Popen([sys.executable, "-m", "strategies.claude_trader_backtest", "worker", "--days", ",".join(ch)] + (["--variant", a.variant] if a.variant else []),
                                          cwd=str(Path(__file__).resolve().parents[1]), stdout=log, stderr=subprocess.STDOUT))
        for p in procs:
            p.wait()
        print(json.dumps(report(), indent=1))
    else:
        print(json.dumps(report(), indent=1))


if __name__ == "__main__":
    main()
