"""Pure strategy rules: no files, no broker. Same functions run live, in paper mode and in replay."""
import math
from dataclasses import dataclass
from datetime import date
from .data import Candidate


@dataclass
class Signal:
    candidate: Candidate
    fut_open: float
    gap_atr: float
    side: str            # "CE" or "PE"


def rank_signals(cands: list[Candidate], opens: dict[str, float], threshold: float) -> list[Signal]:
    """All stocks whose futures opened more than `threshold` x ATR away from yesterday's close,
    biggest gap first."""
    out = []
    for c in cands:
        o = opens.get(c.symbol)
        if not o or o <= 0 or c.atr <= 0:
            continue
        if abs(o / c.prev_close - 1) > 0.15:          # corporate action / bad tick
            continue
        g = (o - c.prev_close) / c.atr
        if abs(g) > threshold:
            out.append(Signal(c, o, g, "CE" if g > 0 else "PE"))
    return sorted(out, key=lambda s: abs(s.gap_atr), reverse=True)


def choose_contract(chain: list[dict], symbol: str, side: str, fut_open: float, today: str,
                    roll_if_dte_le: int = 2):
    """chain: dicts with keys symbol, expiry (YYYY-MM-DD), strike, type.
    Returns (expiry, strike) of the ATM option: current expiry, or next month if it expires within
    `roll_if_dte_le` calendar days."""
    rows = [r for r in chain if r["symbol"] == symbol and r["type"] == side and r["expiry"] > today]
    exps = sorted({r["expiry"] for r in rows})
    if not exps:
        return None
    exp = exps[0]
    if (date.fromisoformat(exp) - date.fromisoformat(today)).days <= roll_if_dte_le and len(exps) > 1:
        exp = exps[1]
    strikes = sorted({r["strike"] for r in rows if r["expiry"] == exp})
    if not strikes:
        return None
    k = min(strikes, key=lambda s: abs(s - fut_open))
    return exp, k


def size_position(entry_price: float, lot_size: int, risk: float, costs: float, stop_pct: float,
                  capital: float) -> int:
    """Number of lots so that a stop-out loses at most `risk` (including costs). 0 = skip trade."""
    if entry_price <= 0 or lot_size <= 0:
        return 0
    lots = math.floor((risk - costs) / (stop_pct * entry_price * lot_size))
    lots = min(lots, math.floor(capital / (entry_price * lot_size)))
    return max(lots, 0)


def round_tick(x: float, tick: float = 0.05, mode: str = "nearest") -> float:
    n = x / tick
    n = math.floor(n + 1e-9) if mode == "down" else math.ceil(n - 1e-9) if mode == "up" else round(n)
    return round(n * tick, 2)
