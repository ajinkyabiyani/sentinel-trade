"""ORB strategy, spec v1.1 (ORB_Strategy_Logic.md, 30 Oct 2026): buy-only breakout of the 09:15-09:30 opening range on
liquid NSE F&O stocks, confirmed by Nifty, with the shared daily INR7000 profit / 5%-of-capital loss stops.

Signals are paper only: nothing in this module places, modifies or cancels an order. The whole session is replayed
from Kite's 5-minute candles on every refresh (deterministic, so a restart rebuilds the same trades); only the
starting capital, setting changes and a halt seen on live prices are persisted per session in .orb_cache.

Simplifications against the spec, shown in the UI too:
- Exits are checked on 5-minute candles (the spec asks for 1-minute execution data); if a candle touches both the
  stop and the 3R target the stop is assumed first (pessimistic).
- The daily stop is checked on each 5-minute close and on live prices every refresh, not tick by tick.
- Whole-share quantity floor(allocation / entry); costs are Zerodha intraday-equity charges plus 1bp slippage a side.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from strategies import momentum

log = logging.getLogger("kite-desk.orb")
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = Path(__file__).resolve().parents[1] / ".orb_cache"

# --- spec defaults (section 1) ----------------------------------------------------------------------------------
OR_BARS = ("09:15", "09:20", "09:25")          # opening range 09:15 inclusive to 09:30 exclusive
FIRST_SIGNAL, LAST_SIGNAL = "09:30", "11:25"   # signal candles (their close confirms the breakout)
LAST_ENTRY, EXIT_BAR = "11:30", "15:10"        # last entry fill; forced exit at the 15:10 open
SESSION_BARS = 72                              # 09:15 ... 15:10: a complete retained session (09:15-15:14)
RVOL_MIN = 2.0
RATIO_MIN, RATIO_MAX = 0.10, 0.80              # opening-range width / prior daily ATR14
STOP_ATR, JUMP_ATR = 1.5, 0.75
RISK_MIN, RISK_MAX = 0.0005, 0.03              # R / entry
TURNOVER_MIN = 1e8                             # INR10 crore median daily volume x close, previous 20 sessions
GAP_MAX, GAP_COOLDOWN = 0.20, 20
PROFIT_STOP, LOSS_PCT = 7000.0, 0.05
TARGET_R = 3.0
NOTIONAL = 100000.0                            # research reference notional per trade
SLIP = 0.0001                                  # 1bp per side
HIST_DAYS = 60                                 # calendar days of 5-minute history (~40 sessions)
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX", "SENSEX50"}
DEFAULT_CAPITAL = 500000.0                     # paper capital when nothing is set and Kite funds show 0
PREFS_FILE = CACHE_DIR / "orb_prefs.json"      # capital / exit mode / per-trade amount, carried across days
LEDGER_FILE = CACHE_DIR / "paper_ledger.json"  # one paper-trading record per session


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_prefs() -> dict:
    return _read_json(PREFS_FILE)


def save_prefs(**kw) -> None:
    _write_json(PREFS_FILE, {**load_prefs(), **{k: v for k, v in kw.items() if v is not None}})


def _hm(ts: datetime) -> str:
    return ts.strftime("%H:%M")


def _add(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    t = h * 60 + m + minutes
    return f"{t // 60:02d}:{t % 60:02d}"


def costs(buy_val: float, sell_val: float) -> float:
    """Zerodha intraday equity (MIS) charges for one round trip, INR."""
    brk = min(20.0, 0.0003 * buy_val) + min(20.0, 0.0003 * sell_val)
    txn = 0.0000297 * (buy_val + sell_val)
    sebi = 1e-6 * (buy_val + sell_val)
    return brk + 0.00025 * sell_val + txn + sebi + 0.18 * (brk + txn + sebi) + 0.00003 * buy_val


def _f(v, d=2):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), d)


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Kite candles -> retained 09:15-15:14 bars with session date / HH:MM labels."""
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "d", "t"])
    df = pd.DataFrame(rows)
    ts = pd.to_datetime(df["date"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["ts"] = ts
    df["d"] = df["ts"].dt.date
    df["t"] = df["ts"].dt.strftime("%H:%M")
    df = df[(df["t"] >= "09:15") & (df["t"] < "15:15")]
    return df[["ts", "open", "high", "low", "close", "volume", "d", "t"]].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def _indicators(df: pd.DataFrame, volume: bool = True) -> pd.DataFrame:
    """Continuous 5-minute EMA20/50 and ATR14 (spec 2: never reset each morning); session VWAP / average reset daily."""
    df = df.copy()
    c, h, l = df["close"], df["high"], df["low"]
    df["ema20"] = c.ewm(span=20, adjust=False, min_periods=20).mean()
    df["ema50"] = c.ewm(span=50, adjust=False, min_periods=50).mean()
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=50).mean()
    tp = (h + l + c) / 3
    g = df["d"]
    df["savg"] = tp.groupby(g).cumsum() / tp.groupby(g).cumcount().add(1)
    if volume:
        cv = df["volume"].groupby(g).cumsum()
        df["vwap"] = (tp * df["volume"]).groupby(g).cumsum() / cv.where(cv > 0)
    return df


def prepare(sym: str, df: pd.DataFrame, sess: date) -> dict:
    """Prior-session filters (liquidity, gap, daily ATR, normal opening volume) and today's opening range."""
    out = {"symbol": sym, "reasons": []}
    df = _indicators(df)
    prior, today = df[df["d"] < sess], df[df["d"] == sess]
    daily = prior.groupby("d").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                   close=("close", "last"), volume=("volume", "sum"), n=("close", "size"))
    daily = daily[daily["n"] == SESSION_BARS]
    orv = prior[prior["t"].isin(OR_BARS)].groupby("d")["volume"].sum()
    orv = orv[orv.index.isin(daily.index)]
    pc = daily["close"].shift(1)
    dtr = pd.concat([daily["high"] - daily["low"], (daily["high"] - pc).abs(), (daily["low"] - pc).abs()], axis=1).max(axis=1)
    datr = dtr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    turnover = (daily["volume"] * daily["close"]).tail(20)
    gaps = (daily["open"] / pc - 1).abs().tail(GAP_COOLDOWN)
    out["prior_atr"] = float(datr.iloc[-1]) if len(datr) and pd.notna(datr.iloc[-1]) else None
    out["turnover"] = float(turnover.median()) if len(turnover) == 20 else None
    out["norm_or_vol"] = float(orv.tail(20).mean()) if len(orv.tail(20)) >= 10 else None
    out["prev_close"] = float(daily["close"].iloc[-1]) if len(daily) else None
    dc = daily["close"]
    out["ret20"] = float((dc.iloc[-1] / dc.iloc[-21] - 1) * 100) if len(dc) > 20 else None
    out["above20"] = bool(dc.iloc[-1] > dc.tail(20).mean()) if len(dc) >= 20 else None
    if out["turnover"] is None or out["prior_atr"] is None or out["norm_or_vol"] is None:
        out["reasons"].append("not enough history")
    elif out["turnover"] < TURNOVER_MIN:
        out["reasons"].append("illiquid (<₹10 cr/day)")
    if len(gaps) and (gaps > GAP_MAX).any():
        out["reasons"].append("20% gap in last 20 sessions")
    out["bars"] = today.reset_index(drop=True)
    first = today[today["t"] == "09:15"]
    if len(first) and out["prev_close"]:
        out["gap"] = float(first["open"].iloc[0] / out["prev_close"] - 1)
        if abs(out["gap"]) > GAP_MAX:
            out["reasons"].append("opening gap >20%")
    return out


