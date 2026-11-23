"""The Agent Pipeline team's swing book: options bought for a multi-day move, never sold. Its own records and paper money. Paper only.

Once a day, at 14:50, each candidate goes down the pipeline, and any step can stop it:
  Scanner       every F&O name on daily candles: a fresh trend-and-momentum setup, strongest first
  Trend         the 30-week trend, the daily trend, today's price confirming, and room to run to the 6-month high or low
  Options       an expiry at least 3 weeks out, delta 0.50 to 0.70, a tight spread and real open interest, an option that isn't priced
                far above the stock's own movement, and time decay the planned hold can afford
  Time Machine  the same setup on the last year of daily candles, every F&O name pooled: it must have made money on average
  Budget        whole lots, sized so the stop loses about Rs 5,000 at most with charges, inside the book's free money
  Risk          the open-trade and daily limits, one trade a name, and reward against risk
  Broker        a paper fill at the ask
Alerts then follow each open trade on the contract's own candles: stop, target, 15 sessions, or a week before expiry.

A call is bought in an uptrend and a put in a downtrend. Nothing is written or sold short, and this module has no way to place an order.
"""
from __future__ import annotations

import json
import logging
import math
import sys
import threading
from datetime import date, datetime
from pathlib import Path

from strategies import agent_desk, common, live_stops, notify

sys.path.insert(0, str(agent_desk.SKILLS / "_india"))
import india  # noqa: E402

log = logging.getLogger("kite-desk.pipeline-swing")
DESK_ID, DESK_NAME = "pipeline_swing", "Agent Pipeline · Swing"
CAPITAL = 200000.0            # the swing book's own paper money
MAX_LOSS = 5000.0             # rupees at the stop, with charges
MAX_OPEN, MAX_NEW, MAX_REVIEW = 4, 2, 4
SCAN_AT = "14:50"             # late enough for today's candle to count as confirmation, early enough to fill before the close
HOLD_SESSIONS = 15            # a 1 to 3 week move
MIN_DTE, MAX_DTE, EXIT_DTE = 21, 75, 7          # days to expiry: buy with at least 3 weeks left, leave a week before it ends
DELTA_LOW, DELTA_HIGH, DELTA_AIM = 0.50, 0.70, 0.60
STOP_ATR, TARGET_R = 1.5, 2.0
MAX_SPREAD_PCT = 4.0          # bid to ask, of the mid price
MIN_OI_LOTS = 25
MAX_IV_TO_HV = 1.4            # the option's implied volatility against the stock's 20-day realised volatility
MAX_DECAY_PCT = 30.0          # of the premium, lost to time over the planned hold with the stock flat
MIN_SAMPLE = 30
RATE = common.RATE
INDICES = ("NIFTY", "BANKNIFTY")
TEAM = [("scanner", "Scanner", "Screens every F&O name's daily chart for a fresh trend-and-momentum setup"), ("trend", "Trend", "Checks the 30-week and daily trend, today's confirmation and the room to run"),
        ("options", "Options", "Picks the expiry and strike: 3 weeks or more, delta 0.50 to 0.70, liquid, fairly priced, affordable decay"),
        ("timemachine", "Time Machine", "Replays the same setup on a year of daily candles across every F&O name"), ("budget", "Budget", "Sizes whole lots to a Rs 5,000 loss at the stop"),
        ("risk", "Risk", "Passes or blocks on the book's limits"), ("broker", "Ticket", "Writes the order, here filled on paper"), ("alerts", "Alerts", "Watches the stop, target, 15 sessions and expiry")]
_lock = threading.RLock()


# ---------------------------------------------------------------- the chart: one rule, used live and in the replay

def _sma(xs: list[float], n: int) -> list[float | None]:
    out, run = [], 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        out.append(run / n if i >= n - 1 else None)
    return out


def _rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    up = dn = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            up, dn = up + g / n, dn + l / n
        else:
            up, dn = (up * (n - 1) + g) / n, (dn * (n - 1) + l) / n
        if i >= n:
            out[i] = 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)
    return out


