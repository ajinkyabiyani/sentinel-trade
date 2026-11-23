"""Every paper trade from every strategy with the confidence it was taken at, and how often each confidence level actually won.

Confidence means what each strategy states at entry:
  Multi Agent Team    the Trader agent's own stated confidence (a judgement)
  Claude Trader       Claude's own stated confidence (a judgement)
A trade "won" when it closed with a positive net after charges. Open trades are listed but never counted as won or lost.
The whole log is also written to trader/logs/trades_by_confidence.csv each time it is read, so a plain-file copy always exists.
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

from strategies import claude_trader, common, multi_agent_team as mat

log = logging.getLogger("kite-desk.confidence-log")
BANDS = ((0, 49, "Under 50%"), (50, 59, "50–59%"), (60, 69, "60–69%"), (70, 100, "70% and up"))
NONE_BAND = "Not stated"
LOG_FILE = Path(__file__).resolve().parents[3] / "trader" / "logs" / "trades_by_confidence.csv"
MIN_TRUST = 30                # closed trades in a band before its win rate is called reliable


def band_of(conf) -> str:
    if conf is None:
        return NONE_BAND
    c = float(conf)
    return next((label for lo, hi, label in BANDS if lo <= c <= hi + 0.999), BANDS[-1][2])


def _row(day, strategy, instrument, kind, conf, source, status, net, exit_reason=None, entry=None, exit_=None, r=None) -> dict:
    closed = status == "closed"
    return {"date": day, "strategy": strategy, "instrument": instrument, "kind": kind, "confidence": None if conf is None else round(float(conf), 1),
            "band": band_of(conf), "source": source, "status": "closed" if closed else "open", "net": None if net is None else round(float(net), 2),
            "won": (float(net or 0) > 0) if closed else None, "exit_reason": exit_reason, "entry": entry, "exit": exit_, "r": r}


def rows() -> list[dict]:
    out = []
    try:
        days = sorted(p.stem[4:] for p in mat.DIR.glob("day_*.json")) if mat.DIR.exists() else []
        for d in days:
            runs = {r["id"]: r for r in mat.runs(d)}
            for e in mat.positions(d):
                conf = e.get("confidence")
                if conf is None:
                    conf = ((runs.get(e["id"]) or {}).get("trade_payload") or {}).get("confidence")
                net = e.get("net") if e["status"] == "closed" else None
                out.append(_row(d, "Multi Agent Team", e["instrument"], "shares" if e.get("kind") == "cash" else "option", conf, "Trader's own view",
                                e["status"], net, e.get("exit_reason"), e.get("entry"), e.get("exit"), e.get("r")))
    except Exception:  # noqa: BLE001
        log.exception("team trades")
    try:
        for e in claude_trader.positions():
            out.append(_row(e.get("exit_day") or e["day"], "Claude Trader", e["instrument"], "option", e.get("confidence"), "Claude's own view",
                            e["status"], e.get("net") if e["status"] == "closed" else None, e.get("exit_reason"), e.get("entry"), e.get("exit"), e.get("r")))
    except Exception:  # noqa: BLE001
        log.exception("claude trader trades")
    out.sort(key=lambda x: (x["date"] or "", x["strategy"]), reverse=True)
    return out


def _stats(group: list[dict]) -> dict:
    closed = [x for x in group if x["status"] == "closed"]
    won = sum(1 for x in closed if x["won"])
    return {"closed": len(closed), "won": won, "lost": len(closed) - won, "open": len(group) - len(closed),
            "win_rate": round(100 * won / len(closed), 1) if closed else None, "net": round(sum(x["net"] or 0 for x in closed), 2)}


def summary(all_rows: list[dict]) -> dict:
    labels = [b[2] for b in BANDS] + [NONE_BAND]
    bands = [{"band": b, **_stats([x for x in all_rows if x["band"] == b])} for b in labels]
    strategies = sorted({x["strategy"] for x in all_rows})
    by_strategy = [{"strategy": s, "source": next(x["source"] for x in all_rows if x["strategy"] == s), "total": _stats([x for x in all_rows if x["strategy"] == s]),
                    "bands": [{"band": b, **_stats([x for x in all_rows if x["strategy"] == s and x["band"] == b])} for b in labels]} for s in strategies]
    total = _stats(all_rows)
    rated = [b for b in bands if b["band"] != NONE_BAND and b["closed"]]
    lines = []
    if rated:
        best = max(rated, key=lambda b: (b["win_rate"], b["closed"]))
        worst = min(rated, key=lambda b: (b["win_rate"], -b["closed"]))
        lines.append(f"Trades taken at {best['band']} confidence won most often: {best['won']} of {best['closed']} ({best['win_rate']:.0f}%), net ₹{best['net']:,.0f}.")
        if worst["band"] != best["band"]:
            lines.append(f"{worst['band']} won least: {worst['won']} of {worst['closed']} ({worst['win_rate']:.0f}%), net ₹{worst['net']:,.0f}.")
        hi = [b for b in rated if b["band"] in ("60–69%", "70% and up")]
        lo = [b for b in rated if b["band"] in ("Under 50%", "50–59%")]
        if hi and lo:
            hw, hc = sum(b["won"] for b in hi), sum(b["closed"] for b in hi)
            lw, lc = sum(b["won"] for b in lo), sum(b["closed"] for b in lo)
            if hc and lc:
                better = "higher" if hw / hc > lw / lc else "lower"
                lines.append(f"So far the {better}-confidence calls have done better: {hw}/{hc} won at 60% and up against {lw}/{lc} under 60%.")
    if total["closed"] < MIN_TRUST:
        lines.append(f"Only {total['closed']} trades have closed, so each band's rate can still swing a lot. About {MIN_TRUST} per band are needed before trusting it.")
    return {"total": total, "bands": bands, "by_strategy": by_strategy, "lines": lines, "min_trust": MIN_TRUST}


def write_csv(all_rows: list[dict], path: Path = LOG_FILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["date", "strategy", "instrument", "kind", "confidence", "band", "source", "status", "won", "net", "r", "entry", "exit", "exit_reason"]
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    tmp.replace(path)
    return path


def view() -> dict:
    all_rows = rows()
    try:
        write_csv(all_rows)
    except OSError:
        log.exception("confidence log file")
    return {"asof": datetime.now(common.IST).isoformat(timespec="seconds"), "rows": all_rows, "log_file": str(LOG_FILE), **summary(all_rows)}
