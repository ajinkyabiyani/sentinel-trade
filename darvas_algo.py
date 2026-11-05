#!/usr/bin/env python3
"""
darvas_algo.py — Darvas Box swing-trading algo for NSE/BSE, built for Zerodha Kite Connect.

One file: strategy, Indian-market filters, trade plan, walk-forward backtest,
Kite data loader and Kite order builders. Long-only by default.

QUICK START
-----------
    pip install numpy pandas requests kiteconnect

    python darvas_algo.py selftest                 # unit tests + offline demo (no Kite needed)
    python darvas_algo.py demo                     # scan + backtest on synthetic data

    Credentials.txt (next to this file):   api_key = xxxx
                                           api_secret_key = yyyy
    python darvas_algo.py login                    # prints the login URL
    python darvas_algo.py login --request-token R  # saves today's token to access_token.json
                                                   # (any command also accepts --request-token R)

    python darvas_algo.py rank                     # every NSE/BSE index + all their stocks:
                                                   #   NSE+BSE token map, daily history, most recent
                                                   #   Darvas breakouts (BUY/SELL, date + intraday time),
                                                   #   Darvas-ready stocks ranked by bullishness,
                                                   #   CSVs in reports/
    python darvas_algo.py rank --universe "NIFTY BANK" --window 10 --ready-pct 5
    python darvas_algo.py scan     --universe nifty500        # after 15:45 IST
    python darvas_algo.py backtest --universe nifty500        # walk-forward, honest verdict
    python darvas_algo.py orders                               # dry-run: print Kite payloads
    python darvas_algo.py orders --live                        # place AMO entries + GTT buy-stops
                                                               # (respects max_open_positions and cash)
    python darvas_algo.py protect --live                       # OCO GTT stop/target for filled entries
    python darvas_algo.py trail                                # recommended stop raises for holdings

DAILY WORKFLOW (end-of-day swing trading)
-----------------------------------------
1. After the close, ``scan`` finds breakouts that closed above a Darvas box on
   volume, plus "setups" pressing against a box top.
2. ``orders`` builds a CNC LIMIT AMO buy for each BUY signal (limit = signal
   close + max chase %, so a big gap-up is not chased) and a single-trigger GTT
   buy-stop above each setup's box top.
3. Once an entry fills, ``protect`` places two OCO GTTs per position (tranche 1:
   stop / T1, tranche 2: stop / T2).
4. Each evening ``trail`` raises stops: breakeven after T1, then the bottom of
   each newly confirmed box (Darvas's own trailing rule).

WHAT THE 70% / LARGE-R:R TARGET MEANS HERE
-------------------------------------------
Breakout systems usually trade a modest win rate for large winners; a high win
rate *and* large winners at the same time is rare. Nothing in this file assumes
the target is met. ``backtest`` runs a walk-forward test and prints a verdict
(SUPPORTED / INCONCLUSIVE / NOT SUPPORTED) with a 95% confidence interval on
the out-of-sample win rate. Believe that, not the target.

VERIFIED (Oct 2026) vs ASSUMED
------------------------------
Verified from public sources:
  * Price bands 2/5/10/20% for non-derivative stocks; F&O stocks have no fixed
    band but a dynamic 10% band flexed in 5% steps.
  * T+1 is the default settlement; T+0 is an optional parallel window.
  * NSE tick sizes: <250: 0.01, 250-1000: 0.05, 1000-5000: 0.10,
    5000-10000: 0.50, 10000-20000: 1.00, >20000: 5.00 (from 15 Apr 2025).
  * Delivery STT 0.1% on both sides, unchanged by Budget 2026; futures STT
    0.05% (sell) from 1 Apr 2026.
  * Retail cannot carry a cash-market short overnight; overnight shorts go
    through stock futures (or SLB). Short signals here are futures-only.
  * Since 3 Aug 2026, F&O stocks' official close comes from a closing auction
    (other stocks: VWAP of the last 30 min). Either way the close is not a
    price you can reliably trade at, so entries are modelled at the next open.
  * kite.place_gtt(trigger_type, tradingsymbol, exchange, trigger_values,
    last_price, orders) with LIMIT legs, as used on the Kite developer forum.
ASSUMED / NOT VERIFIED (check before going live):
  * NSE exchange transaction charge 0.00297%, DP charge ₹15.34 per sell day,
    futures exchange charge and stamp duty. Brokerage default ₹0 for delivery.
  * SEBI LODR results timelines (45 days quarterly, 60 days annual) used to
    mark "results season".
  * Kite historical-data limits per request and rate limits: requests are
    chunked (default 1,000 days) and paced (0.35 s); adjust if Kite rejects.
  * Whether Kite daily candles are adjusted for splits/bonuses. A >35% one-day
    move is treated as a possible unadjusted corporate action and the stock
    is skipped for a year.
  * BSE tick sizes above ₹100. The circuit band of a stock is inferred from
    its price history, not read from exchange files.
  * OCO GTT trigger_values are sent in ascending order ([stop, target] for a
    long). Kite's exact validation rules for GTT legs can change; test one
    small position with --live before trusting the automation.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import itertools
import json
import logging
import math
import os
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

log = logging.getLogger("darvas")
IST = timezone(timedelta(hours=5, minutes=30))  # no DST in India, so a fixed offset is exact
OHLCV = ["open", "high", "low", "close", "volume"]
ProgressFn = Callable[[int, int, str], None]

# =============================================================================
# 1. CONFIGURATION — every tunable, with the reasoning behind its default
# =============================================================================
# Defaults are starting points from how Indian cash-market stocks typically
# behave; they are heuristics, not fitted optima. Calibrate with `backtest`.
# Override any value with a JSON file:  --config my_settings.json
#   {"risk": {"capital": 500000}, "segments": {"SMALL": {"volume_surge": 2.2}}}

SEGMENTS = ("LARGE", "MID", "SMALL", "MICRO")
# LARGE = Nifty 50 + Next 50 | MID = Midcap 150 | SMALL = Smallcap 250 | MICRO = outside Nifty 500


@dataclass
class SegmentRules:
    min_avg_traded_value_cr: float   # 20-day median of close x volume, ₹ crore
    min_price: float                 # ₹
    min_box_height_pct: float
    max_box_height_pct: float
    volume_surge: float              # breakout volume / 50-day average volume
    confirm_bars: int                # bars that must hold for a box top/bottom
    max_circuit_hits_20d: int
    max_position_pct_of_adtv: float  # cap position value as % of daily turnover
    enabled: bool = True


def _default_segments() -> dict[str, SegmentRules]:
    return {
        # Nifty 100 names usually trade hundreds of crores a day; ₹25 Cr only
        # removes anomalies. Daily ATR ~1-2%, so a box under 3% is ~2 ATR of
        # noise; above ~12% the box-bottom stop is too far for a 2R+ swing.
        # Institutional flow makes volume steadier: 1.5x is already a surge.
        # Quant-optimized Darvas (Nov 2026): no minimum height, box <= 12% (large) / 15% (others), volume >= 1.5x.
        "LARGE": SegmentRules(25.0, 50.0, 0.0, 12.0, 1.5, 3, 0, 1.0),
        # Midcaps: wider ranges (ATR ~2-3%) and spikier volume.
        "MID": SegmentRules(10.0, 50.0, 0.0, 15.0, 1.5, 3, 1, 1.0),
        # Smallcaps: retail-heavy flow -> many intraday pokes above a box that
        # fail by the close, and volume bursts on tips. Defences: 2x surge,
        # one extra confirmation bar, reject circuit-prone names (operator or
        # surveillance-framework stocks), size capped at 0.5% of turnover.
        "SMALL": SegmentRules(5.0, 30.0, 0.0, 15.0, 1.5, 4, 2, 0.5),
        # Outside the Nifty 500: disabled. Thin books, 5%/10% bands, and
        # trade-for-trade / ASM / GSM names this file cannot detect.
        "MICRO": SegmentRules(2.0, 20.0, 6.0, 25.0, 2.5, 4, 1, 0.5, enabled=False),
    }


@dataclass
class StrategyConfig:
    timeframe: str = "1d"              # "1d" or "1wk" (weekly bars resampled from daily)
    lookback_high: int = 250           # ~1 NSE trading year
    near_high_pct: float = 5.0         # box top within 5% of the 52-week high (true momentum leaders)
    breakout_on_close: bool = True     # close above the top, not an intraday poke (retail false breaks)
    vol_avg_len: int = 20              # breakout volume vs the 20-day average
    atr_len: int = 14
    max_box_age_bars: int = 60         # unresolved for ~3 months -> stale
    min_box_height_atr: float = 0.0    # (old rule: 1.5 ATR minimum; not in the quant spec)
    min_box_bars: int = 10             # consolidation of at least 10 trading days (box top to breakout)
    close_location_min: float = 0.0    # (old rule: close in the top half of the range; not in the quant spec)
    # Longs only while Nifty closes above its 50-DMA. Re-added 3 Nov 2026: on 5 years of the top-500 universe it took
    # the quant rules from +13% (max drawdown 30%) to +47% (drawdown 14.6%); picked on 2021-24, it also held on
    # Nov 2024-Nov 2026 (+4.4% against -32% without it).
    regime_filter: bool = True
    entry_mode: str = "stop_limit"     # stop-limit above the breakout day's high | "next_open" (old rule)
    entry_above_high_pct: float = 0.05  # trigger this far above the breakout day's high
    entry_limit_pct: float = 0.5       # limit this far above the trigger: a bigger gap is not chased
    entry_valid_bars: int = 3          # the order stays for 3 sessions unless a close falls back into the box
    regime_ma: int = 50
    # Longs only when the stock beat Nifty 50 by >= this many % points over ~3 months (None = off). Oct 2026
    # loser analysis (core universe, 2017-2026): low relative strength was the one breakout trait that underperformed
    # both before and after 2023; >= 10 improved expectancy in both halves.
    min_rs_pct: float | None = None    # (old rule: >= +10%; not in the quant spec)
    allow_shorts: bool = False         # futures-only shorts; off by default
    signal_window_bars: int = 3        # scan also lists breakouts 1-2 bars old if still in range
    setup_proximity_pct: float = 3.0   # setup = close within 3% under an active box top
    avoid_event_days: int = 2          # skip entries 2 sessions before a calendar event
    exclude_corp_action_suspects: bool = True


@dataclass
class RiskConfig:
    capital: float = 1_000_000.0       # ₹10 lakh; `--capital-from-kite` uses live cash instead
    risk_per_trade_pct: float = 1.0    # % of equity lost at the initial stop
    max_open_positions: int = 8
    max_position_pct: float = 20.0     # no MTF/leverage
    stop_mode: str = "box"             # box | atr | tighter. Quant spec: the box's lower boundary
    max_stop_pct: float | None = 7.0   # hard cap: the stop is never more than 7% below the entry
    # 2.5 (was 2.0): 2 ATR stops were hit by 40% of trades, a third of which later reached T1; 2.5 ATR cut stop-outs to
    # ~30% and raised expectancy before and after 2023 (Oct 2026 analysis).
    atr_stop_mult: float = 2.5
    stop_buffer_pct: float = 0.0       # strictly at the box bottom (old rule: 0.5% below)
    min_stop_atr: float = 0.0          # (old rule: never closer than 1 ATR)
    # Quant spec: no fixed target; the stop follows each newly formed higher box (empty list = trail only)
    targets_r: list = field(default_factory=list)
    target_fractions: list = field(default_factory=list)
    move_stop_to_breakeven: bool = False
    trail_with_boxes: bool = True
    final_leg: str = "trail"           # "target" exits at the last target; "trail" rides the box stop
    max_hold_bars: int = 500           # no time exit in practice: winners run for weeks or months
    failed_breakout_bars: int = 0      # >0: exit if close falls back inside the box within N bars
    max_chase_pct: float = 2.0         # skip if next open is >2% above the signal close
    slippage_pct: float = 0.10         # per side on market-like fills


@dataclass
class CostConfig:
    brokerage_pct: float = 0.0         # ₹0 delivery at many discount brokers (Zerodha included, verify)
    brokerage_cap: float = 20.0
    stt_pct: float = 0.1               # VERIFIED: delivery STT both sides
    exchange_txn_pct: float = 0.00297  # ASSUMED
    sebi_fee_per_cr: float = 10.0
    stamp_duty_buy_pct: float = 0.015
    gst_pct: float = 18.0
    dp_charge_per_sell: float = 15.34  # ASSUMED, broker-specific
    futures_stt_sell_pct: float = 0.05  # VERIFIED (from 1 Apr 2026)
    futures_exchange_pct: float = 0.00173  # ASSUMED


@dataclass
class BacktestConfig:
    start: str | None = None
    end: str | None = None
    train_years: float = 3.0
    test_months: int = 6
    min_trades_train: int = 30
    target_win_rate: float = 0.70
    target_avg_win_r: float = 2.0
    nominal_position_value: float = 100_000.0
    param_grid: dict = field(default_factory=dict)   # quant spec: fixed rules, nothing is fitted


@dataclass
class KiteConfig:
    exchange: str = "NSE"
    index_symbol: str = "NIFTY 50"     # tradingsymbol of the regime index in kite.instruments("NSE")
    history_years: float = 8.0
    chunk_days: int = 1000             # per historical_data call (conservative)
    pause_s: float = 0.35              # between historical calls (conservative pacing)
    cache_dir: str = ".darvas_cache"
    cache_ttl_hours: float = 6.0
    session_close_ist: str = "15:45"   # Kite serves a live partial day candle before this
    entry_variety: str = "amo"         # after-market order for next morning's open
    sl_limit_buffer_pct: float = 1.0   # GTT stop leg: LIMIT this far below the trigger so it fills on a fast fall
    setup_limit_buffer_pct: float = 0.5  # GTT buy-stop leg: LIMIT this far above the trigger


@dataclass
class AppConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    kite: KiteConfig = field(default_factory=KiteConfig)
    segments: dict = field(default_factory=_default_segments)
    include_segments: list = field(default_factory=lambda: ["LARGE", "MID", "SMALL"])
    exclude_symbols: list = field(default_factory=list)  # e.g. ASM/GSM names you track yourself
    events_file: str | None = None     # CSV: date,symbol,label  (symbol "*" = market-wide, e.g. Budget)


def _merge(obj, overrides: dict, path: str = "") -> None:
    names = {f.name for f in fields(obj)}
    for key, value in overrides.items():
        if key not in names:
            raise ValueError(f"unknown config key: {path}{key}")
        cur = getattr(obj, key)
        if is_dataclass(cur) and isinstance(value, dict):
            _merge(cur, value, f"{path}{key}.")
        elif key == "segments" and isinstance(value, dict):
            for seg, ov in value.items():
                if seg.upper() not in cur:
                    raise ValueError(f"unknown segment {seg}")
                _merge(cur[seg.upper()], ov or {}, f"segments.{seg}.")
        else:
            setattr(obj, key, value)


def load_config(path: str | None = None, overrides: dict | None = None) -> AppConfig:
    cfg = AppConfig()
    if path:
        _merge(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
    if overrides:
        _merge(cfg, overrides)
    r = cfg.risk
    if not r.targets_r and r.final_leg != "trail":
        raise ValueError("risk.targets_r may only be empty with final_leg 'trail'")
    if len(r.targets_r) != len(r.target_fractions) or (r.targets_r and abs(sum(r.target_fractions) - 1) > 1e-6):
        raise ValueError("risk.targets_r and target_fractions must match in length; fractions must sum to 1")
    if list(r.targets_r) != sorted(r.targets_r) or (r.targets_r and r.targets_r[0] <= 0):
        raise ValueError("risk.targets_r must be positive and ascending")
    if r.stop_mode not in ("box", "atr", "tighter") or r.final_leg not in ("target", "trail"):
        raise ValueError("risk.stop_mode must be box|atr|tighter and final_leg target|trail")
    if cfg.strategy.timeframe not in ("1d", "1wk"):
        raise ValueError("strategy.timeframe must be 1d or 1wk")
    return cfg


def apply_params(cfg: AppConfig, params: dict) -> AppConfig:
    """Copy of cfg with one walk-forward parameter combination applied."""
    out = copy.deepcopy(cfg)
    for seg in out.segments.values():
        seg.confirm_bars = max(2, seg.confirm_bars + int(params.get("confirm_offset", 0)))
        seg.volume_surge = round(seg.volume_surge * float(params.get("surge_mult", 1.0)), 3)
    if "t1_r" in params:
        t1 = float(params["t1_r"])
        rest = [t for t in out.risk.targets_r[1:] if t > t1] or [round(t1 * 2, 2)]
        out.risk.targets_r = [t1] + rest
        if len(out.risk.target_fractions) != len(out.risk.targets_r):
            out.risk.target_fractions = [1.0 / len(out.risk.targets_r)] * len(out.risk.targets_r)
    if "failed_breakout_bars" in params:
        out.risk.failed_breakout_bars = int(params["failed_breakout_bars"])
    return out


# =============================================================================
# 2. INDIAN MARKET RULES — ticks, bands, circuits, costs, calendar
# =============================================================================
_NSE_SLABS = ((250.0, 0.01, True), (1000.0, 0.05, False), (5000.0, 0.10, False),
              (10000.0, 0.50, False), (20000.0, 1.00, False))


def tick_size(price: float, exchange: str = "NSE") -> float:
    """NSE slabs from 15 Apr 2025 (NSE reviews monthly on the month-end close; we use current price)."""
    if exchange.upper() in ("BSE", "BO"):
        return 0.01 if price < 100 else 0.05  # ASSUMED above ₹100
    for limit, tick, strict in _NSE_SLABS:
        if (price < limit) if strict else (price <= limit):
            return tick
    return 5.0


def round_to_tick(price: float, exchange: str = "NSE", direction: str = "nearest") -> float:
    if price is None or not math.isfinite(price) or price <= 0:
        return price
    t = tick_size(price, exchange)
    q = price / t
    n = math.ceil(q - 1e-9) if direction == "up" else math.floor(q + 1e-9) if direction == "down" else round(q)
    return round(n * t, 2)


PRICE_BANDS = (2.0, 5.0, 10.0, 20.0)


def infer_price_band(df: pd.DataFrame, lookback: int = 250, tol: float = 0.15) -> float | None:
    """Best guess of a stock's fixed band, or None (never pressed a band, or moved >20%: F&O / bad data)."""
    if len(df) < 20:
        return None
    chg = (df["close"].pct_change() * 100).iloc[-lookback:].abs().dropna()
    if chg.empty or chg.max() > 20.5:
        return None
    for band in PRICE_BANDS:
        if chg.max() <= band + 0.1 and bool(((chg - band).abs() <= tol).any()):
            return band
    return None


def circuit_flags(df: pd.DataFrame, band: float | None = None, tol: float = 0.15) -> pd.DataFrame:
    """Upper/lower circuit closes and locked (single-price) sessions.

    Without the official band we test 5/10/20% (2% moves are too common to flag).
    """
    chg = df["close"].pct_change() * 100
    near_up = pd.Series(False, index=df.index)
    near_dn = pd.Series(False, index=df.index)
    for b in ((band,) if band else (5.0, 10.0, 20.0)):
        near_up |= (chg >= b - tol) & (chg <= b + tol)
        near_dn |= (chg <= -b + tol) & (chg >= -b - tol)
    uc = near_up & (df["close"] >= df["high"] * (1 - 5e-4))
    lc = near_dn & (df["close"] <= df["low"] * (1 + 5e-4))
    single = (df["high"] - df["low"]) <= df["close"] * 5e-4
    big = chg.abs() >= 1.9
    return pd.DataFrame({"uc": uc, "lc": lc,
                         "uc_locked": (uc & single) | (single & big & (chg > 0)),   # no sellers all day
                         "lc_locked": (lc & single) | (single & big & (chg < 0))},  # no buyers all day
                        index=df.index)


def order_costs(value: float, side: str, costs: CostConfig, product: str = "delivery") -> float:
    """₹ charges for one order. side buy|sell; product delivery|futures."""
    if value <= 0:
        return 0.0
    brokerage = min(costs.brokerage_cap, value * costs.brokerage_pct / 100) if costs.brokerage_pct > 0 else 0.0
    if product == "futures":
        stt = value * costs.futures_stt_sell_pct / 100 if side == "sell" else 0.0
        exch = value * costs.futures_exchange_pct / 100
        stamp = value * 0.002 / 100 if side == "buy" else 0.0  # ASSUMED
        dp = 0.0
    else:
        stt = value * costs.stt_pct / 100
        exch = value * costs.exchange_txn_pct / 100
        stamp = value * costs.stamp_duty_buy_pct / 100 if side == "buy" else 0.0
        dp = costs.dp_charge_per_sell if side == "sell" else 0.0
    sebi = value * costs.sebi_fee_per_cr / 1e7
    gst = (brokerage + exch + sebi) * costs.gst_pct / 100
    return brokerage + stt + exch + stamp + sebi + gst + dp


def round_trip_cost_pct(costs: CostConfig, nominal: float = 100_000.0, product: str = "delivery") -> float:
    return (order_costs(nominal, "buy", costs, product) + order_costs(nominal, "sell", costs, product)) / nominal * 100


def in_results_season(d: date) -> bool:
    """Quarterly results land within ~45 days of quarter end, annual within ~60 (LODR, ASSUMED current)."""
    m, day = d.month, d.day
    return m in (7, 10, 1) or (m in (8, 11, 2) and day <= 14) or m == 4 or (m == 5 and day <= 30)


@dataclass
class Event:
    day: date
    symbol: str
    label: str


class EventCalendar:
    """Known event days. CSV: date,symbol,label (symbol '*' = all). Lines starting '#' ignored."""

    def __init__(self, events: list[Event] | None = None):
        self.events = sorted(events or [], key=lambda e: e.day)

    @classmethod
    def load(cls, path: str | None) -> "EventCalendar":
        ev = []
        if path and Path(path).exists():
            with open(path, encoding="utf-8") as fh:
                for row in csv.DictReader(l for l in fh if not l.startswith("#")):
                    try:
                        ev.append(Event(datetime.strptime(row["date"].strip(), "%Y-%m-%d").date(),
                                        (row.get("symbol") or "*").strip().upper(), (row.get("label") or "").strip()))
                    except (KeyError, ValueError):
                        continue
        return cls(ev)

    def upcoming(self, symbol: str, d: date, within_days: int) -> list[Event]:
        end = d + timedelta(days=within_days)
        return [e for e in self.events if d < e.day <= end and e.symbol in ("*", symbol.upper())]

    def mask_before_events(self, symbol: str, dates: pd.DatetimeIndex, sessions: int) -> np.ndarray:
        mask = np.zeros(len(dates), dtype=bool)
        days = np.array([np.datetime64(e.day) for e in self.events if e.symbol in ("*", symbol.upper())])
        if sessions <= 0 or days.size == 0 or len(dates) == 0:
            return mask
        for j in np.searchsorted(dates.values.astype("datetime64[D]"), days):
            mask[max(0, j - sessions):j] = True
        return mask


def now_ist() -> datetime:
    return datetime.now(IST)


def session_closed(now: datetime | None = None, close_hhmm: str = "15:45") -> bool:
    now = now or now_ist()
    hh, mm = (int(x) for x in close_hhmm.split(":"))
    return now.weekday() >= 5 or (now.hour, now.minute) >= (hh, mm)


