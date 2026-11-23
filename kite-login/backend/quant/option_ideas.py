"""Four option-buying ideas on Nifty, priced on Dhan's real 1-minute option premiums (2026). Rules written before running.

Common to all: buy the at-the-money Nifty option of the nearest weekly expiry; 0.3% slippage each way plus charges; capital
Rs 1 lakh; each trade sized so its hard premium stop loses about Rs 5,000 (one lot allowed up to Rs 6,000). Results in
rupees and R (net / 2,500). January-June and July-November reported separately, month by month. Nothing is tuned.

1. Volatility squeeze -> expansion. Signal day: Nifty's range is the smallest of the last 7 (NR7) or inside the previous day,
   and at the close ATM implied vol is below 20-day realised vol. Next day 09:20-14:00: first 1-minute close above the
   signal day's high buys a call, below its low a put. Exit: underlying back through the signal day's midpoint, or a move
   of 1.5x the signal day's range, or 15:15. Hard stop: premium -40%.
2. Donchian 20-day breakout, held for days. A close above the prior 20-day high buys a call at the next open (below the
   20-day low: a put), only if the nearest expiry is at least 4 days away. Exit at the next open after a close back
   through the 10-day low/high, after 3 sessions (15:15), or at 15:15 the day before expiry. Hard stop: premium -40%.
3. Expiry-day gamma. On a Nifty expiry day after 13:00 until 14:45: the first 1-minute close above the day's high so far
   buys a call, below the low a put. Exit: premium doubles, or -50%, or 15:15.
4. Gap and VIX-spike days. India VIX opens 8% or more above yesterday's close, or Nifty gaps 1% or more. At 09:30, if the
   first 15 minutes moved in the gap's direction, buy in that direction. Exit: underlying back through the other end of
   the first 15 minutes, a 2R move, or 15:15. Hard stop: premium -40%.

    python -m quant.option_ideas
"""
from __future__ import annotations

import gzip
import csv
import math
import sys
from datetime import date

import pandas as pd

from quant import candles
from strategies import claude_trader_backtest as bt
from strategies import common

OPT = candles.ROOT / "options_1m" / "NIFTY"
LOT, RISK, CAP_1LOT, SLIP = 65, 5000.0, 6000.0, 0.003   # Rs 5,000 risk a trade (the user's Quant Desk figure)
FROM, TO = "2026-01-01", "2026-11-20"

# ---------------------------------------------------------------- data

nifty1 = candles.load("NIFTY", 1)
N1 = {d: x for d, x in nifty1.groupby(nifty1.index.date)}
vix1 = candles.load("INDIAVIX", 1)
V1 = {d: x for d, x in vix1.groupby(vix1.index.date)}
DAYS = sorted(d for d in N1 if FROM <= d.isoformat() <= TO and len(N1[d]) > 300)
DAILY = pd.DataFrame({"o": [N1[d]["open"].iloc[0] for d in DAYS], "h": [N1[d]["high"].max() for d in DAYS],
                      "l": [N1[d]["low"].min() for d in DAYS], "c": [N1[d]["close"].iloc[-1] for d in DAYS]}, index=DAYS)

_month: dict = {}


def opt_day(d: date) -> dict:
    """{(HH:MM, CE/PE): {strike: (o, h, l, c, iv, offset)}} for the nearest weekly expiry on day d."""
    ym = f"{d:%Y-%m}"
    if ym not in _month:
        rows: dict = {}
        with gzip.open(OPT / f"{ym}.csv.gz", "rt") as f:
            for r in csv.DictReader(f):
                if not r["close"] or not r["strike"]:
                    continue
                rows.setdefault(r["date"], {}).setdefault((r["time"], r["type"]), {})[float(r["strike"])] = (
                    float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["iv"] or 0), int(float(r["offset"])))
        _month[ym] = rows
    return _month[ym].get(d.isoformat(), {})


def atm(d: date, t: str, leg: str) -> float | None:
    rows = opt_day(d).get((t, leg)) or {}
    return next((k for k, v in rows.items() if v[5] == 0), None)


def path(d: date, leg: str, strike: float) -> dict:
    """The contract's own 1-minute candles on day d: {HH:MM: (o, h, l, c)}."""
    return {t: v[strike][:4] for (t, lg), v in opt_day(d).items() if lg == leg and strike in v}


def atm_iv_close(d: date) -> float | None:
    ivs = []
    for leg in ("CE", "PE"):
        for t in ("15:25", "15:24", "15:20"):
            rows = opt_day(d).get((t, leg)) or {}
            hit = [v[4] for v in rows.values() if v[5] == 0 and v[4] > 0]
            if hit:
                ivs.append(hit[0])
                break
    return sum(ivs) / len(ivs) if ivs else None


