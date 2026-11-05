"""ORB backtest: the live ORB engine's own replay (orb.OrbEngine._replay) run over past sessions of 5-minute candles.

Same rules, filters, sizing, costs, daily profit / loss stops and 15:10 exit as the paper bot, so a backtest trade is
exactly the trade the bot would have taken that day. Each session starts from the same capital (an intraday book is
flat every evening); the equity curve adds each day's net. Read-only: Kite historical data, nothing is ordered.

Honest limits, shown in the UI:
- Universe is today's F&O stock list applied to past sessions (survivorship).
- The momentum check uses the stock's own 20-day trend from the candles; the sweep's RS rank is not known for past
  days, so it is left out (no look-ahead).
- Exits on 5-minute candles, stop assumed first when one candle touches stop and target (as in the live bot).
"""
from __future__ import annotations

import json
import logging
import pickle
import threading
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

from strategies import orb

log = logging.getLogger("kite-desk.orb-bt")
BT_DIR = orb.CACHE_DIR / "bt5m"
RESULT_FILE = orb.CACHE_DIR / "backtest_orb.json"
CHUNK_DAYS = 95            # Kite serves at most 100 days of 5-minute candles per request
WARMUP_SESSIONS = 40       # prior sessions handed to prepare() (20-day turnover / OR volume, daily ATR14, EMA50)


def _sessions_from(nifty: pd.DataFrame) -> list[date]:
    n = nifty.groupby("d").size()
    return sorted(d for d, k in n.items() if k == orb.SESSION_BARS)