# =============================================================================
# 3. INDICATORS
# =============================================================================
def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()  # Wilder


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def traded_value_cr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Median (not mean: one block deal should not make a stock look liquid) turnover in ₹ crore."""
    return (df["close"] * df["volume"]).rolling(n, min_periods=min(n, max(5, n // 2))).median() / 1e7


def close_location(df: pd.DataFrame) -> pd.Series:
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


# =============================================================================
# 4. DARVAS BOX DETECTION (causal)
# =============================================================================
# 1. Top: highest high, confirmed when N later bars fail to exceed it.
# 2. Bottom: lowest low after the top bar, confirmed when N bars fail to undercut
#    it. A new high above the top first voids the half-built box (back to 1).
# 3. The completed box resolves as breakout (close > top), breakdown (close <
#    bottom) or expiry (max_box_age bars). Each resolution restarts the search.
# 4. trail_bottom[i] = bottom of the latest completed box as of bar i: the
#    Darvas trailing stop that rides up one box at a time.
# Index i only ever uses bars <= i, so the output is backtest-safe.
SEEK_TOP, SEEK_BOTTOM, ACTIVE = 0, 1, 2


@dataclass
class Box:
    top: float
    bottom: float
    top_idx: int
    top_confirm_idx: int
    bottom_idx: int
    confirm_idx: int            # bar at whose close the box became known
    end_idx: int | None = None
    status: str = "active"      # active | breakout | breakdown | expired

    @property
    def height_pct(self) -> float:
        return (self.top - self.bottom) / self.bottom * 100.0


@dataclass
class DarvasResult:
    boxes: list
    event: np.ndarray           # +1 breakout, -1 breakdown, 0 none
    event_box: np.ndarray       # index into boxes, -1 if no event
    active_top: np.ndarray      # complete, unresolved box after bar i (NaN if none)
    active_bottom: np.ndarray
    trail_bottom: np.ndarray
    trail_top: np.ndarray
    trail_confirm: np.ndarray   # confirm_idx of the box behind trail_* (-1 if none)
    forming: dict


def detect_boxes(high, low, close, confirm_bars: int = 3, breakout_on_close: bool = True,
                 max_box_age: int = 60) -> DarvasResult:
    if confirm_bars < 1:
        raise ValueError("confirm_bars must be >= 1")
    H, L, C = ([float(x) for x in a] for a in (high, low, close))
    n = len(H)
    if not (len(L) == len(C) == n):
        raise ValueError("high/low/close lengths differ")
    event = np.zeros(n, dtype=np.int8)
    event_box = np.full(n, -1, dtype=np.int64)
    active_top, active_bottom = np.full(n, np.nan), np.full(n, np.nan)
    trail_bottom, trail_top = np.full(n, np.nan), np.full(n, np.nan)
    trail_confirm = np.full(n, -1, dtype=np.int64)

    boxes: list[Box] = []
    state, top, top_idx, cnt = SEEK_TOP, -np.inf, -1, 0
    bottom, bottom_idx, bcnt, top_conf = np.inf, -1, 0, -1
    cur, lt_bot, lt_top, lt_conf = -1, np.nan, np.nan, -1

    for i in range(n):
        h, l, c = H[i], L[i], C[i]
        complete = False
        if state == ACTIVE:
            box = boxes[cur]
            up = (c > box.top) if breakout_on_close else (h > box.top)
            dn = (c < box.bottom) if breakout_on_close else (l < box.bottom)
            if up and dn:  # outside bar in intraday mode: the close decides
                up = c >= (box.top + box.bottom) / 2
                dn = not up
            if up or dn:
                box.status, box.end_idx = ("breakout" if up else "breakdown"), i
                event[i], event_box[i] = (1 if up else -1), cur
                state, top, top_idx, cnt, cur = SEEK_TOP, h, i, 0, -1
            elif i - box.confirm_idx > max_box_age:
                box.status, box.end_idx = "expired", i
                state, top, top_idx, cnt, cur = SEEK_TOP, h, i, 0, -1
        elif state == SEEK_TOP:
            if h > top:
                top, top_idx, cnt = h, i, 0
            else:
                cnt += 1
                if cnt >= confirm_bars:
                    state, top_conf = SEEK_BOTTOM, i
                    seg = L[top_idx + 1: i + 1]
                    rel = int(np.argmin(seg))
                    bottom, bottom_idx = seg[rel], top_idx + 1 + rel
                    bcnt = i - bottom_idx
                    complete = bcnt >= confirm_bars
        else:  # SEEK_BOTTOM
            if h > top:
                state, top, top_idx, cnt = SEEK_TOP, h, i, 0
            elif l < bottom:
                bottom, bottom_idx, bcnt = l, i, 0
            else:
                bcnt += 1
                complete = bcnt >= confirm_bars
        if complete:
            boxes.append(Box(top, bottom, top_idx, top_conf, bottom_idx, i))
            cur, state = len(boxes) - 1, ACTIVE
            lt_bot, lt_top, lt_conf = bottom, top, i
        if state == ACTIVE:
            active_top[i], active_bottom[i] = boxes[cur].top, boxes[cur].bottom
        trail_bottom[i], trail_top[i], trail_confirm[i] = lt_bot, lt_top, lt_conf

    if state == ACTIVE:
        forming = {"phase": "active", "box": cur}
    elif state == SEEK_BOTTOM:
        forming = {"phase": "seeking_bottom", "top": top, "top_idx": top_idx, "bottom": bottom, "held": bcnt}
    else:
        forming = {"phase": "seeking_top", "top": top, "top_idx": top_idx, "held": cnt}
    return DarvasResult(boxes, event, event_box, active_top, active_bottom, trail_bottom, trail_top,
                        trail_confirm, forming)


# =============================================================================
# 5. SIGNALS — filters and confidence score (identical in scan and backtest)
# =============================================================================
def lengths(cfg: AppConfig) -> dict:
    """Indicator lengths in bars; weekly bars use roughly a fifth of the daily lengths."""
    k = 1.0 if cfg.strategy.timeframe == "1d" else 0.2
    sc = lambda x, lo: max(lo, int(round(x * k)))  # noqa: E731
    s = cfg.strategy
    return {"lookback_high": sc(s.lookback_high, 20), "vol_avg": sc(s.vol_avg_len, 8), "atr": s.atr_len,
            "regime_ma": sc(s.regime_ma, 8), "sma_mid": sc(50, 10), "sma_long": sc(200, 30), "rs": sc(63, 10),
            "max_box_age": sc(s.max_box_age_bars, 10), "max_hold": sc(cfg.risk.max_hold_bars, 6),
            "circ_window": sc(20, 4), "adtv": sc(20, 4)}


def build_features(df: pd.DataFrame, cfg: AppConfig, index_close: pd.Series | None = None) -> pd.DataFrame:
    L = lengths(cfg)
    f = pd.DataFrame(index=df.index)
    f["atr"] = atr(df, L["atr"])
    f["atr_pct"] = f["atr"] / df["close"] * 100
    f["vol_avg"] = sma(df["volume"], L["vol_avg"]).shift(1)       # excludes the signal bar
    f["vol_ratio"] = df["volume"] / f["vol_avg"]
    f["hh"] = df["high"].rolling(L["lookback_high"], min_periods=L["lookback_high"] // 2).max().shift(1)
    f["adtv_cr"] = traded_value_cr(df, L["adtv"]) / (5.0 if cfg.strategy.timeframe == "1wk" else 1.0)
    f["sma_mid"] = sma(df["close"], L["sma_mid"])
    f["sma_long"] = sma(df["close"], L["sma_long"])
    f["clv"] = close_location(df)
    prev = df["close"].shift(1)
    f["gap_pct"] = (df["open"] / prev - 1) * 100
    f["chg_pct"] = (df["close"] / prev - 1) * 100
    circ = circuit_flags(df)
    for col in circ.columns:
        f[col] = circ[col].to_numpy()
    f["circ_hits"] = (circ["uc"] | circ["lc"]).astype(int).rolling(L["circ_window"], min_periods=1).sum()
    f["jump_recent"] = (f["chg_pct"].abs() > 35).astype(float).rolling(L["lookback_high"], min_periods=1).max() > 0
    if index_close is not None and len(index_close):
        u = df.index.union(index_close.index)
        idx = index_close.reindex(u).ffill().reindex(df.index)
        ma = sma(index_close, L["regime_ma"]).reindex(u).ffill().reindex(df.index)
        lng = sma(index_close, L["sma_long"]).reindex(u).ffill().reindex(df.index)
        f["regime_long"], f["regime_short"], f["idx_above_long"] = idx > ma, idx < ma, idx > lng
        f["rs"] = (df["close"].pct_change(L["rs"]) - idx.pct_change(L["rs"])) * 100
    else:
        for col in ("regime_long", "regime_short", "idx_above_long", "rs"):
            f[col] = np.nan
    return f


@dataclass
class Candidate:
    idx: int
    date: pd.Timestamp
    side: str                  # BUY | SELL
    action: str                # ENTER_LONG | EXIT_LONG | ENTER_SHORT
    box: Box
    passed: bool
    reasons: list = field(default_factory=list)   # failed filters
    notes: list = field(default_factory=list)     # why it scores as it does
    flags: list = field(default_factory=list)     # check before trading
    score: float = 0.0                            # ranking heuristic 0-100, NOT a probability
    m: dict = field(default_factory=dict)


def _fl(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _tri(x) -> bool | None:
    """True/False, or None for missing (NaN)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return bool(x)


def evaluate_events(df: pd.DataFrame, feats: pd.DataFrame, res: DarvasResult, segment: str, rules: SegmentRules,
                    cfg: AppConfig, *, symbol: str = "", fno: bool | None = None,
                    events: EventCalendar | None = None, band: float | None = None,
                    only_from: int = 0) -> list[Candidate]:
    s = cfg.strategy
    idxs = np.flatnonzero(res.event != 0)
    idxs = idxs[idxs >= only_from]
    if idxs.size == 0:
        return []
    ev_mask = events.mask_before_events(symbol, df.index, s.avoid_event_days) if events else np.zeros(len(df), bool)
    closes = df["close"].to_numpy()
    out = []
    for i in idxs:
        box = res.boxes[int(res.event_box[i])]
        row = feats.iloc[i]
        m = {k: _fl(row[k]) for k in ("vol_ratio", "atr", "atr_pct", "adtv_cr", "clv", "rs", "gap_pct", "hh")}
        m.update(close=float(closes[i]), height_pct=box.height_pct, circ_hits=int(row["circ_hits"]), box_bars=int(i - box.top_idx))
        m["height_atr"] = m["height_pct"] / m["atr_pct"] if m["atr_pct"] > 0 else float("nan")
        common = []
        if not (m["adtv_cr"] >= rules.min_avg_traded_value_cr):
            common.append(f"illiquid: median turnover ₹{m['adtv_cr']:.1f} Cr < ₹{rules.min_avg_traded_value_cr:g} Cr")
        if not (m["close"] >= rules.min_price):
            common.append(f"price ₹{m['close']:.2f} below ₹{rules.min_price:g}")
        if m["circ_hits"] > rules.max_circuit_hits_20d:
            common.append(f"{m['circ_hits']} circuit closes in 20 sessions (operator/surveillance risk)")
        if s.exclude_corp_action_suspects and bool(row["jump_recent"]):
            common.append("unexplained >35% one-day move in the last year (unadjusted corporate action?)")
        if ev_mask[i]:
            common.append("calendar event within the next sessions")
        d = df.index[i]
        if res.event[i] > 0:
            out.append(_long(i, d, box, m, row, rules, s, common, band))
        else:
            out.append(_exit(i, d, box, m, common))
            if s.allow_shorts:
                out.append(_short(i, d, box, m, row, rules, s, common, fno))
    return out


def _box_checks(m, rules, s) -> list:
    r = []
    if m.get("box_bars", 1e9) < s.min_box_bars:
        r.append(f"box only {m['box_bars']} days old (< {s.min_box_bars})")
    if m["height_pct"] < rules.min_box_height_pct:
        r.append(f"box {m['height_pct']:.1f}% < {rules.min_box_height_pct:g}% minimum")
    if m["height_pct"] > rules.max_box_height_pct:
        r.append(f"box {m['height_pct']:.1f}% > {rules.max_box_height_pct:g}% maximum")
    if not (m["height_atr"] >= s.min_box_height_atr):
        r.append(f"box only {m['height_atr']:.1f} ATR tall")
    if not (m["vol_ratio"] >= rules.volume_surge):
        r.append(f"volume {m['vol_ratio']:.2f}x < {rules.volume_surge:g}x surge")
    return r


def _long(i, d, box, m, row, rules, s, common, band) -> Candidate:
    reasons = list(common) + _box_checks(m, rules, s)
    if not (box.top >= m["hh"] * (1 - s.near_high_pct / 100)):
        reasons.append("box top not near the 52-week high")
    if not (m["clv"] >= s.close_location_min):
        reasons.append(f"weak close (bottom {m['clv'] * 100:.0f}% of range)")
    regime = _tri(row["regime_long"])
    if s.regime_filter and regime is False:
        reasons.append("index below its regime MA (risk-off)")
    if s.min_rs_pct is not None and math.isfinite(m["rs"]) and m["rs"] < s.min_rs_pct:
        reasons.append(f"relative strength {m['rs']:+.1f}% < +{s.min_rs_pct:g}% vs index (3M)")
    notes, flags, sc = [], [], 0.0
    vr = m["vol_ratio"] if math.isfinite(m["vol_ratio"]) else 0.0
    sc += 25 * min(vr / rules.volume_surge, 2.0) / 2.0
    notes.append(f"Volume {vr:.1f}x average (needs {rules.volume_surge:g}x)")
    prox = box.top / m["hh"] if m["hh"] > 0 else 0.0
    if prox >= 1:
        sc += 15
        notes.append("Box top is a fresh 52-week high")
    else:
        sc += 15 * max(0.0, 1 - (1 - prox) / (s.near_high_pct / 100))
        notes.append(f"Box top {(1 - prox) * 100:.1f}% under the 52-week high")
    ha = m["height_atr"]
    if math.isfinite(ha):
        sc += 15 if 2 <= ha <= 6 else (15 * max(0, ha - 1) if ha < 2 else 15 * max(0, (10 - ha) / 4))
        notes.append(f"Box {m['height_pct']:.1f}% tall = {ha:.1f} ATR")
    sc += 10 * min(max(m["clv"], 0), 1)
    if math.isfinite(m["rs"]):
        sc += 15 * min(max(m["rs"] / 20, 0), 1)
        notes.append(f"{'Out' if m['rs'] >= 0 else 'Under'}performing the index by {abs(m['rs']):.1f}% over ~3 months")
    if regime is not None:
        above = bool(_tri(row["idx_above_long"]))
        sc += 5 * regime + 5 * above
        notes.append("Index above 50 & 200-DMA" if regime and above else
                     "Index above 50-DMA only" if regime else "Index below 50-DMA")
    else:
        flags.append("No index data: regime unknown")
    smid, slong = _fl(row["sma_mid"]), _fl(row["sma_long"])
    if math.isfinite(smid) and math.isfinite(slong) and m["close"] > smid > slong:
        sc += 10
        notes.append("Up-trend: close > 50-DMA > 200-DMA")
    if in_results_season(d.date()):
        sc -= 8
        flags.append("Results season: check the board-meeting date before entering")
    if math.isfinite(m["gap_pct"]) and m["atr_pct"] > 0 and m["gap_pct"] > m["atr_pct"]:
        sc -= 8
        flags.append(f"Opened {m['gap_pct']:.1f}% up: chase risk")
    if m["circ_hits"]:
        sc -= 5 * m["circ_hits"]
        flags.append(f"{m['circ_hits']} circuit close(s) in 20 sessions")
    if band is not None and band <= 5:
        sc -= 5
        flags.append(f"Looks like a {band:g}% band: a lower circuit can block the exit")
    if bool(row["uc_locked"]):
        sc -= 10
        flags.append("Upper-circuit close: next-day entry may gap or not fill")
    return Candidate(i, d, "BUY", "ENTER_LONG", box, not reasons, reasons, notes, flags, float(np.clip(sc, 0, 100)), m)


def _exit(i, d, box, m, common) -> Candidate:
    flags = ["Breakdown on below-average volume: possible shake-out"] if m["vol_ratio"] < 1 else []
    sc = float(np.clip(40 + 20 * min(m["vol_ratio"] / 2, 1.5) - 10 * (m["clv"] > 0.5), 0, 100))
    reasons = [r for r in common if r.startswith(("illiquid", "price"))]
    return Candidate(i, d, "SELL", "EXIT_LONG", box, not reasons, reasons,
                     [f"Closed below box bottom ₹{box.bottom:.2f} on {m['vol_ratio']:.1f}x volume"], flags, sc, m)


def _short(i, d, box, m, row, rules, s, common, fno) -> Candidate:
    reasons = list(common) + _box_checks(m, rules, s)
    if fno is False:
        reasons.append("not in F&O: retail cannot hold a cash short overnight")
    if not (m["clv"] <= 1 - s.close_location_min):
        reasons.append("close not in the lower half of the range")
    if math.isfinite(_fl(row["sma_long"])) and m["close"] >= _fl(row["sma_long"]):
        reasons.append("stock above its 200-DMA")
    regime = _tri(row["regime_short"])
    if s.regime_filter and regime is False:
        reasons.append("index above its regime MA (risk-on)")
    flags = ["Execute via stock futures (lot sizes apply); overnight cash shorts are not allowed for retail"]
    if fno is None:
        flags.append("F&O eligibility unknown")
    sc = 25 * min(m["vol_ratio"] / rules.volume_surge, 2) / 2 + 10 * (1 - m["clv"]) + 15 * bool(regime) + 15
    if math.isfinite(m["rs"]):
        sc += 15 * min(max(-m["rs"] / 20, 0), 1)
    return Candidate(i, d, "SELL", "ENTER_SHORT", box, not reasons, reasons,
                     [f"Breakdown on {m['vol_ratio']:.1f}x volume", f"Box {m['height_pct']:.1f}% tall"],
                     flags, float(np.clip(sc, 0, 100)), m)


# =============================================================================
# 6. TRADE PLAN — stop, targets, size, cost-aware R:R
# =============================================================================
def initial_stop(side: str, entry: float, box_top: float, box_bottom: float, atr_v: float,
                 risk: RiskConfig) -> tuple[float, str]:
    """Long: box stop = bottom - buffer; ATR stop = entry - k*ATR; 'tighter' = closer to entry.
    Always at least min_stop_atr ATR away so daily noise does not trigger it."""
    buf = risk.stop_buffer_pct / 100
    ok = atr_v is not None and math.isfinite(atr_v) and atr_v > 0
    if side == "BUY":
        box_s, atr_s = box_bottom * (1 - buf), (entry - risk.atr_stop_mult * atr_v if ok else -math.inf)
        if risk.stop_mode == "box" or not ok:
            stop, basis = box_s, "box bottom"
        elif risk.stop_mode == "atr":
            stop, basis = atr_s, f"{risk.atr_stop_mult:g} ATR"
        else:
            stop, basis = (box_s, "box bottom") if box_s >= atr_s else (atr_s, f"{risk.atr_stop_mult:g} ATR")
        if ok and entry - stop < risk.min_stop_atr * atr_v:
            stop, basis = entry - risk.min_stop_atr * atr_v, f"{basis}, widened to {risk.min_stop_atr:g} ATR"
        if risk.max_stop_pct and stop < entry * (1 - risk.max_stop_pct / 100):
            stop, basis = entry * (1 - risk.max_stop_pct / 100), f"{risk.max_stop_pct:g}% cap"
    else:
        box_s, atr_s = box_top * (1 + buf), (entry + risk.atr_stop_mult * atr_v if ok else math.inf)
        if risk.stop_mode == "box" or not ok:
            stop, basis = box_s, "box top"
        elif risk.stop_mode == "atr":
            stop, basis = atr_s, f"{risk.atr_stop_mult:g} ATR"
        else:
            stop, basis = (box_s, "box top") if box_s <= atr_s else (atr_s, f"{risk.atr_stop_mult:g} ATR")
        if ok and stop - entry < risk.min_stop_atr * atr_v:
            stop, basis = entry + risk.min_stop_atr * atr_v, f"{basis}, widened to {risk.min_stop_atr:g} ATR"
        if risk.max_stop_pct and stop > entry * (1 + risk.max_stop_pct / 100):
            stop, basis = entry * (1 + risk.max_stop_pct / 100), f"{risk.max_stop_pct:g}% cap"
    return stop, basis


def _tg(plan, k: int):
    """Target price k of a plan, or None (no plan, or a trail-only plan without fixed targets)."""
    return plan.targets[k]["price"] if plan and plan.targets else None


@dataclass
class TradePlan:
    side: str
    entry: float
    stop: float
    stop_basis: str
    risk_per_share: float
    risk_pct: float
    targets: list               # [{label, price, r, fraction, qty, net_r}]
    qty: int
    position_value: float
    risk_amount: float
    planned_r: float            # fraction-weighted R if all targets fill
    planned_r_net: float        # after round-trip costs
    breakeven_win_rate: float   # needed to break even at planned_r_net vs full -1R losses
    capped_by: str
    warnings: list = field(default_factory=list)


def build_plan(side: str, entry: float, box_top: float, box_bottom: float, atr_v: float, risk: RiskConfig,
               costs: CostConfig, *, equity: float | None = None, adtv_cr: float | None = None,
               rules: SegmentRules | None = None, exchange: str = "NSE") -> TradePlan | None:
    long = side == "BUY"
    entry = round_to_tick(entry, exchange)
    raw, basis = initial_stop(side, entry, box_top, box_bottom, atr_v, risk)
    stop = round_to_tick(raw, exchange, "down" if long else "up")
    rps = (entry - stop) if long else (stop - entry)
    if not (rps > 0) or not math.isfinite(rps):
        return None
    equity = risk.capital if equity is None else equity
    caps = {"risk": math.floor(equity * risk.risk_per_trade_pct / 100 / rps),
            "max_position": math.floor(equity * risk.max_position_pct / 100 / entry)}
    if adtv_cr and rules and math.isfinite(adtv_cr):
        caps["liquidity"] = math.floor(adtv_cr * 1e7 * rules.max_position_pct_of_adtv / 100 / entry)
    capped_by = min(caps, key=caps.get)
    qty = max(0, caps[capped_by])
    warnings = []
    if qty == 0:
        warnings.append("Capital too small for one share at this risk")
    elif capped_by != "risk":
        warnings.append(f"Size cut by the {capped_by.replace('_', ' ')} cap; risk below target")
    product = "delivery" if long else "futures"
    nq = qty if qty > 0 else max(1, math.floor(100_000 / entry))
    targets, planned, planned_net, left = [], 0.0, 0.0, qty
    for k, (r, frac) in enumerate(zip(risk.targets_r, risk.target_fractions)):
        px = round_to_tick(entry + r * rps if long else entry - r * rps, exchange)
        bv, sv = (entry, px) if long else (px, entry)
        cost = (order_costs(bv * nq, "buy", costs, product) + order_costs(sv * nq, "sell", costs, product)) / nq
        tq = left if k == len(risk.targets_r) - 1 else min(left, int(round(qty * frac)))
        left -= tq
        targets.append({"label": f"T{k + 1}", "price": px, "r": round(abs(px - entry) / rps, 2), "fraction": frac,
                        "qty": tq, "net_r": round((abs(px - entry) - cost) / rps, 2)})
        planned += frac * abs(px - entry) / rps
        planned_net += frac * (abs(px - entry) - cost) / rps
    stop_cost = (order_costs(entry * nq, "buy", costs, product) + order_costs(stop * nq, "sell", costs, product)) / nq
    loss_r = 1 + stop_cost / rps
    be = loss_r / (loss_r + planned_net) if planned_net > 0 else 1.0
    return TradePlan(side, entry, stop, basis, round(rps, 4), round(rps / entry * 100, 2), targets, qty,
                     round(qty * entry, 2), round(qty * rps, 2), round(planned, 2), round(planned_net, 2),
                     round(be, 3), capped_by, warnings)


# =============================================================================
# 7. DATA — normalisation, quality checks, universe, Kite / CSV / synthetic sources
# =============================================================================
def normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], name="date"), dtype=float)
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    if "date" in out.columns:
        out = out.set_index("date")
    missing = [c for c in OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    out = out[OHLCV].apply(pd.to_numeric, errors="coerce").astype(float)
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")].sort_index().dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe == "1d" or df.empty:
        return df
    wk = df.index.to_period("W-FRI")
    g = df.groupby(wk)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()})
    out.index = pd.DatetimeIndex(df.index.to_series().groupby(wk).max().values, name="date")
    return out