def _atr(rows: list[dict], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(rows)
    a = 0.0
    for i in range(1, len(rows)):
        tr = max(rows[i]["high"] - rows[i]["low"], abs(rows[i]["high"] - rows[i - 1]["close"]), abs(rows[i]["low"] - rows[i - 1]["close"]))
        a = a + tr / n if i <= n else (a * (n - 1) + tr) / n
        if i >= n:
            out[i] = a
    return out


def indicators(rows: list[dict]) -> dict:
    c = [r["close"] for r in rows]
    return {"c": c, "h": [r["high"] for r in rows], "l": [r["low"] for r in rows], "s20": _sma(c, 20), "s50": _sma(c, 50), "s150": _sma(c, 150), "rsi": _rsi(c), "atr": _atr(rows)}


def read(ind: dict, i: int, way: int) -> dict | None:
    """The multi-timeframe read on day i for a rise (way 1) or a fall (way -1): each check by name, and whether all of them hold."""
    if i < 171 or ind["s150"][i - 20] is None or ind["atr"][i] is None or ind["rsi"][i] is None:
        return None
    c, s20, s50, s150, rsi, atr = ind["c"][i], ind["s20"][i], ind["s50"][i], ind["s150"][i], ind["rsi"][i], ind["atr"][i]
    far = max(ind["h"][i - 120:i]) if way > 0 else min(ind["l"][i - 120:i])            # the 6-month high or low before today
    near = max(ind["c"][i - 20:i + 1]) if way > 0 else min(ind["c"][i - 20:i + 1])
    weekly = (c - s150) * way > 0 and (s50 - s150) * way > 0 and (s150 - ind["s150"][i - 20]) * way > 0
    daily = (c - s20) * way > 0 and (s20 - s50) * way > 0
    momentum = (50 <= rsi <= 72 if way > 0 else 28 <= rsi <= 50) and (c >= 0.97 * near if way > 0 else c <= 1.03 * near)
    confirm = c > ind["h"][i - 1] if way > 0 else c < ind["l"][i - 1]
    room = (far - c) * way
    runway = room <= 0.005 * c or room >= TARGET_R * STOP_ATR * atr                    # already at new ground, or the target fits before the old extreme
    return {"weekly": weekly, "daily": daily, "momentum": momentum, "confirm": confirm, "room": runway, "ok": weekly and daily and momentum and confirm and runway,
            "close": round(c, 2), "avg_30_week": round(s150, 2), "avg_20_day": round(s20, 2), "avg_50_day": round(s50, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
            "six_month_extreme": round(far, 2), "room_in_atr": round(room / atr, 1) if atr else None}


def fresh(ind: dict, i: int, way: int) -> dict | None:
    """The read, only on the first day every check holds: a setup is taken when it appears, not every day it lasts."""
    now = read(ind, i, way)
    if not now or not now["ok"]:
        return None
    before = read(ind, i - 1, way)
    return None if before and before["ok"] else now


def outcome(rows: list[dict], ind: dict, i: int, way: int) -> float | None:
    """R made by the underlying after a setup on day i: stop 1.5 ATR away, target 2R, out after 15 sessions. A day that touches both counts as the stop."""
    if i + HOLD_SESSIONS >= len(rows):
        return None
    e, risk = ind["c"][i], STOP_ATR * ind["atr"][i]
    stop, target = e - way * risk, e + way * TARGET_R * risk
    for r in rows[i + 1:i + 1 + HOLD_SESSIONS]:
        lo, hi = (r["low"], r["high"]) if way > 0 else (-r["high"], -r["low"])
        if lo <= stop * way:
            px = min(r["open"] * way, stop * way) * way
            return (px - e) * way / risk
        if hi >= target * way:
            return TARGET_R
    return (rows[i + HOLD_SESSIONS]["close"] - e) * way / risk


# ---------------------------------------------------------------- the option's price

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black76(fwd: float, k: float, t: float, vol: float, call: bool) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, (fwd - k) if call else (k - fwd))
    d1 = (math.log(fwd / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    df = math.exp(-RATE * t)
    return df * (fwd * _ncdf(d1) - k * _ncdf(d2)) if call else df * (k * _ncdf(-d2) - fwd * _ncdf(-d1))


def decay_pct(premium: float, fwd: float, k: float, dte: float, iv_pct: float, call: bool, days: float) -> float | None:
    """Per cent of the premium that time alone takes over `days` calendar days, with the stock and its volatility unchanged."""
    if not premium or not iv_pct or dte <= days:
        return None
    vol = iv_pct / 100.0
    now, later = black76(fwd, k, dte / 365.0, vol, call), black76(fwd, k, (dte - days) / 365.0, vol, call)
    return round(100.0 * (now - later) / now, 1) if now > 0 else None


def realised_vol(closes: list[float], n: int = 20) -> float | None:
    c = closes[-(n + 1):]
    if len(c) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(c, c[1:]) if a > 0 and b > 0]
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252) * 100


def iv_percentile(closes: list[float], iv: float) -> int | None:
    """Where the option's implied volatility sits against a year of the stock's own 20-day realised volatility. Not a true IV rank: the app keeps no IV history."""
    hist = [v for v in (realised_vol(closes[:j]) for j in range(40, len(closes) + 1, 5)) if v]
    return round(100 * sum(v < iv for v in hist) / len(hist)) if len(hist) >= 10 else None


# ---------------------------------------------------------------- data, each one replaceable in a test

def _rows(result: dict) -> list[dict]:
    q = result["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result.get("timestamp") or []):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        out.append({"date": datetime.fromtimestamp(ts, common.IST).date().isoformat(), "open": float(q["open"][i]), "high": float(q["high"][i]), "low": float(q["low"][i]), "close": float(q["close"][i])})
    return out


def fetch_daily(sym: str) -> list[dict]:
    return _rows(india.chart(sym, "1y", "1d"))


def fetch_today(sym: str) -> dict | None:
    """Today's candle so far, from the live quote."""
    rows = _rows(india.chart(sym, "1d", "1d"))
    return rows[-1] if rows else None


def fetch_chain(sym: str, expiry: str | None = None) -> dict:
    from urllib.parse import quote
    return india.app_get(f"/api/options/chain?underlying={quote(sym)}&strikes=12" + (f"&expiry={expiry}" if expiry else ""))


class SwingDesk:
    def __init__(self, cache_dir: Path | None = None, universe=lambda: [], daily=fetch_daily, today=fetch_today, chain=fetch_chain, bars=agent_desk.fetch_bars, send=None):
        self.dir = (cache_dir or common.CACHE_DIR) / DESK_ID
        self.universe, self.daily, self.today, self.chain, self.bars = universe, daily, today, chain, bars
        self.send = send if send is not None else notify.send_once
        self._hist: dict = {}
        self.status = {"scanning": False, "last_scan": None, "note": ""}

    # ---- records
    def _read(self, name: str, default):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = (self.dir / name).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.dir / name)

    def positions(self) -> list[dict]:
        return self._read("positions.json", [])

    def day(self, session: str) -> dict:
        return self._read(f"day_{session}.json", {"session": session, "scanned": 0, "setups": 0, "candidates": [], "at": None, "note": ""})

    # ---- the Time Machine: the same rule on every name's last year
    def history(self, way: int, session: str, charts: dict[str, list[dict]]) -> dict:
        key = (session, way)
        if key not in self._hist:
            rs = []
            for rows in charts.values():
                if len(rows) < 190:
                    continue
                ind = indicators(rows)
                for i in range(172, len(rows) - HOLD_SESSIONS):
                    if fresh(ind, i, way):
                        r = outcome(rows, ind, i, way)
                        if r is not None:
                            rs.append(r)
            n = len(rs)
            self._hist = {k: v for k, v in self._hist.items() if k[0] == session}
            self._hist[key] = {"sample": n, "names": len(charts), "target_first_pct": round(100 * sum(r >= TARGET_R for r in rs) / n, 1) if n else None,
                               "won_pct": round(100 * sum(r > 0 for r in rs) / n, 1) if n else None, "avg_R": round(sum(rs) / n, 3) if n else None}
        return self._hist[key]

    # ---- one candidate through the pipeline
    def review(self, sym: str, way: int, rows: list[dict], scan: dict, hist: dict, now: datetime, held: list[dict], new_today: int) -> dict:
        session = now.date().isoformat()
        ind = indicators(rows)
        t = read(ind, len(rows) - 1, way)
        side = "call" if way > 0 else "put"
        steps = [{"agent": "scanner", "state": "ok", "text": f"{sym}: a fresh {'up' if way > 0 else 'down'}trend setup on the daily chart, ranked {scan['rank']} of {scan['setups']} today by 3-month strength.",
                  "data": agent_desk._facts(symbol=sym, direction="up" if way > 0 else "down", buy=side, setup="swing trend and momentum", return_60d_pct=scan.get("ret60"), rank=scan["rank"])}]
        c = {"id": f"{session}|{sym}|{side}", "symbol": sym, "book": "swing", "direction": "up" if way > 0 else "down", "at": now.strftime("%H:%M"), "date": session, "decision": "skipped", "why": "", "steps": steps, "reviewed": True}

        def stop(agent: str, why: str, **facts) -> dict:
            steps.append({"agent": agent, "state": "no", "text": why, "data": agent_desk._facts(blocked=why, **facts)})
            c["why"] = why
            return c

        names = {"weekly": "30-week trend", "daily": "daily trend", "momentum": "momentum", "confirm": "today's confirmation", "room": "room to run"}
        facts = {k: t[k] for k in ("close", "avg_30_week", "avg_20_day", "avg_50_day", "rsi", "atr", "six_month_extreme", "room_in_atr")} if t else {}
        failed = [names[k] for k in names if not (t or {}).get(k)]
        if failed:
            return stop("trend", "The multi-timeframe read fails on: " + ", ".join(failed) + ".", **facts)
        spot, atr = t["close"], t["atr"]
        stop_u, target_u = round(spot - way * STOP_ATR * atr, 2), round(spot + way * TARGET_R * STOP_ATR * atr, 2)
        steps.append({"agent": "trend", "state": "ok", "text": f"30-week and daily trend {'up' if way > 0 else 'down'}, RSI {t['rsi']}, today {'above' if way > 0 else 'below'} yesterday's {'high' if way > 0 else 'low'}, "
                      f"{t['room_in_atr']} ATR to the 6-month {'high' if way > 0 else 'low'}. Stop {stop_u}, target {target_u} on the stock.", "data": {**facts, "stop": stop_u, "target": target_u}})

        # ---- Options: the expiry first, then the strike
        try:
            first = self.chain(sym, None)
        except Exception as exc:  # noqa: BLE001 - no chain, no option trade
            return stop("options", f"No option chain for {sym}: {str(exc)[:120]}")
        today_d = now.date()
        exps = [(e, (date.fromisoformat(e) - today_d).days) for e in (first.get("expiries") or [])]
        pick = next(((e, d) for e, d in exps if MIN_DTE <= d <= MAX_DTE), None)
        if not pick:
            return stop("options", f"No expiry between {MIN_DTE} and {MAX_DTE} days out, so time decay would have too little buffer.", expiries=", ".join(f"{e} ({d}d)" for e, d in exps[:4]))
        expiry, dte = pick
        try:
            chain = first if first.get("expiry") == expiry else self.chain(sym, expiry)
        except Exception as exc:  # noqa: BLE001
            return stop("options", f"No option chain for the {expiry} expiry: {str(exc)[:120]}")
        lot = int(chain.get("lot_size") or 0)
        leg = "ce" if way > 0 else "pe"
        cands = [(r["strike"], r[leg]) for r in chain.get("rows") or [] if r.get(leg) and r[leg].get("delta") is not None and DELTA_LOW <= abs(r[leg]["delta"]) <= DELTA_HIGH]
        if not cands or not lot:
            return stop("options", f"No {side} with a delta between {DELTA_LOW} and {DELTA_HIGH} in the {expiry} expiry.", expiry=expiry, days_to_expiry=dte)
        strike, o = min(cands, key=lambda x: abs(abs(x[1]["delta"]) - DELTA_AIM))
        bid, ask, ltp, oi, iv, delta = o.get("bid") or 0, o.get("ask") or 0, o.get("ltp") or 0, o.get("oi") or 0, o.get("iv"), abs(o["delta"])
        ofacts = agent_desk._facts(contract=o.get("tradingsymbol"), expiry=expiry, days_to_expiry=dte, strike=strike, delta=round(delta, 2), bid=bid, ask=ask, open_interest_lots=round(oi / lot), implied_vol=iv)
        if not bid or not ask or ask < bid:
            return stop("options", f"{o.get('tradingsymbol')} has no two-sided quote right now.", **ofacts)
        mid = (bid + ask) / 2
        spread = round(100 * (ask - bid) / mid, 2)
        hv = realised_vol(ind["c"])
        ratio = round(iv / hv, 2) if iv and hv else None
        ivp = iv_percentile(ind["c"], iv) if iv else None
        decay = decay_pct(ask, float(chain.get("forward") or spot), strike, dte, iv or 0, way > 0, HOLD_SESSIONS * 7 / 5)
        ofacts.update(agent_desk._facts(spread_pct=spread, realised_vol_20d=round(hv, 1) if hv else None, iv_to_realised=ratio, iv_percentile_proxy=ivp, decay_over_hold_pct=decay))
        problems = [w for bad, w in ((spread > MAX_SPREAD_PCT, f"the bid-ask spread is {spread}% of the price, over {MAX_SPREAD_PCT}%"), (oi < MIN_OI_LOTS * lot, f"open interest is only {round(oi / lot)} lots, under {MIN_OI_LOTS}"),
                                     (ratio is not None and ratio > MAX_IV_TO_HV, f"implied volatility {iv} is {ratio} times the stock's realised {round(hv or 0, 1)}, over {MAX_IV_TO_HV}"),
                                     (decay is not None and decay > MAX_DECAY_PCT, f"time alone would take {decay}% of the premium over the hold, over {MAX_DECAY_PCT}%")) if bad]
        if problems:
            return stop("options", f"{o.get('tradingsymbol')}: " + "; ".join(problems) + ".", **ofacts)
        prem = float(ask)
        stop_p = round(max(prem - delta * STOP_ATR * atr, 0.5 * prem), 2)                        # the stock's stop, through delta; never more than half the premium
        target_p = round(prem + delta * TARGET_R * STOP_ATR * atr, 2)
        steps.append({"agent": "options", "state": "ok", "text": f"{o.get('tradingsymbol')}: {dte} days to expiry for a hold of up to {HOLD_SESSIONS} sessions, delta {round(delta, 2)}, spread {spread}%, "
                      f"IV {iv} against realised {round(hv or 0, 1)}. Premium ₹{prem}, stop ₹{stop_p}, target ₹{target_p}.", "data": {**ofacts, "premium": prem, "stop": stop_p, "target": target_p}})

        # ---- Time Machine
        hf = agent_desk._facts(past_triggers=hist.get("sample"), target_first_pct=hist.get("target_first_pct"), success_rate_pct=hist.get("won_pct"), avg_R=hist.get("avg_R"))
        c["odds"] = {"sample": hist.get("sample") or 0, "success_rate_pct": hist.get("won_pct"), "avg_R_net": hist.get("avg_R"),
                     "confidence": "High" if (hist.get("sample") or 0) >= 100 and (hist.get("avg_R") or 0) >= 0.1 else "Medium" if (hist.get("sample") or 0) >= MIN_SAMPLE and (hist.get("avg_R") or 0) > 0 else "Low"}
        if (hist.get("sample") or 0) < MIN_SAMPLE:
            return stop("timemachine", f"Only {hist.get('sample') or 0} past setups in a year of daily candles, under the {MIN_SAMPLE} needed to judge it.", **hf)
        if (hist.get("avg_R") or 0) <= 0:
            return stop("timemachine", f"The same setup averaged {hist.get('avg_R')}R on the stock over {hist['sample']} past cases, not above zero.", **hf)
        steps.append({"agent": "timemachine", "state": "ok", "text": f"{hist['sample']} past setups across {hist.get('names')} names: {hist.get('won_pct')}% made money, {hist.get('target_first_pct')}% reached the target, "
                      f"average {hist.get('avg_R')}R on the stock, before option costs.", "data": {**hf, "confidence": c["odds"]["confidence"]}})

        # ---- Budget
        slip = agent_desk.OPTION_SLIP["index" if sym in INDICES else "stock"]
        per_lot = round((prem * (1 + slip) - stop_p * (1 - slip)) * lot + agent_desk.option_fees(prem * lot, stop_p * lot), 2)
        tied = sum(p["entry"] * p["qty"] for p in held)
        booked = sum(p.get("net") or 0 for p in self.positions() if p["status"] == "closed")
        free = CAPITAL + booked - tied
        lots = min(int(MAX_LOSS // per_lot) if per_lot > 0 else 0, int(free // (prem * lot)))
        bf = agent_desk._facts(lot_size=lot, loss_per_lot=per_lot, premium_per_lot=round(prem * lot, 2), free=round(free, 2))
        if lots < 1:
            why = f"One lot would lose about ₹{per_lot:,.0f} at the stop with charges, over the ₹{MAX_LOSS:,.0f} limit." if per_lot > MAX_LOSS else f"One lot costs ₹{prem * lot:,.0f} and ₹{free:,.0f} is free in the swing book."
            return stop("budget", why, **bf)
        qty, max_loss = lots * lot, round(per_lot * lots, 2)
        steps.append({"agent": "budget", "state": "ok", "text": f"{lots} lot(s), {qty} units, ₹{prem * qty:,.0f} of premium. Loss at the stop with charges about ₹{max_loss:,.0f}.", "data": {**bf, "lots": lots, "qty": qty, "max_loss": max_loss}})

        # ---- Risk
        fails = [w for bad, w in ((len(held) >= MAX_OPEN, f"{MAX_OPEN} swing trades are already open"), (new_today >= MAX_NEW, f"{MAX_NEW} new swing trades were already taken today"),
                                  (any(p["underlying"] == sym for p in held), f"a swing trade on {sym} is already open"), (target_p - prem < 1.5 * (prem - stop_p), "the reward is under 1.5 times the risk"),
                                  (max_loss > MAX_LOSS, "the loss at the stop is over the limit")) if bad]
        if fails:
            return stop("risk", "Risk blocked it: " + "; ".join(fails) + ".", verdict="BLOCK")
        steps.append({"agent": "risk", "state": "ok", "text": f"PASS. A bought {side}, {len(held)} of {MAX_OPEN} open, loss at the stop ₹{max_loss:,.0f} within ₹{MAX_LOSS:,.0f}.", "data": {"verdict": "PASS"}})
        steps.append({"agent": "broker", "state": "ok", "text": f"Buy {lots} lot(s) of {o['tradingsymbol']} at ₹{prem}, held for up to {HOLD_SESSIONS} sessions.", "note": "filled on the paper book only",
                      "data": agent_desk._facts(order="BUY", symbol=o["tradingsymbol"], lots=lots, qty=qty, entry=prem, stop=stop_p, target=target_p, book="paper")})
        c.update(decision="traded", why="Every agent agreed.", contract=o["tradingsymbol"], expiry=expiry, strike=strike, lots=lots, qty=qty, entry=prem, stop=stop_p, target=target_p, max_loss=max_loss,
                 stop_underlying=stop_u, target_underlying=target_u, days_to_expiry=dte, delta=round(delta, 2))
        return c

    # ---- the daily scan
    def scan(self, now: datetime) -> dict:
        session = now.date().isoformat()
        self.status.update(scanning=True, note="")
        try:
            charts, setups = {}, []
            for sym in self.universe():
                try:
                    rows = [r for r in self.daily(sym) if r["date"] < session]
                except Exception:  # noqa: BLE001 - one missing chart must not stop the scan
                    continue
                if len(rows) >= 190:
                    charts[sym] = rows
            for sym, rows in charts.items():                # stage one, on closed candles: the trend is already in place, so only today's confirmation is missing
                ind = indicators(rows)
                i = len(rows) - 1
                for way in (1, -1):
                    t = read(ind, i, way)
                    if t and t["weekly"] and t["daily"] and not t["ok"]:
                        ret = (ind["c"][i] / ind["c"][i - 60] - 1) * 100 if i >= 60 else 0.0
                        setups.append((ret * way, sym, way, round(ret, 1)))
            setups.sort(reverse=True)
            found = []
            for _, sym, way, ret in setups[:40]:            # stage two: today's candle so far, for the strongest names only
                try:
                    bar = self.today(sym)
                except Exception:  # noqa: BLE001
                    bar = None
                if not bar:
                    continue
                rows = charts[sym] + [dict(bar, date=session)]
                if fresh(indicators(rows), len(rows) - 1, way):
                    found.append((sym, way, ret, rows))
            rec = self.day(session)
            done = {c["id"] for c in rec["candidates"]}
            held = [p for p in self.positions() if p["status"] == "open"]
            new_today = sum(1 for p in self.positions() if p["day"] == session)
            for k, (sym, way, ret, rows) in enumerate(found[:MAX_REVIEW]):
                cid = f"{session}|{sym}|{'call' if way > 0 else 'put'}"
                if cid in done:
                    continue
                c = self.review(sym, way, rows, {"rank": k + 1, "setups": len(found), "ret60": ret}, self.history(way, session, charts), now, held, new_today)
                rec["candidates"].append(c)
                if c["decision"] == "traded":
                    p = self._take(c, now)
                    held.append(p)
                    new_today += 1
            rec.update(scanned=len(charts), setups=len(found), at=now.strftime("%H:%M"), note="" if charts else "No daily charts could be read.")
            with _lock:
                self._write(f"day_{session}.json", rec)
            self.status.update(last_scan=f"{session} {now.strftime('%H:%M')}")
            return rec
        finally:
            self.status["scanning"] = False

    def _take(self, c: dict, now: datetime) -> dict:
        p = {"id": c["id"], "day": c["date"], "entry_at": now.strftime("%H:%M"), "underlying": c["symbol"], "instrument": c["contract"], "key": f"NFO:{c['contract']}", "side": "LONG", "direction": c["direction"],
             "expiry": c["expiry"], "lots": c["lots"], "qty": c["qty"], "entry": round(c["entry"] * (1 + agent_desk.OPTION_SLIP["index" if c["symbol"] in INDICES else "stock"]), 2), "stop": c["stop"], "target": c["target"],
             "max_loss": c["max_loss"], "stop_underlying": c["stop_underlying"], "status": "open", "ltp": c["entry"], "sessions": 1, "checked": f"{c['date']} {common.completed_bar(now) or '15:10'}",
             "confidence": (c.get("odds") or {}).get("confidence"), "success_rate_pct": (c.get("odds") or {}).get("success_rate_pct")}
        with _lock:
            self._write("positions.json", self.positions() + [p])
        try:
            self.send(f"pswing|{p['id']}", notify.signal_text({"desk": DESK_NAME, "instrument": p["instrument"], "side": "BUY", "direction": p["direction"], "setup": "Swing trend and momentum", "time": p["entry_at"], "date": p["day"],
                                                              "entry": p["entry"], "stop": p["stop"], "target": p["target"], "qty": p["qty"], "lots": p["lots"], "max_loss": p["max_loss"], "expiry": p["expiry"],
                                                              "confidence": p["confidence"], "success_rate_pct": p["success_rate_pct"]}), now)
        except Exception:  # noqa: BLE001 - a Telegram problem must never lose the trade
            log.exception("swing call not sent")
        return p

    # ---- Alerts: each open trade on its own contract's candles
    def follow(self, now: datetime) -> None:
        session, bar = now.date().isoformat(), common.completed_bar(now)
        if not bar:
            return
        rows = self.positions()
        changed = False
        live = live_stops.prices([p["key"] for p in rows if p["status"] == "open"], now)
        for p in rows:
            if p["status"] != "open":
                continue
            slip = agent_desk.OPTION_SLIP["index" if p["underlying"] in INDICES else "stock"]
            try:
                bars = {t: v for (d, t), v in self.bars(p["key"]).items() if d == session}
            except Exception:  # noqa: BLE001 - the candles are retried on the next pass; the live price is still checked
                bars = None
            if bars is None:
                changed |= self._live(p, live.get(p["key"]), now, slip)
                continue
            if p["checked"][:10] < session:
                p["sessions"] += 1
                start = "00:00"
            else:
                start = p["checked"][11:]
            dte = (date.fromisoformat(p["expiry"]) - now.date()).days
            for t in common.LABELS:
                if t <= start or t > bar or t not in bars:
                    continue
                o, h, lo, cl = bars[t]
                p["ltp"] = cl
                at = common.plus_minutes(t, 5)
                if lo <= p["stop"]:
                    self._close(p, min(o, p["stop"]), "stop", session, at, slip)
                elif h >= p["target"]:
                    self._close(p, p["target"], "target", session, at, slip)
                elif t == agent_desk.FLAT_BAR and p["sessions"] >= HOLD_SESSIONS:
                    self._close(p, cl, f"{HOLD_SESSIONS} sessions", session, "15:15", slip)
                elif t == agent_desk.FLAT_BAR and dte <= EXIT_DTE:
                    self._close(p, cl, "a week to expiry", session, "15:15", slip)
                if p["status"] != "open":
                    break
            p["checked"] = f"{session} {bar}"
            self._live(p, live.get(p["key"]), now, slip)
            changed = True
        if changed:
            with _lock:
                self._write("positions.json", rows)

    def _live(self, p: dict, px: float | None, now: datetime, slip: float) -> bool:
        """Closes an open trade as soon as its last traded price crosses the stop or the target, without waiting for the candle."""
        if p["status"] != "open" or px is None:
            return False
        p["ltp"] = px
        got = live_stops.hit(px, p["stop"], p.get("target"))
        if got:
            self._close(p, got[1], got[0], now.date().isoformat(), now.strftime("%H:%M"), slip)
        return True

    @staticmethod
    def _close(p: dict, price: float, reason: str, day: str, at: str, slip: float) -> None:
        out = price * (1 - slip)
        fees = agent_desk.option_fees(p["entry"] * p["qty"], out * p["qty"])
        gross = (out - p["entry"]) * p["qty"]
        risk = (p["entry"] - p["stop"]) * p["qty"]
        p.update(status="closed", exit=round(out, 2), exit_day=day, exit_at=at, exit_reason=reason, costs=round(fees, 2), net=round(gross - fees, 2), r=round((gross - fees) / risk, 2) if risk > 0 else None)

    def tick(self, now: datetime) -> None:
        if not india.trading_day(now.date()):
            return
        hm = now.strftime("%H:%M")
        if "09:20" <= hm <= "15:30":
            self.follow(now)
        if SCAN_AT <= hm < "15:15" and not self.day(now.date().isoformat())["at"] and not self.status["scanning"]:
            self.scan(now)

    def view(self, session: str | None = None) -> dict:
        today = datetime.now(common.IST).date().isoformat()
        session = session or today
        book = self.positions()
        closed = [p for p in book if p["status"] == "closed"]
        return {"day": self.day(session), "today": today, "positions": [p for p in book if p["status"] == "open" or p.get("exit_day") == session or p["day"] == session][::-1], "status": dict(self.status),
                "book": {"capital": CAPITAL, "open": sum(p["status"] == "open" for p in book), "closed": len(closed), "won": sum((p.get("net") or 0) > 0 for p in closed), "realized": round(sum(p.get("net") or 0 for p in closed), 2)},
                "limits": {"max_loss": MAX_LOSS, "max_open": MAX_OPEN, "max_new": MAX_NEW, "hold_sessions": HOLD_SESSIONS, "min_dte": MIN_DTE, "exit_dte": EXIT_DTE, "delta": [DELTA_LOW, DELTA_HIGH], "scan_at": SCAN_AT},
                "team": [{"id": a, "name": n, "job": j} for a, n, j in TEAM]}
