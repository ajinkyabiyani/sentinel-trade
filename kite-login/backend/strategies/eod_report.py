"""The end-of-day Telegram report: every call the desks made today, and how each one did.

Covers the Agent Pipeline's paper trades and every BUY the Multi Agent Team made.
A call that wasn't on a paper book is followed on Kite's 5-minute candles under the same rules. The net is what every call would have made had it been taken.
Goes out once at 15:40 on a trading day. Paper only: this module sends text and nothing else.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from strategies import agent_desk as ad, common, multi_agent_team as team, notify, paper_desk

log = logging.getLogger("kite-desk.eod-report")
REPORT_AT = "15:40"
HEADER = notify.REPORT_PREFIX
CODES = {"Agent Pipeline · Cash": "PC", "Agent Pipeline · Options": "PO", "Agent Pipeline · Swing": "PS", team.DESK_NAME: "MAT", "Claude Skill Desk": "CSD"}
KEY = {"PC": "Agent Pipeline Cash", "PO": "Agent Pipeline Options", "PS": "Agent Pipeline Swing", "MAT": "Multi Agent Team", "CSD": "Claude Skill Desk"}
CONTRACT = notify.CONTRACT
SPREAD = re.compile(r"(\d+)/\d+\s*(CE|PE)$")
NAME_W, NET_W, R_W = 16, 10, 6
WIDTH = NAME_W + NET_W + R_W


def _row(desk: str, symbol, entry, exit_, at, reason, net, r, done: bool) -> dict:
    return {"desk": desk, "symbol": symbol, "entry": entry, "exit": exit_, "at": at, "reason": reason, "net": net if done else None, "r": r if done else None, "done": done}


def _book_rows(day: str) -> list[dict]:
    rows = []
    for k, name in paper_desk.DESKS.items():
        v = paper_desk.view(k)
        for t in (v.get("closed") or []) + (v.get("open") or []):
            if t.get("session") != day:
                continue
            book = t.get("book")
            desk = f"{name} · Options" if book == "options" else (f"{name} · Cash" if book == "cash" else name)
            rows.append(_row(desk, t.get("symbol"), t.get("entry"), t.get("exit"), t.get("exit_time"), t.get("exit_reason"), t.get("net"), t.get("r"), t.get("status") == "closed"))
    return rows


def _team_rows(day: str) -> list[dict]:
    """Every BUY the Multi Agent Team made, followed from the next 5-minute candle."""
    rows = []
    for r in team.runs(day):
        p = r.get("trade_payload") or {}
        if p.get("action") != "BUY":
            continue
        h, m = int(r["at"][:2]), int(r["at"][3:])
        start = common.plus_minutes(f"{h:02d}:{m - m % 5:02d}", 5)
        e = {"id": r["id"], "symbol": p["instrument"], "key": f"NFO:{p['instrument']}", "product": "options", "date": day, "qty": p["quantity"], "entry": p["entry"], "stop": p["stop_loss"], "target": p["target"], "entry_at": start, "status": "open"}
        try:
            team.follow(e, team.bars_of(e["key"], day), ad.FLAT_BAR)
        except Exception:  # noqa: BLE001 - one missing chart must not lose the whole report
            log.warning("no candles for %s", e["symbol"])
        rows.append(_row(team.DESK_NAME, e["symbol"], e["entry"], e.get("exit"), e.get("exit_at"), e.get("exit_reason"), e.get("net"), e.get("r"), e["status"] == "closed"))
    return rows


def _claude_trader_rows(day: str) -> list[dict]:
    """Claude Trader's trades opened or closed that day."""
    from strategies import claude_trader
    rows = []
    for e in claude_trader.positions():
        if e["day"] != day and e.get("exit_day") != day:
            continue
        done = e["status"] == "closed" and e.get("exit_day") == day
        rows.append(_row(claude_trader.DESK_NAME, e["instrument"], e["entry"], e.get("exit") if done else None, e.get("exit_at") if done else None, e.get("exit_reason") if done else None,
                         e.get("net") if done else None, e.get("r") if done else None, done))
    return rows


def collect(day: str) -> list[dict]:
    return _book_rows(day) + _team_rows(day) + _claude_trader_rows(day)


def _signed(v: float, unit: str = "") -> str:
    return f"{'+' if v >= 0 else '−'}{unit}{abs(float(v)):,.2f}"


def _label(r: dict) -> str:
    """A name that fits the table: the desk's short code, then the stock or strike."""
    code = CODES.get(r["desk"], r["desk"][:4].upper())
    name = str(r["symbol"])
    spread = SPREAD.search(name)
    if spread:
        return f"{code} {spread[1]}{spread[2]}"[:NAME_W]
    contract = CONTRACT.match(name)
    if not contract:
        return f"{code} {name}"[:NAME_W]
    strike = contract[4] + contract[5]
    under = {"NIFTY": "N", "BANKNIFTY": "BN"}.get(contract[1], contract[1])
    return f"{code} {under[:NAME_W - len(code) - len(strike) - 2]} {strike}"


def _row_line(r: dict) -> str:
    if not r["done"]:
        return f"{_label(r):<{NAME_W}}{'open':>{NET_W}}"
    multiple = "" if r.get("r") is None else f"{float(r['r']):+.2f}".replace("-", "−")
    return f"{_label(r):<{NAME_W}}{_signed(r['net']):>{NET_W}}{multiple:>{R_W}}"


def report_text(rows: list[dict], day: str) -> list[str]:
    """The table, best call first, in one or more messages under Telegram's limit. notify shows it in fixed-width text."""
    title = f"{HEADER} · {date.fromisoformat(day).strftime('%d %b %Y')}"
    if not rows:
        return [f"{title}\n\nNo calls today."]
    done = [r for r in rows if r["done"]]
    won = sum(r["net"] > 0 for r in done)
    net = sum(r["net"] for r in done)
    still = f" | {len(rows) - len(done)} open" if len(rows) > len(done) else ""
    rate = f" | {100 * won / len(done):.0f}%" if done else ""
    head = [f"{'Stock/Option':<{NAME_W}}{'Net ₹':>{NET_W}}{'R':>{R_W}}", "-" * WIDTH]
    ordered = sorted(rows, key=lambda r: (not r["done"], -(r["net"] or 0)))
    pages, page = [], [title, "", f"Net {_signed(net, '₹')} | {won}W {len(done) - won}L{rate}{still}", "", *head]
    for r in ordered:
        line = _row_line(r)
        if len("\n".join([*page, line])) > notify.MAX_LEN - 400:
            pages.append(page)
            page = [f"{title} (continued)", "", *head]
        page.append(line)
    page += ["-" * WIDTH, f"{'TOTAL':<{NAME_W}}{_signed(net):>{NET_W}}", ""]
    page += [f"{code} = {KEY[code]}" for code in sorted({CODES[r["desk"]] for r in rows if r["desk"] in CODES})]
    pages.append(page)
    return ["\n".join(x) for x in pages]


def tick(now: datetime) -> None:
    day = now.date().isoformat()
    if not team.india.trading_day(now.date()) or now.strftime("%H:%M") < REPORT_AT or not notify.enabled() or not notify.configured() or notify.was_sent(f"eod|{day}|1", now):
        return
    for i, text in enumerate(report_text(collect(day), day), 1):
        notify.send_once(f"eod|{day}|{i}", text, now)
