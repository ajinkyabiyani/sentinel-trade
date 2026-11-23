"""All tunable settings in one place. Defaults are exactly what was backtested (Feb-Nov 2026)."""
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path


@dataclass
class Config:
    # --- data ---
    # Everything comes from Kite (instruments, quotes, daily candles); see kite_data.py.
    special_sessions: tuple = ("2026-11-08",)                   # Muhurat / special sessions: ignored as history (weekends are skipped anyway)
    history_sessions: int = 30                                  # Nifty sessions read each morning for the regime

    # --- strategy (backtested values; change only after re-testing) ---
    universe_size: int = 15            # top-N stocks by previous-day stock-option turnover
    gap_threshold_atr: float = 0.30    # |gap| / ATR(5) must exceed this
    atr_days: int = 5
    regime_sma: int = 20               # Nifty futures SMA for the market regime
    regime_slope_lag: int = 5          # SMA must be rising/falling vs this many sessions ago
    roll_if_dte_le: int = 2            # use next-month options if current expiry is this close
    min_prev_contracts: int = 300      # option strike must have traded this many contracts yesterday
    min_premium: float = 2.0

    # --- risk ---
    risk_per_trade: float = 3000.0     # rupees lost if the stop is hit (incl. costs)
    est_costs: float = 120.0           # brokerage + charges per round trip (for sizing)
    stop_pct: float = 0.30             # stop 30% below the entry premium
    capital: float = 100000.0          # never spend more premium than this
    max_trades_per_day: int = 1
    kill_switch_drawdown: float = 15000.0   # halt new trades if realized P&L falls this far below its peak

    prepare_by: dt.time = dt.time(9, 13)   # the morning read needs last-session quotes (~90 s of Kite calls)

    # --- execution ---
    paper: bool = True                 # True = no real orders, fills simulated from live quotes
    product: str = "MIS"
    entry_time: dt.time = dt.time(9, 15, 20)
    last_entry_time: dt.time = dt.time(9, 25, 0)   # don't chase after this
    exit_time: dt.time = dt.time(15, 15, 0)
    entry_max_slippage: float = 0.02   # walk the limit price up to 2% above the first ask, then give up
    entry_fill_timeout_s: int = 45
    sl_limit_buffer: float = 0.05      # SL order limit = trigger x (1 - 5%) so it fills in a fast fall
    poll_seconds: int = 5
    tick: float = 0.05

    # --- files ---
    state_dir: str = str(Path(__file__).resolve().parents[2] / ".orb_cache" / "gap_momentum")   # ledger + daily journals (JSON)
