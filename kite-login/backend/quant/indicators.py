"""Indicators and the regime label, computed only from candles up to each day (no look-ahead).

Regime (per instrument, per day), as the brief defines it:
  TRENDING_UP    close above a rising 200-day EMA, with ADX(14) at 20 or more
  TRENDING_DOWN  close below a falling 200-day EMA, with ADX(14) at 20 or more
  FLAT           ADX(14) under 20
  UNCLEAR        ADX at 20 or more but price and the EMA's slope disagree; no new trades
"Rising" means today's EMA is above its value 20 days earlier. A regime counts only once it has held for 3 days in a row;
until then the confirmed regime is the previous one.
"""
from __future__ import annotations

import math

EMA_LONG, SLOPE_LAG, ADX_N, ADX_FLAT, CONFIRM = 200, 20, 14, 20.0, 3


def ema(xs: list[float], n: int) -> list[float | None]:
    out, k, e = [], 2 / (n + 1), None
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(xs[i - n + 1:i + 1]) / n if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def sma(xs: list[float], n: int) -> list[float | None]:
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def stdev(xs: list[float], n: int) -> list[float | None]:
    out = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out.append(math.sqrt(sum((v - m) ** 2 for v in w) / n))
    return out


def atr(h, l, c, n: int = 14) -> list[float | None]:
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    out, a = [], None
    for i, t in enumerate(tr):
        if i < n - 1:
            out.append(None)
            continue
        a = sum(tr[i - n + 1:i + 1]) / n if a is None else (a * (n - 1) + t) / n
        out.append(a)
    return out


def adx(h, l, c, n: int = ADX_N) -> list[float | None]:
    """Wilder's ADX."""
    size = len(c)
    out = [None] * size
    if size < 2 * n + 1:
        return out
    tr, pdm, ndm = [0.0], [0.0], [0.0]
    for i in range(1, size):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    s_tr, s_p, s_n = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dx = []
    for i in range(n, size):
        if i > n:
            s_tr, s_p, s_n = s_tr - s_tr / n + tr[i], s_p - s_p / n + pdm[i], s_n - s_n / n + ndm[i]
        pdi, ndi = (100 * s_p / s_tr if s_tr else 0), (100 * s_n / s_tr if s_tr else 0)
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0)
        if len(dx) == n:
            out[i] = sum(dx) / n
        elif len(dx) > n:
            out[i] = (out[i - 1] * (n - 1) + dx[-1]) / n
    return out


def rsi(c: list[float], n: int) -> list[float | None]:
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    d = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, ad = sum(g[:n]) / n, sum(d[:n]) / n
    out[n] = 100 - 100 / (1 + ag / ad) if ad else 100.0
    for i in range(n + 1, len(c)):
        ag, ad = (ag * (n - 1) + g[i - 1]) / n, (ad * (n - 1) + d[i - 1]) / n
        out[i] = 100 - 100 / (1 + ag / ad) if ad else 100.0
    return out


def raw_regime(close, e200, slope_ok_up, slope_ok_dn, a) -> str | None:
    if e200 is None or a is None:
        return None
    if a < ADX_FLAT:
        return "FLAT"
    if close > e200 and slope_ok_up:
        return "TRENDING_UP"
    if close < e200 and slope_ok_dn:
        return "TRENDING_DOWN"
    return "UNCLEAR"


def regimes(bars: list[list]) -> list[str | None]:
    """The confirmed regime for each day, using that day's close and earlier days only."""
    c = [b[4] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    e = ema(c, EMA_LONG)
    a = adx(h, l, c)
    raw = []
    for i in range(len(c)):
        prev = e[i - SLOPE_LAG] if i >= SLOPE_LAG else None
        up = e[i] is not None and prev is not None and e[i] > prev
        dn = e[i] is not None and prev is not None and e[i] < prev
        raw.append(raw_regime(c[i], e[i], up, dn, a[i]))
    out, cur, run, last = [], None, 0, None
    for r in raw:
        run = run + 1 if r == last else 1
        last = r
        if r is not None and run >= CONFIRM:
            cur = r
        out.append(cur)
    return out


def split_breaks(bars: list[list], limit: float = 0.30) -> set[int]:
    """Days where the open is more than 30% away from the previous close: very likely a split or bonus the candles weren't
    adjusted for. Trades are not allowed to span these days."""
    return {i for i in range(1, len(bars)) if bars[i - 1][4] and abs(bars[i][1] / bars[i - 1][4] - 1) > limit}
