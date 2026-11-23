"""Indian statutory friction for a round trip, Zerodha rates (Nov 2026). All figures in rupees.

Equity delivery (CNC, also BTST): no brokerage; STT 0.1% on buy and sell; NSE transaction 0.00297%; SEBI Rs 10 a crore;
  stamp duty 0.015% on the buy; GST 18% on transaction and SEBI charges; DP charge Rs 15.34 (incl. GST) on the sell day.
Equity intraday (MIS): brokerage the lower of Rs 20 or 0.03% a side; STT 0.025% on the sell; NSE 0.00297%; SEBI; stamp 0.003%
  on the buy; GST 18% on brokerage, transaction and SEBI.
Options: strategies.common.charges (Rs 20 an order, STT 0.15% on the sell premium from 1 Apr 2026, NSE 0.03553%, SEBI, stamp, GST).
Slippage is separate: SLIP of the price on each side, an estimate for liquid F&O stocks.
"""
from __future__ import annotations

from strategies import common

SLIP = 0.0005


def delivery(buy_value: float, sell_value: float) -> float:
    turnover = buy_value + sell_value
    txn, sebi = 0.0000297 * turnover, 1e-6 * turnover
    return 0.001 * turnover + txn + sebi + 0.00015 * buy_value + 0.18 * (txn + sebi) + 15.34


def intraday(buy_value: float, sell_value: float) -> float:
    brok = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    turnover = buy_value + sell_value
    txn, sebi = 0.0000297 * turnover, 1e-6 * turnover
    return brok + 0.00025 * sell_value + txn + sebi + 0.00003 * buy_value + 0.18 * (brok + txn + sebi)


def options(buy_value: float, sell_value: float) -> float:
    return common.charges([(True, buy_value), (False, sell_value)])