def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["high"] = out[["open", "close", "high"]].max(axis=1)
    out["low"] = out[["open", "close", "low"]].min(axis=1)
    return out[(out["close"] > 0) & (out["low"] > 0)]


@dataclass
class QualityReport:
    ok: bool = True
    issues: list = field(default_factory=list)


def check_quality(df: pd.DataFrame, *, min_bars: int, asof: date | None = None) -> QualityReport:
    rep = QualityReport()
    if df.empty or len(df) < min_bars:
        rep.ok = False
        rep.issues.append(f"only {len(df)} bars (need {min_bars})")
        return rep
    if float((df["volume"].iloc[-60:] <= 0).mean()) > 0.10:
        rep.ok = False
        rep.issues.append("frequent zero-volume days (suspended/illiquid)")
    if asof is not None and (pd.Timestamp(asof) - df.index[-1]).days > 10:
        rep.ok = False
        rep.issues.append(f"stale: last bar {df.index[-1].date()}")
    jumps = (df["close"].pct_change().abs() * 100).iloc[-260:]
    if (jumps > 35).any():
        rep.issues.append("one-day move >35% in the last year (unadjusted split/bonus?)")
    return rep


@dataclass
class Instrument:
    symbol: str
    exchange: str = "NSE"
    segment: str = "AUTO"
    name: str = ""
    fno: bool | None = None
    token: int | None = None   # Kite instrument_token on `exchange` (the one used for data)
    nse_token: int | None = None
    bse_token: int | None = None
    indices: list = field(default_factory=list)  # Kite index names this stock belongs to

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


def classify_by_liquidity(adtv_cr: float) -> str:
    """Segment for symbols without index membership (HEURISTIC breakpoints, ₹ Cr median turnover)."""
    return "LARGE" if adtv_cr >= 100 else "MID" if adtv_cr >= 20 else "SMALL" if adtv_cr >= 5 else "MICRO"


_INDEX_FILES = {"LARGE": ["ind_nifty50list.csv", "ind_niftynext50list.csv"],
                "MID": ["ind_niftymidcap150list.csv"], "SMALL": ["ind_niftysmallcap250list.csv"]}
_INDEX_HOSTS = ["https://www.niftyindices.com/IndexConstituent/", "https://nsearchives.nseindia.com/content/indices/"]
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _download_cached(fname: str, urls: list[str], cache: Path, ttl_s: float = 7 * 86400) -> str | None:
    p = cache / fname
    if p.exists() and time.time() - p.stat().st_mtime < ttl_s:
        return p.read_text(encoding="utf-8")
    try:
        import requests
        for u in urls:
            try:
                r = requests.get(u, headers=_UA, timeout=15)
                if r.status_code == 200 and "," in r.text.splitlines()[0]:
                    cache.mkdir(parents=True, exist_ok=True)
                    p.write_text(r.text, encoding="utf-8")
                    return r.text
            except Exception as exc:  # network
                log.warning("GET %s failed: %s", u, exc)
    except ImportError:
        log.warning("requests not installed; cannot download index lists")
    return p.read_text(encoding="utf-8") if p.exists() else None


# Kite index tradingsymbol -> niftyindices.com constituent file (all verified downloadable, Oct 2026).
# Indices missing here (strategy/bond/BSE indices) get price data only: no public constituent list.
INDEX_CONSTITUENTS = {
    "NIFTY 50": "ind_nifty50list.csv", "NIFTY NEXT 50": "ind_niftynext50list.csv",
    "NIFTY 100": "ind_nifty100list.csv", "NIFTY 200": "ind_nifty200list.csv", "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY MIDCAP 50": "ind_niftymidcap50list.csv", "NIFTY MIDCAP 100": "ind_niftymidcap100list.csv",
    "NIFTY MIDCAP 150": "ind_niftymidcap150list.csv", "NIFTY SMLCAP 50": "ind_niftysmallcap50list.csv",
    "NIFTY SMLCAP 100": "ind_niftysmallcap100list.csv", "NIFTY SMLCAP 250": "ind_niftysmallcap250list.csv",
    "NIFTY MIDSML 400": "ind_niftymidsmallcap400list.csv", "NIFTY LARGEMID250": "ind_niftylargemidcap250list.csv",
    "NIFTY MICROCAP250": "ind_niftymicrocap250_list.csv", "NIFTY TOTAL MKT": "ind_niftytotalmarket_list.csv",
    "NIFTY BANK": "ind_niftybanklist.csv", "NIFTY AUTO": "ind_niftyautolist.csv",
    "NIFTY FIN SERVICE": "ind_niftyfinancelist.csv", "NIFTY FMCG": "ind_niftyfmcglist.csv",
    "NIFTY IT": "ind_niftyitlist.csv", "NIFTY MEDIA": "ind_niftymedialist.csv", "NIFTY METAL": "ind_niftymetallist.csv",
    "NIFTY PHARMA": "ind_niftypharmalist.csv", "NIFTY PVT BANK": "ind_nifty_privatebanklist.csv",
    "NIFTY PSU BANK": "ind_niftypsubanklist.csv", "NIFTY REALTY": "ind_niftyrealtylist.csv",
    "NIFTY CONSR DURBL": "ind_niftyconsumerdurableslist.csv", "NIFTY OIL AND GAS": "ind_niftyoilgaslist.csv",
    "NIFTY HEALTHCARE": "ind_niftyhealthcarelist.csv", "NIFTY ENERGY": "ind_niftyenergylist.csv",
    "NIFTY INFRA": "ind_niftyinfralist.csv", "NIFTY COMMODITIES": "ind_niftycommoditieslist.csv",
    "NIFTY CONSUMPTION": "ind_niftyconsumptionlist.csv", "NIFTY CPSE": "ind_niftycpselist.csv",
    "NIFTY PSE": "ind_niftypselist.csv", "NIFTY MNC": "ind_niftymnclist.csv",
    "NIFTY IND DEFENCE": "ind_niftyindiadefence_list.csv", "NIFTY IND DIGITAL": "ind_niftyindiadigital_list.csv",
    "NIFTY INDIA MFG": "ind_niftyindiamanufacturing_list.csv", "NIFTY IND TOURISM": "ind_niftyindiatourism_list.csv",
    "NIFTY HOUSING": "ind_niftyhousing_list.csv", "NIFTY MOBILITY": "ind_niftymobility_list.csv",
    "NIFTY CAPITAL MKT": "ind_niftycapitalmarkets_list.csv", "NIFTY CHEMICALS": "ind_niftychemicals_list.csv",
    "NIFTY CORE HOUSING": "ind_niftycorehousing_list.csv", "NIFTY RURAL": "ind_niftyrural_list.csv",
    "NIFTY IPO": "ind_niftyipo_list.csv", "NIFTY MID SELECT": "ind_niftymidcapselect_list.csv",
    "NIFTY FINSRV25 50": "ind_niftyfinancialservices25_50list.csv",
    "NIFTY FINSEREXBNK": "ind_niftyfinancialservicesexbank_list.csv",
    "NIFTY NEW CONSUMP": "ind_niftyindianewageconsumption_list.csv",
    "NIFTY MIDSML HLTH": "ind_niftymidsmallhealthcare_list.csv",
    "NIFTY MS IT TELCM": "ind_niftymidsmallitandtelecom_list.csv",
    "NIFTY MS IND CONS": "ind_niftymidsmallindiaconsumption_list.csv",
    "NIFTY200MOMENTM30": "ind_nifty200momentum30_list.csv", "NIFTY500MOMENTM50": "ind_nifty500momentum50_list.csv",
    "NIFTYM150MOMNTM50": "ind_niftymidcap150momentum50_list.csv",
    "NIFTY100 LOWVOL30": "ind_nifty100lowvolatility30list.csv", "NIFTY100 QUALTY30": "ind_nifty100quality30list.csv",
    "NIFTY200 QUALTY30": "ind_nifty200quality30_list.csv", "NIFTY500 MULTICAP": "ind_nifty500multicap502525_list.csv",
    "NIFTY WAVES": "ind_niftywaves_list.csv", "NIFTY TOP 10 EW": "ind_niftytop10equalweight_list.csv",
    "NIFTY TOP 15 EW": "ind_niftytop15equalweight_list.csv", "NIFTY TOP 20 EW": "ind_niftytop20equalweight_list.csv",
}
_SEGMENT_OF_INDEX = {"NIFTY 50": "LARGE", "SENSEX": "LARGE", "NIFTY NEXT 50": "LARGE", "NIFTY MIDCAP 100": "MID",
                     "NIFTY MIDCAP 150": "MID", "NIFTY SMLCAP 100": "SMALL", "NIFTY SMLCAP 250": "SMALL"}

# The indices the "core" universe tracks (Kite EXCHANGE:tradingsymbol): only these indices' prices and stocks are
# scanned, which keeps a scan to ~250 stocks. BSE publishes no downloadable constituent list, so SENSEX members come
# from index_lists/sensex.csv: update it after each BSE rebalance (June and December).
CORE_INDICES = ["BSE:SENSEX", "NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:NIFTY MIDCAP 100", "NSE:NIFTY SMLCAP 100"]
CORE_NAMES = [k.split(":", 1)[1] for k in CORE_INDICES]
LOCAL_INDEX_LISTS = {"SENSEX": Path(__file__).with_name("index_lists") / "sensex.csv"}


def _constituent_rows(text: str) -> list[dict]:
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        sym = row.get("symbol", "")
        if sym and not sym.startswith("#") and row.get("series", "EQ") in ("EQ", "BE", "BZ", "SM", "ST"):
            rows.append({"symbol": sym.upper(), "name": row.get("company name", ""),
                         "industry": row.get("industry", ""), "isin": row.get("isin code", "")})
    return rows


def load_index_constituents(cfg: AppConfig, names: list[str] | None = None) -> tuple[dict[str, list[dict]], list[str]]:
    """Kite index name -> [{symbol, name, industry, isin}] for `names` (default: every index in INDEX_CONSTITUENTS)."""
    cache = Path(cfg.kite.cache_dir) / "universe"
    out, warnings = {}, []
    for idx in names or list(INDEX_CONSTITUENTS):
        if idx in LOCAL_INDEX_LISTS:
            p = LOCAL_INDEX_LISTS[idx]
            text = p.read_text(encoding="utf-8-sig") if p.exists() else None
            src = str(p)
        elif idx in INDEX_CONSTITUENTS:
            fname = INDEX_CONSTITUENTS[idx]
            text = _download_cached(fname, [h + fname for h in _INDEX_HOSTS], cache)
            src = fname
        else:
            warnings.append(f"no constituent list known for {idx}")
            continue
        if not text:
            warnings.append(f"could not load constituents of {idx} ({src})")
            continue
        out[idx] = _constituent_rows(text)
    industry = {r["symbol"]: r["industry"] for rows in out.values() for r in rows if r["industry"]}
    for rows in out.values():   # a list without an industry column borrows it from the other lists
        for r in rows:
            r["industry"] = r["industry"] or industry.get(r["symbol"], "")
    return out, warnings


def universe_from_indices(constituents: dict[str, list[dict]]) -> list[Instrument]:
    """One Instrument per stock across all indices, tagged with every index it belongs to."""
    by_sym: dict[str, Instrument] = {}
    for idx, rows in constituents.items():
        for r in rows:
            inst = by_sym.setdefault(r["symbol"], Instrument(r["symbol"], "NSE", "AUTO", r["name"]))
            inst.indices.append(idx)
    for inst in by_sym.values():
        segs = [_SEGMENT_OF_INDEX[i] for i in inst.indices if i in _SEGMENT_OF_INDEX]
        inst.segment = segs[0] if segs else "AUTO"   # AUTO -> classified by turnover later
    return sorted(by_sym.values(), key=lambda i: i.symbol)


def load_universe(name: str, cfg: AppConfig) -> tuple[list[Instrument], list[str]]:
    """allindices | nifty50 | nifty100 | nifty500 | midsmall | "NIFTY BANK" (any key of INDEX_CONSTITUENTS)
    | path/to/list.csv (symbol[,exchange][,segment][,name]).

    SURVIVORSHIP: backtesting today's index members over past years includes
    stocks *because* they did well enough to join. Results are biased upward.
    """
    warnings = []
    if name.lower().endswith(".csv") or Path(name).exists():
        out = []
        for row in csv.DictReader(io.StringIO(Path(name).read_text(encoding="utf-8"))):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            sym = (row.get("symbol") or "").upper()
            if not sym or sym.startswith("#"):
                continue
            exch = {"NS": "NSE", "BO": "BSE"}.get((row.get("exchange") or cfg.kite.exchange).upper(),
                                                    (row.get("exchange") or cfg.kite.exchange).upper())
            out.append(Instrument(sym, exch, (row.get("segment") or "AUTO").upper(), row.get("name", "")))
        return out, warnings
    if name.lower() in ("allindices", "core") or name.upper() in INDEX_CONSTITUENTS or name.upper() in LOCAL_INDEX_LISTS:
        names = None if name.lower() == "allindices" else CORE_NAMES if name.lower() == "core" else [name.upper()]
        cons, warnings = load_index_constituents(cfg, names)
        out = universe_from_indices(cons)
        if not out:
            raise RuntimeError("index constituent download failed; pass --universe path/to/list.csv instead")
        return out, warnings
    plan = {"nifty50": [("LARGE", "ind_nifty50list.csv")],
            "nifty100": [("LARGE", f) for f in _INDEX_FILES["LARGE"]],
            "nifty500": [(s, f) for s, fs in _INDEX_FILES.items() for f in fs],
            "midsmall": [("MID", _INDEX_FILES["MID"][0]), ("SMALL", _INDEX_FILES["SMALL"][0])]}.get(name.lower())
    if plan is None:
        raise ValueError(f"unknown universe {name}")
    cache = Path(cfg.kite.cache_dir) / "universe"
    out, seen = [], set()
    for seg, fname in plan:
        text = _download_cached(fname, [h + fname for h in _INDEX_HOSTS], cache)
        if not text:
            warnings.append(f"could not download {fname}")
            continue
        for row in csv.DictReader(io.StringIO(text)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            sym = row.get("symbol")
            if sym and sym not in seen and row.get("series", "EQ") in ("EQ", "BE", "BZ"):
                seen.add(sym)
                out.append(Instrument(sym, "NSE", seg, row.get("company name", "")))
    if not out:
        raise RuntimeError("index constituent download failed; pass --universe path/to/list.csv instead")
    return out, warnings


class DataSource:
    name = "base"
    is_demo = False

    def fetch_many(self, insts: list[Instrument], start: date, end: date,
                   progress: ProgressFn | None = None) -> dict[str, pd.DataFrame]:
        raise NotImplementedError

    def index_history(self, start: date, end: date) -> pd.DataFrame | None:
        return None


class KiteData(DataSource):
    """Daily candles from Kite Connect historical API, cached per symbol as CSV.

    Needs a Kite Connect app with historical-data access. Candles for the
    current day are live/partial until the close and are dropped before
    ~15:45 IST.
    """

    name = "kite"

    def __init__(self, kite, cfg: AppConfig):
        self.kite = kite
        self.cfg = cfg
        self.cache = Path(cfg.kite.cache_dir) / "ohlcv"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._tokens: dict[str, int] = {}
        self.indices: dict[str, int] = {}   # "NSE:NIFTY BANK" -> instrument_token
        self._fno: set[str] | None = None

    def _load_instruments(self) -> None:
        if self._tokens:
            return
        for exch in ("NSE", "BSE"):
            try:
                for ins in self.kite.instruments(exch):
                    key = f"{exch}:{ins['tradingsymbol']}"
                    self._tokens[key] = int(ins["instrument_token"])
                    if ins.get("segment") == "INDICES":
                        self.indices[key] = int(ins["instrument_token"])
            except Exception as exc:
                log.warning("instruments(%s) failed: %s", exch, exc)
        try:
            self._fno = {i["name"] for i in self.kite.instruments("NFO") if i.get("instrument_type") == "FUT"}
        except Exception as exc:
            log.warning("instruments(NFO) failed (%s); F&O status unknown", exc)

    def annotate(self, insts: list[Instrument]) -> list[str]:
        """Attach NSE and BSE tokens and F&O flags. A stock missing on its exchange falls back to the other.
        Returns symbols Kite knows on neither."""
        self._load_instruments()
        missing = []
        for i in insts:
            i.nse_token = self._tokens.get(f"NSE:{i.symbol}")
            i.bse_token = self._tokens.get(f"BSE:{i.symbol}")
            i.token = self._tokens.get(i.key)
            if i.token is None:
                alt = "BSE" if i.exchange == "NSE" else "NSE"
                if self._tokens.get(f"{alt}:{i.symbol}") is not None:
                    i.exchange, i.token = alt, self._tokens[f"{alt}:{i.symbol}"]
                else:
                    missing.append(i.key)
            if self._fno is not None and i.exchange == "NSE":
                i.fno = i.symbol in self._fno
        return missing

    def fetch_indices(self, start: date, end: date, progress: ProgressFn | None = None,
                      only: list[str] | None = None) -> dict[str, pd.DataFrame]:
        """Daily history of the indices in `only` (EXCHANGE:NAME), or every NSE and BSE index Kite lists."""
        self._load_instruments()
        todo = sorted((k, t) for k, t in self.indices.items() if only is None or k in only)
        for k in sorted(set(only or []) - self.indices.keys()):
            log.warning("index %s not found on Kite", k)
        out = {}
        for n, (key, tok) in enumerate(todo):
            df = self._cached(key, tok, start, end)
            if len(df):
                out[key] = df[df.index >= pd.Timestamp(start)]
            if progress:
                progress(n + 1, len(todo), key)
        return out

    def intraday(self, token: int, day: date, interval: str = "5minute") -> pd.DataFrame:
        """Intraday candles for one session (IST-naive timestamps), cached on disk."""
        p = Path(self.cfg.kite.cache_dir) / "intraday" / f"{token}_{day.isoformat()}_{interval}.csv"
        if p.exists():
            return pd.read_csv(p, index_col=0, parse_dates=True)
        rows = []
        for attempt in range(3):
            try:
                rows = self.kite.historical_data(token, datetime.combine(day, datetime.min.time()).replace(hour=9),
                                                 datetime.combine(day, datetime.min.time()).replace(hour=15, minute=30),
                                                 interval)
                break
            except Exception as exc:
                log.warning("intraday %s %s failed (%s); retry", token, day, exc)
                time.sleep(1.5 * (attempt + 1))
        time.sleep(self.cfg.kite.pause_s)
        if not rows:
            return pd.DataFrame(columns=OHLCV)
        df = pd.DataFrame(rows).set_index("date")[OHLCV]
        idx = pd.DatetimeIndex(pd.to_datetime(df.index))
        df.index = idx.tz_convert("Asia/Kolkata").tz_localize(None) if idx.tz is not None else idx
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p)
        return df

    def _history(self, token: int, start: date, end: date) -> pd.DataFrame:
        rows, a = [], start
        while a <= end:
            b = min(end, a + timedelta(days=self.cfg.kite.chunk_days - 1))
            for attempt in range(3):
                try:
                    rows += self.kite.historical_data(token, a, b, "day")
                    break
                except Exception as exc:
                    log.warning("historical_data %s %s..%s failed (%s); retry", token, a, b, exc)
                    time.sleep(1.5 * (attempt + 1))
            time.sleep(self.cfg.kite.pause_s)
            a = b + timedelta(days=1)
        return normalize_ohlcv(pd.DataFrame(rows)) if rows else normalize_ohlcv(None)

    def _cached(self, key: str, token: int, start: date, end: date) -> pd.DataFrame:
        p = self.cache / (key.replace(":", "_").replace("&", "AND") + ".csv")
        meta_p = self.cache / "_requested_from.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        old = None
        # "covered" = we already asked Kite for history from this start (a young listing simply starts later)
        covered = meta.get(key, "9999") <= start.isoformat()
        if p.exists():
            old = normalize_ohlcv(pd.read_csv(p, index_col=0, parse_dates=True))
            fresh = time.time() - p.stat().st_mtime < self.cfg.kite.cache_ttl_hours * 3600
            if fresh and len(old) and covered:
                return old
        if old is not None and len(old) and covered:
            new = self._history(token, old.index[-1].date() - timedelta(days=10), end)
            if not len(new):
                return old  # network trouble: serve the stale cache rather than nothing
            ov = old.index.intersection(new.index)
            # If overlapping closes disagree, history was retro-adjusted (split/bonus): refetch all.
            if len(new) and (not len(ov) or (old.loc[ov, "close"] / new.loc[ov, "close"] - 1).abs().max() <= 0.01):
                df = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
            else:
                df = self._history(token, start, end)
        else:
            df = self._history(token, start, end)
        if len(df):
            df.to_csv(p)
            if not covered:
                meta[key] = start.isoformat()
                meta_p.write_text(json.dumps(meta))
        return df

    def fetch_many(self, insts, start, end, progress=None):
        self._load_instruments()
        out = {}
        for k, i in enumerate(insts):
            if i.token is None:
                i.token = self._tokens.get(i.key)
            if i.token is not None:
                df = self._cached(i.key, i.token, start, end)
                if len(df):
                    out[i.key] = df[df.index >= pd.Timestamp(start)]
            if progress:
                progress(k + 1, len(insts), f"{i.key}")
        return out

    def index_history(self, start, end):
        self._load_instruments()
        key = f"NSE:{self.cfg.kite.index_symbol}"
        tok = self._tokens.get(key)
        return self._cached(key, tok, start, end) if tok else None


class CSVFolderData(DataSource):
    """Offline source: one CSV per symbol named EXCHANGE_SYMBOL.csv (date,open,high,low,close,volume)."""

    name = "csv"

    def __init__(self, folder: str, index_file: str | None = None):
        self.folder, self.index_file = Path(folder), index_file

    def fetch_many(self, insts, start, end, progress=None):
        out = {}
        for i in insts:
            p = self.folder / f"{i.exchange}_{i.symbol}.csv"
            if p.exists():
                out[i.key] = normalize_ohlcv(pd.read_csv(p, index_col=0, parse_dates=True))
        return out

    def index_history(self, start, end):
        if self.index_file and Path(self.index_file).exists():
            return normalize_ohlcv(pd.read_csv(self.index_file, index_col=0, parse_dates=True))
        return None


# ---- synthetic market (selftest/demo only; numbers say nothing about real stocks)
_DEMO = [("ARJUNAINFRA", "LARGE"), ("BHARATGRID", "LARGE"), ("CHAKRAFIN", "LARGE"), ("DEVBANK", "LARGE"),
         ("EKAMTELE", "LARGE"), ("GANGASTEEL", "LARGE"), ("HIMALAYAOIL", "LARGE"), ("INDRAAUTO", "LARGE"),
         ("NARMADACEM", "MID"), ("OJASCHEM", "MID"), ("PRITHVIMET", "MID"), ("RATNAJEWEL", "MID"),
         ("SAMUDRASHIP", "MID"), ("TEJASDEF", "MID"), ("UDAYHOTELS", "MID"), ("VAYUAERO", "MID"),
         ("EKTAPOLY", "SMALL"), ("GARUDAENG", "SMALL"), ("HARITAGRO", "SMALL"), ("ISHANPACK", "SMALL"),
         ("JAGRUTISW", "SMALL"), ("KIRANLAB", "SMALL"), ("MANTRAFOOD", "SMALL"), ("NAVYAPAPER", "SMALL")]
_SIG = {"LARGE": 0.014, "MID": 0.019, "SMALL": 0.026}
_ADTV = {"LARGE": 180.0, "MID": 35.0, "SMALL": 9.0}
_BOXH = {"LARGE": 0.055, "MID": 0.075, "SMALL": 0.10}
_SCEN = ["breakout", "breakout", "setup", "breakdown", "none", "breakout", "setup", "none"]


