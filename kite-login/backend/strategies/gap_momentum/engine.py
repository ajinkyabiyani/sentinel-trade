"""Runs one trading day of the Gap Momentum strategy on paper.

Adapted from the gap_momentum_kite package: the same steps and rules, with these changes for the app:
  - paper broker only (no real orders);
  - a holiday guard: futures opens count only if they traded today;
  - booked P&L uses the app's full Zerodha option charges (sizing still uses the backtested ₹120 estimate);
  - the open position and the day's progress are saved, so an app restart mid-day carries on;
  - on_open / on_close hooks, which the app uses for Telegram.

Steps, called by the app's scheduler:
  engine.prepare()      before 09:15
  engine.try_entry()    at/after 09:15:20 (until it returns True or last_entry_time passes)
  engine.poll()         every few seconds while a position is open
  engine.square_off()   at 15:15
"""
import json
import logging
import os
import time
from datetime import datetime

from . import data as D
from . import kite_data as K
from .config import Config
from .strategy import choose_contract, rank_signals, round_tick, size_position

log = logging.getLogger("gap_momentum")


def option_charges(entry: float, exit_: float, qty: int) -> float:
    from strategies import common
    return round(common.charges([(True, entry * qty), (False, exit_ * qty)]), 2)


class Ledger:
    """Realized P&L across days, for the kill switch. Stored as JSON in state_dir."""
    def __init__(self, path):
        self.path = path
        self.d = {"cum": 0.0, "peak": 0.0, "halted": False, "trades": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2)

    def record(self, trade: dict, kill_dd: float):
        self.d["trades"].append(trade)
        self.d["cum"] += trade["pnl"]
        self.d["peak"] = max(self.d["peak"], self.d["cum"])
        if self.d["peak"] - self.d["cum"] >= kill_dd:
            self.d["halted"] = True
            log.error("KILL SWITCH: realized P&L is %.0f below its peak. New trades halted until reset.", self.d["peak"] - self.d["cum"])
        self.save()


