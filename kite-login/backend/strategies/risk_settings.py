"""Your paper capital and risk limits, shared by the multi-agent teams. Paper only: nothing here can place an order."""
from __future__ import annotations

import json
import threading
from datetime import datetime

from strategies import common

FILE = common.CACHE_DIR / "risk_settings.json"
MAX_RISK_PCT = 2.0
DAILY_LOSS_PCT = 4.0          # two full-risk losing trades; it must sit above the 2% a trade or nothing can pass
_lock = threading.Lock()


def settings() -> dict | None:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(capital: float, confirmed: bool) -> dict:
    if capital <= 0:
        raise ValueError("Capital must be above zero.")
    if not confirmed:
        raise ValueError(f"Confirm the risk limits first: {MAX_RISK_PCT}% a trade and {DAILY_LOSS_PCT}% a day.")
    s = {"capital": float(capital), "max_risk_pct": MAX_RISK_PCT, "max_risk_inr": round(capital * MAX_RISK_PCT / 100.0, 2), "daily_loss_pct": DAILY_LOSS_PCT,
         "daily_loss_inr": round(capital * DAILY_LOSS_PCT / 100.0, 2), "paper": True, "confirmed": True, "saved": datetime.now(common.IST).isoformat(timespec="seconds")}
    with _lock:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        tmp.replace(FILE)
    return s


def limits() -> dict:
    return {"max_risk_pct": MAX_RISK_PCT, "daily_loss_pct": DAILY_LOSS_PCT}