class SyntheticData(DataSource):
    name = "demo"
    is_demo = True

    def __init__(self, seed: int = 11, years: float = 8.0, end: date | None = None):
        self.seed = seed
        end = end or date.today()
        while end.weekday() >= 5:
            end -= timedelta(days=1)
        self.dates = pd.bdate_range(end=pd.Timestamp(end), periods=int(years * 250), name="date")
        rng = np.random.default_rng(seed)
        r, bear = np.empty(len(self.dates)), False
        for i in range(len(r)):
            if rng.random() < (0.02 if bear else 0.012):
                bear = not bear
            r[i] = (-0.0009 if bear else 0.0006) + (0.013 if bear else 0.009) * rng.standard_normal()
        r[-70:] = 0.0012 + 0.007 * rng.standard_normal(70)
        r[0] = 0
        self.mkt = r

    @staticmethod
    def universe() -> list[Instrument]:
        return [Instrument(s, "NSE", g, s.title() + " (demo)", fno=(g != "SMALL" and k % 3 != 0))
                for k, (s, g) in enumerate(_DEMO)]

    def index_history(self, start, end):
        c = 15000 * np.exp(np.cumsum(self.mkt))
        p = np.concatenate([[c[0]], c[:-1]])
        return pd.DataFrame({"open": p, "high": np.maximum(p, c) * 1.003, "low": np.minimum(p, c) * 0.997,
                             "close": c, "volume": 3e8}, index=self.dates)

    def fetch_many(self, insts, start, end, progress=None):
        return {i.key: self._stock(i) for i in insts if any(i.symbol == s for s, _ in _DEMO)}

    def _stock(self, inst: Instrument) -> pd.DataFrame:
        seg = inst.segment
        rng = np.random.default_rng((zlib.crc32(inst.symbol.encode()) + self.seed) % 2**32)
        n, sig, beta = len(self.dates), _SIG[seg] * rng.uniform(0.85, 1.2), rng.uniform(0.7, 1.3)
        band = float(rng.choice([20.0, 20.0, 10.0])) if seg == "SMALL" else None
        lp, rets = np.empty(n), np.zeros(n)
        lp[0] = np.log(rng.uniform(80, 2500))
        state, base = 0, lp[0]
        for i in range(1, n):
            u = rng.random()
            if state == 0 and u < 0.035:
                state, base = 1, lp[i - 1]
            elif state == 1 and u < 0.04:
                state = 0 if rng.random() < 0.62 else 2
            elif state == 2 and u < 0.05:
                state, base = 1, lp[i - 1]
            drift = 0.0018 if state == 0 else -0.0016 if state == 2 else 0.12 * (base - lp[i - 1])
            x = beta * self.mkt[i] + drift + sig * (0.6 if state == 1 else 1) * rng.standard_normal()
            if rng.random() < 0.006:
                x += rng.choice([-1, 1]) * rng.uniform(3, 6) * sig
            if band:
                x = float(np.clip(x, np.log(1 - band / 100), np.log(1 + band / 100)))
            rets[i], lp[i] = x, lp[i - 1] + x
        c = np.exp(lp)
        p = np.concatenate([[c[0]], c[:-1]])
        o = p * np.exp(0.25 * sig * rng.standard_normal(n))
        h = np.maximum(o, c) * np.exp(np.abs(0.55 * sig * rng.standard_normal(n)))
        lo = np.minimum(o, c) * np.exp(-np.abs(0.55 * sig * rng.standard_normal(n)))
        if band:
            o = np.clip(o, p * (1 - band / 100), p * (1 + band / 100))
            h, lo = np.minimum(np.maximum(h, o), p * (1 + band / 100)), np.maximum(np.minimum(lo, o), p * (1 - band / 100))
        bv = _ADTV[seg] * 1e7 / np.median(c) * rng.uniform(0.7, 1.4)
        v = bv * np.exp(0.35 * rng.standard_normal(n)) * (1 + 1.6 * np.clip(rets / sig - 1, 0, 4))
        df = pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": v}, index=self.dates)
        scen = _SCEN[(zlib.crc32(inst.symbol.encode()) + 3) % len(_SCEN)]
        return df if scen == "none" else _script_tail(df, scen, seg, sig, bv, rng)


def _script_tail(df, scen, seg, sig, bv, rng) -> pd.DataFrame:
    """Overwrite the last bars with a run-up, a clean box, and a breakout/setup/breakdown ending."""
    tn, bn = 12, 13
    k = tn + bn + (1 if scen in ("breakout", "breakdown") else 0)
    o, h, l, c, v = (df[x].to_numpy().copy() for x in OHLCV)
    n = len(c)
    s = n - k
    top = max(h[max(0, s - 260):s].max() * 1.012, c[s - 1] * 1.10)
    bot = top * (1 - _BOXH[seg])

    def bar(i, op, cl, hp, lp, vm):
        o[i], c[i] = op, cl
        h[i], l[i] = max(op, cl) * (1 + hp), min(op, cl) * (1 - lp)
        v[i] = bv * vm * np.exp(0.15 * rng.standard_normal())

    prev = c[s - 1]
    for j, px in enumerate(prev * (top * 0.975 / prev) ** (np.arange(1, tn + 1) / tn)):
        bar(s + j, prev * (1 + 0.002 * rng.standard_normal()), px, 0.3 * sig, 0.3 * sig, 1.4)
        h[s + j] = min(h[s + j], top * 0.996)
        prev = px
    for j in range(bn):
        i = s + tn + j
        if j == 0:
            bar(i, prev, top * 0.985, 0, 0.004, 1.2)
            h[i] = top
        elif j == 3:
            bar(i, prev, bot * 1.02, 0.003, 0, 0.9)
            l[i] = bot
        else:
            lo_c, hi_c = (bot * 1.02, top * 0.975)
            if j >= bn - 2:
                lo_c, hi_c = (bot * 1.008, bot * 1.02) if scen == "breakdown" else (top * 0.972, top * 0.985)
            op = float(np.clip(prev * (1 + 0.003 * rng.standard_normal()), bot * 1.006, top * 0.992))
            bar(i, op, rng.uniform(lo_c, hi_c), 0.004, 0.004, 0.7)
            h[i], l[i] = min(h[i], top * 0.993), max(l[i], bot * 1.004)
        prev = c[i]
    if scen == "breakout":
        bar(n - 1, prev * 1.004, max(prev * 1.035, top * 1.012), 0.006, 0.003, 3.2)
    elif scen == "breakdown":
        bar(n - 1, prev * 0.997, min(prev * 0.965, bot * 0.985), 0.003, 0.006, 2.8)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=df.index)


# =============================================================================
# 8. PIPELINE + SCANNER
# =============================================================================
@dataclass
class Prepared:
    inst: Instrument
    segment: str
    df: pd.DataFrame        # bars in the configured timeframe
    feats: pd.DataFrame
    band: float | None
    issues: list
    _boxes: dict = field(default_factory=dict, repr=False)

    def darvas(self, confirm: int, on_close: bool, max_age: int) -> DarvasResult:
        k = (confirm, on_close, max_age)
        if k not in self._boxes:
            self._boxes[k] = detect_boxes(self.df["high"], self.df["low"], self.df["close"], confirm, on_close, max_age)
        return self._boxes[k]


def prepare(inst: Instrument, daily: pd.DataFrame | None, cfg: AppConfig, index_tf: pd.Series | None,
            asof: date | None = None) -> tuple[Prepared | None, str | None]:
    if daily is None or daily.empty:
        return None, "no data"
    daily = clean_ohlcv(daily)
    q = check_quality(daily, min_bars=cfg.strategy.lookback_high + 60, asof=asof)
    if not q.ok:
        return None, "; ".join(q.issues)
    df = resample_ohlcv(daily, cfg.strategy.timeframe)
    feats = build_features(df, cfg, index_tf)
    seg = inst.segment if inst.segment in SEGMENTS else classify_by_liquidity(_fl(feats["adtv_cr"].iloc[-1]) or 0.0)
    return Prepared(inst, seg, df, feats, infer_price_band(daily), q.issues), None


def load_prepared(source: DataSource, insts: list[Instrument], cfg: AppConfig,
                  progress: ProgressFn | None = None) -> tuple[list[Prepared], dict, pd.DataFrame | None]:
    end = now_ist().date()
    start = end - timedelta(days=int(cfg.kite.history_years * 365.25))
    excl = {s.upper() for s in cfg.exclude_symbols}
    insts = [i for i in insts if i.symbol.upper() not in excl]
    data = source.fetch_many(insts, start, end, progress)
    index = source.index_history(start, end)
    if not source.is_demo and not session_closed(close_hhmm=cfg.kite.session_close_ist):
        today = pd.Timestamp(end)
        data = {k: v[v.index < today] for k, v in data.items()}
        if index is not None:
            index = index[index.index < today]
    index_tf = resample_ohlcv(index, cfg.strategy.timeframe)["close"] if index is not None and len(index) else None
    asof = max((d.index[-1] for d in data.values() if len(d)), default=pd.Timestamp(end)).date()
    out, rejected = [], {}
    for i in insts:
        p, why = prepare(i, data.get(i.key), cfg, index_tf, asof)
        if p is None:
            rejected[i.key] = why
        else:
            out.append(p)
    return out, rejected, index


def _rules(cfg: AppConfig, p: Prepared) -> SegmentRules:
    return cfg.segments[p.segment]


def _allowed(cfg: AppConfig, p: Prepared) -> bool:
    return _rules(cfg, p).enabled and p.segment in cfg.include_segments


def _res(cfg: AppConfig, p: Prepared) -> DarvasResult:
    return p.darvas(_rules(cfg, p).confirm_bars, cfg.strategy.breakout_on_close, lengths(cfg)["max_box_age"])


def regime_summary(index: pd.DataFrame | None) -> dict:
    if index is None or len(index) < 200:
        return {"state": "UNKNOWN"}
    c = index["close"]
    last, s50, s200 = float(c.iloc[-1]), float(sma(c, 50).iloc[-1]), float(sma(c, 200).iloc[-1])
    above = (last > s50, last > s200)
    return {"state": "RISK-ON" if all(above) else "RISK-OFF" if not any(above) else "CAUTION",
            "close": round(last, 2), "sma50": round(s50, 2), "sma200": round(s200, 2),
            "date": index.index[-1].strftime("%Y-%m-%d")}


def scan(prepared: list[Prepared], cfg: AppConfig, events: EventCalendar | None = None,
         equity: float | None = None) -> dict:
    """Latest actionable signals and pre-breakout setups across the prepared universe."""
    signals, setups = [], []
    for p in prepared:
        if not _allowed(cfg, p):
            continue
        res = _res(cfg, p)
        n = len(p.df)
        cands = evaluate_events(p.df, p.feats, res, p.segment, _rules(cfg, p), cfg, symbol=p.inst.symbol,
                                fno=p.inst.fno, events=events, band=p.band,
                                only_from=max(0, n - cfg.strategy.signal_window_bars))
        latest: dict[str, Candidate] = {}
        for c in cands:
            if c.passed and (c.action not in latest or c.idx >= latest[c.action].idx):
                latest[c.action] = c
        if "ENTER_SHORT" in latest:
            latest.pop("EXIT_LONG", None)
        for c in latest.values():
            row = _signal_row(p, c, cfg, equity)
            if row:
                signals.append(row)
        s = _setup_row(p, res, cfg, equity)
        if s:
            setups.append(s)
    signals.sort(key=lambda r: (r["side"] != "BUY", -r["score"]))
    setups.sort(key=lambda r: (r["distance_pct"], -r["score"]))
    return {"signals": signals, "setups": setups}


def _signal_row(p: Prepared, c: Candidate, cfg: AppConfig, equity: float | None) -> dict | None:
    n, last = len(p.df), float(p.df["close"].iloc[-1])
    age = n - 1 - c.idx
    adtv = _fl(p.feats["adtv_cr"].iloc[-1])
    plan = None
    if c.action in ("ENTER_LONG", "ENTER_SHORT"):
        side = "BUY" if c.action == "ENTER_LONG" else "SELL"
        chase = cfg.risk.max_chase_pct / 100
        if (side == "BUY" and last > c.m["close"] * (1 + chase)) or (side == "SELL" and last < c.m["close"] * (1 - chase)):
            return None  # ran away from the signal: the plan's R:R no longer holds
        plan = build_plan(side, last, c.box.top, c.box.bottom, _fl(p.feats["atr"].iloc[-1]), cfg.risk, cfg.costs,
                          equity=equity, adtv_cr=adtv, rules=_rules(cfg, p), exchange=p.inst.exchange)
        if plan is None:
            return None
        after = p.df.iloc[c.idx + 1:]
        if len(after) and ((side == "BUY" and after["low"].min() <= plan.stop) or
                           (side == "SELL" and after["high"].max() >= plan.stop)):
            return None  # already stopped out since the signal
    row = {"symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment, "fno": p.inst.fno,
           "side": c.side, "action": c.action, "date": c.date.strftime("%Y-%m-%d"), "age_bars": age,
           "signal_close": round(c.m["close"], 2), "last_close": round(last, 2),
           "box_top": round(c.box.top, 2), "box_bottom": round(c.box.bottom, 2),
           "box_height_pct": round(c.box.height_pct, 2), "vol_ratio": round(c.m["vol_ratio"], 2),
           "adtv_cr": round(adtv, 1) if math.isfinite(adtv) else None, "score": round(c.score, 1),
           "notes": c.notes, "flags": c.flags, "band": p.band}
    if plan:
        row.update(entry=plan.entry, stop=plan.stop, t1=_tg(plan, 0), t2=_tg(plan, -1),
                   rr=plan.planned_r, rr_net=plan.planned_r_net, qty=plan.qty, risk_pct=plan.risk_pct,
                   plan=asdict(plan))
    else:
        row.update(entry=None, stop=None, t1=None, t2=None, rr=None, rr_net=None, qty=None, risk_pct=None,
                   plan=None, exit_below=round(c.box.bottom, 2))
    return row


def _setup_row(p: Prepared, res: DarvasResult, cfg: AppConfig, equity: float | None) -> dict | None:
    """Complete, unresolved box with price pressing its top: a buy-stop (GTT) candidate."""
    if res.forming.get("phase") != "active":
        return None
    s, rules = cfg.strategy, _rules(cfg, p)
    box = res.boxes[res.forming["box"]]
    row = p.feats.iloc[-1]
    last = float(p.df["close"].iloc[-1])
    dist = (box.top / last - 1) * 100
    if dist < 0 or dist > s.setup_proximity_pct:
        return None
    h, atr_pct, adtv, hh = box.height_pct, _fl(row["atr_pct"]), _fl(row["adtv_cr"]), _fl(row["hh"])
    if not (rules.min_box_height_pct <= h <= rules.max_box_height_pct):
        return None
    if atr_pct > 0 and h / atr_pct < s.min_box_height_atr:
        return None
    if math.isfinite(hh) and box.top < hh * (1 - s.near_high_pct / 100):
        return None
    if not (adtv >= rules.min_avg_traded_value_cr) or last < rules.min_price:
        return None
    if s.regime_filter and _tri(row["regime_long"]) is False:
        return None
    if int(row["circ_hits"]) > rules.max_circuit_hits_20d:
        return None
    trigger = round_to_tick(box.top + tick_size(box.top, p.inst.exchange), p.inst.exchange, "up")
    plan = build_plan("BUY", trigger, box.top, box.bottom, _fl(row["atr"]), cfg.risk, cfg.costs, equity=equity,
                      adtv_cr=adtv, rules=rules, exchange=p.inst.exchange)
    if plan is None:
        return None
    vol_in_box = p.df["volume"].iloc[box.top_idx:].mean()
    dry = vol_in_box / _fl(row["vol_avg"]) if _fl(row["vol_avg"]) > 0 else float("nan")
    sc = 50 - 8 * dist
    notes = [f"Close {dist:.1f}% under box top {box.top:.2f}", f"Box {h:.1f}% tall since {p.df.index[box.top_idx].date()}"]
    if math.isfinite(dry) and dry < 1:
        sc += 15 * (1 - dry)
        notes.append(f"Volume drying up inside the box ({dry:.2f}x average)")
    rs = _fl(row["rs"])
    if math.isfinite(rs):
        sc += 15 * min(max(rs / 20, 0), 1)
        notes.append(f"Relative strength vs index {rs:+.1f}%")
    notes.append("Backtest confirms on a CLOSE above the top; a GTT buy-stop fills intraday and takes more false breakouts")
    return {"symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment, "side": "BUY",
            "action": "SETUP", "last_close": round(last, 2), "box_top": round(box.top, 2),
            "box_bottom": round(box.bottom, 2), "box_height_pct": round(h, 2), "distance_pct": round(dist, 2),
            "trigger": trigger, "entry": plan.entry, "stop": plan.stop, "t1": _tg(plan, 0),
            "t2": _tg(plan, -1), "rr": plan.planned_r or None, "qty": plan.qty,
            "score": round(float(np.clip(sc, 0, 100)), 1), "notes": notes, "plan": asdict(plan)}


def trail_recommendation(p: Prepared, cfg: AppConfig, entry: float, current_stop: float,
                         entry_date: str, t1_hit: bool) -> dict:
    """Stop a long position should have now: max(current, breakeven if T1 hit, latest box bottom since entry)."""
    res = _res(cfg, p)
    buf = cfg.risk.stop_buffer_pct / 100
    new, why = current_stop, "unchanged"
    if t1_hit and cfg.risk.move_stop_to_breakeven and entry > new:
        new, why = entry, "breakeven after T1"
    ei = int(p.df.index.searchsorted(pd.Timestamp(entry_date)))
    if cfg.risk.trail_with_boxes and res.trail_confirm[-1] >= ei and math.isfinite(res.trail_bottom[-1]):
        tb = res.trail_bottom[-1] * (1 - buf)
        if tb > new:
            new, why = tb, f"new box bottom {res.trail_bottom[-1]:.2f} confirmed"
    new = round_to_tick(new, p.inst.exchange, "down") if new > current_stop else current_stop
    last = float(p.df["close"].iloc[-1])
    return {"symbol": p.inst.symbol, "last_close": last, "current_stop": current_stop, "new_stop": new,
            "raise": new > current_stop + 1e-9, "reason": why, "stop_breached": last <= new}


# =============================================================================
# 8b. RANKINGS — most recent breakouts, Darvas-ready bullishness, index summary
# =============================================================================
def _idx_label(inst: Instrument, limit: int = 4) -> str:
    """Most specific indices first (sectoral/thematic are shorter lists than broad ones)."""
    broad = {"NIFTY TOTAL MKT", "NIFTY 500", "NIFTY500 MULTICAP", "NIFTY 200", "NIFTY 100", "NIFTY MIDSML 400",
             "NIFTY LARGEMID250"}
    names = sorted(inst.indices, key=lambda x: (x in broad, x))
    return ", ".join(names[:limit]) + (f" +{len(names) - limit}" if len(names) > limit else "")


def entry_trigger(cfg: AppConfig, breakout_high: float, exchange: str = "NSE") -> float:
    """Stop-limit trigger: entry_above_high_pct above the breakout day's high, on the tick grid."""
    return round_to_tick(breakout_high * (1 + cfg.strategy.entry_above_high_pct / 100), exchange, "up")


def rank_breakouts(prepared: list[Prepared], cfg: AppConfig, window: int = 20,
                   events: EventCalendar | None = None) -> list[dict]:
    """Latest Darvas box breakout (BUY) or breakdown (SELL) per stock within the last `window` bars,
    ranked newest first, then by score. Every breakout is listed; `filters` says whether it passes the
    strategy's liquidity/volume/trend filters (PASS) or why not."""
    rows = []
    for p in prepared:
        res = _res(cfg, p)
        n = len(p.df)
        start = max(0, n - window)
        ev = np.flatnonzero(res.event[start:] != 0)
        if ev.size == 0:
            continue
        i = start + int(ev[-1])
        cands = evaluate_events(p.df, p.feats, res, p.segment, _rules(cfg, p), cfg, symbol=p.inst.symbol,
                                fno=p.inst.fno, events=events, band=p.band, only_from=i)
        c = next((c for c in cands if c.idx == i and c.action in ("ENTER_LONG", "EXIT_LONG")), None)
        if c is None:
            continue
        buy = res.event[i] > 0
        last = float(p.df["close"].iloc[-1])
        level = c.box.top if buy else c.box.bottom
        holding = (last > c.box.top) if buy else (last < c.box.bottom)
        # BUY breakouts still above the box get a trade plan from the latest close (next-session entry):
        # stop = tighter of box bottom - buffer / 2 ATR (>= 1 ATR away), targets at risk.targets_r multiples.
        plan = None
        if buy and holding:
            f = p.feats.iloc[-1]
            ref = entry_trigger(cfg, float(p.df["high"].iloc[i]), p.inst.exchange) if cfg.strategy.entry_mode == "stop_limit" else last
            plan = build_plan("BUY", ref, c.box.top, c.box.bottom, _fl(f["atr"]), cfg.risk, cfg.costs,
                              adtv_cr=_fl(f["adtv_cr"]), rules=_rules(cfg, p), exchange=p.inst.exchange)
        rows.append({
            "symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment,
            "signal": "BUY" if buy else "SELL", "breakout_date": c.date.strftime("%Y-%m-%d"), "breakout_time": "",
            "bars_ago": n - 1 - i, "box_top": round(c.box.top, 2), "box_bottom": round(c.box.bottom, 2),
            "box_height_pct": round(c.box.height_pct, 2), "breakout_close": round(c.m["close"], 2),
            "last_close": round(last, 2), "move_since_pct": round((last / c.m["close"] - 1) * 100, 2),
            "beyond_level_pct": round((last / level - 1) * 100, 2),
            "status": ("above box" if buy else "below box") if holding else "back inside box",
            "vol_ratio": round(c.m["vol_ratio"], 2) if math.isfinite(c.m["vol_ratio"]) else None,
            "score": round(c.score, 1), "filters": "PASS" if c.passed else "; ".join(c.reasons),
            "entry": plan.entry if plan else None,
            "max_entry": round_to_tick(plan.entry * (1 + (cfg.strategy.entry_limit_pct if cfg.strategy.entry_mode == "stop_limit" else cfg.risk.max_chase_pct) / 100), p.inst.exchange, "down")
            if plan else None,
            "stop": plan.stop if plan else None, "stop_basis": plan.stop_basis if plan else None,
            "risk_pct": plan.risk_pct if plan else None,
            "t1": _tg(plan, 0), "t2": _tg(plan, -1),
            "planned_r": plan.planned_r if plan else None, "qty": plan.qty if plan else None,
            "indices": _idx_label(p.inst), "nse_token": p.inst.nse_token, "bse_token": p.inst.bse_token,
            "_token": p.inst.token, "_ord": c.date.value})
    rows.sort(key=lambda r: (-r["_ord"], r["filters"] != "PASS", -r["score"]))
    for k, r in enumerate(rows, 1):
        r["rank"] = k
        del r["_ord"]
    return rows