class GapMomentumEngine:
    def __init__(self, broker, cfg: Config = None, now=datetime.now, sleep=time.sleep, on_open=None, on_close=None):
        self.b, self.cfg, self.now, self.sleep = broker, cfg or Config(), now, sleep
        self.on_open, self.on_close = on_open, on_close
        self.ledger = Ledger(os.path.join(self.cfg.state_dir, "ledger.json"))
        self.today = self.now().strftime("%Y%m%d")
        self.journal = {"date": self.today, "events": []}
        self.cands, self.contracts, self.regime = [], {}, None
        self.signals = []
        self.pos = None
        self.trades_today = 0
        self.prepared = False
        self.done = False
        self._restore()

    # ------------------------------------------------------------------ files
    def _jpath(self):
        return os.path.join(self.cfg.state_dir, f"journal_{self.today}.json")

    def _save(self):
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        self.journal.update(regime=self.regime, signals=self.signals, pos=self.pos, done=self.done, trades_today=self.trades_today,
                            universe=[dict(symbol=x.symbol, prev_close=round(x.prev_close, 2), atr=round(x.atr, 2)) for x in self.cands] or self.journal.get("universe"))
        with open(self._jpath(), "w", encoding="utf-8") as f:
            json.dump(self.journal, f, indent=2, default=str)

    def _restore(self):
        """Carry on after an app restart: the day's events, whether it already traded, and an open position."""
        try:
            with open(self._jpath(), encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            return
        self.journal = j
        self.regime, self.signals = j.get("regime"), j.get("signals") or []
        self.trades_today, self.done = j.get("trades_today", 0), bool(j.get("done"))
        p = j.get("pos")
        if p:
            px = round_tick(p["trigger"] * (1 - self.cfg.sl_limit_buffer), self.cfg.tick, "down")
            p["sl_id"] = self.b.sell_stoploss(p["tradingsymbol"], p["qty"], p["trigger"], px)
            self.pos = p
            self._ev("Restarted: position restored with its stop", tradingsymbol=p["tradingsymbol"], trigger=p["trigger"])

    def _ev(self, msg, **kw):
        log.info(msg + (" " + json.dumps(kw, default=str) if kw else ""))
        self.journal.setdefault("events", []).append(dict(t=self.now().isoformat(timespec="seconds"), msg=msg, **kw))
        self._save()

    def _instr(self, segment, name, expiry=None, strike=None, itype=None):
        for i in self.b.nfo_instruments():
            if i["segment"] != segment or i["name"] != name:
                continue
            if expiry and str(i["expiry"]) != expiry:
                continue
            if strike is not None and float(i["strike"]) != float(strike):
                continue
            if itype and i["instrument_type"] != itype:
                continue
            return i
        return None

    # ------------------------------------------------------------------ steps
    def prepare(self):
        """Before the open: regime, universe, ATR, previous closes (from Kite)."""
        c = self.cfg
        self.prepared = True
        if self.done:
            return False
        if self.ledger.d.get("halted"):
            self.done = True
            self._ev("Kill switch is on: no trading. Reset 'halted' in ledger.json after review.")
            return False
        if self.now().time() >= c.prepare_by:
            self.done = True
            self._ev(f"Started after {c.prepare_by:%H:%M}: Kite quotes now show today's trading, not the last session's. No trade today.")
            return False
        k, today = self.b.kite, K.as_date(self.today)
        K.SPECIAL.update(c.special_sessions)
        self.regime, last = K.nifty_regime(k, today, c.regime_sma, c.regime_slope_lag, c.history_sessions)
        self._ev("Prepared", last_session=last, regime=self.regime)
        if self.regime not in ("UP", "DOWN"):
            self.done = True
            self._ev("Market regime is not trending: no trade today.")
            return False
        instruments = self.b.nfo_instruments()
        turnover, self.contracts = K.option_activity(k, instruments, sleep=self.sleep)
        self.cands = K.prepare_universe(k, instruments, turnover, today, c.universe_size, c.atr_days, sleep=self.sleep)
        self._ev("Universe", stocks=[(x.symbol, round(x.prev_close, 2), round(x.atr, 2)) for x in self.cands])
        return True

    def try_entry(self):
        """At the open: rank gaps, pick the biggest tradable one, buy it. True when finished trying for today."""
        c = self.cfg
        if self.done or self.trades_today >= c.max_trades_per_day:
            return True
        if self.now().time() > c.last_entry_time:
            self.done = True
            self._ev("Entry window closed without a trade.")
            return True
        fut_keys = {}
        for x in self.cands:
            i = self._instr("NFO-FUT", x.symbol, expiry=x.fut_expiry)
            if i:
                fut_keys[x.symbol] = "NFO:" + i["tradingsymbol"]
        if not fut_keys:
            self.done = True
            self._ev("No futures contracts found for the universe.")
            return True
        q = self.b.quote(list(fut_keys.values()))
        today = self.now().date()
        opens = {}
        for s, k in fut_keys.items():
            row = q.get(k, {})
            traded = row.get("last_trade_time")
            if traded is not None and getattr(traded, "date", lambda: today)() != today:
                continue                                    # no trade yet today (or a holiday): yesterday's open isn't a gap
            opens[s] = (row.get("ohlc", {}) or {}).get("open")
        if not any(opens.values()):
            return False                                    # opens not available yet; retry on the next pass
        # The gap is measured from the futures candles' last close: Kite's quoted previous close must agree.
        checked = [(x.prev_close, (q.get(fut_keys[x.symbol], {}).get("ohlc") or {}).get("close")) for x in self.cands if x.symbol in fut_keys]
        off = [1 for b, k in checked if k and abs(b / k - 1) > 0.005]
        if checked and len(off) * 2 > len(checked):
            self.done = True
            self._ev("Futures candles don't match Kite's previous close (data glitch). No trade today.", mismatched=len(off), of=len(checked))
            return True
        signals = rank_signals(self.cands, opens, c.gap_threshold_atr)
        self.signals = [dict(symbol=s.candidate.symbol, side=s.side, gap_atr=round(s.gap_atr, 2), fut_open=s.fut_open, prev_close=round(s.candidate.prev_close, 2)) for s in signals]
        self._ev("Signals", ranked=[(s.candidate.symbol, s.side, round(s.gap_atr, 2)) for s in signals])
        chain = [dict(symbol=i["name"], expiry=str(i["expiry"]), strike=float(i["strike"]), type=i["instrument_type"])
                 for i in self.b.nfo_instruments() if i["segment"] == "NFO-OPT"]
        today_iso = D.iso(self.today)
        for s in signals:
            sym = s.candidate.symbol
            pick = choose_contract(chain, sym, s.side, s.fut_open, today_iso, c.roll_if_dte_le)
            if not pick:
                continue
            exp, k = pick
            contracts = self.contracts.get(inst["tradingsymbol"], 0) if (inst := self._instr("NFO-OPT", sym, exp, k, s.side)) else 0
            if contracts < c.min_prev_contracts:
                self._ev("Skip: illiquid strike", symbol=sym, strike=k, prev_contracts=contracts)
                continue
            if not inst:
                continue
            key = "NFO:" + inst["tradingsymbol"]
            oq = self.b.quote([key])[key]
            depth = oq.get("depth") or {}
            ask = (depth.get("sell") or [{}])[0].get("price") or oq["last_price"]
            if ask < c.min_premium:
                continue
            lot = int(inst["lot_size"])
            lots = size_position(ask, lot, c.risk_per_trade, c.est_costs, c.stop_pct, c.capital)
            if lots < 1:
                self._ev("Skip: one lot risks more than the limit", symbol=sym, ask=ask, lot=lot)
                continue
            self._enter(inst, lots, lot, ask, s, exp)
            self.done = self.pos is None                  # if the entry did not fill, stop for the day
            self._save()
            return True
        self.done = True
        self._ev("No tradable gap today.")
        return True

    def _enter(self, inst, lots, lot, ask, sig, exp):
        c, ts = self.cfg, inst["tradingsymbol"]
        qty = lots * lot
        limit = round_tick(ask, c.tick, "up")
        cap = round_tick(ask * (1 + c.entry_max_slippage), c.tick, "up")
        oid = self.b.buy_limit(ts, qty, limit)
        self._ev("Entry order", tradingsymbol=ts, qty=qty, limit=limit, gap_atr=round(sig.gap_atr, 2), regime=self.regime)
        t0 = time.monotonic()
        st = self.b.order(oid)
        while st["status"] not in ("COMPLETE", "REJECTED", "CANCELLED"):
            if time.monotonic() - t0 > c.entry_fill_timeout_s:
                break
            self.sleep(c.poll_seconds)
            st = self.b.order(oid)
            if st["status"] == "OPEN" and limit < cap:                # walk the price up, never past cap
                key = "NFO:" + ts
                q = self.b.quote([key])[key]
                new_ask = ((q.get("depth") or {}).get("sell") or [{}])[0].get("price") or q["last_price"]
                new_limit = min(cap, round_tick(new_ask, c.tick, "up"))
                if new_limit > limit:
                    limit = new_limit
                    self.b.modify(oid, price=limit)
        if st["status"] != "COMPLETE":
            self.b.cancel(oid)
            st = self.b.order(oid)
        filled = st["filled"]
        if filled <= 0:
            self._ev("Entry not filled within limits: no trade.", status=st["status"])
            return
        avg = st["average_price"]
        trig = round_tick(avg * (1 - c.stop_pct), c.tick, "down")
        px = round_tick(trig * (1 - c.sl_limit_buffer), c.tick, "down")
        sl = self.b.sell_stoploss(ts, filled, trig, px)
        risk = (avg - trig) * filled
        self.pos = dict(tradingsymbol=ts, key="NFO:" + ts, qty=filled, lots=filled // lot, lot_size=lot, entry=avg, sl_id=sl, trigger=trig,
                        ref_1r=round_tick(avg + (avg - trig), c.tick, "up"), max_loss=round(risk, 2), symbol=sig.candidate.symbol,
                        side=sig.side, strike=float(inst["strike"]), expiry=exp, gap_atr=round(sig.gap_atr, 2), regime=self.regime,
                        entry_time=self.now().isoformat(timespec="seconds"))
        self.trades_today += 1
        self._ev("Position open", **{k: v for k, v in self.pos.items() if k != "sl_id"})
        if self.on_open:
            try:
                self.on_open(dict(self.pos), self.today)
            except Exception:  # noqa: BLE001 - a Telegram problem must never stop the strategy
                log.exception("gap momentum on_open hook failed")

    def poll(self):
        """While a position is open: detect a stop-out. True when the position is closed."""
        if not self.pos:
            return True
        st = self.b.order(self.pos["sl_id"])
        if st["status"] == "COMPLETE":
            self._close(st["average_price"], "STOP")
            return True
        return False

    def square_off(self):
        """15:15: cancel the stop and sell at the bid, re-pricing until filled."""
        if not self.pos:
            return
        c, ts = self.cfg, self.pos["tradingsymbol"]
        st = self.b.order(self.pos["sl_id"])
        if st["status"] == "COMPLETE":
            self._close(st["average_price"], "STOP")
            return
        self.b.cancel(self.pos["sl_id"])
        key = "NFO:" + ts
        oid = None
        for attempt in range(12):
            q = self.b.quote([key])[key]
            bid = ((q.get("depth") or {}).get("buy") or [{}])[0].get("price") or q["last_price"]
            px = round_tick(bid * (1 - 0.01 * (attempt // 4)), c.tick, "down")   # widen after every 4 tries
            if oid is None:
                oid = self.b.sell_limit(ts, self.pos["qty"], px)
            else:
                self.b.modify(oid, price=px)
            self.sleep(c.poll_seconds)
            st = self.b.order(oid)
            if st["status"] == "COMPLETE":
                self._close(st["average_price"], "TIME EXIT")
                return
        self._ev("WARNING: paper exit not filled after retries; will retry on the next pass.", order_id=oid)

    def _close(self, exit_px, reason):
        p = self.pos
        gross = (exit_px - p["entry"]) * p["qty"]
        fees = option_charges(p["entry"], exit_px, p["qty"])
        pnl = gross - fees
        trade = dict({k: v for k, v in p.items() if k != "sl_id"}, exit=exit_px, reason=reason, gross=round(gross, 2), charges=fees, pnl=round(pnl, 2),
                     r=round(pnl / p["max_loss"], 2) if p.get("max_loss") else None, date=self.today, exit_time=self.now().isoformat(timespec="seconds"))
        self.ledger.record(trade, self.cfg.kill_switch_drawdown)
        self.pos = None
        self.done = True
        self._ev("Position closed", reason=reason, exit=exit_px, pnl=round(pnl, 2), cum=round(self.ledger.d["cum"], 2))
        if self.on_close:
            try:
                self.on_close(trade, self.today)
            except Exception:  # noqa: BLE001 - a Telegram problem must never stop the strategy
                log.exception("gap momentum on_close hook failed")
