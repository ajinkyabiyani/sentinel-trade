"""Gap Momentum's morning data from Kite (replaces the NSE bhavcopy files). Read only: instruments, quotes and daily candles.

What each piece is, and how it relates to the bhavcopy version it replaces:
- Nifty regime: NIFTY 50 index daily closes for the last 30 sessions, same 20-day average and 5-session slope test.
  (The bhavcopy version chained near-month Nifty futures; the index has no rollover jumps and tracks it closely.)
- Universe: the 15 stocks with the most stock-option turnover in the last session. Turnover is computed like NSE's
  bhavcopy figure: quantity traded × (strike + average premium), summed over every strike and expiry. Read from Kite
  quotes before 09:15, when they still show the last session's volume.
- Previous close and ATR(5): daily candles of each stock's near-month futures contract.
- Yesterday's contracts at a strike (the liquidity check): that option's last-session volume ÷ lot size.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from .data import Candidate

log = logging.getLogger("gap_momentum")
NIFTY_INDEX = 256265                      # NSE:NIFTY 50
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
QUOTE_BATCH = 250                         # Kite allows 500, but 500 long option symbols make the URL too long
QUOTE_PACE = 1.05                         # Kite allows about one quote call a second
CANDLE_PACE = 0.4


SPECIAL = {"2026-11-08"}                  # Muhurat and other one-off sessions: not part of the history


def _day(r) -> date:
    return r["date"].date() if hasattr(r["date"], "date") else r["date"]


def _sessions_before(rows: list[dict], today: date) -> list[dict]:
    """Regular sessions before today: no weekend or special (Muhurat) candles, as in the bhavcopy version."""
    return [r for r in rows if _day(r) < today and _day(r).weekday() < 5 and _day(r).isoformat() not in SPECIAL]


def nifty_regime(kite, today: date, sma: int = 20, lag: int = 5, sessions: int = 30) -> tuple[str, str | None]:
    """(UP / DOWN / SIDEWAYS / UNKNOWN, last session date) from NIFTY 50 daily closes before today."""
    rows = _sessions_before(kite.historical_data(NIFTY_INDEX, today - timedelta(days=75), today - timedelta(days=1), "day"), today)[-sessions:]
    closes = [float(r["close"]) for r in rows]
    last = str(rows[-1]["date"])[:10] if rows else None
    if len(closes) < sma + lag:
        return "UNKNOWN", last

    def avg(i):                           # rolling mean ending at i, at least 15 values (as the bhavcopy version)
        w = closes[max(0, i - sma + 1):i + 1]
        return sum(w) / len(w) if len(w) >= 15 else None

    m_now, m_then = avg(len(closes) - 1), avg(len(closes) - 1 - lag)
    if m_now is None or m_then is None:
        return "UNKNOWN", last
    if closes[-1] > m_now and m_now > m_then:
        return "UP", last
    if closes[-1] < m_now and m_now < m_then:
        return "DOWN", last
    return "SIDEWAYS", last


def option_activity(kite, instruments: list[dict], sleep=time.sleep) -> tuple[dict[str, float], dict[str, int]]:
    """({stock: last-session option turnover in ₹}, {option tradingsymbol: last-session contracts})."""
    opts = [i for i in instruments if i["segment"] == "NFO-OPT" and i["name"] not in INDEX_NAMES]
    by_key = {"NFO:" + i["tradingsymbol"]: i for i in opts}
    keys = list(by_key)
    turnover: dict[str, float] = {}
    contracts: dict[str, int] = {}
    for n in range(0, len(keys), QUOTE_BATCH):
        if n:
            sleep(QUOTE_PACE)
        for attempt in range(3):
            try:
                q = kite.quote(keys[n:n + QUOTE_BATCH])
                break
            except Exception as exc:  # noqa: BLE001 - a rate limit or network blip; a login problem is re-raised
                if type(exc).__name__ == "TokenException" or attempt == 2:
                    raise
                sleep(2 * (attempt + 1))
        for k, row in q.items():
            vol = row.get("volume") or 0
            if not vol:
                continue
            i = by_key[k]
            turnover[i["name"]] = turnover.get(i["name"], 0.0) + vol * (float(i["strike"]) + float(row.get("average_price") or row.get("last_price") or 0))
            contracts[i["tradingsymbol"]] = int(vol // max(1, int(i["lot_size"])))
    return turnover, contracts


def near_future(instruments: list[dict], name: str, today: date) -> dict | None:
    futs = [i for i in instruments if i["segment"] == "NFO-FUT" and i["name"] == name and i["expiry"] > today]
    return min(futs, key=lambda i: i["expiry"]) if futs else None


def prepare_universe(kite, instruments: list[dict], turnover: dict[str, float], today: date, top_n: int = 15, atr_days: int = 5, sleep=time.sleep) -> list[Candidate]:
    """Top-N stocks by option turnover, with the near-month futures' last close and ATR from its daily candles."""
    out = []
    for name in sorted(turnover, key=lambda s: -turnover[s])[:top_n]:
        fut = near_future(instruments, name, today)
        if not fut:
            continue
        sleep(CANDLE_PACE)
        rows = _sessions_before(kite.historical_data(fut["instrument_token"], today - timedelta(days=21), today - timedelta(days=1), "day"), today)
        if len(rows) < atr_days + 1:
            continue
        trs = [max(r["high"] - r["low"], abs(r["high"] - p["close"]), abs(r["low"] - p["close"])) for p, r in zip(rows, rows[1:])]
        atr = sum(trs[-atr_days:]) / atr_days
        out.append(Candidate(name, str(fut["expiry"]), float(rows[-1]["close"]), float(atr), int(fut["lot_size"])))
    return out


def as_date(today: str) -> date:
    return datetime.strptime(today, "%Y%m%d").date()