def expiry(d: date) -> date:
    return bt.expiries("NIFTY", d)[0]


def nxt(t: str) -> str:
    return common.plus_minutes(t, 1)


# ---------------------------------------------------------------- one position

def size(entry: float, stop_frac: float) -> int:
    per_lot = entry * stop_frac * LOT
    lots = int(RISK // per_lot)
    return lots if lots >= 1 else (1 if per_lot <= CAP_1LOT else 0)


def book(name: str, d0: date, entry: float, exit_px: float, lots: int, reason: str, d1: date | None = None) -> dict:
    qty = lots * LOT
    buy, sell = entry * (1 + SLIP), exit_px * (1 - SLIP)
    net = (sell - buy) * qty - common.charges([(True, buy * qty), (False, sell * qty)])
    return {"idea": name, "day": d0.isoformat(), "exit_day": (d1 or d0).isoformat(), "entry": round(buy, 2), "exit": round(sell, 2),
            "lots": lots, "reason": reason, "net": round(net, 2), "r": round(net / RISK, 2)}


def intraday(name: str, d: date, t_in: str, call: bool, und_stop: float | None, und_target: float | None, prem_stop: float,
             prem_target: float | None = None) -> dict | None:
    """Buy the ATM option at the open of minute t_in, follow the underlying and the premium minute by minute until 15:15."""
    leg = "CE" if call else "PE"
    k = atm(d, t_in, leg)
    if k is None:
        return None
    p = path(d, leg, k)
    if t_in not in p:
        return None
    entry = p[t_in][0]
    lots = size(entry, prem_stop)
    if not lots or entry <= 0:
        return None
    und = N1[d]
    for t, b in und[und.index.strftime("%H:%M") >= t_in].iterrows():
        hm = t.strftime("%H:%M")
        o = p.get(hm)
        if hm >= "15:15":
            return book(name, d, entry, (o or p[max(x for x in p if x <= hm)])[0], lots, "15:15")
        if o:
            if o[2] <= entry * (1 - prem_stop):
                return book(name, d, entry, min(o[0], entry * (1 - prem_stop)), lots, "premium stop")
            if prem_target and o[1] >= entry * (1 + prem_target):
                return book(name, d, entry, max(o[0], entry * (1 + prem_target)), lots, "premium target")
        if und_stop is not None and ((call and b["low"] <= und_stop) or (not call and b["high"] >= und_stop)):
            return book(name, d, entry, (o or p[max(x for x in p if x <= hm)])[3], lots, "stop")
        if und_target is not None and ((call and b["high"] >= und_target) or (not call and b["low"] <= und_target)):
            return book(name, d, entry, (o or p[max(x for x in p if x <= hm)])[3], lots, "target")
    return None


# ---------------------------------------------------------------- the four ideas

def realised_vol(i: int) -> float | None:
    c = DAILY["c"].iloc[max(0, i - 20):i + 1]
    r = [math.log(b / a) for a, b in zip(c, c[1:])]
    if len(r) < 15:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252) * 100


def squeeze() -> list[dict]:
    out = []
    for i in range(7, len(DAYS) - 1):
        d, nd = DAYS[i], DAYS[i + 1]
        rng = DAILY["h"] - DAILY["l"]
        nr7 = rng.iloc[i] <= rng.iloc[i - 6:i + 1].min()
        inside = DAILY["h"].iloc[i] <= DAILY["h"].iloc[i - 1] and DAILY["l"].iloc[i] >= DAILY["l"].iloc[i - 1]
        iv, rv = atm_iv_close(d), realised_vol(i)
        if not (nr7 or inside) or not iv or not rv or iv >= rv:
            continue
        hi, lo = DAILY["h"].iloc[i], DAILY["l"].iloc[i]
        mid, width = (hi + lo) / 2, hi - lo
        x = N1[nd]
        for t, b in x[(x.index.strftime("%H:%M") >= "09:20") & (x.index.strftime("%H:%M") <= "14:00")].iterrows():
            hm = t.strftime("%H:%M")
            if b["close"] > hi or b["close"] < lo:
                call = b["close"] > hi
                tr = intraday("1 squeeze", nd, nxt(hm), call, mid, b["close"] + (1.5 if call else -1.5) * width, 0.40)
                if tr:
                    out.append(tr)
                break
    return out


