"""Trade-by-trade workbook for the free-hand backtests: every trade with its stock, strike, side, expiry, times,
premiums, lots, money and Claude's reason, plus a month summary built from formulas.

Usage: python -m quant.trade_report OUT.xlsx RUN_DIR[=LABEL] [RUN_DIR[=LABEL] ...]
"""
import json
import re
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

WEEKLY = re.compile(r"^([A-Z&-]+?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
MONTHLY = re.compile(r"^([A-Z&-]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$")
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
WMON = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
FONT = "Arial"


def parse(sym: str) -> dict:
    m = WEEKLY.match(sym)
    if m:
        name, yy, mo, dd, k, side = m.groups()
        return {"underlying": name, "expiry": f"20{yy}-{WMON[mo]:02d}-{dd}", "strike": float(k), "side": side, "expiry_type": "weekly"}
    m = MONTHLY.match(sym)
    if m:
        name, yy, mon, k, side = m.groups()
        return {"underlying": name, "expiry": f"20{yy}-{MON[mon]:02d} (monthly)", "strike": float(k), "side": side, "expiry_type": "monthly"}
    return {"underlying": sym, "expiry": "", "strike": None, "side": "", "expiry_type": ""}


def buy_reasons(rec: dict) -> dict:
    """(instrument, fill time) -> Claude's reason for the BUY, and the spot level it saw."""
    out = {}
    for lk in rec["looks"]:
        for res in lk.get("results") or []:
            a = res.get("action") or {}
            if a.get("type") == "BUY" and res.get("ok"):
                out.setdefault(a.get("instrument"), []).append((lk["at"], a.get("why", ""), lk.get("read", "")))
    return out


def exit_kind(why: str) -> str:
    w = why.lower()
    if w.startswith("stop"):
        return "stop hit"
    if w.startswith("profit"):
        return "profit order"
    if w in ("month end", "expiry day"):
        return "forced: " + w
    return "Claude sold"


def rows_for(run: Path, label: str) -> list[dict]:
    rows = []
    for p in sorted((run / "months").glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        lots = rec["book"].get("lots", {})
        reasons = buy_reasons(rec)
        for x in rec["book"]["closed"]:
            c = parse(x["instrument"])
            lot = lots.get(c["underlying"]) or 0
            cand = [r for r in reasons.get(x["instrument"], []) if r[0] <= x["bought"]]
            why_buy = cand[-1][1] if cand else ""
            rows.append({
                "run": label, "month": x["bought"][:7], "status": "finished" if rec.get("done") else "in progress",
                "buy_date": x["bought"][:10], "buy_time": x["bought"][11:], "sell_date": x["sold"][:10], "sell_time": x["sold"][11:],
                "kind": "index" if c["underlying"] in ("NIFTY", "BANKNIFTY") else "stock",
                "underlying": c["underlying"], "strike": c["strike"], "side": c["side"], "expiry": c["expiry"],
                "contract": x["instrument"], "lots": x["lots"], "lot_size": lot, "qty": x["lots"] * lot,
                "buy_premium": x["buy"], "sell_premium": x["sell"], "costs": x["costs"], "net": x["net"],
                "exit": exit_kind(x["why"]), "why_bought": why_buy, "why_sold": x["why"],
            })
    return rows


COLS = [("Run", "run", 16), ("Month", "month", 9), ("Status", "status", 11), ("Buy date", "buy_date", 11), ("Buy time", "buy_time", 8),
        ("Sell date", "sell_date", 11), ("Sell time", "sell_time", 8), ("Index/stock", "kind", 10), ("Underlying", "underlying", 12),
        ("Strike", "strike", 9), ("CE/PE", "side", 6), ("Expiry", "expiry", 16), ("Contract", "contract", 24), ("Lots", "lots", 5),
        ("Lot size", "lot_size", 8), ("Qty", "qty", 7), ("Buy premium (₹)", "buy_premium", 10), ("Sell premium (₹)", "sell_premium", 10),
        ("Premium paid (₹)", None, 12), ("Change %", None, 9), ("Costs (₹)", "costs", 9), ("Net P&L (₹)", "net", 12), ("Exit", "exit", 14),
        ("Why Claude bought", "why_bought", 60), ("Why it was sold", "why_sold", 50)]


def main() -> None:
    out = Path(sys.argv[1])
    runs = []
    for arg in sys.argv[2:]:
        d, _, lab = arg.partition("=")
        runs.append((Path(d), lab or Path(d).name))
    rows = [r for d, lab in runs for r in rows_for(d, lab)]
    rows.sort(key=lambda r: (r["run"], r["buy_date"], r["buy_time"]))

    wb = Workbook()
    hdr = Font(name=FONT, bold=True, color="FFFFFF")
    fill = PatternFill("solid", start_color="1F3864")
    body = Font(name=FONT, size=10)
    money = '#,##0;[Red]-#,##0;-'

    ws = wb.active
    ws.title = "Trades"
    for j, (h, _, w) in enumerate(COLS, 1):
        c = ws.cell(row=1, column=j, value=h)
        c.font, c.fill, c.alignment = hdr, fill, Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(j)].width = w
    col = {h: get_column_letter(j) for j, (h, _, _) in enumerate(COLS, 1)}
    for i, r in enumerate(rows, 2):
        for j, (h, k, _) in enumerate(COLS, 1):
            if h == "Premium paid (₹)":
                v = f"={col['Buy premium (₹)']}{i}*{col['Qty']}{i}"
            elif h == "Change %":
                v = f"=IF({col['Buy premium (₹)']}{i}=0,0,{col['Sell premium (₹)']}{i}/{col['Buy premium (₹)']}{i}-1)"
            else:
                v = r[k]
            c = ws.cell(row=i, column=j, value=v)
            c.font = body
            if h in ("Premium paid (₹)", "Costs (₹)", "Net P&L (₹)"):
                c.number_format = money
            elif h == "Change %":
                c.number_format = '0.0%;[Red]-0.0%;-'
            elif h in ("Buy premium (₹)", "Sell premium (₹)"):
                c.number_format = '#,##0.00'
            if h in ("Why Claude bought", "Why it was sold"):
                c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{max(2, len(rows) + 1)}"
    n = len(rows) + 1

    sm = wb.create_sheet("Summary", 0)
    heads = ["Run", "Month", "Trades", "Winners", "Win %", "Net P&L (₹)", "Index trades", "Index net (₹)", "Stock trades", "Stock net (₹)", "Best trade (₹)", "Worst trade (₹)"]
    for j, h in enumerate(heads, 1):
        c = sm.cell(row=1, column=j, value=h)
        c.font, c.fill, c.alignment = hdr, fill, Alignment(wrap_text=True)
        sm.column_dimensions[get_column_letter(j)].width = 18 if j == 1 else 12
    R, M, K, NET = (f"Trades!${col[h]}$2:${col[h]}${n}" for h in ("Run", "Month", "Index/stock", "Net P&L (₹)"))
    keys = sorted({(r["run"], r["month"]) for r in rows})
    i = 1
    for run in dict.fromkeys(k[0] for k in keys):
        first = i + 1
        for _, m in [k for k in keys if k[0] == run]:
            i += 1
            a, b = f"$A{i}", f"$B{i}"
            vals = [run, m, f"=COUNTIFS({R},{a},{M},{b})", f'=COUNTIFS({R},{a},{M},{b},{NET},">0")', f"=IF(C{i}=0,0,D{i}/C{i})",
                    f"=SUMIFS({NET},{R},{a},{M},{b})", f'=COUNTIFS({R},{a},{M},{b},{K},"index")', f'=SUMIFS({NET},{R},{a},{M},{b},{K},"index")',
                    f'=COUNTIFS({R},{a},{M},{b},{K},"stock")', f'=SUMIFS({NET},{R},{a},{M},{b},{K},"stock")',
                    f"=SUMPRODUCT(MAX(({R}={a})*({M}={b})*{NET}))", f"=SUMPRODUCT(MIN(({R}={a})*({M}={b})*{NET}))"]
            for j, v in enumerate(vals, 1):
                c = sm.cell(row=i, column=j, value=v)
                c.font = body
        i += 1
        tot = [run, "Total", f"=SUM(C{first}:C{i - 1})", f"=SUM(D{first}:D{i - 1})", f"=IF(C{i}=0,0,D{i}/C{i})", f"=SUM(F{first}:F{i - 1})",
               f"=SUM(G{first}:G{i - 1})", f"=SUM(H{first}:H{i - 1})", f"=SUM(I{first}:I{i - 1})", f"=SUM(J{first}:J{i - 1})",
               f"=MAX(K{first}:K{i - 1})", f"=MIN(L{first}:L{i - 1})"]
        for j, v in enumerate(tot, 1):
            c = sm.cell(row=i, column=j, value=v)
            c.font = Font(name=FONT, size=10, bold=True)
        i += 1
    for row in sm.iter_rows(min_row=2, max_row=i):
        for c in row:
            if c.column == 5:
                c.number_format = '0%'
            elif c.column in (6, 8, 10, 11, 12):
                c.number_format = money
    sm.cell(row=i + 1, column=1, value="Each month starts with ₹1,00,000. Net P&L is after slippage, brokerage and taxes. "
            "Best/worst trade cells show 0 when a month has no winning or no losing trade.").font = Font(name=FONT, size=9, italic=True)
    sm.freeze_panes = "A2"
    wb.save(out)
    print(out, len(rows), "trades")


if __name__ == "__main__":
    main()