def bullishness(p: Prepared, box: Box, cfg: AppConfig, prox_pct: float) -> tuple[float, dict]:
    """0-100 bullishness of a stock sitting under an unbroken Darvas box top. Heuristic ranking, NOT a
    probability. Parts: proximity 20, relative strength 20, trend 15, 52-week-high 15, volume dry-up 10,
    box quality 10, momentum 5, market regime 5."""
    f, df = p.feats.iloc[-1], p.df
    last = float(df["close"].iloc[-1])
    dist = (box.top / last - 1) * 100
    parts = {"proximity": 20 * max(0.0, 1 - dist / prox_pct) if prox_pct > 0 else 0.0}
    rs = _fl(f["rs"])
    parts["rel_strength"] = 20 * min(max(rs / 20, 0), 1) if math.isfinite(rs) else 0.0
    smid, slong = _fl(f["sma_mid"]), _fl(f["sma_long"])
    parts["trend"] = float(5 * (math.isfinite(smid) and last > smid) + 5 * (math.isfinite(smid) and
                           math.isfinite(slong) and smid > slong) + 5 * (math.isfinite(slong) and last > slong))
    hh = _fl(f["hh"])
    prox = box.top / hh if hh > 0 else float("nan")
    near = cfg.strategy.near_high_pct / 100
    parts["near_52w_high"] = (15.0 if prox >= 1 else 15 * max(0.0, 1 - (1 - prox) / near)) if math.isfinite(prox) else 0.0
    vavg = _fl(f["vol_avg"])
    dry = df["volume"].iloc[box.top_idx:].mean() / vavg if vavg > 0 else float("nan")
    parts["volume_dryup"] = 10 * min(max((1 - dry) * 2, 0), 1) if math.isfinite(dry) else 0.0
    atr_pct = _fl(f["atr_pct"])
    ha = box.height_pct / atr_pct if atr_pct > 0 else float("nan")
    parts["box_quality"] = (10.0 if 2 <= ha <= 6 else 10 * max(0, ha - 1) if ha < 2 else 10 * max(0, (10 - ha) / 4)) \
        if math.isfinite(ha) else 0.0
    ret20 = (last / float(df["close"].iloc[-21]) - 1) * 100 if len(df) > 21 else float("nan")
    parts["momentum"] = 5 * min(max(ret20 / 10, 0), 1) if math.isfinite(ret20) else 0.0
    parts["market_regime"] = 5.0 if _tri(f["regime_long"]) else 0.0
    parts = {k: round(float(v), 1) for k, v in parts.items()}
    return round(float(np.clip(sum(parts.values()), 0, 100)), 1), parts


def rank_ready(prepared: list[Prepared], cfg: AppConfig, ready_pct: float | None = None,
               equity: float | None = None) -> list[dict]:
    """Darvas-ready stocks: a confirmed, unbroken box with the close within `ready_pct`% under its top.
    Ranked by bullishness. A daily close above `trigger` would be the BUY breakout."""
    s = cfg.strategy
    prox = ready_pct if ready_pct is not None else s.setup_proximity_pct
    rows = []
    for p in prepared:
        res = _res(cfg, p)
        if res.forming.get("phase") != "active":
            continue
        box = res.boxes[res.forming["box"]]
        last = float(p.df["close"].iloc[-1])
        dist = (box.top / last - 1) * 100
        if dist < 0 or dist > prox:
            continue
        rules, f = _rules(cfg, p), p.feats.iloc[-1]
        score, parts = bullishness(p, box, cfg, prox)
        h, atr_pct, adtv, hh = box.height_pct, _fl(f["atr_pct"]), _fl(f["adtv_cr"]), _fl(f["hh"])
        reasons = []
        if not (rules.min_box_height_pct <= h <= rules.max_box_height_pct):
            reasons.append(f"box {h:.1f}% outside {rules.min_box_height_pct:g}-{rules.max_box_height_pct:g}%")
        if atr_pct > 0 and h / atr_pct < s.min_box_height_atr:
            reasons.append(f"box only {h / atr_pct:.1f} ATR tall")
        if math.isfinite(hh) and box.top < hh * (1 - s.near_high_pct / 100):
            reasons.append("box top not near the 52-week high")
        if not (adtv >= rules.min_avg_traded_value_cr):
            reasons.append(f"illiquid (₹{adtv:.1f} Cr/day)")
        if last < rules.min_price:
            reasons.append(f"price below ₹{rules.min_price:g}")
        if s.regime_filter and _tri(f["regime_long"]) is False:
            reasons.append("index below its regime MA")
        if int(f["circ_hits"]) > rules.max_circuit_hits_20d:
            reasons.append("circuit-prone")
        trigger = round_to_tick(box.top + tick_size(box.top, p.inst.exchange), p.inst.exchange, "up")
        plan = build_plan("BUY", trigger, box.top, box.bottom, _fl(f["atr"]), cfg.risk, cfg.costs, equity=equity,
                          adtv_cr=adtv, rules=rules, exchange=p.inst.exchange)
        rows.append({
            "symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment, "signal": "BUY-READY",
            "last_close": round(last, 2), "trigger": trigger, "box_top": round(box.top, 2),
            "box_bottom": round(box.bottom, 2), "box_height_pct": round(h, 2), "distance_pct": round(dist, 2),
            "box_since": p.df.index[box.top_idx].strftime("%Y-%m-%d"), "bullishness": score,
            **{f"b_{k}": v for k, v in parts.items()},
            "stop": plan.stop if plan else None, "t1": _tg(plan, 0),
            "t2": _tg(plan, -1), "qty": plan.qty if plan else None,
            "filters": "PASS" if not reasons else "; ".join(reasons), "indices": _idx_label(p.inst),
            "nse_token": p.inst.nse_token, "bse_token": p.inst.bse_token})
    rows.sort(key=lambda r: (-r["bullishness"], r["distance_pct"]))
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return rows


def add_breakout_times(src: "KiteData", rows: list[dict], top_n: int = 50, interval: str = "5minute") -> None:
    """Fill breakout_time for the first `top_n` rows: start of the first intraday candle that traded beyond the
    box (high > top for BUY, low < bottom for SELL) on the breakout day. The daily close confirmed it."""
    todo = [r for r in rows if r.get("_token")][:top_n]
    for k, r in enumerate(todo):
        df = src.intraday(r["_token"], date.fromisoformat(r["breakout_date"]), interval)
        if df.empty:
            r["breakout_time"] = "no data"
        else:
            buy = r["signal"] == "BUY"
            hit = df[df["high"] > r["box_top"]] if buy else df[df["low"] < r["box_bottom"]]
            if not len(hit):
                r["breakout_time"] = "at close"
            else:
                first = df.iloc[0]
                gap = first["open"] > r["box_top"] if buy else first["open"] < r["box_bottom"]
                r["breakout_time"] = hit.index[0].strftime("%H:%M") + (" gap" if gap and hit.index[0] == df.index[0] else "")
        _progress(k + 1, len(todo), f"intraday {r['symbol']} {r['breakout_date']}")


def index_summary(index_data: dict[str, pd.DataFrame], constituents: dict[str, list[dict]],
                  breakouts: list[dict], ready: list[dict], cfg: AppConfig, window: int = 20) -> list[dict]:
    """Each index: returns and trend (price only: Darvas is applied to stocks, not to indices), and how many of
    its member stocks have a Darvas breakout / breakdown / ready setup."""
    sig = {(r["symbol"], r["signal"]) for r in breakouts if r["bars_ago"] < window}
    rdy = {r["symbol"] for r in ready}
    rows = []
    for key, df in index_data.items():
        exch, name = key.split(":", 1)
        c = df["close"]
        if len(c) < 2:
            continue
        last = float(c.iloc[-1])
        pct = lambda k: round((last / float(c.iloc[-k - 1]) - 1) * 100, 2) if len(c) > k else None  # noqa: E731
        s50, s200 = sma(c, 50).iloc[-1], sma(c, 200).iloc[-1]
        members = [m["symbol"] for m in constituents.get(name, [])]
        rows.append({
            "index": name, "exchange": exch, "last_close": round(last, 2), "date": c.index[-1].strftime("%Y-%m-%d"),
            "chg_1d_pct": pct(1), "chg_1m_pct": pct(21), "chg_3m_pct": pct(63), "chg_1y_pct": pct(250),
            "above_50dma": bool(last > s50) if math.isfinite(s50) else None,
            "above_200dma": bool(last > s200) if math.isfinite(s200) else None,
            "members": len(members) if members else None,
            "member_buys": sum((m, "BUY") in sig for m in members) if members else None,
            "member_sells": sum((m, "SELL") in sig for m in members) if members else None,
            "member_ready": sum(m in rdy for m in members) if members else None})
    rows.sort(key=lambda r: (r["exchange"], -(r["chg_3m_pct"] if r["chg_3m_pct"] is not None else -1e9)))
    return rows


def track_breakouts(prepared: list[Prepared], cfg: AppConfig, events: EventCalendar | None = None,
                    lookback: int = 60) -> list[dict]:
    """Follow every Darvas BUY breakout of the last `lookback` bars through the strategy's own trade rules
    (simulate_symbol: next-open entry within the chase limit, stop, half out at T1 then stop to breakeven, rest at
    T2, box trailing, time exit) to show which hit the stop-loss and which reached their targets. Signals that
    failed a filter are tracked as if taken, and keep their filter note."""
    L, r = lengths(cfg), cfg.risk
    rows = []
    for p in prepared:
        n = len(p.df)
        start = max(L["lookback_high"], n - lookback)
        res = _res(cfg, p)
        cands = [c for c in evaluate_events(p.df, p.feats, res, p.segment, _rules(cfg, p), cfg, symbol=p.inst.symbol,
                                            fno=p.inst.fno, events=events, band=p.band, only_from=start)
                 if c.action == "ENTER_LONG" and c.idx >= start]
        if not cands:
            continue
        sims = simulate_symbol(p.inst.key, p.df, p.feats, res, [replace(c, passed=True) for c in cands], cfg,
                               segment=p.segment, max_hold=L["max_hold"])
        by_sig = {t.signal_idx: t for t in sims}
        O, C = p.df["open"].to_numpy(float), p.df["close"].to_numpy(float)
        last = float(C[-1])
        for c in cands:
            base = {"symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment,
                    "signal_date": c.date.strftime("%Y-%m-%d"), "breakout_close": round(float(C[c.idx]), 2),
                    "box_top": round(c.box.top, 2), "box_bottom": round(c.box.bottom, 2), "score": round(c.score, 1),
                    "filters": "PASS" if c.passed else "; ".join(c.reasons), "last_close": round(last, 2)}
            t = by_sig.get(c.idx)
            if t is None:
                e = c.idx + 1
                if e >= n:
                    status, outcome = ("Buy-stop above the breakout high, next session" if cfg.strategy.entry_mode == "stop_limit"
                                       else "Entry next session"), "pending"
                elif cfg.strategy.entry_mode == "stop_limit":
                    status, outcome = ("Order live: not triggered yet" if n - 1 - c.idx < cfg.strategy.entry_valid_bars
                                       else "Not entered: stop-limit not filled"), ("pending" if n - 1 - c.idx < cfg.strategy.entry_valid_bars else "skipped")
                elif O[e] > C[c.idx] * (1 + r.max_chase_pct / 100):
                    status, outcome = "Not entered: opened above the chase limit", "skipped"
                elif any(s.signal_idx < c.idx <= s.exit_idx for s in sims):
                    status, outcome = "Not entered: already in an earlier trade", "skipped"
                else:
                    status, outcome = "Not entered", "skipped"
                rows.append({**base, "status": status, "outcome": outcome})
                continue
            tg = [t.entry + k * t.risk_ps for k in r.targets_r]
            hit = {f.reason: f for f in t.fills}
            last_t = f"T{len(tg)}" if tg else "-"
            why = t.exit_reason
            if t.is_open:
                status, outcome = ("T1 hit · running" if t.t1_hit else "Open · trailing the box" if not tg else "Open"), "open"
            elif why == "stop":
                status, outcome = "Stop-loss hit", "loss"
            elif why == last_t:
                status, outcome = f"Target {len(tg)} hit", "win"
            elif why == "breakeven":
                status, outcome = "T1 hit, then breakeven", "win"
            elif why == "trail":
                status, outcome = ("T1 hit, trailed out" if t.t1_hit else "Trailed out"), ("win" if t.net_r > 0 else "loss")
            elif why == "time":
                status, outcome = "Time exit", ("win" if t.net_r > 0 else "loss")
            else:
                status, outcome = why.capitalize(), ("win" if t.net_r > 0 else "loss")
            ret = sum(f.fraction * (f.price / t.entry - 1) for f in t.fills) * 100
            rows.append({**base, "status": status, "outcome": outcome, "entry_date": t.entry_date,
                         "entry": round(t.entry, 2), "stop": round(t.stop0, 2), "t1": round(tg[0], 2) if tg else None,
                         "t2": round(tg[-1], 2) if tg else None, "t1_date": hit["T1"].date if "T1" in hit else "",
                         "t2_date": hit[last_t].date if last_t in hit and len(tg) > 1 else "",
                         "stop_date": t.exit_date if why == "stop" else "",
                         "exit_date": "" if t.is_open else t.exit_date,
                         "exit_price": None if t.is_open else round(t.fills[-1].price, 2),
                         "r": round(t.net_r, 2), "return_pct": round(ret, 2), "bars_held": t.bars_held})
    rows.sort(key=lambda x: (x["signal_date"], x["symbol"]), reverse=True)
    return rows


def active_boxes(prepared: list[Prepared], cfg: AppConfig) -> list[dict]:
    """Every stock's confirmed, unbroken Darvas box as of the last daily close, with what an intraday watcher
    needs to judge a live price: trigger (first tick above the top), ATR for the stop, 50-day average volume and
    relative strength. A live price above `trigger` is a provisional breakout until the daily close confirms it."""
    rows = []
    for p in prepared:
        res = _res(cfg, p)
        if res.forming.get("phase") != "active":
            continue
        box = res.boxes[res.forming["box"]]
        f = p.feats.iloc[-1]
        rows.append({"symbol": p.inst.symbol, "exchange": p.inst.exchange, "segment": p.segment,
                     "box_top": round(box.top, 2), "box_bottom": round(box.bottom, 2),
                     "trigger": round_to_tick(box.top + tick_size(box.top, p.inst.exchange), p.inst.exchange, "up"),
                     "box_since": p.df.index[box.top_idx].strftime("%Y-%m-%d"), "box_height_pct": round(box.height_pct, 2),
                     "last_close": round(float(p.df["close"].iloc[-1]), 2), "atr": round(_fl(f["atr"]), 2),
                     "vol_avg": round(_fl(f["vol_avg"]), 0) if math.isfinite(_fl(f["vol_avg"])) else None,
                     "rs": round(_fl(f["rs"]), 2) if math.isfinite(_fl(f["rs"])) else None,
                     "near_52wh": bool(box.top >= _fl(f["hh"]) * (1 - cfg.strategy.near_high_pct / 100))
                     if math.isfinite(_fl(f["hh"])) else None})
    return rows


RETURN_WINDOWS = {"1d": 1, "1w": 5, "1m": 21, "3m": 63, "6m": 126, "1y": 250}   # trading sessions


def stock_stats(prepared: list[Prepared], constituents: dict[str, list[dict]], lag: int = 5) -> list[dict]:
    """Per-stock trailing returns (now and `lag` sessions earlier, for rank-change arrows), YTD, trend and an
    RS rating: 1-99 percentile of 0.4*3m + 0.2*6m + 0.2*9m + 0.2*12m return across the scanned universe
    (an IBD-style relative-strength rank; heuristic)."""
    industry = {r["symbol"]: r["industry"] for rows in constituents.values() for r in rows if r.get("industry")}
    names = {r["symbol"]: r["name"] for rows in constituents.values() for r in rows}

    def rets(c: pd.Series, off: int) -> dict:
        e = len(c) - 1 - off
        if e < 1:
            return {}
        last = float(c.iloc[e])
        out = {k: round((last / float(c.iloc[e - n]) - 1) * 100, 2) if e - n >= 0 else None
               for k, n in RETURN_WINDOWS.items()}
        prior = c.iloc[: e + 1][c.index[: e + 1] < pd.Timestamp(c.index[e].year, 1, 1)]
        out["ytd"] = round((last / float(prior.iloc[-1]) - 1) * 100, 2) if len(prior) else None
        return out

    rows = []
    for p in prepared:
        c = p.df["close"].dropna()
        if len(c) < lag + 2:
            continue
        now, prev = rets(c, 0), rets(c, lag)
        def rs_raw(off: int) -> float:
            r = lambda n: float(c.iloc[-1 - off] / c.iloc[-1 - off - n] - 1) if len(c) > n + off else float("nan")  # noqa: E731
            parts = [(w, v) for w, v in [(0.4, r(63)), (0.2, r(126)), (0.2, r(189)), (0.2, r(250))] if math.isfinite(v)]
            return sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else float("nan")

        s50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else float("nan")
        s200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else float("nan")
        rows.append({
            "symbol": p.inst.symbol, "name": names.get(p.inst.symbol, p.inst.name), "exchange": p.inst.exchange,
            "sector": industry.get(p.inst.symbol, ""), "segment": p.segment, "indices": "|".join(p.inst.indices),
            "last_close": round(float(c.iloc[-1]), 2), "date": c.index[-1].strftime("%Y-%m-%d"),
            **{f"chg_{k}": v for k, v in now.items()}, **{f"prev_{k}": v for k, v in prev.items()},
            "above_50dma": bool(c.iloc[-1] > s50) if math.isfinite(s50) else None,
            "above_200dma": bool(c.iloc[-1] > s200) if math.isfinite(s200) else None,
            "_rs": rs_raw(0), "_rs_prev": rs_raw(lag)})
    for key, out in (("_rs", "rs_rating"), ("_rs_prev", "prev_rs_rating")):   # prev = `lag` sessions earlier
        pct = pd.Series([x[key] for x in rows], dtype=float).rank(pct=True)
        for x, q in zip(rows, pct):
            x[out] = int(min(99, max(1, round(q * 99)))) if math.isfinite(q) else None
    for x in rows:
        del x["_rs"], x["_rs_prev"]
    return rows


def sector_rotation(prepared: list[Prepared], constituents: dict[str, list[dict]], benchmark: pd.DataFrame | None,
                    breakouts: list[dict], ready: list[dict], weeks: int = 10, tail: int = 8,
                    group_of: dict[str, str] | None = None) -> list[dict]:
    """Relative rotation of sectors (the constituent lists' "industry") against the benchmark (Nifty 50), from
    weekly closes. Each sector is an equal-weight index of its scanned members.

    RS        = sector index / benchmark, smoothed (3-week EMA)
    RS-Ratio  = 100 + z-score of RS over `weeks` weeks          (> 100: outperforming its recent average)
    RS-Mom.   = 100 + z-score of RS-Ratio's smoothed weekly change (> 100: relative strength improving)
    Quadrants as in a relative rotation graph: Leading (>100, >100) -> Weakening (>100, <100) ->
    Lagging (<100, <100) -> Improving (<100, >100). A ranking heuristic, not a forecast."""
    if benchmark is None or not len(benchmark):
        return []
    industry = group_of or {r["symbol"]: r["industry"] for rows in constituents.values() for r in rows if r.get("industry")}
    bench = benchmark["close"].resample("W-FRI").last().dropna()
    groups: dict[str, list[pd.Series]] = {}
    names: dict[str, list[tuple[str, float]]] = {}
    for p in prepared:
        sec = industry.get(p.inst.symbol)
        if not sec:
            continue
        wk = p.df["close"].resample("W-FRI").last().dropna()
        groups.setdefault(sec, []).append(wk.pct_change().rename(p.inst.symbol))
        r3 = (wk.iloc[-1] / wk.iloc[-14] - 1) * 100 if len(wk) > 14 else float("nan")
        names.setdefault(sec, []).append((p.inst.symbol, r3))
    sig_b = {r["symbol"] for r in breakouts if r["signal"] == "BUY"}
    sig_s = {r["symbol"] for r in breakouts if r["signal"] == "SELL"}
    rdy = {r["symbol"] for r in ready}

    def z(s: pd.Series) -> pd.Series:
        return 100 + (s - s.rolling(weeks).mean()) / s.rolling(weeks).std().replace(0, np.nan)

    def chg(s: pd.Series, k: int) -> float | None:
        return round(float((s.iloc[-1] / s.iloc[-1 - k] - 1) * 100), 2) if len(s) > k else None

    rows = []
    for sec, series in groups.items():
        if len(series) < 2:
            continue
        ret = pd.concat(series, axis=1).mean(axis=1, skipna=True).dropna()
        idx = (1 + ret).cumprod() * 100
        both = pd.concat([idx.rename("s"), bench.rename("b")], axis=1).dropna()
        if len(both) < 2 * weeks + 2:
            continue
        rs = (100 * both["s"] / both["b"]).ewm(span=3, adjust=False).mean()   # smoothed, as RRGs are
        ratio = z(rs)
        mom = z(ratio.diff().ewm(span=3, adjust=False).mean())
        pts = pd.concat([ratio.rename("r"), mom.rename("m")], axis=1).dropna().iloc[-tail:]
        if not len(pts):
            continue
        r, m = float(pts["r"].iloc[-1]), float(pts["m"].iloc[-1])
        quad = ("Leading" if m >= 100 else "Weakening") if r >= 100 else ("Improving" if m >= 100 else "Lagging")
        mem = [s for s, _ in names[sec]]
        best = sorted((x for x in names[sec] if math.isfinite(x[1])), key=lambda x: -x[1])
        rows.append({
            "sector": sec, "members": len(mem), "rs_ratio": round(r, 2), "rs_momentum": round(m, 2), "quadrant": quad,
            "chg_1w_pct": chg(both["s"], 1), "chg_1m_pct": chg(both["s"], 4), "chg_3m_pct": chg(both["s"], 13),
            "rel_3m_pct": round(chg(both["s"], 13) - chg(both["b"], 13), 2) if len(both) > 13 else None,
            "buys": sum(s in sig_b for s in mem), "sells": sum(s in sig_s for s in mem), "ready": sum(s in rdy for s in mem),
            "leaders": [{"symbol": s, "chg_3m_pct": round(v, 2)} for s, v in best[:3]],
            # last year of weekly sector (equal-weight) and benchmark levels, so a client can redo the RS-Ratio /
            # RS-Momentum maths with live prices for the current week
            "series": {"dates": [d.strftime("%Y-%m-%d") for d in both.index[-52:]],
                       "s": [round(float(v), 4) for v in both["s"].iloc[-52:]],
                       "b": [round(float(v), 2) for v in both["b"].iloc[-52:]], "members": mem},
            "tail": [{"date": d.strftime("%Y-%m-%d"), "rs_ratio": round(float(a), 2), "rs_momentum": round(float(b), 2)}
                     for d, a, b in zip(pts.index, pts["r"], pts["m"])]})
    order = {"Leading": 0, "Improving": 1, "Weakening": 2, "Lagging": 3}
    rows.sort(key=lambda x: (order[x["quadrant"]], -x["rs_ratio"]))
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]).to_csv(path, index=False)


# =============================================================================
# 9. BACKTEST — simulator, portfolio, metrics, walk-forward, verdict
# =============================================================================
# Execution model (conservative):
# * Signal at a close -> entry at the NEXT open + slippage. Skipped if that open
#   is > max_chase above the signal close, or the bar is locked upper-circuit.
# * Stops fill at the stop, or at the open on a gap through it. A bar touching
#   stop and target is assumed to hit the STOP first.
# * Lower-circuit-locked bars cannot be exited (no buyers); exit moves later.
# * Breakeven after T1 and box-trailing apply from the NEXT bar.
# * Same-day exit is allowed (broker books it as intraday; T+1 settlement).
@dataclass
class Fill:
    idx: int
    date: str
    price: float
    fraction: float
    reason: str


@dataclass
class SimTrade:
    key: str
    segment: str
    side: str
    signal_idx: int
    signal_date: str
    entry_idx: int
    entry_date: str
    entry: float
    stop0: float
    risk_ps: float
    fills: list = field(default_factory=list)
    exit_idx: int = -1
    exit_date: str = ""
    exit_reason: str = ""
    is_open: bool = False
    net_r: float = 0.0
    t1_hit: bool = False
    bars_held: int = 0
    score: float = 0.0
    adtv_cr: float = float("nan")