def donchian() -> list[dict]:
    out, i = [], 20
    while i < len(DAYS) - 1:
        c = DAILY["c"].iloc[i]
        hi20, lo20 = DAILY["h"].iloc[i - 20:i].max(), DAILY["l"].iloc[i - 20:i].min()
        d0 = DAYS[i + 1]
        if (c > hi20 or c < lo20) and (expiry(d0) - d0).days >= 4:
            call = c > hi20
            leg = "CE" if call else "PE"
            k = atm(d0, "09:16", leg)
            p0 = path(d0, leg, k) if k else {}
            if k and "09:16" in p0:
                entry = p0["09:16"][0]
                lots = size(entry, 0.40)
                exp = expiry(d0)
                res, j = None, i + 1
                while j < len(DAYS) and res is None:
                    d = DAYS[j]
                    p = path(d, leg, k)
                    last_day = j == i + 3 or (j + 1 < len(DAYS) and DAYS[j + 1] >= exp)
                    for hm in sorted(p):
                        if hm < "09:16" and j == i + 1:
                            continue
                        if hm >= "15:15":
                            if last_day:
                                res = book("2 donchian", d0, entry, p[hm][0], lots, "time", d)
                            break
                        if p[hm][2] <= entry * 0.60:
                            res = book("2 donchian", d0, entry, min(p[hm][0], entry * 0.60), lots, "premium stop", d)
                            break
                    if res is None and not last_day:
                        lo10, hi10 = DAILY["l"].iloc[j - 9:j + 1].min(), DAILY["h"].iloc[j - 9:j + 1].max()
                        cl = DAILY["c"].iloc[j]
                        if (call and cl < lo10) or (not call and cl > hi10):
                            pn = path(DAYS[j + 1], leg, k)
                            if "09:16" in pn:
                                res = book("2 donchian", d0, entry, pn["09:16"][0], lots, "trailing exit", DAYS[j + 1])
                    j += 1
                if res and lots:
                    out.append(res)
                i = j
                continue
        i += 1
    return out


def expiry_gamma() -> list[dict]:
    out = []
    for d in DAYS:
        if expiry(d) != d:
            continue
        x = N1[d]
        before = x[x.index.strftime("%H:%M") < "13:00"]
        hi, lo = before["high"].max(), before["low"].min()
        for t, b in x[(x.index.strftime("%H:%M") >= "13:00") & (x.index.strftime("%H:%M") <= "14:45")].iterrows():
            if b["close"] > hi or b["close"] < lo:
                tr = intraday("3 expiry gamma", d, nxt(t.strftime("%H:%M")), b["close"] > hi, None, None, 0.50, prem_target=1.0)
                if tr:
                    out.append(tr)
                break
            hi, lo = max(hi, b["high"]), min(lo, b["low"])
    return out


def gap_days() -> list[dict]:
    out = []
    for i in range(1, len(DAYS)):
        d, pd_ = DAYS[i], DAYS[i - 1]
        gap = DAILY["o"].iloc[i] / DAILY["c"].iloc[i - 1] - 1
        v, vp = V1.get(d), V1.get(pd_)
        vix_jump = v is not None and vp is not None and v["open"].iloc[0] / vp["close"].iloc[-1] - 1 >= 0.08
        if abs(gap) < 0.01 and not vix_jump:
            continue
        x = N1[d]
        first = x[x.index.strftime("%H:%M") < "09:30"]
        move = first["close"].iloc[-1] - first["open"].iloc[0]
        direction = gap if abs(gap) >= 0.01 else -1.0          # a VIX spike without a gap: fear, so the put side
        if move * direction <= 0:
            continue
        call = direction > 0
        stop = first["low"].min() if call else first["high"].max()
        spot = first["close"].iloc[-1]
        risk = abs(spot - stop)
        if risk <= 0:
            continue
        tr = intraday("4 gap/VIX", d, "09:30", call, stop, spot + (2 if call else -2) * risk, 0.40)
        if tr:
            out.append(tr)
    return out


def main() -> None:
    allt = squeeze() + donchian() + expiry_gamma() + gap_days()
    df = pd.DataFrame(allt)
    if df.empty:
        print("no trades")
        return
    df["month"] = df["day"].str[:7]
    df["half"] = df["day"].apply(lambda s: "Jan-Jun" if s < "2026-07-01" else "Jul-Nov")
    for idea, g in df.groupby("idea"):
        print(f"\n== {idea}")
        for half, h in g.groupby("half", sort=False):
            print(f"   {half}: trades {len(h)}, wins {int((h.net > 0).sum())}, net Rs {h.net.sum():+,.0f}, avg {h.r.mean():+.2f}R")
        print("   by month:", {m: (len(v), int(v.net.sum())) for m, v in g.groupby("month")})
        print("   exits:", g.reason.value_counts().to_dict())
    df.to_csv(candles.ROOT / "option_ideas_trades.csv", index=False)


if __name__ == "__main__":
    sys.exit(main())