class OrbBacktest:
    def __init__(self):
        self.lock = threading.Lock()
        self.progress = {"running": False, "done": 0, "total": 0, "stage": "", "error": ""}

    def result(self) -> dict | None:
        try:
            return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # ---------------- data ----------------
    def _candles(self, eng: orb.OrbEngine, kite, sym: str, token: int, start: date, end: date) -> pd.DataFrame:
        """5-minute candles for [start, end], cached on disk per symbol; only the missing days are fetched."""
        BT_DIR.mkdir(parents=True, exist_ok=True)
        f = BT_DIR / f"{sym.replace(' ', '_')}.pkl"
        have = None
        try:
            have = pickle.loads(f.read_bytes())
        except (OSError, ValueError, EOFError, pickle.UnpicklingError):
            pass
        if have is not None and have["start"] <= start:
            frames, a, lo = [have["df"][have["df"]["d"] < have["end"]]], have["end"], have["start"]   # refetch the last day
        else:
            frames, a, lo = [], start, start
        while a <= end:
            b = min(end, a + timedelta(days=CHUNK_DAYS))
            frames.append(orb._frame(eng._hist(kite, token, a, b)))
            a = b + timedelta(days=1)
        frames = [x for x in frames if len(x)]
        df = pd.concat(frames, ignore_index=True) if frames else orb._frame([])
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        f.write_bytes(pickle.dumps({"start": lo, "end": end, "df": df}))
        return df

    # ---------------- run ----------------
    def run(self, kite, instruments, sessions: int, capital: float, notional: float, mode: str, momentum: bool) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.progress = {"running": True, "done": 0, "total": 0, "stage": "Loading the F&O universe", "error": ""}
            eng = orb.OrbEngine()
            eng.load_universe(instruments)
            syms = sorted(eng.fno)
            today = datetime.now(orb.IST).date()
            start = today - timedelta(days=int((sessions + WARMUP_SESSIONS) * 1.5) + 10)
            self.progress.update(total=len(syms) + 1, stage="Loading 5-minute history")
            frames = {"NIFTY 50": self._candles(eng, kite, "NIFTY 50", eng.nifty_token, start, today)}
            self.progress["done"] = 1
            for i, s in enumerate(syms):
                frames[s] = self._candles(eng, kite, s, eng.universe[s], start, today)
                self.progress["done"] = i + 2
            nifty_all = frames["NIFTY 50"]
            days = _sessions_from(nifty_all)
            now = datetime.now(orb.IST)
            if days and days[-1] == today and now.strftime("%H:%M") < "15:15":
                days = days[:-1]                    # today's session is not complete yet
            test = days[WARMUP_SESSIONS:][-sessions:]
            if len(test) < 5:
                raise RuntimeError("not enough 5-minute history for a backtest")
            self.progress.update(done=0, total=len(test), stage="Replaying sessions")
            by_day = {s: {d: g for d, g in f.groupby("d")} for s, f in frames.items()}
            idx = {d: i for i, d in enumerate(days)}
            out_days, trades = [], []
            for k, d in enumerate(test):
                lo = days[max(0, idx[d] - WARMUP_SESSIONS)]
                st = {"c0": capital, "base": {"mode": mode, "notional": notional, "momentum": momentum}}
                eng.state = lambda st=st: st                       # in-memory state: nothing is written per session
                eng.save_state = lambda x, st=st: st.update(x)
                eng.sess = d
                completed = {orb._add("09:15", 5 * i) for i in range(orb.SESSION_BARS)}
                preps = {}
                for s in syms:
                    f = frames[s]
                    if d not in by_day[s]:
                        continue
                    part = f[(f["d"] >= lo) & (f["d"] <= d)]
                    p = orb.prepare(s, part, d)
                    orb.opening(p, completed)
                    p["source"], p["rs"] = "F&O", None
                    p["mom_ok"] = bool(p.get("above20") and (p.get("ret20") or 0) > 0)
                    preps[s] = p
                nf = orb._indicators(nifty_all[(nifty_all["d"] >= lo) & (nifty_all["d"] <= d)], volume=False)
                nifty = nf[nf["d"] == d].set_index("t")
                eng.universe = {s: 0 for s in preps}
                snap = eng._replay(preps, nifty, completed, datetime.combine(d + timedelta(days=1), dtime(9), orb.IST))
                c = snap["controller"]
                day_trades = [{"session": d.isoformat(), **{x: t.get(x) for x in (
                    "symbol", "signal_time", "entry_time", "entry_ref", "qty", "R", "sl", "target", "exit_ref",
                    "exit_time", "exit_reason", "gross", "costs", "net")}} for t in snap["trades"]]
                trades += day_trades
                sig = snap["signals"]
                out_days.append({"session": d.isoformat(), "net": c["net"], "trades": len(day_trades),
                                 "state": c["state"], "eligible": snap["counts"]["eligible"],
                                 "signals": len(sig), "filtered": sum(x["status"] == "filtered" for x in sig),
                                 "nifty_ok": (snap.get("nifty") or {}).get("ok")})
                self.progress["done"] = k + 1
            res = {"built": now.isoformat(timespec="seconds"),
                   "params": {"sessions": len(test), "capital": capital, "notional": notional, "mode": mode,
                              "momentum": momentum, "universe": len(syms)},
                   "period": [test[0].isoformat(), test[-1].isoformat()], "days": out_days, "trades": trades,
                   **summarize(out_days, trades, capital)}
            RESULT_FILE.write_text(json.dumps(res), encoding="utf-8")
            self.progress.update(stage="Finished")
            log.info("ORB backtest: %d sessions, %d trades, net %.0f", len(test), len(trades), res["stats"]["net"])
        except Exception as exc:  # noqa: BLE001
            log.exception("ORB backtest failed")
            self.progress.update(error=str(exc), stage="Failed")
        finally:
            self.progress["running"] = False
            self.lock.release()


def _stats(days: list[dict], trades: list[dict], capital: float) -> dict:
    nets = [t["net"] for t in trades if t.get("net") is not None]
    wins, losses = [x for x in nets if x > 0], [x for x in nets if x <= 0]
    rs = [(t["exit_ref"] - t["entry_ref"]) / t["R"] for t in trades if t.get("R") and t.get("exit_ref") is not None]
    cum = peak = dd = 0.0
    for d in days:
        cum += d["net"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    green = [d for d in days if d["net"] > 0]
    traded = [d for d in days if d["trades"]]
    return {"sessions": len(days), "traded_days": len(traded), "green_days": len(green),
            "trades": len(nets), "net": round(sum(nets), 2), "return_pct": round(sum(nets) / capital * 100, 2) if capital else None,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(nets) * 100, 1) if nets else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "costs": round(sum(t.get("costs") or 0 for t in trades), 2),
            "best_day": max((d["net"] for d in days), default=None),
            "worst_day": min((d["net"] for d in days), default=None),
            "max_drawdown": round(dd, 2), "per_day": round(sum(nets) / len(days), 2) if days else None}