def simulate_symbol(key: str, df: pd.DataFrame, feats: pd.DataFrame, res: DarvasResult, cands: list[Candidate],
                    cfg: AppConfig, *, segment: str, max_hold: int, skipped: dict | None = None) -> list[SimTrade]:
    r = cfg.risk
    O, H, Lo, C = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    dates = list(df.index.strftime("%Y-%m-%d"))
    ucl, lcl = feats["uc_locked"].to_numpy(bool), feats["lc_locked"].to_numpy(bool)
    atr_a, adtv = feats["atr"].to_numpy(float), feats["adtv_cr"].to_numpy(float)
    n, slip, buf = len(C), r.slippage_pct / 100, r.stop_buffer_pct / 100
    nominal = cfg.backtest.nominal_position_value
    skipped = skipped if skipped is not None else {}
    wanted = {"ENTER_LONG"} | ({"ENTER_SHORT"} if cfg.strategy.allow_shorts else set())
    trades, busy = [], -1
    for cand in sorted((c for c in cands if c.passed and c.action in wanted), key=lambda c: c.idx):
        i = cand.idx
        if i < busy:
            skipped["already in a trade"] = skipped.get("already in a trade", 0) + 1
            continue
        e = i + 1
        if e >= n:
            continue
        long = cand.action == "ENTER_LONG"
        sg = 1.0 if long else -1.0
        why = None
        fill = None
        if long and cfg.strategy.entry_mode == "stop_limit":
            # stop-limit above the breakout day's high, valid entry_valid_bars sessions; cancelled by a close back
            # inside the box; a gap past the limit is not chased (fills only if the day trades back to the limit)
            trig = H[i] * (1 + cfg.strategy.entry_above_high_pct / 100)
            lim = trig * (1 + cfg.strategy.entry_limit_pct / 100)
            for k in range(i + 1, min(n, i + 1 + cfg.strategy.entry_valid_bars)):
                if k > i + 1 and C[k - 1] < cand.box.top:
                    break
                if H[k] < trig or ucl[k]:
                    continue
                if O[k] <= lim:
                    e, fill = k, max(O[k], trig)
                    break
                if Lo[k] <= lim:
                    e, fill = k, lim
                    break
            if fill is None:
                why = "stop-limit not filled"
        elif long and O[e] > C[i] * (1 + r.max_chase_pct / 100):
            why = "gap above chase limit"
        elif not long and O[e] < C[i] * (1 - r.max_chase_pct / 100):
            why = "gap below chase limit"
        elif long and ucl[e]:
            why = "upper circuit locked at entry"
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        entry = (fill if fill is not None else O[e]) * (1 + sg * slip)
        stop, _ = initial_stop("BUY" if long else "SELL", entry, cand.box.top, cand.box.bottom, atr_a[i], r)
        rps = (entry - stop) * sg
        if why is None and (not (rps > 0) or ((fill if fill is not None else O[e]) - stop) * sg <= 0):
            why = "opened beyond the stop"
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        tgts = [entry + sg * k * rps for k in r.targets_r]
        t = SimTrade(key, segment, "BUY" if long else "SELL", i, dates[i], e, dates[e], entry, stop, rps,
                     score=cand.score, adtv_cr=adtv[i])
        rem, k, cur, be_pend = 1.0, 0, stop, False
        last = min(n - 1, e + max_hold - 1)
        j = e
        while j <= last:
            o, h, lo, c = O[j], H[j], Lo[j], C[j]
            blocked = long and lcl[j] and j > e
            if not blocked:
                if (lo <= cur) if long else (h >= cur):
                    px = cur if j == e else (min(o, cur) if long else max(o, cur))
                    moved = (cur - stop) * sg > 1e-9
                    reason = "stop" if not moved else ("breakeven" if abs(cur - entry) <= 1e-9 * entry else "trail")
                    t.fills.append(Fill(j, dates[j], px * (1 - sg * slip), rem, reason))
                    rem = 0.0
                    break
                while k < len(tgts) and rem > 1e-9 and ((h >= tgts[k]) if long else (lo <= tgts[k])):
                    is_last = k == len(tgts) - 1
                    if is_last and r.final_leg == "trail":
                        break
                    px = tgts[k] if j == e else (max(o, tgts[k]) if long else min(o, tgts[k]))
                    frac = rem if is_last else min(r.target_fractions[k], rem)
                    t.fills.append(Fill(j, dates[j], px, frac, f"T{k + 1}"))
                    rem -= frac
                    k += 1
                    if k == 1:
                        t.t1_hit, be_pend = True, r.move_stop_to_breakeven
                if rem <= 1e-9:
                    break
                if r.failed_breakout_bars > 0 and k == 0 and j - e < r.failed_breakout_bars and \
                        ((c < cand.box.top) if long else (c > cand.box.bottom)):
                    t.fills.append(Fill(j, dates[j], c * (1 - sg * slip), rem, "failed breakout"))
                    rem = 0.0
                    break
            if be_pend:
                cur, be_pend = (max(cur, entry) if long else min(cur, entry)), False
            if r.trail_with_boxes and res.trail_confirm[j] > i:
                if long and math.isfinite(res.trail_bottom[j]):
                    cur = max(cur, res.trail_bottom[j] * (1 - buf))
                elif not long and math.isfinite(res.trail_top[j]):
                    cur = min(cur, res.trail_top[j] * (1 + buf))
            if blocked and j == last and last < n - 1:
                last += 1
            j += 1
        if rem > 1e-9:
            if e + max_hold - 1 <= n - 1 or last < n - 1:
                jx = min(last, n - 1)
                t.fills.append(Fill(jx, dates[jx], C[jx] * (1 - sg * slip), rem, "time"))
            else:
                t.fills.append(Fill(n - 1, dates[n - 1], C[n - 1], rem, "open"))
                t.is_open = True
        prod = "delivery" if long else "futures"
        bp, sp = order_costs(nominal, "buy", cfg.costs, prod) / nominal, order_costs(nominal, "sell", cfg.costs, prod) / nominal
        gross = sum(f.fraction * (f.price - entry) * sg for f in t.fills)
        costs = entry * (bp if long else sp) + sum(f.fraction * f.price * (sp if long else bp) for f in t.fills)
        t.net_r = (gross - costs) / rps
        lf = t.fills[-1]
        t.exit_idx, t.exit_date, t.exit_reason, t.bars_held = lf.idx, lf.date, lf.reason, lf.idx - e + 1
        trades.append(t)
        busy = n if t.is_open else t.exit_idx
    return trades


SHORT_MARGIN = 0.25  # ASSUMED futures margin fraction; lot sizes ignored


def run_portfolio(trades: list[SimTrade], closes: dict[str, pd.Series], cfg: AppConfig):
    """Size trades in rupees: fixed-fractional risk on prior-day equity, position/cash/liquidity caps,
    max open positions. Exit proceeds usable next session (conservative under T+1)."""
    r = cfg.risk
    if not trades:
        return pd.DataFrame(columns=["equity", "positions", "drawdown"]), [], {}
    by_entry: dict[str, list] = {}
    for t in trades:
        by_entry.setdefault(t.entry_date, []).append(t)
    first, last = min(t.entry_date for t in trades), max(t.exit_date for t in trades)
    union, px = pd.DatetimeIndex([]), {}
    for k in {t.key for t in trades}:
        s = closes.get(k)
        if s is None:
            continue
        s = s[(s.index >= pd.Timestamp(first)) & (s.index <= pd.Timestamp(last))]
        union = union.union(s.index)
        px[k] = dict(zip(s.index.strftime("%Y-%m-%d"), s.to_numpy(float)))
    cash, eq_prev, pending = float(r.capital), float(r.capital), 0.0
    pos: dict[str, dict] = {}
    done, skipped, curve, lastpx = [], {}, [], {}
    for day in union.strftime("%Y-%m-%d"):
        cash, pending = cash + pending, 0.0
        for t in sorted(by_entry.get(day, []), key=lambda x: -x.score):
            why = ("already holding" if t.key in pos else
                   "max open positions" if len(pos) >= r.max_open_positions else None)
            long = t.side == "BUY"
            q = 0
            if why is None:
                per = t.entry * (1.0012 if long else SHORT_MARGIN)
                q = min(math.floor(eq_prev * r.risk_per_trade_pct / 100 / t.risk_ps),
                        math.floor(eq_prev * r.max_position_pct / 100 / t.entry), math.floor(cash / per))
                rules = cfg.segments.get(t.segment)
                if rules and math.isfinite(t.adtv_cr):
                    q = min(q, math.floor(t.adtv_cr * 1e7 * rules.max_position_pct_of_adtv / 100 / t.entry))
                if q < 1:
                    why = "insufficient capital"
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            val = q * t.entry
            cost = order_costs(val, "buy" if long else "sell", cfg.costs, "delivery" if long else "futures")
            blocked = val if long else val * SHORT_MARGIN
            cash -= blocked + cost
            pos[t.key] = {"t": t, "qty": q, "rem": q, "blocked": blocked, "costs": cost, "pnl": 0.0, "exits": []}
        for key in list(pos):
            p = pos[key]
            t, long = p["t"], p["t"].side == "BUY"
            for f in t.fills:
                if f.date != day or f.reason == "open":
                    continue
                q = p["rem"] if f is t.fills[-1] else min(p["rem"], int(round(f.fraction * p["qty"])))
                if q <= 0:
                    continue
                cost = order_costs(q * f.price, "sell" if long else "buy", cfg.costs, "delivery" if long else "futures")
                pnl = (f.price - t.entry) * q * (1 if long else -1)
                pending += q / p["qty"] * p["blocked"] + pnl - cost
                p["rem"] -= q
                p["pnl"] += pnl - cost
                p["costs"] += cost
                p["exits"].append({"date": f.date, "price": round(f.price, 2), "qty": q, "reason": f.reason})
            if p["rem"] <= 0:
                done.append(_closed(p))
                del pos[key]
        mtm = 0.0
        for key, p in pos.items():
            v = px.get(key, {}).get(day)
            if v is not None:
                lastpx[key] = v
            v = lastpx.get(key, p["t"].entry)
            mtm += p["rem"] * v if p["t"].side == "BUY" else p["rem"] / p["qty"] * p["blocked"] + (p["t"].entry - v) * p["rem"]
        eq_prev = cash + pending + mtm
        curve.append((day, eq_prev, len(pos)))
    for p in pos.values():
        row = _closed(p)
        row["open"] = True
        done.append(row)
    eq = pd.DataFrame(curve, columns=["date", "equity", "positions"]).set_index("date")
    eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1
    return eq, done, skipped


def _closed(p: dict) -> dict:
    t = p["t"]
    risk_amt = p["qty"] * t.risk_ps
    return {"key": t.key, "segment": t.segment, "side": t.side, "signal_date": t.signal_date,
            "entry_date": t.entry_date, "entry": round(t.entry, 2), "stop": round(t.stop0, 2), "qty": p["qty"],
            "exit_date": p["exits"][-1]["date"] if p["exits"] else t.exit_date,
            "exit_reason": p["exits"][-1]["reason"] if p["exits"] else t.exit_reason, "exits": p["exits"],
            "pnl": round(p["pnl"], 2), "costs": round(p["costs"], 2),
            "r": round(p["pnl"] / risk_amt, 3) if risk_amt > 0 else 0.0, "bars_held": t.bars_held,
            "t1_hit": t.t1_hit, "open": False}


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    den = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, ctr - half), min(1.0, ctr + half))


