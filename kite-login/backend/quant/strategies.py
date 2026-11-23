"""The Quant Desk's strategy library: daily-bar rules, each tied to one regime, each explainable in a sentence.

Every strategy turns one symbol's candles into candidate trades (before any capital limit). Signals use the day's close and
earlier data only; entries fill at the next day's open (BTST: at the signal day's close), so nothing sees the future.
A trade's stop is checked against each later day's low (a gap through the stop fills at the open); exits on a close rule
fill at that close. Short-side strategies stand for buying puts: their rupee results are estimates from the stock's own move,
because past option premiums aren't available.
"""
from __future__ import annotations

from quant import indicators as ind

STRATEGIES = {
    "donchian_breakout": {
        "regime": "TRENDING_UP", "side": "long", "product": "CNC",
        "thesis": "Buy a stock making a new N-day high in a confirmed uptrend; ride it until it breaks its N/2-day low.",
        "edge": "Momentum: slow institutional buying keeps trends going longer than sellers expect; late sellers and short-covering pay.",
        "rules": "Entry: next open after close > highest high of the prior N days. Stop: entry − k×ATR(14). Exit: close < lowest low of the prior N/2 days.",
        "grid": [{"n": n, "k": k} for n in (20, 40, 55) for k in (2.0, 3.0)],
    },
    "rsi2_pullback": {
        "regime": "TRENDING_UP", "side": "long", "product": "CNC",
        "thesis": "Buy a short, sharp dip (RSI(2) below a threshold) in a stock that is in a confirmed uptrend.",
        "edge": "Short-term overreaction: weak hands sell a dip that the trend usually recovers; they pay the bounce.",
        "rules": "Entry: next open after RSI(2) < t and close > 200 EMA. Stop: entry − k×ATR(14). Exit: close > 5-day SMA, or after 10 days.",
        "grid": [{"t": t, "k": k} for t in (5, 10, 15) for k in (2.0, 3.0)],
    },
    "bollinger_revert": {
        "regime": "FLAT", "side": "long", "product": "CNC",
        "thesis": "In a range-bound stock, buy a close below the lower Bollinger band and sell at the middle band.",
        "edge": "Mean reversion in ranges: with no trend, stretches away from the average tend to snap back; panic sellers pay.",
        "rules": "Entry: next open after close < SMA20 − b×SD20. Stop: entry − k×ATR(14). Exit: close ≥ SMA20, or after 10 days.",
        "grid": [{"b": b, "k": k} for b in (1.5, 2.0, 2.5) for k in (1.5, 2.0)],
    },
    "rsi2_range": {
        "regime": "FLAT", "side": "long", "product": "CNC",
        "thesis": "In a range-bound stock, buy when RSI(2) is deeply oversold and sell on the first close above the 5-day average.",
        "edge": "The same overreaction, without a trend underneath: shorter holds, quicker exits.",
        "rules": "Entry: next open after RSI(2) < t. Stop: entry − 2×ATR(14). Exit: close > 5-day SMA, or after h days.",
        "grid": [{"t": t, "h": h} for t in (5, 10, 15) for h in (3, 5)],
    },
    "breakdown_put": {
        "regime": "TRENDING_DOWN", "side": "short", "product": "PUT (estimate)",
        "thesis": "Buy a put when a stock in a confirmed downtrend closes at a new N-day low; exit when it closes above its N/2-day high.",
        "edge": "Downside momentum: forced selling and stop-outs extend falls; late buyers of the dip pay.",
        "rules": "Entry: next open after close < lowest low of the prior N days. Stop: entry + k×ATR(14). Exit: close > highest high of the prior N/2 days. Results use the stock's move as a stand-in for the put.",
        "grid": [{"n": n, "k": k} for n in (20, 40) for k in (2.0, 3.0)],
    },
    "btst_strong_close": {
        "regime": "TRENDING_UP", "side": "long", "product": "BTST",
        "thesis": "Buy at the close a stock that finishes near its high on heavy volume above yesterday's high; sell at the next open.",
        "edge": "Overnight drift: strong closes on volume often see follow-through buying at the open from those who missed the day.",
        "rules": "Entry: the close, when close is in the top 10% of the day's range, volume > v × 20-day average and close > yesterday's high. Exit: next open. Stop: none overnight (a gap is the risk).",
        "grid": [{"v": v} for v in (1.5, 2.0, 2.5)],
    },
}


class Series:
    """One symbol's candles and indicators, computed once."""

    def __init__(self, sym: str, bars: list[list]):
        self.sym, self.bars = sym, bars
        self.d = [b[0] for b in bars]
        self.o = [b[1] for b in bars]
        self.h = [b[2] for b in bars]
        self.l = [b[3] for b in bars]
        self.c = [b[4] for b in bars]
        self.v = [b[5] for b in bars]
        self.regime = ind.regimes(bars)
        self.atr = ind.atr(self.h, self.l, self.c)
        self.e200 = ind.ema(self.c, 200)
        self.sma5 = ind.sma(self.c, 5)
        self.sma20 = ind.sma(self.c, 20)
        self.sd20 = ind.stdev(self.c, 20)
        self.rsi2 = ind.rsi(self.c, 2)
        self.vol20 = ind.sma([float(x) for x in self.v], 20)
        self.breaks = ind.split_breaks(bars)

    def hh(self, i: int, n: int) -> float | None:
        return max(self.h[i - n:i]) if i >= n else None

    def ll(self, i: int, n: int) -> float | None:
        return min(self.l[i - n:i]) if i >= n else None