def opening(p: dict, completed: set[str]) -> None:
    """Freeze the opening range once the 09:25 candle has closed; opening RVOL and range / ATR filters."""
    b = p["bars"]
    orb = b[b["t"].isin(OR_BARS)]
    p["or_ready"] = all(t in completed for t in OR_BARS)
    if not p["or_ready"]:
        return
    if len(orb) < 3:
        p["reasons"].append("opening candles missing")
        p["or_ready"] = False
        return
    p["or_high"], p["or_low"] = float(orb["high"].max()), float(orb["low"].min())
    p["or_vol"] = float(orb["volume"].sum())
    p["rvol"] = p["or_vol"] / p["norm_or_vol"] if p.get("norm_or_vol") else None
    p["ratio"] = (p["or_high"] - p["or_low"]) / p["prior_atr"] if p.get("prior_atr") else None
    if p["rvol"] is not None and p["rvol"] < RVOL_MIN:
        p["reasons"].append(f"opening RVOL {p['rvol']:.2f} < 2")
    if p["ratio"] is not None and not RATIO_MIN <= p["ratio"] <= RATIO_MAX:
        p["reasons"].append(f"range/ATR {p['ratio']:.2f} outside 0.10–0.80")


class OrbEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.busy = threading.Lock()
        self.universe: dict[str, int] = {}      # symbol -> NSE instrument token
        self.nifty_token: int | None = None
        self.prior: dict[str, pd.DataFrame] = {}  # bars before the session (fetched once a day)
        self.today: dict[str, pd.DataFrame] = {}
        self.sess: date | None = None
        self.loaded_on: date | None = None
        self.preps: dict[str, dict] = {}
        self.ltp: dict[str, float] = {}
        self.fno: set[str] = set()
        self.snapshot: dict | None = None
        self.progress = {"running": False, "done": 0, "total": 0, "stage": ""}
        self.error = ""
        self.last_bar_fetch: str | None = None
        self.last_quote = 0.0
        self.last_probe = 0.0
        self.or_fetched = False

    # ---------------- persistence ----------------
    def _state_file(self) -> Path:
        return CACHE_DIR / f"orb_state_{self.sess}.json"

    def state(self) -> dict:
        return _read_json(self._state_file())

    def save_state(self, st: dict) -> None:
        _write_json(self._state_file(), st)

    def record(self) -> None:
        """Write this session's paper trades to the ledger (rewritten every refresh, final after 15:10)."""
        s = self.snapshot
        if not s:
            return
        c = s["controller"]
        keep = ("symbol", "signal_time", "entry_time", "entry_ref", "qty", "R", "sl", "target", "mode", "status",
                "exit_ref", "exit_time", "exit_reason", "gross", "costs", "net", "ltp", "r_now")
        trades = [{k: t.get(k) for k in keep if k in t} for t in s["trades"]]
        sig = s["signals"]
        rec = {"session": s["session"], "updated": s["asof"], "phase": s["phase"], "final": s["phase"] == "closed",
               "c0": c["c0"], "settings": s["settings"], "state": c["state"], "halt": c["halt"],
               "realized": c["realized"], "unrealized": c["unrealized"], "net": c["net"], "trades": trades,
               "signals": len(sig), "filtered": sum(x["status"] == "filtered" for x in sig),
               "rejected": sum(x["status"] == "rejected" for x in sig), "eligible": s["counts"]["eligible"]}
        led = _read_json(LEDGER_FILE)
        if led.get(s["session"], {}).get("final"):
            return     # a finished session is never rewritten (e.g. by a rule change approved in the improvement loop)
        old = {k: v for k, v in led.get(s["session"], {}).items() if k != "updated"}
        if old != {k: v for k, v in rec.items() if k != "updated"}:
            led[s["session"]] = rec
            _write_json(LEDGER_FILE, led)

    # ---------------- data ----------------
    def _hist(self, kite, token: int, frm: date, to: date) -> list[dict]:
        for attempt in range(4):
            try:
                rows = kite.historical_data(token, frm, to, "5minute")
                time.sleep(0.34)       # Kite allows ~3 historical requests a second
                return rows
            except Exception as exc:   # noqa: BLE001 - rate limit / network: back off and retry
                if "Too many" in str(exc) and attempt < 3:
                    time.sleep(1.5)
                    continue
                if type(exc).__name__ == "TokenException":   # the login is gone; a bad instrument token is skipped
                    raise
                log.warning("ORB history %s failed: %s", token, exc)
                return []
        return []

    def load_universe(self, instruments) -> None:
        """F&O stocks, plus the momentum candidates a session may add (the sweep's picks and any list already
        stored for a recent session, so a restart reloads the same names)."""
        names = {r["name"] for r in instruments("NFO")
                 if r.get("segment") == "NFO-FUT" and r.get("name") and r["name"] not in INDEX_NAMES}
        uni = {}
        tokens = {}
        for r in instruments("NSE"):
            if r.get("instrument_type") == "EQ" and r.get("segment") == "NSE":
                tokens[r["tradingsymbol"]] = r["instrument_token"]
                if r["tradingsymbol"] in names:
                    uni[r["tradingsymbol"]] = r["instrument_token"]
            if r.get("segment") == "INDICES" and r["tradingsymbol"] == "NIFTY 50":
                self.nifty_token = r["instrument_token"]
        self.fno = set(uni)
        extra = [x["symbol"] for x in momentum.picks(self.fno)] if load_prefs().get("wide", True) else []
        for f in sorted(CACHE_DIR.glob("orb_state_*.json"))[-2:]:
            extra += _read_json(f).get("extra", [])
        for sym in dict.fromkeys(extra):
            if sym in tokens and sym not in uni:
                uni[sym] = tokens[sym]
        self.universe = dict(sorted(uni.items()))

    def full_load(self, kite, instruments) -> None:
        """Once a day: universe plus ~40 sessions of 5-minute bars for every F&O stock and Nifty."""
        now = datetime.now(IST)
        self.load_universe(instruments)
        syms = ["NIFTY 50", *self.universe]
        self.progress = {"running": True, "done": 0, "total": len(syms), "stage": "Loading 5-minute history"}
        frames: dict[str, pd.DataFrame] = {}
        for i, s in enumerate(syms):
            tok = self.nifty_token if s == "NIFTY 50" else self.universe[s]
            frames[s] = _frame(self._hist(kite, tok, now.date() - timedelta(days=HIST_DAYS), now.date()))
            self.progress["done"] = i + 1
        nifty = frames["NIFTY 50"]
        if nifty.empty:
            raise RuntimeError("Kite returned no Nifty 50 candles")
        sess = max(nifty["d"])
        # the session's momentum names are fixed the first time it is loaded; a session that has already started
        # trading on the F&O list (no stored list) keeps F&O only, so a change never rewrites trades already taken
        self.sess = sess
        st = self.state()
        if "extra" not in st:
            st["extra"] = [] if st.get("c0") else sorted(set(self.universe) - self.fno)
            self.save_state(st)
        keep = self.fno | set(st["extra"])
        self.universe = {k: v for k, v in self.universe.items() if k in keep}
        frames = {k: v for k, v in frames.items() if k == "NIFTY 50" or k in keep}
        with self.lock:
            self.sess, self.loaded_on = sess, now.date()
            self.prior = {s: f[f["d"] < sess].reset_index(drop=True) for s, f in frames.items()}
            self.today = {s: f[f["d"] == sess].reset_index(drop=True) for s, f in frames.items()}
            self.last_bar_fetch = None
            self.or_fetched = sess < now.date() or "09:25" in self._completed(now)
        self.progress = {"running": False, "done": len(syms), "total": len(syms), "stage": "Loaded"}

    def refresh_today(self, kite, syms: list[str]) -> None:
        now = datetime.now(IST)
        for i, s in enumerate(["NIFTY 50", *syms]):
            tok = self.nifty_token if s == "NIFTY 50" else self.universe.get(s)
            if tok:
                f = _frame(self._hist(kite, tok, now.date(), now.date()))
                if not f.empty:   # a failed request keeps the candles already held
                    self.today[s] = f[f["d"] == self.sess].reset_index(drop=True)
            self.progress["done"] = i + 1

    # ---------------- strategy ----------------
    def _completed(self, now: datetime) -> set[str]:
        if self.sess < now.date():
            return {_add("09:15", 5 * i) for i in range(SESSION_BARS)}
        cut = now - timedelta(seconds=10)
        return {_add("09:15", 5 * i) for i in range(SESSION_BARS)
                if datetime.combine(self.sess, datetime.strptime(_add("09:15", 5 * i + 5), "%H:%M").time(), IST) <= cut}

    def build(self, now: datetime) -> None:
        """Prepare every stock and replay the session; result in self.snapshot."""
        completed = self._completed(now)
        preps = {}
        sw = momentum.latest() or {}
        rs_map = {x["symbol"]: x for x in sw.get("stocks", [])}
        for s in self.universe:
            df = pd.concat([self.prior.get(s, pd.DataFrame()), self.today.get(s, pd.DataFrame())], ignore_index=True)
            if df.empty:
                continue
            p = prepare(s, df, self.sess)
            opening(p, completed)
            g = rs_map.get(s) or {}
            p["source"] = "F&O" if s in self.fno else "Momentum"
            p["rs"] = g.get("rs")
            p["mom_ok"] = bool(p.get("above20") and (p.get("ret20") or 0) > 0 and (p["rs"] is None or p["rs"] >= 50))
            preps[s] = p
        nf = pd.concat([self.prior.get("NIFTY 50"), self.today.get("NIFTY 50")], ignore_index=True)
        nf = _indicators(nf, volume=False)
        nifty = nf[nf["d"] == self.sess].set_index("t")
        self.preps = preps
        self.snapshot = self._replay(preps, nifty, completed, now)

    def _setting(self, st: dict, key: str, at: str, default):
        val = st.get("base", {}).get(key, default)
        for ch in st.get("changes", []):
            if ch["at"] <= at and key in ch:
                val = ch[key]
        return val

    def _replay(self, preps: dict, nifty: pd.DataFrame, completed: set[str], now: datetime) -> dict:
        st = self.state()
        c0 = st.get("c0")
        loss_amt = LOSS_PCT * c0 if c0 else None
        live_day = self.sess == now.date()
        latch = st.get("latch")
        shortlist = [p for p in preps.values() if p.get("or_ready") and not p["reasons"]]
        rank = {p["symbol"]: -(p.get("rvol") or 0) for p in shortlist}
        bars = {p["symbol"]: p["bars"].set_index("t") for p in shortlist}
        trades, signals = [], []
        pos: dict[str, dict] = {}
        pending: dict[str, dict] = {}
        realized = 0.0
        halt = None

        def fill_exit(tr, price, at, reason):
            tr.update(exit_ref=round(price, 2), exit_fill=round(price * (1 - SLIP), 4), exit_time=at, exit_reason=reason,
                      status="closed")
            bv, sv = tr["entry_fill"] * tr["qty"], tr["exit_fill"] * tr["qty"]
            tr["gross"] = round(sv - bv, 2)
            tr["costs"] = round(costs(bv, sv), 2)
            tr["net"] = round(tr["gross"] - tr["costs"], 2)
            pos.pop(tr["symbol"], None)
            return tr["net"]

        def mark(prices: dict[str, float]) -> float:
            u = 0.0
            for s, tr in pos.items():
                px = prices.get(s)
                if px is None:
                    continue
                bv, sv = tr["entry_fill"] * tr["qty"], px * (1 - SLIP) * tr["qty"]
                u += sv - bv - costs(bv, sv)
            return u

        def flatten(prices, at, reason):
            nonlocal realized
            for s in list(pos):
                realized += fill_exit(pos[s], prices.get(s, pos[s]["entry_ref"]), at, reason)
            for s, sig in pending.items():
                sig.update(status="cancelled", note=reason)
            pending.clear()

        grid = [_add("09:15", 5 * i) for i in range(SESSION_BARS)]
        for t in grid:
            if t not in completed:
                break
            end = _add(t, 5)
            if latch and not halt and latch["time"] < end:
                halt = {"state": latch["state"], "time": latch["time"], "pnl": latch["pnl"], "source": latch.get("source", "live price")}
                flatten(latch.get("prices", {}), latch["time"], "manual stop" if latch["state"] == "HALTED_MANUAL" else "daily stop")
            # 1. pending entries fill at this candle's open
            for s, sig in sorted(pending.items(), key=lambda kv: rank.get(kv[0], 0)):
                b = bars[s]
                reason = None
                if halt:
                    reason = "daily stop hit"
                elif t not in b.index:
                    reason = "next candle missing"
                elif t > LAST_ENTRY:
                    reason = "after 11:30"
                trade = None if reason else self._enter(sig, float(b.loc[t, "open"]), t, st, c0, pos)
                if isinstance(trade, str):
                    reason, trade = trade, None
                if trade:
                    pos[s] = trade
                    trades.append(trade)
                    sig.update(status="entered")
                else:
                    sig.update(status="rejected", note=reason)
            pending.clear()
            # 2. open trades against this candle: forced exit at 15:10 open, else stop (first) then target
            for s, tr in list(pos.items()):
                b = bars[s]
                if t not in b.index:
                    continue
                o, h, l = (float(b.loc[t, k]) for k in ("open", "high", "low"))
                if t == EXIT_BAR:
                    realized += fill_exit(tr, o, t, "15:10 exit")
                elif o <= tr["sl"]:
                    realized += fill_exit(tr, o, t, "stop (gap)")
                elif l <= tr["sl"]:
                    realized += fill_exit(tr, tr["sl"], t, "stop")
                elif tr["target"] and o >= tr["target"]:
                    realized += fill_exit(tr, o, t, "target")
                elif tr["target"] and h >= tr["target"]:
                    realized += fill_exit(tr, tr["target"], t, "target")
            # 3. shared daily controller on this close
            closes = {s: float(bars[s].loc[t, "close"]) for s in pos if t in bars[s].index}
            net = realized + mark(closes)
            if not halt and c0 and (net <= -loss_amt or net >= PROFIT_STOP):
                halt = {"state": "HALTED_LOSS" if net < 0 else "HALTED_PROFIT", "time": end, "pnl": round(net, 2),
                        "source": "5-minute close"}
                flatten(closes, end, "daily stop")
            # 4. fresh breakout crossings on this close
            if halt or not FIRST_SIGNAL <= t <= LAST_SIGNAL or t not in nifty.index:
                continue
            n = nifty.loc[t]
            nifty_ok = bool(n["close"] > n["savg"] and n["ema20"] > n["ema50"])
            for p in shortlist:
                s, b = p["symbol"], bars[p["symbol"]]
                if s in pos or s in pending or t not in b.index:
                    continue
                i = b.index.get_loc(t)
                if i == 0:
                    continue
                r, prev = b.iloc[i], b.iloc[i - 1]
                if not (r["close"] > p["or_high"] >= prev["close"]):
                    continue
                checks = {"vwap": bool(r["close"] > r["vwap"]), "trend": bool(r["ema20"] > r["ema50"]),
                          "nifty": nifty_ok, "atr": bool(r["atr"] > 0)}
                if self._setting(st, "momentum", t, False):
                    checks["momentum"] = p["mom_ok"]
                sig = {"symbol": s, "time": t, "confirmed": end, "close": round(float(r["close"]), 2),
                       "atr": _f(r["atr"], 3), "or_high": round(p["or_high"], 2), "checks": checks,
                       "rvol": _f(p.get("rvol")), "status": "pending"}
                if not all(checks.values()):
                    sig.update(status="filtered", note=", ".join(
                        {"vwap": "below VWAP", "trend": "EMA20 ≤ EMA50", "nifty": "Nifty not confirming", "atr": "no ATR",
                         "momentum": "weak momentum"}[k]
                        for k, v in checks.items() if not v))
                    signals.append(sig)
                    continue
                R = STOP_ATR * float(r["atr"])
                sig.update(R=round(R, 2), sl_est=round(sig["close"] - R, 2), target_est=round(sig["close"] + TARGET_R * R, 2))
                signals.append(sig)
                pending[s] = sig

        # the forming candle: pending entries take its open as soon as Kite has it
        nxt = next((t for t in grid if t not in completed), None)
        if live_day and nxt and pending:
            for s, sig in sorted(pending.items(), key=lambda kv: rank.get(kv[0], 0)):
                b = bars[s]
                if halt or nxt > LAST_ENTRY:
                    sig.update(status="rejected", note="daily stop hit" if halt else "after 11:30")
                elif nxt in b.index:
                    trade = self._enter(sig, float(b.loc[nxt, "open"]), nxt, st, c0, pos)
                    if isinstance(trade, str):
                        sig.update(status="rejected", note=trade)
                    else:
                        pos[s] = trade
                        trades.append(trade)
                        sig.update(status="entered")
                else:
                    sig.update(note=f"buys at the {nxt} open")
        if latch and not halt:
            halt = {"state": latch["state"], "time": latch["time"], "pnl": latch["pnl"], "source": latch.get("source", "live price")}
            flatten(latch.get("prices", {}), latch["time"], "manual stop" if latch["state"] == "HALTED_MANUAL" else "daily stop")

        # live marks for open trades
        last = {s: float(bars[s]["close"].iloc[-1]) for s in pos if len(bars[s])}
        marks = {s: self.ltp.get(s, last.get(s)) for s in pos}
        unreal = mark({s: v for s, v in marks.items() if v is not None})
        for s, tr in pos.items():
            px = marks.get(s)
            tr["ltp"] = _f(px)
            tr["r_now"] = _f((px - tr["entry_ref"]) / tr["R"]) if px else None
            if px:
                bv, sv = tr["entry_fill"] * tr["qty"], px * (1 - SLIP) * tr["qty"]
                tr["net"] = round(sv - bv - costs(bv, sv), 2)
            tr["stop_touched"] = bool(px is not None and px <= tr["sl"])
        net = realized + unreal
        if live_day and not halt and c0 and pos and (net <= -loss_amt or net >= PROFIT_STOP) and nxt and nxt <= EXIT_BAR:
            # latch on live prices; the next refresh flattens at these marks and stays halted for the day
            st["latch"] = {"state": "HALTED_LOSS" if net < 0 else "HALTED_PROFIT", "time": now.strftime("%H:%M:%S"),
                           "pnl": round(net, 2), "prices": {s: v for s, v in marks.items() if v is not None}}
            self.save_state(st)
            return self._replay(preps, nifty, completed, now)

        return self._snapshot(preps, nifty, completed, now, st, trades, signals, realized, unreal, halt, nxt)

    def _enter(self, sig: dict, ref: float, t: str, st: dict, c0: float | None, pos: dict) -> dict | str:
        R = STOP_ATR * sig["atr"]
        if abs(ref - sig["close"]) > JUMP_ATR * sig["atr"]:
            return "opened too far from the signal close"
        if not RISK_MIN <= R / ref <= RISK_MAX:
            return "stop distance outside 0.05–3%"
        if not c0:
            return "starting capital unknown"
        if self._setting(st, "paused", t, False):
            return "new entries paused by you"
        notional = self._setting(st, "notional", t, NOTIONAL)
        avail = c0 - sum(p["entry_ref"] * p["qty"] for p in pos.values())
        if avail < notional:   # whole allocations only: no leftover 1-share trades
            return "no capital free"
        qty = math.floor(notional / ref)
        if qty < 1:
            return "price above the per-trade amount"
        mode = self._setting(st, "mode", t, "3R")
        return {"symbol": sig["symbol"], "signal_time": sig["time"], "signal_close": sig["close"], "entry_time": t,
                "entry_ref": round(ref, 2), "entry_fill": round(ref * (1 + SLIP), 4), "qty": qty, "R": round(R, 2),
                "sl": round(ref - R, 2), "one_r": round(ref + R, 2),
                "target": round(ref + TARGET_R * R, 2) if mode == "3R" else None, "mode": mode, "status": "open",
                "net": 0.0}

    def _snapshot(self, preps, nifty, completed, now, st, trades, signals, realized, unreal, halt, nxt) -> dict:
        c0 = st.get("c0")
        last_t = max(completed) if completed else None
        n = nifty.loc[last_t] if last_t and last_t in nifty.index else None
        live_day = self.sess == now.date()
        if not completed:
            phase = "pre-open"
        elif "09:25" not in completed:
            phase = "opening range"
        elif last_t <= LAST_SIGNAL:
            phase = "entry window"
        elif last_t < EXIT_BAR:
            phase = "managing"
        else:
            phase = "closed"
        open_syms = {t["symbol"]: t for t in trades if t["status"] == "open"}
        rows, excluded = [], []
        for p in preps.values():
            b = p["bars"]
            lb = b[b["t"].isin(completed)].tail(1)
            last = lb.iloc[0] if len(lb) else None
            base = {"symbol": p["symbol"], "source": p.get("source", "F&O"), "rs": p.get("rs"), "ret20": _f(p.get("ret20")),
                    "above20": p.get("above20"), "mom_ok": p.get("mom_ok"),
                    "ltp": _f(self.ltp.get(p["symbol"], last["close"] if last is not None else None)),
                    "or_high": _f(p.get("or_high")), "or_low": _f(p.get("or_low")), "rvol": _f(p.get("rvol")),
                    "ratio": _f(p.get("ratio")), "prior_atr": _f(p.get("prior_atr")),
                    "turnover_cr": _f((p.get("turnover") or 0) / 1e7, 1), "gap_pct": _f((p.get("gap") or 0) * 100)}
            if p["reasons"]:
                excluded.append({**base, "reason": p["reasons"][0]})
                continue
            if not p.get("or_ready"):
                rows.append({**base, "status": "waiting for the 09:30 opening range"})
                continue
            close = float(last["close"])
            checks = {"above_or": close > p["or_high"], "vwap": bool(close > last["vwap"]),
                      "trend": bool(last["ema20"] > last["ema50"])}
            if p["symbol"] in open_syms:
                status = "in trade"
            elif any(s["symbol"] == p["symbol"] and s["status"] == "pending" for s in signals):
                status = "entry pending"
            elif phase not in ("entry window",):
                status = "entry window closed" if phase in ("managing", "closed") else "waiting"
            elif close > p["or_high"]:
                status = "above range: needs a fresh cross"
            else:
                status = f"{(p['or_high'] / close - 1) * 100:.2f}% below range high"
            rows.append({**base, "close": _f(close), "vwap": _f(last["vwap"]), "ema20": _f(last["ema20"]),
                         "ema50": _f(last["ema50"]), "checks": checks, "status": status,
                         "dist_pct": _f((close / p["or_high"] - 1) * 100)})
        rows.sort(key=lambda r: (r.get("rvol") is None, -(r.get("rvol") or 0), -(r.get("turnover_cr") or 0)))
        reasons: dict[str, int] = {}
        for e in excluded:
            k = e["reason"]
            k = "opening RVOL < 2" if k.startswith("opening RVOL") else "range/ATR outside 0.10–0.80" if k.startswith("range/ATR") else k
            reasons[k] = reasons.get(k, 0) + 1
        net = realized + unreal
        return {
            "session": self.sess.isoformat(), "live": live_day, "asof": now.isoformat(timespec="seconds"),
            "last_bar": last_t, "next_bar": nxt, "phase": phase,
            "nifty": None if n is None else {"close": _f(n["close"]), "avg": _f(n["savg"]), "ema20": _f(n["ema20"]),
                                             "ema50": _f(n["ema50"]),
                                             "ok": bool(n["close"] > n["savg"] and n["ema20"] > n["ema50"])},
            "controller": {"c0": c0, "c0_source": st.get("c0_source"), "profit_stop": PROFIT_STOP,
                           "loss_amount": round(LOSS_PCT * c0, 2) if c0 else None, "realized": round(realized, 2),
                           "unrealized": round(unreal, 2), "net": round(net, 2),
                           "state": halt["state"] if halt else ("CLOSED" if phase == "closed" else "ACTIVE"), "halt": halt},
            "settings": {"momentum": bool(self._setting(st, "momentum", "99:99", False)), "wide": bool(st.get("extra")),
                         "wide_next": load_prefs().get("wide", True),
                         "mode": self._setting(st, "mode", "99:99", "3R"),
                         "notional": self._setting(st, "notional", "99:99", NOTIONAL),
                         "paused": bool(self._setting(st, "paused", "99:99", False))},
            "counts": {"universe": len(self.universe), "eligible": len(rows), "excluded": len(excluded),
                       "fno": len([s for s in self.universe if s in self.fno]), "momentum": len([s for s in self.universe if s not in self.fno])},
            "shortlist": rows, "excluded": sorted(excluded, key=lambda e: e["symbol"]), "excluded_reasons": reasons,
            "trades": trades, "signals": signals,
        }

    # ---------------- driver ----------------
    def tick(self, kite, instruments, capital) -> None:
        """Called every ~15 s: load the history once a day, refresh candles after each 5-minute close, live marks."""
        if not self.busy.acquire(blocking=False):
            return
        try:
            now = datetime.now(IST)
            hm = now.strftime("%H:%M")
            if self.loaded_on != now.date():
                self.full_load(kite, instruments)
            elif (self.sess != now.date() and now.weekday() < 5 and "09:16" <= hm <= "15:30"
                  and time.time() - self.last_probe > 120):
                # loaded before the open (or a holiday): switch to today once Nifty has today's first candle
                self.last_probe = time.time()
                if not _frame(self._hist(kite, self.nifty_token, now.date(), now.date())).empty:
                    self.full_load(kite, instruments)
            live = self.sess == now.date()
            st = self.state()
            if not st.get("c0") or "base" not in st:
                # each session starts from the carried-forward settings, so later changes never rewrite earlier trades
                prefs = load_prefs()
                st.setdefault("base", {"mode": prefs.get("mode", "3R"), "notional": prefs.get("notional", NOTIONAL),
                                       "momentum": prefs.get("momentum", True)})
                if not st.get("c0"):
                    c0, src = prefs.get("capital"), "your paper capital (carried forward)"
                    if not c0:
                        c0 = capital()
                        src = ("Kite equity funds (net) at the first scan" if live
                               else "Kite equity funds (net) now: this session was replayed later")
                    if not c0:
                        c0, src = DEFAULT_CAPITAL, "default paper capital (Kite equity funds show ₹0)"
                    st.update(c0=c0, c0_source=src)
                self.save_state(st)
            completed = self._completed(now)
            last_t = max(completed) if completed else None
            if live and last_t and last_t >= "09:25" and last_t != self.last_bar_fetch and self.snapshot is not None:
                snap = self.snapshot
                open_syms = [t["symbol"] for t in snap["trades"] if t["status"] == "open"]
                if not self.or_fetched:
                    syms = list(self.universe)          # opening range just closed: every stock once
                elif last_t <= LAST_SIGNAL:
                    syms = sorted({r["symbol"] for r in snap["shortlist"] if "checks" in r} | set(open_syms))
                else:
                    syms = open_syms if last_t <= EXIT_BAR else []
                self.progress = {"running": True, "done": 0, "total": len(syms) + 1, "stage": f"Candles to {_add(last_t, 5)}"}
                self.refresh_today(kite, syms)
                self.or_fetched = True
                self.progress = {"running": False, "done": 0, "total": 0, "stage": ""}
                self.last_bar_fetch = last_t
            if live and self.snapshot and time.time() - self.last_quote > 10 and "09:15" <= hm <= "15:30":
                keys = [f"NSE:{r['symbol']}" for r in self.snapshot["shortlist"]][:250]
                if keys:
                    try:
                        q = kite.ltp(*keys)
                        self.ltp = {k.split(":", 1)[1]: v["last_price"] for k, v in q.items()}
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ORB quote failed: %s", exc)
                self.last_quote = time.time()
            with self.lock:
                self.build(now)
            self.record()
            self.error = ""
        except Exception as exc:  # noqa: BLE001
            log.exception("ORB refresh failed")
            self.error = str(exc)
            self.progress["running"] = False
        finally:
            self.busy.release()

    def update_settings(self, mode: str | None, notional: float | None, capital: float | None,
                        mom: bool | None = None, wide: bool | None = None) -> None:
        if self.sess is None:
            raise ValueError("ORB data is still loading")
        st = self.state()
        now = datetime.now(IST)
        at = now.strftime("%H:%M") if self.sess == now.date() else "00:00"
        if capital is not None:
            if self.snapshot and self.snapshot["trades"]:
                raise ValueError("Starting capital is frozen once the first ORB trade of the day is in")
            st.update(c0=capital, c0_source="entered by you")
        ch = {"at": at}
        if mode is not None:
            ch["mode"] = mode
        if notional is not None:
            ch["notional"] = notional
        if mom is not None:
            ch["momentum"] = mom
        if len(ch) > 1:
            st.setdefault("changes", []).append(ch)
        self.save_state(st)
        save_prefs(capital=capital, mode=mode, notional=notional, momentum=mom, wide=wide)
        with self.lock:
            self.build(now)
        self.record()

    def pause(self, paused: bool) -> None:
        """Stop (or allow again) new ORB paper entries from now on; trades already open keep running."""
        if self.sess is None:
            raise ValueError("ORB data is still loading")
        st = self.state()
        now = datetime.now(IST)
        st.setdefault("changes", []).append({"at": now.strftime("%H:%M") if self.sess == now.date() else "00:00", "paused": paused})
        self.save_state(st)
        with self.lock:
            self.build(now)
        self.record()

    def kill(self) -> None:
        """Emergency stop: close every open ORB paper trade at its last price and halt the session."""
        s = self.snapshot
        now = datetime.now(IST)
        if not s or self.sess != now.date():
            raise ValueError("No live ORB session to stop")
        st = self.state()
        if st.get("latch"):
            raise ValueError("The ORB session is already halted")
        prices = {t["symbol"]: t["ltp"] for t in s["trades"] if t["status"] == "open" and t.get("ltp") is not None}
        st["latch"] = {"state": "HALTED_MANUAL", "time": now.strftime("%H:%M:%S"), "pnl": s["controller"]["net"],
                       "prices": prices, "source": "emergency stop"}
        self.save_state(st)
        with self.lock:
            self.build(now)
        self.record()

    def paper(self) -> dict:
        """Paper-trading journal: every recorded session, all trades and running totals."""
        led = _read_json(LEDGER_FILE)
        days = sorted(led.values(), key=lambda d: d["session"])
        cum, peak, dd = 0.0, 0.0, 0.0
        curve, trades = [], []
        for d in days:
            cum += d["net"]
            peak = max(peak, cum)
            dd = min(dd, cum - peak)
            curve.append([d["session"], round(cum, 2)])
            closed = [t for t in d["trades"] if t["status"] == "closed"]
            d["wins"] = sum(t["net"] > 0 for t in closed)
            d["losses"] = sum(t["net"] <= 0 for t in closed)
            d["costs"] = round(sum(t.get("costs") or 0 for t in closed), 2)
            trades += [{**t, "session": d["session"]} for t in d["trades"]]
        closed = [t for t in trades if t["status"] == "closed"]
        wins = [t["net"] for t in closed if t["net"] > 0]
        losses = [t["net"] for t in closed if t["net"] <= 0]
        rs = [(t["exit_ref"] - t["entry_ref"]) / t["R"] for t in closed if t.get("R")]
        return {
            "days": days[::-1], "trades": trades[::-1], "curve": curve,
            "stats": {"days": len(days), "trades": len(closed), "open": len(trades) - len(closed),
                      "net": round(cum, 2), "wins": len(wins), "losses": len(losses),
                      "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
                      "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                      "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                      "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                      "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                      "costs": round(sum(t.get("costs") or 0 for t in closed), 2),
                      "best_day": max((d["net"] for d in days), default=None),
                      "worst_day": min((d["net"] for d in days), default=None),
                      "max_drawdown": round(dd, 2)},
        }

    def chart(self, sym: str) -> dict:
        p = self.preps.get(sym)
        if not p:
            raise KeyError(sym)
        b = p["bars"]
        snap = self.snapshot or {}
        return {"symbol": sym, "session": snap.get("session"), "or_high": _f(p.get("or_high")), "or_low": _f(p.get("or_low")),
                "bars": [{"t": r.t, "o": float(r.open), "h": float(r.high), "l": float(r.low), "c": float(r.close), "v": int(r.volume),
                          "vwap": _f(r.vwap), "ema20": _f(r.ema20), "ema50": _f(r.ema50)} for r in b.itertuples()],
                "trades": [t for t in snap.get("trades", []) if t["symbol"] == sym],
                "signals": [s for s in snap.get("signals", []) if s["symbol"] == sym]}


engine = OrbEngine()