def r_stats(rs: list[float]) -> dict:
    a = np.asarray(rs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0, "expectancy_r": 0.0, "t_stat": 0.0}
    w, l = a[a > 0], a[a <= 0]
    sd = float(a.std(ddof=1)) if n > 1 else 0.0
    return {"trades": n, "win_rate": w.size / n, "avg_win_r": float(w.mean()) if w.size else 0.0,
            "avg_loss_r": float(l.mean()) if l.size else 0.0, "expectancy_r": float(a.mean()),
            "t_stat": float(a.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0}


def compute_metrics(rows: list[dict], eq: pd.DataFrame, capital: float) -> dict:
    closed = [t for t in rows if not t.get("open")]
    rs = [t["r"] for t in closed]
    m = r_stats(rs)
    wins = sum(1 for x in rs if x > 0)
    gp = sum(t["pnl"] for t in closed if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    payoff = m["avg_win_r"] / abs(m["avg_loss_r"]) if m["avg_loss_r"] < 0 else float("nan")
    m.update(win_rate_ci95=wilson_interval(wins, m["trades"]), payoff_ratio=payoff,
             profit_factor=gp / gl if gl > 0 else float("nan"),
             breakeven_win_rate=1 / (1 + payoff) if math.isfinite(payoff) and payoff > 0 else float("nan"),
             t1_hit_rate=(sum(t["t1_hit"] for t in closed) / len(closed)) if closed else 0.0,
             avg_bars_held=float(np.mean([t["bars_held"] for t in closed])) if closed else 0.0,
             open_trades=len(rows) - len(closed),
             exit_reasons={k: sum(1 for t in closed if t["exit_reason"] == k) for k in {t["exit_reason"] for t in closed}})
    if eq is not None and len(eq):
        days = max(1, (pd.Timestamp(eq.index[-1]) - pd.Timestamp(eq.index[0])).days)
        end = float(eq["equity"].iloc[-1])
        m.update(end_equity=end, total_return=end / capital - 1,
                 cagr=(end / capital) ** (365.25 / days) - 1 if end > 0 else -1.0,
                 max_drawdown=float(eq["drawdown"].min()), exposure=float((eq["positions"] > 0).mean()))
    m["by_segment"] = {s: r_stats([t["r"] for t in closed if t["segment"] == s]) for s in sorted({t["segment"] for t in closed})}
    m["by_year"] = {y: r_stats([t["r"] for t in closed if t["entry_date"][:4] == y])
                    for y in sorted({t["entry_date"][:4] for t in closed})}
    return m


def verdict(m: dict, target_wr: float, target_win_r: float, demo: bool = False) -> dict:
    """Judges the EDGE (expectancy after costs and its t-stat), not a win-rate target: a breakout system wins well
    under half its trades by design and lives on payoff. The old win-rate / winner-size target is still reported."""
    n, wr, (lo, hi), aw, ex = m["trades"], m["win_rate"], m["win_rate_ci95"], m["avg_win_r"], m["expectancy_r"]
    t, pf = m.get("t_stat", 0.0), m.get("profit_factor", float("nan"))
    if n < 30:
        status, head = "INSUFFICIENT DATA", f"Only {n} out-of-sample trades: too few to tell an edge from luck."
    elif ex > 0 and t >= 2 and not (pf < 1.2):
        status, head = "EDGE CONFIRMED", ("Unseen-data trades made money after costs, and a result this strong is "
                                          "unlikely to be luck (t-stat >= 2).")
    elif ex > 0:
        status, head = "WEAK EDGE", "Positive on average after costs, but not yet distinguishable from luck."
    else:
        status, head = "NO EDGE", "Unseen-data trades lost money on average after costs."
    hit = wr >= target_wr and aw >= target_win_r
    detail = (f"Expectancy {ex:+.2f}R per trade (t-stat {t:.2f}, profit factor {pf:.2f}) over {n} closed trades; win rate "
              f"{wr:.1%} (95% CI {lo:.1%}-{hi:.1%}), average winner {aw:.2f}R. Original target {target_wr:.0%} win rate "
              f"with {target_win_r:g}R winners: {'met' if hit else 'not met'}.")
    if demo:
        head = "DEMO DATA - " + head + " Synthetic prices: says nothing about real stocks."
    return {"status": status, "headline": head, "detail": detail}


def buy_and_hold(index: pd.DataFrame | None, start: str, end: str) -> dict | None:
    """Benchmark: holding the index over [start, end] (total return, CAGR, max drawdown)."""
    if index is None or not len(index):
        return None
    c = index["close"][(index.index >= pd.Timestamp(start)) & (index.index <= pd.Timestamp(end))]
    if len(c) < 2:
        return None
    yrs = max((c.index[-1] - c.index[0]).days / 365.25, 1e-9)
    tot = float(c.iloc[-1] / c.iloc[0] - 1)
    return {"start": c.index[0].strftime("%Y-%m-%d"), "end": c.index[-1].strftime("%Y-%m-%d"), "total_return": tot,
            "cagr": (1 + tot) ** (1 / yrs) - 1, "max_drawdown": float((c / c.cummax() - 1).min())}


ASSUMPTIONS = [
    "Survivorship bias: today's index members applied to past years (flatters results).",
    "Entry at next open after an EOD signal; slippage on entries, stops and time exits.",
    "Stop assumed to fill before target when one bar touches both.",
    "Lower-circuit-locked bars cannot be exited.",
    "Delivery costs: STT 0.1% each side, stamp, exchange/SEBI fees, GST, DP charge per sell.",
    "Fixed-fractional sizing, no leverage, exit cash usable next session.",
    "Circuit bands inferred from prices; historical results dates unknown unless in events file.",
]


def grid_combos(cfg: AppConfig) -> list[dict]:
    g = cfg.backtest.param_grid or {}
    keys = list(g)
    combos = [dict(zip(keys, v)) for v in itertools.product(*(g[k] for k in keys))] if keys else [{}]
    base = {k: v for k, v in {"confirm_offset": 0, "surge_mult": 1.0, "t1_r": (cfg.risk.targets_r or [None])[0],
                              "failed_breakout_bars": cfg.risk.failed_breakout_bars}.items() if k in keys}
    if base in combos:
        combos.remove(base)
    return [base] + combos  # index 0 = defaults


def run_combo(prepared: list[Prepared], cfg_v: AppConfig, events: EventCalendar | None) -> tuple[list[SimTrade], dict]:
    L = lengths(cfg_v)
    trades, skipped = [], {}
    for p in prepared:
        rules = cfg_v.segments[p.segment]
        res = p.darvas(rules.confirm_bars, cfg_v.strategy.breakout_on_close, L["max_box_age"])
        cands = evaluate_events(p.df, p.feats, res, p.segment, rules, cfg_v, symbol=p.inst.symbol, fno=p.inst.fno,
                                events=events, only_from=L["lookback_high"])
        trades += simulate_symbol(p.inst.key, p.df, p.feats, res, cands, cfg_v, segment=p.segment,
                                  max_hold=L["max_hold"], skipped=skipped)
    return trades, skipped


def make_windows(dates: list[str], cfg: AppConfig) -> list[tuple[str, str, str, str]]:
    start, end = pd.Timestamp(dates[0]), pd.Timestamp(dates[-1])
    train = pd.DateOffset(months=int(round(cfg.backtest.train_years * 12)))
    test = pd.DateOffset(months=cfg.backtest.test_months)
    out, t0 = [], start
    while t0 + train < end:
        a, b = t0 + train, min(t0 + train + test, end + pd.Timedelta(days=1))
        if (b - a).days >= 45:
            out.append((t0.strftime("%Y-%m-%d"), a.strftime("%Y-%m-%d"), a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")))
        t0 = t0 + test
    if not out:  # short history: single 60/40 split
        cut = dates[int(len(dates) * 0.6)]
        out.append((dates[0], cut, cut, (end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")))
    return out


def walk_forward(prepared: list[Prepared], cfg: AppConfig, events: EventCalendar | None = None,
                 demo: bool = False, progress: ProgressFn | None = None) -> dict:
    """Rolling windows: pick the param set with the best in-sample t-stat of net R (min trades enforced),
    trade it only on the next unseen window; size the stitched OOS trades through the portfolio."""
    prog = progress or (lambda *a: None)
    L = lengths(cfg)
    prepared = [p for p in prepared if _allowed(cfg, p)]
    if not prepared:
        raise ValueError("no symbols with enough clean history")
    dates = sorted({d for p in prepared for d in p.df.index[L["lookback_high"]:].strftime("%Y-%m-%d")})
    dates = [d for d in dates if (not cfg.backtest.start or d >= cfg.backtest.start)
             and (not cfg.backtest.end or d <= cfg.backtest.end)]
    if len(dates) < 60:
        raise ValueError("backtest period too short after warm-up")
    combos = grid_combos(cfg)
    results = []
    for k, params in enumerate(combos):
        prog(k + 1, len(combos), f"param set {params}")
        tr, sk = run_combo(prepared, apply_params(cfg, params), events)
        results.append({"params": params, "trades": [t for t in tr if dates[0] <= t.entry_date <= dates[-1]], "skipped": sk})
    oos, wrows = [], []
    # fixed rules (an empty parameter grid): nothing is trained, so every bar after the warm-up is test data
    wins = make_windows(dates, cfg) if len(combos) > 1 else         [(dates[0], dates[0], dates[0], (pd.Timestamp(dates[-1]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))]
    for tr0, tr1, te0, te1 in wins:
        best, best_t = 0, None
        for ci, res in enumerate(results):
            st = r_stats([t.net_r for t in res["trades"] if tr0 <= t.entry_date < tr1 and not t.is_open])
            if st["trades"] >= cfg.backtest.min_trades_train and (best_t is None or st["t_stat"] > best_t):
                best, best_t = ci, st["t_stat"]
        ch = results[best]
        test = [t for t in ch["trades"] if te0 <= t.entry_date < te1]
        oos += test
        ist = r_stats([t.net_r for t in ch["trades"] if tr0 <= t.entry_date < tr1 and not t.is_open])
        ost = r_stats([t.net_r for t in test if not t.is_open])
        wrows.append({"train": f"{tr0}..{tr1}", "test": f"{te0}..{te1}", "params": ch["params"],
                      "picked": "t-stat" if best_t is not None else "default", "is_n": ist["trades"],
                      "is_exp": ist["expectancy_r"], "oos_n": ost["trades"], "oos_wr": ost["win_rate"],
                      "oos_exp": ost["expectancy_r"]})
    closes = {p.inst.key: p.df["close"] for p in prepared}
    oos.sort(key=lambda t: (t.entry_date, -t.score))
    eq, rows, cap_sk = run_portfolio(oos, closes, cfg)
    m = compute_metrics(rows, eq, cfg.risk.capital)
    eq_b, rows_b, _ = run_portfolio(sorted(results[0]["trades"], key=lambda t: (t.entry_date, -t.score)), closes, cfg)
    grid = [{"params": r_["params"], **r_stats([t.net_r for t in r_["trades"] if not t.is_open]),
             "t1_hit_rate": (sum(t.t1_hit for t in r_["trades"]) / len(r_["trades"])) if r_["trades"] else 0.0}
            for r_ in results]
    return {"generated_at": datetime.now().isoformat(timespec="seconds"), "demo": demo, "symbols": len(prepared),
            "period": [dates[0], dates[-1]], "round_trip_cost_pct": round(round_trip_cost_pct(cfg.costs), 3),
            "verdict": verdict(m, cfg.backtest.target_win_rate, cfg.backtest.target_avg_win_r, demo),
            "oos": {"metrics": m, "equity": eq, "trades": rows, "skipped_capacity": cap_sk,
                    "skipped_signals": results[0]["skipped"]},
            "baseline": {"metrics": compute_metrics(rows_b, eq_b, cfg.risk.capital), "equity": eq_b},
            "windows": wrows, "grid": grid, "assumptions": ASSUMPTIONS}


# =============================================================================
# 10. ZERODHA KITE CONNECT — session, entry orders, GTT exits, stop updates
# =============================================================================
CREDENTIALS_FILE = Path(__file__).with_name("Credentials.txt")
TOKEN_FILE = Path(__file__).with_name("access_token.json")


def load_credentials(path: Path = CREDENTIALS_FILE) -> dict[str, str]:
    """api_key / api_secret_key from Credentials.txt ("key = value" or "key: value" lines); env vars win."""
    creds = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line or ":" in line:
                k, _, v = line.replace(":", "=", 1).partition("=")
                creds[k.strip().lower()] = v.strip().strip("\"'")
    api_key = os.environ.get("KITE_API_KEY") or creds.get("api_key")
    secret = os.environ.get("KITE_API_SECRET") or creds.get("api_secret_key") or creds.get("api_secret")
    if not api_key:
        raise SystemExit(f"api_key not found in {path}")
    return {"api_key": api_key, "api_secret": secret}


def _token_valid_since() -> datetime:
    """Kite access tokens expire at ~06:00 IST the next morning."""
    now = now_ist()
    six = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if now >= six else six - timedelta(days=1)


def save_access_token(token: str) -> None:
    TOKEN_FILE.write_text(json.dumps({"access_token": token, "created_at": now_ist().isoformat()}), encoding="utf-8")


def load_access_token() -> str | None:
    if os.environ.get("KITE_ACCESS_TOKEN"):
        return os.environ["KITE_ACCESS_TOKEN"]
    if not TOKEN_FILE.exists():
        return None
    d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    created = d.get("created_at") or (d.get("date", "1970-01-01") + "T07:00:00+05:30")
    if datetime.fromisoformat(created) < _token_valid_since():
        return None
    return d.get("access_token")


def kite_login(request_token: str):
    """Exchange a one-time request_token (valid ~2 minutes) for today's access token and save it."""
    from kiteconnect import KiteConnect
    c = load_credentials()
    if not c["api_secret"]:
        raise SystemExit(f"api_secret_key not found in {CREDENTIALS_FILE}")
    kite = KiteConnect(api_key=c["api_key"])
    sess = kite.generate_session(request_token.strip(), api_secret=c["api_secret"])
    save_access_token(sess["access_token"])
    kite.set_access_token(sess["access_token"])
    return kite


def kite_session(api_key: str | None = None, access_token: str | None = None):
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise SystemExit("pip install kiteconnect") from exc
    api_key = api_key or load_credentials()["api_key"]
    access_token = access_token or load_access_token()
    if not access_token:
        kite = KiteConnect(api_key=api_key)
        raise SystemExit("No access token for today. Log in here, then run with --request-token <token>:\n"
                         + kite.login_url())
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def entry_order(sig: dict, cfg: AppConfig) -> dict:
    """CNC LIMIT AMO buy for next morning. Limit = signal close + max chase %, so a big gap-up is not chased
    (mirrors the backtest's skip rule). Kwargs for kite.place_order(**payload)."""
    ex = sig["exchange"]
    limit = round_to_tick(sig["signal_close"] * (1 + cfg.risk.max_chase_pct / 100), ex, "down")
    return {"variety": cfg.kite.entry_variety, "exchange": ex, "tradingsymbol": sig["symbol"],
            "transaction_type": "BUY", "quantity": int(sig["qty"]), "product": "CNC", "order_type": "LIMIT",
            "price": limit, "validity": "DAY", "tag": "darvas"}


def setup_gtt(setup: dict, cfg: AppConfig) -> dict:
    """Single-trigger GTT buy-stop just above the box top. Kwargs for kite.place_gtt(**payload)."""
    ex, trig = setup["exchange"], setup["trigger"]
    limit = round_to_tick(trig * (1 + cfg.kite.setup_limit_buffer_pct / 100), ex, "up")
    return {"trigger_type": "single", "tradingsymbol": setup["symbol"], "exchange": ex, "trigger_values": [trig],
            "last_price": setup["last_close"],
            "orders": [{"exchange": ex, "tradingsymbol": setup["symbol"], "transaction_type": "BUY",
                        "quantity": int(setup["qty"]), "order_type": "LIMIT", "product": "CNC", "price": limit}]}


def exit_gtts(symbol: str, exchange: str, qty: int, entry: float, stop: float, targets: list[dict],
              last_price: float, cfg: AppConfig) -> list[dict]:
    """One OCO GTT per target tranche: [stop, target] both covering that tranche's quantity.

    The stop leg is a LIMIT a little below the trigger (sl_limit_buffer_pct) so it can still fill on a
    fast fall; a gap below that limit leaves it unfilled - watch it. Kwargs for kite.place_gtt(**payload).
    """
    out, left = [], qty
    if not targets:
        sl_limit = round_to_tick(stop * (1 - cfg.kite.sl_limit_buffer_pct / 100), exchange, "down")
        return [{"trigger_type": "single", "tradingsymbol": symbol, "exchange": exchange, "trigger_values": [stop],
                 "last_price": round(last_price, 2), "_tranche": "trail",
                 "orders": [{"exchange": exchange, "tradingsymbol": symbol, "transaction_type": "SELL", "quantity": qty,
                             "order_type": "LIMIT", "product": "CNC", "price": sl_limit}]}]
    for k, t in enumerate(targets):
        q = left if k == len(targets) - 1 else min(left, max(1, int(round(qty * t["fraction"]))))
        left -= q
        if q <= 0:
            continue
        sl_limit = round_to_tick(stop * (1 - cfg.kite.sl_limit_buffer_pct / 100), exchange, "down")
        legs = [{"exchange": exchange, "tradingsymbol": symbol, "transaction_type": "SELL", "quantity": q,
                 "order_type": "LIMIT", "product": "CNC", "price": sl_limit},
                {"exchange": exchange, "tradingsymbol": symbol, "transaction_type": "SELL", "quantity": q,
                 "order_type": "LIMIT", "product": "CNC", "price": t["price"]}]
        out.append({"trigger_type": "two-leg", "tradingsymbol": symbol, "exchange": exchange,
                    "trigger_values": [stop, t["price"]], "last_price": round(last_price, 2), "orders": legs,
                    "_tranche": t["label"]})
    return out


def _gtt_type(kite, name: str):
    return {"single": getattr(kite, "GTT_TYPE_SINGLE", "single"), "two-leg": getattr(kite, "GTT_TYPE_OCO", "two-leg")}[name]


def place_gtt(kite, payload: dict, live: bool) -> str:
    p = {k: v for k, v in payload.items() if not k.startswith("_")}
    p["trigger_type"] = _gtt_type(kite, p["trigger_type"])
    if not live:
        return "DRY-RUN"
    resp = kite.place_gtt(**p)
    return str(resp.get("trigger_id", resp)) if isinstance(resp, dict) else str(resp)


def place_order(kite, payload: dict, live: bool) -> str:
    if not live:
        return "DRY-RUN"
    return str(kite.place_order(**payload))


def stop_update_payloads(kite, symbol: str, exchange: str, new_stop: float, cfg: AppConfig) -> list[dict]:
    """modify_gtt kwargs that raise the stop leg of this symbol's active OCO GTTs to new_stop."""
    out = []
    for g in kite.get_gtts():
        cond = g.get("condition", {})
        if g.get("status") != "active" or g.get("type") != "two-leg" or cond.get("tradingsymbol") != symbol:
            continue
        tv = list(cond.get("trigger_values", []))
        if len(tv) != 2 or new_stop <= tv[0]:
            continue
        orders = [{k: o[k] for k in ("exchange", "tradingsymbol", "transaction_type", "quantity", "order_type",
                                     "product", "price") if k in o} for o in g.get("orders", [])]
        if orders:
            orders[0]["price"] = round_to_tick(new_stop * (1 - cfg.kite.sl_limit_buffer_pct / 100), exchange, "down")
        out.append({"trigger_id": g["id"], "trigger_type": _gtt_type(kite, "two-leg"), "tradingsymbol": symbol,
                    "exchange": exchange, "trigger_values": [new_stop, tv[1]],
                    "last_price": cond.get("last_price"), "orders": orders})
    return out


# =============================================================================
# 11. OUTPUT HELPERS
# =============================================================================
def jsonable(o):
    if o is None or isinstance(o, (bool, str)):
        return o
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return round(float(o), 6) if math.isfinite(float(o)) else None
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.DataFrame):
        return [[str(i), *jsonable(list(r))] for i, r in zip(o.index, o.to_numpy())]
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v) for v in o]
    if is_dataclass(o):
        return jsonable(asdict(o))
    return str(o)


def table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "  (none)"
    def fmt(v):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return "-"
        return f"{v:,.2f}" if isinstance(v, float) else str(v)
    cells = [[fmt(r.get(k)) for k, _ in cols] for r in rows]
    w = [max(len(h), *(len(c[i]) for c in cells)) for i, (_, h) in enumerate(cols)]
    num = [all(isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool) for r in rows) for k, _ in cols]
    line = lambda vals: "  ".join(v.rjust(x) if num[i] else v.ljust(x) for i, (v, x) in enumerate(zip(vals, w)))  # noqa: E731
    return "\n".join([line([h for _, h in cols]), "  ".join("-" * x for x in w)] + [line(c) for c in cells])


def print_scan(result: dict, regime: dict) -> None:
    print(f"\nMarket regime (Nifty 50): {regime.get('state')}  close {regime.get('close')}  "
          f"50DMA {regime.get('sma50')}  200DMA {regime.get('sma200')}")
    buys = [s for s in result["signals"] if s["action"] == "ENTER_LONG"]
    sells = [s for s in result["signals"] if s["action"] != "ENTER_LONG"]
    print(f"\nBUY signals ({len(buys)})")
    print(table(buys, [("symbol", "Symbol"), ("exchange", "Exch"), ("segment", "Seg"), ("date", "Signal"),
                       ("entry", "Entry"), ("stop", "Stop"), ("t1", "T1"), ("t2", "T2"), ("rr", "R:R"),
                       ("rr_net", "R:R net"), ("qty", "Qty"), ("vol_ratio", "Vol x"), ("score", "Score")]))
    print(f"\nSELL signals ({len(sells)})  EXIT_LONG = box breakdown alert for holders; ENTER_SHORT = futures only")
    print(table(sells, [("symbol", "Symbol"), ("action", "Action"), ("date", "Signal"), ("last_close", "Close"),
                        ("box_bottom", "Box bottom"), ("entry", "Entry"), ("stop", "Stop"), ("t1", "T1"),
                        ("vol_ratio", "Vol x"), ("score", "Score")]))
    print(f"\nSETUPS - pressing a box top, GTT buy-stop candidates ({len(result['setups'])})")
    print(table(result["setups"][:30], [("symbol", "Symbol"), ("segment", "Seg"), ("last_close", "Close"),
                                         ("trigger", "Trigger"), ("stop", "Stop"), ("t1", "T1"), ("t2", "T2"),
                                         ("distance_pct", "Dist %"), ("qty", "Qty"), ("score", "Score")]))
    for s in buys[:10]:
        print(f"\n  {s['symbol']}: " + "; ".join(s["notes"]))
        for f in s["flags"]:
            print(f"    ! {f}")
    print("\nScore is a ranking heuristic (0-100), not a probability of success.")


def print_backtest(rep: dict) -> None:
    m, v = rep["oos"]["metrics"], rep["verdict"]
    print(f"\nWALK-FORWARD BACKTEST  {rep['period'][0]} -> {rep['period'][1]}  ({rep['symbols']} symbols)")
    print(f"Round-trip cost at ₹1 lakh: {rep['round_trip_cost_pct']:.3f}%")
    print(table(rep["windows"], [("train", "Train"), ("test", "Test"), ("params", "Chosen params"), ("picked", "By"),
                                 ("is_n", "IS n"), ("is_exp", "IS exp R"), ("oos_n", "OOS n"),
                                 ("oos_wr", "OOS win"), ("oos_exp", "OOS exp R")]))

    def block(title, mm):
        ci = mm.get("win_rate_ci95", (0, 0))
        print(f"\n{title}")
        print(f"  trades {mm['trades']}  win rate {mm['win_rate']:.1%} (95% CI {ci[0]:.1%}-{ci[1]:.1%})  "
              f"avg win {mm['avg_win_r']:.2f}R  avg loss {mm['avg_loss_r']:.2f}R  expectancy {mm['expectancy_r']:+.3f}R")
        print(f"  profit factor {mm.get('profit_factor', float('nan')):.2f}  max DD {mm.get('max_drawdown', 0):.1%}  "
              f"CAGR {mm.get('cagr', 0):.1%}  T1 hit {mm['t1_hit_rate']:.1%}  avg hold {mm['avg_bars_held']:.1f} bars")
        print(f"  exits: {mm.get('exit_reasons')}")
    block("OUT-OF-SAMPLE (stitched test windows, portfolio-sized)", m)
    block("Full period, default parameters (no selection)", rep["baseline"]["metrics"])
    print("\nParameter grid, full period, unsized (in-sample view of win rate vs winner size - do not pick from this):")
    print(table(sorted(rep["grid"], key=lambda g: -g["expectancy_r"]),
                [("params", "Params"), ("trades", "n"), ("win_rate", "Win"), ("avg_win_r", "Avg win R"),
                 ("expectancy_r", "Exp R"), ("t1_hit_rate", "T1 hit")]))
    print(f"\nVERDICT: {v['status']}\n  {v['headline']}\n  {v['detail']}")
    print("\nAssumptions:\n  - " + "\n  - ".join(rep["assumptions"]))


# =============================================================================
# 12. SELF-TEST (unit tests for box detection and friends) + CLI
# =============================================================================
def _bars(highs, lows, closes=None):
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return np.array(highs, float), np.array(lows, float), np.array(closes, float)


def selftest(verbose: bool = True) -> bool:
    tests = []

    def test(fn):
        tests.append(fn)
        return fn

    @test
    def textbook_box_and_breakout():
        # top 110 at bar 5; bars 6-8 lower -> top confirmed at 8; lowest low after top = 100 at bar 7;
        # bars 8,9,10 hold above -> box complete at bar 10; close 112 at bar 11 -> breakout.
        H = [100, 102, 104, 106, 108, 110, 109, 105, 107, 108, 107, 113]
        Lw = [98, 100, 102, 104, 106, 107, 104, 100, 103, 104, 103, 107]
        C = [99, 101, 103, 105, 107, 108, 105, 101, 106, 106, 105, 112]
        r = detect_boxes(*_bars(H, Lw, C), confirm_bars=3)
        assert len(r.boxes) == 1, r.boxes
        b = r.boxes[0]
        assert (b.top, b.bottom, b.top_idx, b.bottom_idx, b.confirm_idx) == (110, 100, 5, 7, 10), b
        assert r.event[11] == 1 and r.event[:11].sum() == 0
        assert b.status == "breakout" and b.end_idx == 11
        assert np.isnan(r.active_top[9]) and r.active_top[10] == 110 and np.isnan(r.active_top[11])
        assert r.trail_bottom[11] == 100 and r.trail_confirm[11] == 10

    @test
    def new_high_voids_half_built_box():
        H = [100, 110, 108, 107, 106, 111, 109, 108, 107]
        Lw = [95, 105, 103, 101, 102, 106, 104, 103, 102]
        r = detect_boxes(*_bars(H, Lw), confirm_bars=3)
        assert not r.boxes, "bar 5 exceeded the top before the bottom confirmed"
        assert r.forming["phase"] == "seeking_bottom" and r.forming["top"] == 111

    @test
    def intraday_poke_is_not_a_breakout_on_close():
        H = [100, 110, 109, 105, 107, 108, 107, 111]
        Lw = [95, 107, 104, 100, 103, 104, 103, 106]
        C = [98, 108, 105, 101, 106, 106, 105, 109]  # last bar trades 111 but closes 109 < 110
        r = detect_boxes(*_bars(H, Lw, C), confirm_bars=3, breakout_on_close=True)
        assert len(r.boxes) == 1 and r.event.sum() == 0 and r.boxes[0].status == "active"
        r2 = detect_boxes(*_bars(H, Lw, C), confirm_bars=3, breakout_on_close=False)
        assert r2.event[-1] == 1

    @test
    def breakdown_event():
        H = [100, 110, 109, 105, 107, 108, 107, 103]
        Lw = [95, 107, 104, 100, 103, 104, 103, 97]
        C = [98, 108, 105, 101, 106, 106, 105, 98]
        r = detect_boxes(*_bars(H, Lw, C), confirm_bars=3)
        assert r.event[-1] == -1 and r.boxes[0].status == "breakdown"

    @test
    def stale_box_expires():
        H = [100, 110, 109, 105, 107, 108, 107] + [107] * 10
        Lw = [95, 107, 104, 100, 103, 104, 103] + [103] * 10
        r = detect_boxes(*_bars(H, Lw), confirm_bars=3, max_box_age=5)
        assert r.boxes[0].status == "expired" and r.boxes[0].end_idx == 6 + 6

    @test
    def confirm_bars_parameter():
        H = [100, 110, 109, 105, 107, 108, 107, 106]
        Lw = [95, 107, 104, 100, 103, 104, 103, 104]
        assert detect_boxes(*_bars(H, Lw), confirm_bars=3).boxes[0].confirm_idx == 6
        assert detect_boxes(*_bars(H, Lw), confirm_bars=4).boxes[0].confirm_idx == 7

    @test
    def no_lookahead():
        rng = np.random.default_rng(3)
        c = 100 * np.exp(np.cumsum(0.015 * rng.standard_normal(600)))
        h, l = c * (1 + 0.01 * rng.random(600)), c * (1 - 0.01 * rng.random(600))
        full = detect_boxes(h, l, c, 3)
        for cut in (150, 333, 599):
            part = detect_boxes(h[:cut], l[:cut], c[:cut], 3)
            assert np.array_equal(part.event, full.event[:cut])
            assert np.allclose(part.active_top, full.active_top[:cut], equal_nan=True)
            assert np.allclose(part.trail_bottom, full.trail_bottom[:cut], equal_nan=True)

    @test
    def tick_rounding():
        assert tick_size(249.99) == 0.01 and tick_size(250) == 0.05 and tick_size(1000) == 0.05
        assert tick_size(1000.5) == 0.10 and tick_size(7000) == 0.50 and tick_size(25000) == 5.0
        assert round_to_tick(1234.56, direction="down") == 1234.5 and round_to_tick(1234.51, direction="up") == 1234.6

    @test
    def plan_uses_tighter_stop_and_caps():
        r = RiskConfig(atr_stop_mult=2.0, targets_r=[1.5, 3.0], stop_mode="tighter", stop_buffer_pct=0.5, min_stop_atr=1.0, max_stop_pct=None, target_fractions=[0.5, 0.5], final_leg="target", move_stop_to_breakeven=True)   # the pre-Nov-2026 rule set
        p = build_plan("BUY", 500, 495, 450, 8.0, r, CostConfig())  # box stop 447.75, ATR stop 484 -> ATR
        assert p.stop == 484.0 and "ATR" in p.stop_basis
        assert p.qty == math.floor(min(1_000_000 * 0.01 / 16, 1_000_000 * 0.2 / 500))
        assert p.targets[0]["price"] == 524.0 and p.targets[1]["price"] == 548.0
        assert sum(t["qty"] for t in p.targets) == p.qty
        p2 = build_plan("BUY", 500, 499, 497, 8.0, r, CostConfig())  # box stop 494.5 < 1 ATR away -> widened
        assert p2.stop == 492.0 and "widened" in p2.stop_basis
        p3 = build_plan("BUY", 500, 495, 450, 8.0, r, CostConfig(), adtv_cr=0.5, rules=_default_segments()["SMALL"])
        assert p3.capped_by == "liquidity" and p3.qty == 50

    @test
    def simulator_stop_first_and_gaps():
        idx = pd.bdate_range("2024-01-01", periods=6)
        df = pd.DataFrame({"open": [100, 101, 102, 103, 104, 90], "high": [101, 103, 110, 104, 105, 91],
                           "low": [99, 100, 90, 102, 103, 89], "close": [100, 102, 103, 103, 104, 90],
                           "volume": 1e6}, index=idx)
        feats = pd.DataFrame({"uc_locked": False, "lc_locked": False, "atr": 2.0, "adtv_cr": 50.0}, index=idx)
        res = detect_boxes(df["high"], df["low"], df["close"], 3)
        box = Box(101.5, 97.0, 0, 0, 0, 0)
        cfg = load_config()
        cfg.risk = RiskConfig(slippage_pct=0.0, atr_stop_mult=2.0, targets_r=[2.0, 4.0], stop_mode="tighter", stop_buffer_pct=0.5, min_stop_atr=1.0, max_stop_pct=None, target_fractions=[0.5, 0.5], final_leg="target", move_stop_to_breakeven=True)
        cfg.strategy.entry_mode = "next_open"   # the pre-Nov-2026 rule set
        cand = Candidate(1, idx[1], "BUY", "ENTER_LONG", box, True)
        t = simulate_symbol("X", df, feats, res, [cand], cfg, segment="LARGE", max_hold=40)[0]
        # entry 102 at bar 2 open; ATR stop 98 (tighter than box 96.5); bar 2 hits both 98 and T1 -> stop first
        assert t.entry == 102 and t.stop0 == 98 and t.exit_reason == "stop" and t.fills[0].price == 98
        df2 = df.copy()
        df2.loc[idx[2], ["high", "low", "close"]] = [104, 101, 103]
        t2 = simulate_symbol("X", df2, feats, res, [cand], cfg, segment="LARGE", max_hold=40)[0]
        assert t2.exit_reason == "stop" and t2.fills[-1].price == 90 and t2.exit_idx == 5  # gap-down fills at open
        feats2 = feats.copy()
        feats2.loc[idx[5], "lc_locked"] = True
        t3 = simulate_symbol("X", df2, feats2, res, [cand], cfg, segment="LARGE", max_hold=40)[0]
        assert t3.is_open, "lower circuit lock on the last bar: cannot exit"

    @test
    def quant_stop_limit_entry_and_cap():
        idx = pd.bdate_range("2024-01-01", periods=6)
        df = pd.DataFrame({"open": [100, 101, 103, 104, 106, 107], "high": [101, 104, 106, 107, 108, 109],
                           "low": [99, 100, 102, 103, 105, 106], "close": [100, 103, 105, 106, 107, 108],
                           "volume": 1e6}, index=idx)
        feats = pd.DataFrame({"uc_locked": False, "lc_locked": False, "atr": 2.0, "adtv_cr": 50.0}, index=idx)
        res = detect_boxes(df["high"], df["low"], df["close"], 3)
        cfg = load_config()
        cfg.risk.slippage_pct = 0.0
        cand = Candidate(1, idx[1], "BUY", "ENTER_LONG", Box(102.0, 80.0, 0, 0, 0, 0), True)
        t = simulate_symbol("X", df, feats, res, [cand], cfg, segment="LARGE", max_hold=40)[0]
        trig = 104 * 1.0005                       # 0.05% above the breakout day's high
        assert abs(t.entry - trig) < 1e-9 and t.entry_idx == 2, (t.entry, t.entry_idx)
        assert abs(t.stop0 - trig * 0.93) < 1e-9, t.stop0   # box bottom 80 is >7% away: capped at 7%
        assert t.is_open and not t.t1_hit          # no fixed target: still riding
        df2 = df.copy()
        df2.loc[idx[2], "open"] = 110              # gap beyond the 0.5% limit and never back to it: not chased
        df2.loc[idx[2], "low"] = 109
        df2.loc[idx[2:], "close"] = 101            # then closes back inside the box: order cancelled
        sk = {}
        assert not simulate_symbol("X", df2, feats, res, [cand], cfg, segment="LARGE", max_hold=40, skipped=sk)
        assert sk == {"stop-limit not filled": 1}, sk
        p = build_plan("BUY", 500, 495, 480, 8.0, cfg.risk, cfg.costs)
        assert p.stop == 480 and p.targets == [] and "box" in p.stop_basis   # strictly the box bottom
        g = exit_gtts("ABC", "NSE", p.qty, p.entry, p.stop, p.targets, 500, cfg)
        assert len(g) == 1 and g[0]["trigger_type"] == "single" and g[0]["orders"][0]["quantity"] == p.qty

    @test
    def circuit_detection():
        idx = pd.bdate_range("2024-01-01", periods=3)
        df = pd.DataFrame({"open": [100, 105, 104], "high": [100, 105, 110.25], "low": [100, 105, 104],
                           "close": [100, 105, 110.25], "volume": 1}, index=idx)
        f = circuit_flags(df)
        assert f["uc"].iloc[1] and f["uc_locked"].iloc[1] and f["uc"].iloc[2] and not f["uc_locked"].iloc[2]

    @test
    def wilson_and_verdict():
        lo, hi = wilson_interval(70, 100)
        assert 0.60 < lo < 0.61 and 0.78 < hi < 0.79
        v = verdict({"trades": 100, "win_rate": 0.7, "win_rate_ci95": (lo, hi), "avg_win_r": 2.5,
                     "expectancy_r": 1.2, "t_stat": 5.0, "profit_factor": 3.0}, 0.70, 2.0)
        assert v["status"] == "EDGE CONFIRMED" and "met" in v["detail"]
        v2 = verdict({"trades": 300, "win_rate": 0.38, "win_rate_ci95": (0.33, 0.43), "avg_win_r": 1.9,
                      "expectancy_r": 0.05, "t_stat": 0.8, "profit_factor": 1.1}, 0.70, 2.0)
        assert v2["status"] == "WEAK EDGE"
        assert verdict({"trades": 10, "win_rate": 0.5, "win_rate_ci95": (0.2, 0.8), "avg_win_r": 1,
                        "expectancy_r": 0.1}, 0.7, 2.0)["status"] == "INSUFFICIENT DATA"

    @test
    def costs_delivery():
        c = order_costs(100_000, "buy", CostConfig()) + order_costs(100_000, "sell", CostConfig())
        assert 220 < c < 250, c  # STT 200 + stamp 15 + fees + DP

    @test
    def exit_gtt_quantities():
        cfg = load_config()
        cfg.risk = RiskConfig(targets_r=[2.0, 4.0], stop_mode="tighter", stop_buffer_pct=0.5, min_stop_atr=1.0, max_stop_pct=None, target_fractions=[0.5, 0.5], final_leg="target", move_stop_to_breakeven=True)
        p = build_plan("BUY", 500, 495, 450, 8.0, cfg.risk, cfg.costs)
        g = exit_gtts("ABC", "NSE", p.qty, p.entry, p.stop, p.targets, 500, cfg)
        assert len(g) == 2 and sum(x["orders"][0]["quantity"] for x in g) == p.qty
        assert all(x["trigger_values"][0] < x["trigger_values"][1] for x in g)

    @test
    def demo_end_to_end():
        cfg = load_config()
        src = SyntheticData()
        prepared, rejected, index = load_prepared(src, src.universe(), cfg)
        assert len(prepared) >= 20, rejected
        res = scan(prepared, cfg)
        buys = [s for s in res["signals"] if s["action"] == "ENTER_LONG"]
        assert buys and res["setups"], (len(buys), len(res["setups"]))
        for s in buys:
            assert s["stop"] < s["entry"] and s["t1"] is None and s["qty"] > 0   # trail only: no fixed targets

    ok = 0
    for fn in tests:
        try:
            fn()
            ok += 1
            if verbose:
                print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # report and continue
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(tests)} tests passed")
    return ok == len(tests)


def _progress(done: int, total: int, msg: str) -> None:
    if sys.stderr.isatty():
        print(f"\r  [{done}/{total}] {msg[:70]:<70}", end="", file=sys.stderr, flush=True)
        if done >= total:
            print(file=sys.stderr)
    elif done == total or done % 50 == 0:
        print(f"  [{done}/{total}] {msg}", file=sys.stderr, flush=True)


def _source_and_universe(args, cfg) -> tuple[DataSource, list[Instrument], Any]:
    if getattr(args, "demo", False):
        return SyntheticData(), SyntheticData.universe(), None
    insts, warns = load_universe(args.universe, cfg)
    for w in warns:
        log.warning(w)
    if args.csv_dir:
        return CSVFolderData(args.csv_dir, args.index_csv), insts, None
    kite = kite_session()
    src = KiteData(kite, cfg)
    missing = src.annotate(insts)
    if missing:
        log.warning("%d symbols not found on Kite (e.g. %s)", len(missing), ", ".join(missing[:5]))
    return src, insts, kite


def _held(kite) -> dict[str, int]:
    """EXCHANGE:SYMBOL -> quantity across holdings (incl. T+1 unsettled) and today's CNC buys."""
    held: dict[str, int] = {}
    for h in kite.holdings():
        q = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
        if q > 0:
            held[f"{h['exchange']}:{h['tradingsymbol']}"] = q
    for pos in kite.positions().get("net", []):
        if pos.get("product") == "CNC" and int(pos.get("quantity", 0)) > 0:
            k = f"{pos['exchange']}:{pos['tradingsymbol']}"
            held[k] = max(held.get(k, 0), int(pos["quantity"]))
    return held


def _state(cfg: AppConfig) -> Path:
    p = Path(cfg.kite.cache_dir) / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_rank(args, cfg: AppConfig, events: EventCalendar) -> int:
    cfg.kite.history_years = args.years
    kite = kite_session()
    src = KiteData(kite, cfg)
    uni = args.universe
    index_keys = CORE_INDICES   # only these indices' prices are fetched, unless the universe is allindices
    if uni.lower() in ("allindices", "core") or uni.upper() in INDEX_CONSTITUENTS or uni.upper() in LOCAL_INDEX_LISTS:
        names = None if uni.lower() == "allindices" else CORE_NAMES if uni.lower() == "core" else [uni.upper()]
        cons, warns = load_index_constituents(cfg, names)
        insts = universe_from_indices(cons)
        if uni.lower() == "allindices":
            index_keys = None
    else:
        cons, _ = load_index_constituents(cfg, CORE_NAMES)   # for sector names of the stocks
        insts, warns = load_universe(uni, cfg)
    for w in warns:
        log.warning(w)
    missing = src.annotate(insts)
    print(f"Universe: {len(insts)} stocks from {len(cons) or 1} index list(s); "
          f"{sum(i.nse_token is not None for i in insts)} on NSE, {sum(i.bse_token is not None for i in insts)} on BSE, "
          f"{len(missing)} unknown to Kite", file=sys.stderr)
    out_dir = Path(args.out_dir)
    stamp = now_ist().strftime("%Y%m%d_%H%M")
    write_csv([{"symbol": i.symbol, "name": i.name, "data_exchange": i.exchange, "nse_token": i.nse_token,
                "bse_token": i.bse_token, "fno": i.fno, "segment": i.segment, "indices": "; ".join(i.indices)}
               for i in insts], out_dir / "instrument_tokens.csv")
    tok = {i.symbol: i for i in insts}
    write_csv([{"index": idx, "index_token": src.indices.get(f"NSE:{idx}"), "symbol": m["symbol"], "name": m["name"],
                "industry": m["industry"], "isin": m["isin"],
                "nse_token": tok[m["symbol"]].nse_token if m["symbol"] in tok else None,
                "bse_token": tok[m["symbol"]].bse_token if m["symbol"] in tok else None}
               for idx, ms in cons.items() for m in ms], out_dir / "index_constituents.csv")

    end = now_ist().date()
    start = end - timedelta(days=int(args.years * 365.25))
    idx_data = {}
    if not args.no_indices:
        print(f"Fetching daily history for {len(index_keys or src.indices)} NSE/BSE indices...", file=sys.stderr)
        idx_data = src.fetch_indices(start, end, _progress, only=index_keys)
        if not session_closed(close_hhmm=cfg.kite.session_close_ist):
            idx_data = {k: v[v.index < pd.Timestamp(end)] for k, v in idx_data.items()}
    print(f"Fetching daily history for {len(insts)} stocks...", file=sys.stderr)
    prepared, rejected, nifty = load_prepared(src, insts, cfg, progress=_progress)
    print(f"{len(prepared)} stocks analysed, {len(rejected)} skipped (too little history / stale / illiquid data)",
          file=sys.stderr)

    brk = rank_breakouts(prepared, cfg, args.window, events)
    if args.times > 0 and cfg.strategy.timeframe == "1d":
        print(f"Looking up intraday breakout time for the newest {min(args.times, len(brk))}...", file=sys.stderr)
        add_breakout_times(src, brk, args.times, args.interval)
    ready = rank_ready(prepared, cfg, args.ready_pct)
    isum = index_summary(idx_data, cons, brk, ready, cfg, args.window)
    sectors = sector_rotation(prepared, cons, nifty, brk, ready)
    industry = {r["symbol"]: r["industry"] for rows in cons.values() for r in rows if r.get("industry")}
    outcomes = track_breakouts(prepared, cfg, events, args.track_bars)
    for r in brk + ready + outcomes:
        r["sector"] = industry.get(r["symbol"], "")

    reg = regime_summary(nifty)
    print(f"\nMarket regime (Nifty 50): {reg.get('state')}  close {reg.get('close')}  "
          f"50DMA {reg.get('sma50')}  200DMA {reg.get('sma200')}  as of {reg.get('date')}")
    if isum:
        print(f"\nINDICES ({len(isum)}) - price trend; BUY / SELL / Ready count member stocks' Darvas signals")
        print(table(isum[:args.top],
                    [("index", "Index"), ("exchange", "Exch"), ("last_close", "Close"), ("chg_1d_pct", "1d %"),
                     ("chg_1m_pct", "1m %"), ("chg_3m_pct", "3m %"), ("above_50dma", ">50D"),
                     ("above_200dma", ">200D"), ("members", "Stocks"), ("member_buys", "BUY"),
                     ("member_sells", "SELL"), ("member_ready", "Ready")]))
    print(f"\nMOST RECENT DARVAS BREAKOUTS ({len(brk)} in last {args.window} bars) - newest first")
    print(table(brk[:args.top], [("rank", "#"), ("symbol", "Symbol"), ("signal", "Signal"),
                                  ("breakout_date", "Date"), ("breakout_time", "Time"), ("box_top", "Box top"),
                                  ("box_bottom", "Box bot"), ("breakout_close", "BO close"), ("last_close", "Close"),
                                  ("status", "Status"), ("vol_ratio", "Vol x"), ("score", "Score"),
                                  ("filters", "Filters")]))
    print(f"\nDARVAS-READY STOCKS ({len(ready)}) - ranked by bullishness (close within "
          f"{args.ready_pct if args.ready_pct is not None else cfg.strategy.setup_proximity_pct:g}% under box top)")
    print(table(ready[:args.top], [("rank", "#"), ("symbol", "Symbol"), ("signal", "Signal"),
                                    ("bullishness", "Bull"), ("last_close", "Close"), ("trigger", "Trigger"),
                                    ("distance_pct", "Dist %"), ("box_bottom", "Box bot"), ("stop", "Stop"),
                                    ("t1", "T1"), ("t2", "T2"), ("box_since", "Box since"), ("filters", "Filters")]))
    files = {"breakouts": brk, "darvas_ready": ready, "index_summary": isum, "outcomes": outcomes}
    for name, rows in files.items():
        write_csv(rows, out_dir / f"{name}_{stamp}.csv")
    (out_dir / f"sector_rotation_{stamp}.json").write_text(json.dumps(sectors, indent=1), encoding="utf-8")
    caps = sector_rotation(prepared, cons, nifty, brk, ready, group_of={p.inst.symbol: p.segment for p in prepared})
    leadership = {"stocks": stock_stats(prepared, cons), "rotation": {"sector": sectors, "segment": caps},
                  "boxes": [{**b, "sector": industry.get(b["symbol"], "")} for b in active_boxes(prepared, cfg)],
                  "asof": nifty.index[-1].strftime("%Y-%m-%d") if nifty is not None and len(nifty) else None,
                  "regime": regime_summary(nifty),   # live check: Nifty above `sma50` = longs allowed
                  "params": {"min_rs_pct": cfg.strategy.min_rs_pct, "atr_stop_mult": cfg.risk.atr_stop_mult,
                             "stop_buffer_pct": cfg.risk.stop_buffer_pct, "min_stop_atr": cfg.risk.min_stop_atr,
                             "targets_r": cfg.risk.targets_r, "max_chase_pct": cfg.risk.max_chase_pct,
                             "volume_surge": {k: v.volume_surge for k, v in cfg.segments.items()}}}
    (out_dir / f"leadership_{stamp}.json").write_text(json.dumps(jsonable(leadership)), encoding="utf-8")
    if sectors:
        print(f"\nSECTOR ROTATION vs Nifty 50 ({len(sectors)} sectors, weekly)")
        print(table(sectors, [("sector", "Sector"), ("quadrant", "Quadrant"), ("rs_ratio", "RS-Ratio"),
                              ("rs_momentum", "RS-Mom"), ("chg_1m_pct", "1m %"), ("rel_3m_pct", "vs Nifty 3m"),
                              ("members", "Stocks"), ("buys", "BUY"), ("ready", "Ready")]))
    print(f"\nFiles in {out_dir.resolve()}: instrument_tokens.csv, index_constituents.csv, "
          + ", ".join(f"{n}_{stamp}.csv" for n in files) + f", sector_rotation_{stamp}.json")
    print("Breakout time = start of the first intraday candle that traded beyond the box; the DAILY CLOSE confirms "
          "the signal. Score/bullishness are ranking heuristics (0-100), not probabilities.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Darvas Box swing algo for NSE/BSE (Zerodha Kite Connect)")
    ap.add_argument("--config", help="JSON overrides for AppConfig")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    sub.add_parser("demo")
    lg = sub.add_parser("login")
    lg.add_argument("--request-token")
    for name in ("scan", "backtest"):
        sp = sub.add_parser(name)
        sp.add_argument("--universe", default="nifty500", help="nifty50|nifty100|nifty500|midsmall|file.csv")
        sp.add_argument("--csv-dir", help="offline OHLCV folder instead of Kite (EXCHANGE_SYMBOL.csv)")
        sp.add_argument("--index-csv", help="offline Nifty 50 OHLCV CSV")
        sp.add_argument("--demo", action="store_true")
        sp.add_argument("--out", help="write results JSON here")
        if name == "scan":
            sp.add_argument("--capital-from-kite", action="store_true", help="size with live equity cash")
    od = sub.add_parser("orders", help="build/place entry AMOs and setup GTTs from the last scan")
    od.add_argument("--live", action="store_true")
    od.add_argument("--no-setups", action="store_true")
    pr = sub.add_parser("protect", help="place OCO GTT stop/targets for entries that have filled")
    pr.add_argument("--live", action="store_true")
    tr = sub.add_parser("trail", help="recommend (and optionally apply) stop raises for holdings")
    tr.add_argument("--live", action="store_true")
    tr.add_argument("--universe", default="nifty500")
    tr.add_argument("--csv-dir")
    tr.add_argument("--index-csv")
    rk = sub.add_parser("rank", help="all indices + their stocks: recent Darvas breakouts and Darvas-ready ranking")
    rk.add_argument("--universe", default="core", help="core (SENSEX, NIFTY 50, NIFTY BANK, NIFTY MIDCAP 100, NIFTY SMLCAP 100) | one of those | allindices | file.csv")
    rk.add_argument("--years", type=float, default=2.0, help="daily history to fetch (min ~1.3 for 52-week checks)")
    rk.add_argument("--window", type=int, default=20, help="breakouts within the last N daily bars")
    rk.add_argument("--ready-pct", type=float, default=None, help="Darvas-ready = close within this %% under box top")
    rk.add_argument("--times", type=int, default=50, help="look up intraday breakout time for the newest N (0=off)")
    rk.add_argument("--interval", default="5minute", help="candle size for breakout time (minute|5minute|15minute)")
    rk.add_argument("--no-indices", action="store_true", help="skip fetching every index's own price history")
    rk.add_argument("--top", type=int, default=40, help="rows to print per table (CSV files have all)")
    rk.add_argument("--out-dir", default="reports")
    rk.add_argument("--track-bars", type=int, default=60, help="follow BUY breakouts of the last N bars to SL / targets")
    for p_ in (lg, rk, od, pr, tr, *[sub.choices[n] for n in ("scan", "backtest")]):
        if p_ is not lg:
            p_.add_argument("--request-token", help="log in first with this one-time Kite request_token")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/redirects default to cp1252, which has no ₹
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    events = EventCalendar.load(cfg.events_file)

    if args.cmd == "selftest":
        return 0 if selftest() else 1

    if getattr(args, "request_token", None):
        kite_login(args.request_token)
        print(f"Logged in; access token saved to {TOKEN_FILE.name} (valid until ~06:00 IST).", file=sys.stderr)
        if args.cmd == "login":
            return 0

    if args.cmd == "login":
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=load_credentials()["api_key"])
        print("Open, log in, and copy request_token from the redirect URL, then run\n"
              "  python darvas_algo.py login --request-token <token>\n" + kite.login_url())
        return 0

    if args.cmd == "rank":
        return run_rank(args, cfg, events)

    if args.cmd == "demo":
        src, insts = SyntheticData(), SyntheticData.universe()
        prepared, rejected, index = load_prepared(src, insts, cfg)
        print_scan(scan(prepared, cfg, events), regime_summary(index))
        print_backtest(walk_forward(prepared, cfg, events, demo=True, progress=_progress))
        return 0

    if args.cmd in ("scan", "backtest"):
        src, insts, kite = _source_and_universe(args, cfg)
        prepared, rejected, index = load_prepared(src, insts, cfg, progress=_progress)
        print(f"{len(prepared)} symbols ready, {len(rejected)} skipped (data/quality).", file=sys.stderr)
        if args.cmd == "scan":
            equity = None
            if getattr(args, "capital_from_kite", False) and kite is not None:
                equity = float(kite.margins("equity")["net"])
            res = scan(prepared, cfg, events, equity)
            reg = regime_summary(index)
            print_scan(res, reg)
            payload = jsonable({"generated_at": datetime.now().isoformat(timespec="seconds"), "regime": reg,
                                "demo": src.is_demo, **res})
            (_state(cfg) / "last_scan.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        else:
            payload = walk_forward(prepared, cfg, events, demo=src.is_demo, progress=_progress)
            oos_eq, base_eq = payload["oos"]["equity"], payload["baseline"]["equity"]
            payload["benchmark"] = {   # holding Nifty 50 over the same dates as each equity curve
                "name": cfg.kite.index_symbol,
                "oos": buy_and_hold(index, oos_eq.index[0], oos_eq.index[-1]) if len(oos_eq) else None,
                "baseline": buy_and_hold(index, base_eq.index[0], base_eq.index[-1]) if len(base_eq) else None}
            print_backtest(payload)
            payload = jsonable(payload)
        if args.out:
            Path(args.out).write_text(json.dumps(jsonable(payload), indent=1), encoding="utf-8")
            print(f"\nwrote {args.out}")
        return 0

    if args.cmd in ("orders", "protect"):
        scan_file = _state(cfg) / "last_scan.json"
        if not scan_file.exists():
            raise SystemExit("run `scan` first")
        last = json.loads(scan_file.read_text(encoding="utf-8"))
        if last.get("demo"):
            raise SystemExit("last scan was on demo data; refusing to build real orders")
        use_kite = args.live or args.cmd == "protect" or bool(os.environ.get("KITE_ACCESS_TOKEN"))
        kite = kite_session() if use_kite else None  # dry-run still reads holdings/cash when logged in
        if args.live:
            ans = input(f"Place LIVE {args.cmd} on Zerodha? type YES: ")
            if ans.strip() != "YES":
                return 1
        plans_file = _state(cfg) / "plans.json"
        plans = json.loads(plans_file.read_text(encoding="utf-8")) if plans_file.exists() else {}
        if args.cmd == "orders":
            held = _held(kite) if kite is not None else {}
            busy = set(held) | {k for k, v in plans.items() if v.get("status") in ("pending", "protected")}
            slots = max(0, cfg.risk.max_open_positions - len(busy))
            budget = float(kite.margins("equity")["net"]) if kite is not None else cfg.risk.capital
            print(f"{slots} free position slot(s), budget ₹{budget:,.0f} "
                  f"({len(busy)} held/pending, max {cfg.risk.max_open_positions})")

            def take(sym_key: str, qty: int, price: float) -> int:
                nonlocal slots, budget
                if slots <= 0 or sym_key in busy or qty < 1:
                    return 0
                q = min(qty, math.floor(budget / (price * 1.0012)))
                if q < 1:
                    return 0
                slots, budget = slots - 1, budget - q * price * 1.0012
                busy.add(sym_key)
                return q

            for s in sorted(last["signals"], key=lambda x: -x["score"]):
                if s["action"] != "ENTER_LONG" or not s.get("qty"):
                    continue
                pl = entry_order(s, cfg)
                pl["quantity"] = take(f"{s['exchange']}:{s['symbol']}", int(s["qty"]), pl["price"])
                if not pl["quantity"]:
                    print(f"skip {s['symbol']}: no slot/budget or already held")
                    continue
                s["plan"]["qty"] = pl["quantity"]
                oid = place_order(kite, pl, args.live)
                print(f"ENTRY {s['symbol']}: {json.dumps(pl)} -> {oid}")
                plans[f"{s['exchange']}:{s['symbol']}"] = {**s["plan"], "symbol": s["symbol"], "exchange": s["exchange"],
                                                           "order_id": oid, "signal_date": s["date"], "status": "pending"}
            if not args.no_setups:
                for s in sorted(last["setups"], key=lambda x: -x["score"]):
                    if not s.get("qty"):
                        continue
                    pl = setup_gtt(s, cfg)
                    q = take(f"{s['exchange']}:{s['symbol']}", int(s["qty"]), pl["orders"][0]["price"])
                    if not q:
                        continue
                    pl["orders"][0]["quantity"] = s["plan"]["qty"] = q
                    gid = place_gtt(kite, pl, args.live)
                    print(f"SETUP GTT {s['symbol']}: {json.dumps(pl)} -> {gid}")
                    plans[f"{s['exchange']}:{s['symbol']}"] = {**s["plan"], "symbol": s["symbol"],
                                                               "exchange": s["exchange"], "gtt_id": gid,
                                                               "signal_date": None, "status": "pending"}
            if args.live:
                plans_file.write_text(json.dumps(plans, indent=1), encoding="utf-8")
            return 0
        # protect: positions/holdings that match a pending plan get OCO GTTs sized to the filled quantity
        held = _held(kite)
        for key, pl in plans.items():
            if pl.get("status") != "pending" or held.get(key, 0) <= 0:
                continue
            qty = min(held[key], int(pl["qty"]))
            ltp = kite.ltp([key])[key]["last_price"]
            for g in exit_gtts(pl["symbol"], pl["exchange"], qty, pl["entry"], pl["stop"], pl["targets"], ltp, cfg):
                gid = place_gtt(kite, g, args.live)
                print(f"PROTECT {key} {g['_tranche']}: trigger {g['trigger_values']} qty "
                      f"{g['orders'][0]['quantity']} -> {gid}")
            if args.live:
                pl["status"], pl["entry_date"] = "protected", now_ist().date().isoformat()
        if args.live:
            plans_file.write_text(json.dumps(plans, indent=1), encoding="utf-8")
        return 0

    if args.cmd == "trail":
        plans_file = _state(cfg) / "plans.json"
        plans = json.loads(plans_file.read_text(encoding="utf-8")) if plans_file.exists() else {}
        active = {k: v for k, v in plans.items() if v.get("status") == "protected"}
        if not active:
            print("no protected positions recorded (run orders/protect with --live first)")
            return 0
        insts = [Instrument(v["symbol"], v["exchange"], "AUTO") for v in active.values()]
        if args.csv_dir:
            src, kite = CSVFolderData(args.csv_dir, args.index_csv), None
        else:
            kite = kite_session()
            src = KiteData(kite, cfg)
            src.annotate(insts)
        prepared, _, _ = load_prepared(src, insts, cfg)
        by_key = {p.inst.key: p for p in prepared}
        rows = []
        for key, pl in active.items():
            p = by_key.get(key)
            if p is None:
                continue
            t1_hit = float(p.df["high"].loc[p.df.index >= pd.Timestamp(pl.get("entry_date") or p.df.index[-1])].max()) \
                >= pl["targets"][0]["price"]
            rec = trail_recommendation(p, cfg, pl["entry"], pl["stop"], pl.get("entry_date") or str(p.df.index[-1].date()), t1_hit)
            rows.append(rec)
            if rec["raise"] and kite is not None:
                for mp in stop_update_payloads(kite, pl["symbol"], pl["exchange"], rec["new_stop"], cfg):
                    if args.live:
                        kite.modify_gtt(**mp)
                    print(f"{'MODIFIED' if args.live else 'DRY-RUN modify'} GTT {mp['trigger_id']} {key}: "
                          f"stop -> {rec['new_stop']}")
                if args.live:
                    pl["stop"] = rec["new_stop"]
        print(table(rows, [("symbol", "Symbol"), ("last_close", "Close"), ("current_stop", "Stop"),
                           ("new_stop", "New stop"), ("raise", "Raise"), ("reason", "Why"), ("stop_breached", "Breached")]))
        if args.live:
            plans_file.write_text(json.dumps(plans, indent=1), encoding="utf-8")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
