"""Gap Momentum desk: runs the strategy's day on a schedule inside the app, on paper, and sends Telegram calls.

Telegram: one entry call when the paper trade is booked (contract, entry, stop, exit at 15:15, a 1R reference level,
max loss), and one exit message when it closes (stop or 15:15) with the booked P&L. Nothing is sent for a setup that
isn't booked. The strategy has no profit target: the backtest held to 15:15, so the call says so.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime

from strategies import common, notify
from .broker import PaperBroker
from .config import Config
from .engine import GapMomentumEngine

log = logging.getLogger("gap_momentum")
DESK_ID, DESK_NAME = "gap_momentum", "Gap Momentum"
PREPARE_AT = dtime(9, 0)
CFG = Config()

_lock = threading.Lock()
_engine: GapMomentumEngine | None = None
_sleep = time.sleep
_kite_factory = None            # set by the route: returns a logged-in KiteConnect for quotes (read only)


def now() -> datetime:
    return datetime.now(common.IST).replace(tzinfo=None)


def _rs(v) -> str:
    return "–" if v is None else f"₹{float(v):,.2f}"


def _title(p: dict) -> str:
    exp = datetime.strptime(p["expiry"], "%Y-%m-%d").strftime("%d %b")
    return f"{p['symbol']} {p['strike']:g} {p['side']} · Exp {exp}"


def entry_text(p: dict, day: str) -> str:
    row, rule = notify._card_row, "-" * notify.CARD_WIDTH
    when = datetime.strptime(day, "%Y%m%d").strftime("%d %b %Y")
    at = (p.get("entry_time") or "")[11:16]
    return "\n".join([
        f"{notify.CALL_PREFIX} · {when} · {at}", "",
        _title(p), f"{'BUY CALL' if p['side'] == 'CE' else 'BUY PUT'} · {p['lots']} lot{'' if p['lots'] == 1 else 's'} ({p['qty']} qty)", rule,
        row("Entry", _rs(p["entry"])), row("Stop (SL)", _rs(p["trigger"])), row("Target", "Hold to 15:15"), row("1R level", _rs(p["ref_1r"])), row("Exit", "15:15 or stop"), rule,
        row("Max loss", _rs(p["max_loss"])), row("Gap", f"{p['gap_atr']:+.2f} ATR"), row("Nifty", str(p.get("regime") or "–").title()), rule,
        f"{DESK_NAME} · paper",
    ])[:notify.MAX_LEN]


def exit_text(t: dict, day: str) -> str:
    row, rule = notify._card_row, "-" * notify.CARD_WIDTH
    when = datetime.strptime(day, "%Y%m%d").strftime("%d %b %Y")
    at = (t.get("exit_time") or "")[11:16]
    why = "Stop hit" if t["reason"] == "STOP" else "15:15 exit"
    return "\n".join([
        f"EXIT · {when} · {at}", "",
        _title(t), f"SOLD · {why}", rule,
        row("Entry", _rs(t["entry"])), row("Exit", _rs(t["exit"])), row("Qty", str(t["qty"])), rule,
        row("Charges", _rs(t["charges"])), row("Booked P&L", ("+" if t["pnl"] >= 0 else "−") + _rs(abs(t["pnl"]))), row("R", f"{t['r']:+.2f}R" if t.get("r") is not None else "–"), rule,
        f"{DESK_NAME} · paper",
    ])[:notify.MAX_LEN]


def _on_open(p: dict, day: str) -> None:
    notify.send_once(f"{DESK_ID}|{day}|{p['tradingsymbol']}|open", entry_text(p, day))


def _on_close(t: dict, day: str) -> None:
    notify.send_once(f"{DESK_ID}|{day}|{t['tradingsymbol']}|exit", exit_text(t, day))


def _engine_for(today: str) -> GapMomentumEngine | None:
    global _engine
    if _engine is None or _engine.today != today:
        if _kite_factory is None:
            return None
        _engine = GapMomentumEngine(PaperBroker(_kite_factory()), CFG, now=lambda: now(), sleep=lambda s: _sleep(s), on_open=_on_open, on_close=_on_close)
    return _engine


def tick(t: datetime | None = None) -> None:
    """One scheduler pass. Weekdays only; a holiday is caught by the engine (no futures traded today)."""
    t = t or now()
    if t.weekday() >= 5 or t.time() < PREPARE_AT:
        return
    with _lock:
        e = _engine_for(t.strftime("%Y%m%d"))
        if e is None:
            return
        if not e.prepared and not e.pos:
            e.prepare()
        if e.pos is None and not e.done and t.time() >= CFG.entry_time:
            e.try_entry()
        if e.pos is not None:
            if t.time() >= CFG.exit_time:
                e.square_off()
            else:
                e.poll()


# ---------------------------------------------------------------- page
def _journal(day: str) -> dict:
    try:
        with open(os.path.join(CFG.state_dir, f"journal_{day}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sessions() -> list[str]:
    try:
        names = os.listdir(CFG.state_dir)
    except OSError:
        return []
    return sorted((n[8:16] for n in names if n.startswith("journal_") and n.endswith(".json")), reverse=True)


def rules_text() -> list[str]:
    c = CFG
    return [
        f"09:00, from Kite: Nifty 50's last close vs its {c.regime_sma}-day average (rising or falling over {c.regime_slope_lag} sessions). Sideways → no trade today.",
        f"Universe: the {c.universe_size} stocks with the most stock-option turnover in the last session (Kite quotes before the open: quantity × (strike + premium), as NSE counts it).",
        f"Previous close and ATR({c.atr_days}) from each stock's near-month futures daily candles on Kite. The morning read must start by {c.prepare_by:%H:%M}.",
        f"09:15:20: gap = (futures open − yesterday's close) ÷ ATR({c.atr_days}). Above +{c.gap_threshold_atr} → buy the ATM call; below −{c.gap_threshold_atr} → buy the ATM put. Biggest gap first.",
        f"The strike must have traded at least {c.min_prev_contracts} contracts yesterday; roll to next month if expiry is {c.roll_if_dte_le} days away or less.",
        f"Size: lots so a stop-out loses at most ₹{c.risk_per_trade:,.0f} including charges; at most ₹{c.capital:,.0f} of premium.",
        f"Entry: limit at the ask, walked up to +{c.entry_max_slippage:.0%} for {c.entry_fill_timeout_s} s, then given up. No entry after {c.last_entry_time:%H:%M}.",
        f"Stop: {c.stop_pct:.0%} below the entry premium. No profit target: exit at {c.exit_time:%H:%M}.",
        f"One trade a day. Kill switch: no new trades once realized P&L falls ₹{c.kill_switch_drawdown:,.0f} below its peak.",
        "Paper only: real Kite quotes, simulated fills. Telegram gets the booked entry and the exit, nothing else.",
    ]


def view(day: str | None = None) -> dict:
    today = now().strftime("%Y%m%d")
    day = (day or today).replace("-", "")
    j = _journal(day)
    e = _engine if _engine is not None and _engine.today == day else None      # read without the run lock: an entry can take 45 s
    live = e.pos if e else None
    pos = dict(live) if live else j.get("pos")
    ledger = e.ledger.d if e else None
    if ledger is None:
        try:
            with open(os.path.join(CFG.state_dir, "ledger.json"), encoding="utf-8") as f:
                ledger = json.load(f)
        except (OSError, ValueError):
            ledger = {"cum": 0.0, "peak": 0.0, "halted": False, "trades": []}
    if pos:
        pos.pop("sl_id", None)
    trades = ledger.get("trades") or []
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "day": f"{day[:4]}-{day[4:6]}-{day[6:]}", "today": f"{today[:4]}-{today[4:6]}-{today[6:]}",
        "sessions": [f"{s[:4]}-{s[4:6]}-{s[6:]}" for s in sessions()],
        "regime": j.get("regime"), "universe": j.get("universe") or [], "signals": j.get("signals") or [],
        "events": j.get("events") or [], "position": pos, "done": bool(j.get("done")),
        "day_trades": [t for t in trades if t.get("date") == day],
        "trades": list(reversed(trades))[:50],
        "stats": {"trades": len(trades), "net": round(sum(t["pnl"] for t in trades), 2), "win_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
                  "peak": ledger.get("peak", 0.0), "halted": bool(ledger.get("halted"))},
        "last_session": next((ev.get("last_session") for ev in j.get("events") or [] if ev.get("last_session")), None), "telegram": notify.enabled() and notify.configured(),
        "rules": rules_text(), "capital": CFG.capital, "risk": CFG.risk_per_trade,
    }
