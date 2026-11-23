"""Shared data types for Gap Momentum. (The bhavcopy reader that lived here was replaced by kite_data.py.)"""
from dataclasses import dataclass


def iso(key: str) -> str:
    return f"{key[:4]}-{key[4:6]}-{key[6:]}"


@dataclass
class Candidate:
    symbol: str
    fut_expiry: str        # near-month futures expiry for the trade day (YYYY-MM-DD)
    prev_close: float      # last session's close of that same futures contract
    atr: float             # ATR(5) of that futures contract's daily candles
    lot_size: int | None