def scorecard(trades: list[dict], capital: float, days: list[str]) -> dict:
    """The user's six targets: win rate, net profit factor, max drawdown % of equity, average reward / risk,
    annualised Sharpe of daily returns, Calmar (CAGR / max drawdown). trades need day, net, r."""
    pnl = {d: 0.0 for d in days}
    for t in trades:
        if t["day"] in pnl:
            pnl[t["day"]] += t["net"]
    eq = peak = capital
    dd, rets = 0.0, []
    for d in days:
        prev = eq
        eq += pnl[d]
        rets.append(pnl[d] / prev if prev > 0 else 0.0)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 1.0)
    nets = [t["net"] for t in trades]
    wins, losses = [x for x in nets if x > 0], [-x for x in nets if x <= 0]
    rw = [t["r"] for t in trades if t["net"] > 0]
    rl = [-t["r"] for t in trades if t["net"] <= 0]
    mu = sum(rets) / len(rets) if rets else 0.0
    sd = (sum((x - mu) ** 2 for x in rets) / max(1, len(rets) - 1)) ** 0.5 if rets else 0.0
    years = max(len(days) / 248, 1e-9)
    cagr = (eq / capital) ** (1 / years) - 1 if eq > 0 else -1.0
    return {"win_rate": round(len(wins) / len(nets) * 100, 1) if nets else None,
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) > 0 else None,
            "max_dd_pct": round(dd * 100, 1),
            "avg_rr": round((sum(rw) / len(rw)) / (sum(rl) / len(rl)), 2) if rw and rl and sum(rl) > 0 else None,
            "sharpe": round(mu / sd * 248 ** 0.5, 2) if sd > 0 else None,
            "calmar": round(cagr / dd, 2) if dd > 0 else None, "cagr_pct": round(cagr * 100, 1)}


def summarize(days: list[dict], trades: list[dict], capital: float) -> dict:
    """Overall stats, the earlier 60% of sessions against the later 40% (a consistency check: the ORB rules are fixed
    by the spec, nothing is fitted, so the later part is as unseen as the earlier), exits, months and stocks."""
    cut = int(len(days) * 0.6)
    early, late = days[:cut], days[cut:]
    ed = {d["session"] for d in early}
    reasons: dict[str, dict] = {}
    for t in trades:
        r = t.get("exit_reason") or "open"
        r = "stop" if r.startswith("stop") else r
        x = reasons.setdefault(r, {"n": 0, "net": 0.0})
        x["n"] += 1
        x["net"] = round(x["net"] + (t.get("net") or 0), 2)
    months: dict[str, dict] = {}
    for d in days:
        m = months.setdefault(d["session"][:7], {"net": 0.0, "trades": 0, "days": 0})
        m["net"] = round(m["net"] + d["net"], 2)
        m["trades"] += d["trades"]
        m["days"] += 1
    stocks: dict[str, dict] = {}
    for t in trades:
        x = stocks.setdefault(t["symbol"], {"symbol": t["symbol"], "n": 0, "net": 0.0, "wins": 0})
        x["n"] += 1
        x["net"] = round(x["net"] + (t.get("net") or 0), 2)
        x["wins"] += (t.get("net") or 0) > 0
    cum, curve = 0.0, []
    for d in days:
        cum += d["net"]
        curve.append([d["session"], round(capital + cum, 2)])
    ranked = sorted(stocks.values(), key=lambda x: -x["net"])
    sc = scorecard([{"day": t["session"], "net": t.get("net") or 0.0,
                                 "r": (t.get("net") or 0.0) / (t["R"] * t["qty"]) if t.get("R") and t.get("qty") else 0.0} for t in trades],
                               capital, [d["session"] for d in days])
    return {"stats": _stats(days, trades, capital), "scorecard": sc,
            "split": {"early": {"period": [early[0]["session"], early[-1]["session"]] if early else None,
                                **_stats(early, [t for t in trades if t["session"] in ed], capital)},
                      "late": {"period": [late[0]["session"], late[-1]["session"]] if late else None,
                               **_stats(late, [t for t in trades if t["session"] not in ed], capital)}},
            "exits": reasons, "months": [{"month": k, **v} for k, v in sorted(months.items())],
            "best_stocks": ranked[:8], "worst_stocks": ranked[::-1][:8], "curve": curve}


bt = OrbBacktest()