def _trade(s: Series, sig: int, entry_i: int, entry: float, stop: float | None, side: str, exit_fn, max_hold: int | None, strategy: str, params: dict) -> dict | None:
    """Follows one trade from its entry until a stop, an exit rule or the time limit. None if it can't be followed."""
    last = len(s.c) - 1
    for j in range(entry_i, last + 1):
        if j in s.breaks and j > entry_i:
            return None                                   # a split inside the trade: unadjusted prices, skip it
        if stop is not None:
            if side == "long" and s.l[j] <= stop:
                px = min(s.o[j], stop) if j > entry_i else stop
                return dict(_base(s, sig, entry_i, entry, stop, side, strategy, params), exit_i=j, exit=px, reason="stop")
            if side == "short" and s.h[j] >= stop:
                px = max(s.o[j], stop) if j > entry_i else stop
                return dict(_base(s, sig, entry_i, entry, stop, side, strategy, params), exit_i=j, exit=px, reason="stop")
        if exit_fn(j):
            return dict(_base(s, sig, entry_i, entry, stop, side, strategy, params), exit_i=j, exit=s.c[j], reason="exit rule")
        if max_hold and j - entry_i + 1 >= max_hold:
            return dict(_base(s, sig, entry_i, entry, stop, side, strategy, params), exit_i=j, exit=s.c[j], reason="time")
    return dict(_base(s, sig, entry_i, entry, stop, side, strategy, params), exit_i=last, exit=s.c[last], reason="open")


def _base(s, sig, entry_i, entry, stop, side, strategy, params):
    return {"sym": s.sym, "strategy": strategy, "params": params, "side": side, "signal_day": s.d[sig], "entry_i": entry_i, "entry_day": s.d[entry_i], "entry": entry, "stop": stop,
            "turnover": s.c[sig] * s.v[sig]}


def candidates(s: Series, name: str, p: dict) -> list[dict]:
    """Every trade the rule would take on this symbol, one at a time (no new entry while one is open)."""
    st = STRATEGIES[name]
    out, i, n = [], 201, len(s.c)
    while i < n - 1:
        sig = None
        if s.regime[i] == st["regime"] and s.atr[i]:
            if name == "donchian_breakout":
                hh = s.hh(i, p["n"])
                sig = hh is not None and s.c[i] > hh
            elif name == "rsi2_pullback":
                sig = s.rsi2[i] is not None and s.rsi2[i] < p["t"] and s.e200[i] and s.c[i] > s.e200[i]
            elif name == "bollinger_revert":
                sig = s.sma20[i] and s.c[i] < s.sma20[i] - p["b"] * s.sd20[i]
            elif name == "rsi2_range":
                sig = s.rsi2[i] is not None and s.rsi2[i] < p["t"]
            elif name == "breakdown_put":
                ll = s.ll(i, p["n"])
                sig = ll is not None and s.c[i] < ll
            elif name == "btst_strong_close":
                rng = s.h[i] - s.l[i]
                sig = rng > 0 and (s.c[i] - s.l[i]) / rng >= 0.9 and s.vol20[i] and s.v[i] > p["v"] * s.vol20[i] and s.c[i] > s.h[i - 1]
        if not sig:
            i += 1
            continue
        a = s.atr[i]
        if name == "btst_strong_close":
            if i + 1 in s.breaks:
                i += 1
                continue
            t = dict(_base(s, i, i, s.c[i], None, "long", name, p), exit_i=i + 1, exit=s.o[i + 1], reason="next open")
        else:
            e = s.o[i + 1]
            if name == "donchian_breakout":
                half = max(2, p["n"] // 2)
                t = _trade(s, i, i + 1, e, e - p["k"] * a, "long", lambda j: s.ll(j, half) is not None and s.c[j] < s.ll(j, half), None, name, p)
            elif name == "rsi2_pullback":
                t = _trade(s, i, i + 1, e, e - p["k"] * a, "long", lambda j: s.sma5[j] is not None and s.c[j] > s.sma5[j], 10, name, p)
            elif name == "bollinger_revert":
                t = _trade(s, i, i + 1, e, e - p["k"] * a, "long", lambda j: s.sma20[j] is not None and s.c[j] >= s.sma20[j], 10, name, p)
            elif name == "rsi2_range":
                t = _trade(s, i, i + 1, e, e - 2.0 * a, "long", lambda j: s.sma5[j] is not None and s.c[j] > s.sma5[j], p["h"], name, p)
            else:
                half = max(2, p["n"] // 2)
                t = _trade(s, i, i + 1, e, e + p["k"] * a, "short", lambda j: s.hh(j, half) is not None and s.c[j] > s.hh(j, half), None, name, p)
        if t:
            t["exit_day"] = s.d[t["exit_i"]]
            t["atr"] = a
            out.append(t)
            i = t["exit_i"] + 1
        else:
            i += 1
    return out
