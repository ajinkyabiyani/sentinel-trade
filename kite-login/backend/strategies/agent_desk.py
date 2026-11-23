"""Agent Desk: the trader skills in .claude/skills, adapted to the Indian market, run as a team on a paper desk, so the app can show how they trade.

₹5 lakh of paper capital in two books:
  Cash, ₹3 lakh: opening-range breakouts in the 60 F&O stocks up most today, bought in the cash market, intraday, up to ₹1 lakh a trade and 3 trades a day.
  Options, ₹2 lakh: opening-range breakouts both ways, a call on a break up and a put on a break down, in Nifty, Bank Nifty and the
  60 F&O stocks up most and the 60 down most today. No daily cap on option buys: each is sized so the stop loses at most about ₹5,000 with
  charges, and needs free money in the ₹2 lakh book.

After each 5-minute close: one quote call reads all of NSE's F&O stocks, and the Scanner looks for breakouts.
A cash candidate goes through News, the Analyst and Big Money for context (they inform, they do not decide), then Entry, Exit, the Time Machine, Budget, Risk and the Ticket.
An option candidate goes to the Options agent (the F&O agent's review: strike, liquidity, the Time Machine on the premium after charges, and lots).
Every candidate carries its confidence: how many past sessions the same setup fired in, how often that made money after charges, and a grade from that sample.

Only a breakout on a bar that closed since the last pass can be traded, so a fill is never dated back to an earlier price. At most 10 candidates
in each book are reviewed a day, to stay within Kite's request limit; one skipped without a review, for the day's trade limit or no free money, does not use one.
None of this has been backtested to show an edge, and a similar cash replay over seven years lost money.

Fills are assumed: cash at the trigger candle's close plus 0.05% slippage, with 0.05% more on the way out and Zerodha's intraday charges.
Options at the premium the chain showed, with 0.3% slippage a side on index options and 1% on stock options, and Zerodha's option charges.
A candle touching both stop and target counts as the stop. Everything is out at 15:15.

The scripts get their prices from the app's local route and never see a Kite token. Paper only: nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from strategies import common, live_stops, paper_desk

log = logging.getLogger("kite-desk.agent-desk")
DESK = "agents"
ALGO_DIR = Path(__file__).resolve().parents[3]
SKILLS = ALGO_DIR / ".claude" / "skills"
CONSTITUENTS = ALGO_DIR / "reports" / "index_constituents.csv"
APP_URL = "http://127.0.0.1:8000"
CAPITAL = 500000.0            # the Agent Desk: the cash and option books below
CASH_CAPITAL = 300000.0
PER_TRADE = 100000.0          # the most one cash trade may use, no leverage
OPTION_CAPITAL = 200000.0
OPTION_MAX_LOSS = 5000.0      # rupees at an option's stop, with charges
OPTION_MAX_TRADES = None      # no daily cap on option trades; each is still held to the max loss, the book's free money and the risk gate
HIGH_CONF_MAX_LOSS = 8000.0   # a setup over OPTION_MAX_LOSS may still be taken up to this loss at the stop...
HIGH_CONF_MIN_SUCCESS = 50.0  # ...when its success rate over past triggers is above this, in %
MIN_SUCCESS_PCT = 51.0        # both books: a setup is taken only if its success rate is above this; it replaces the Time Machine's other verdicts
TM_WORDS = ("too few", "history averages", "paths average")   # the Time Machine's own objections in an option review


def success_rule(rate) -> str | None:
    """None when the success rate clears the bar; otherwise why the setup is skipped."""
    if rate is None:
        return "no success rate from past triggers"
    if rate <= MIN_SUCCESS_PCT:
        return f"success rate {rate:.1f}% is not above {MIN_SUCCESS_PCT:g}%"
    return None
OPTION_SLIP = {"index": 0.003, "stock": 0.01}
MIN_PREMIUM_FREE = 1000.0
INDEX_SCAN = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY"}
TARGET_R = 2.0
SLIP = 0.0005
MAX_TRADES = 3
MAX_CANDIDATES = 10           # reviews a day in each book
MIN_ORDER = 500
SETUP_NAME = "15-minute opening-range breakout"
SKIP_CARDS = 5                # skips shown as cards in each book a day; any more are only counted
LAST_SIGNAL = "15:00"
FLAT_BAR = "15:10"            # the candle closing at 15:15
TOOL_TIMEOUT = 240
SCAN_SIZE = 60                # about a second a chart, so this fits well inside the 5-minute cycle
TOOLS = {"scanner": "trader-scanner/scan.py", "news": "trader-news/news.py", "analyst": "trader-analyst/facts.py", "insiders": "trader-insiders/insiders.py", "entry": "trader-entry/entry.py",
         "exit": "trader-exit/exit.py", "timemachine": "trader-timemachine/timemachine.py", "budget": "trader-budget/size.py", "risk": "trader-risk/check.py", "broker": "trader-broker/ticket.py",
         "options": "trader-fno-agent/fno_agent.py"}
TEAM = [("scanner", "Scanner", "Finds opening-range breakouts in the F&O stocks moving most today, and in Nifty and Bank Nifty"), ("news", "News", "Reads the latest headlines"), ("analyst", "Analyst", "Reads the latest quarterly results"),
        ("insiders", "Big Money", "Checks NSE's bulk and block deals"), ("entry", "Entry", "Confirms a candle closed above the range"), ("exit", "Exit", "Sets the stop and target before entry"),
        ("timemachine", "Time Machine", "Replays the rest of the day 10,000 times and every past session the setup fired in"), ("budget", "Budget", "Sizes the position to the limits"), ("risk", "Risk", "Passes or blocks the order"),
        ("options", "Options", "For an option buy: picks the strike, checks liquidity, replays the premium after charges and sizes it to a ₹5,000 loss at the stop"),
        ("broker", "Ticket", "Writes the order, here filled on paper"), ("alerts", "Alerts", "Watches the stop, target and 15:15 exit")]


def nifty50() -> list[str]:
    """The fallback when Kite's F&O list isn't available."""
    try:
        with CONSTITUENTS.open(encoding="utf-8-sig", newline="") as fh:
            return [r["symbol"] for r in csv.DictReader(fh) if r.get("index") == "NIFTY 50" and r.get("symbol")]
    except (OSError, KeyError):
        return []


def _moves(quotes: dict) -> list[tuple[float, str]]:
    out = []
    for key, q in quotes.items():
        last, first = q.get("last_price"), (q.get("ohlc") or {}).get("open")
        if last and first:
            out.append((last / first - 1, key.split(":", 1)[-1]))
    return out


def strongest(quotes: dict, n: int = SCAN_SIZE) -> list[str]:
    """The stocks up most from today's open, from a Kite ohlc answer keyed NSE:SYMBOL. Only rising stocks."""
    return [s for m, s in sorted(_moves(quotes), key=lambda x: (-x[0], x[1])) if m > 0][:n]


def weakest(quotes: dict, n: int = SCAN_SIZE) -> list[str]:
    """The stocks down most from today's open. Only falling stocks: the option book buys puts on their breaks down."""
    return [s for m, s in sorted(_moves(quotes), key=lambda x: (x[0], x[1])) if m < 0][:n]


def movers(quotes: dict, n: int = SCAN_SIZE) -> dict:
    return {"up": strongest(quotes, n), "down": weakest(quotes, n)}


def run_tool(agent: str, args: list[str], cwd: Path) -> tuple[int, str]:
    """One skill script, with only the environment it needs: no credential reaches it."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME")}
    env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", SENTINEL_URL=APP_URL)
    try:
        p = subprocess.run([sys.executable, str(SKILLS / TOOLS[agent]), *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TOOL_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 1, f"{agent} did not finish in {TOOL_TIMEOUT} seconds"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def fetch_bars(symbol: str) -> dict:
    """Today's 5-minute candles from the app's Kite route, as {(date, HH:MM): (o, h, l, c)}. NFO:SYMBOL for an option contract."""
    url = f"{APP_URL}/api/skills/chart/{urllib.parse.quote(symbol)}?range=1d&interval=5m"
    with urllib.request.urlopen(url, timeout=120) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = {}
    for i, ts in enumerate(res["timestamp"]):
        at = datetime.fromtimestamp(ts, common.IST)
        out[(at.date().isoformat(), at.strftime("%H:%M"))] = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
    return out


def _json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    return None if v is None else round(float(v) * 100, 1)


def _facts(**kw) -> dict:
    """What one agent hands the next, as named values the page can draw. Empty values are left out."""
    return {k: v for k, v in kw.items() if v is not None and v != "" and v != []}


def _file_facts(path: Path, *keys: str) -> dict:
    """Named values from a JSON file an agent wrote, looked for at the top level and one level down. Never raises."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(doc.get("orders"), list) and doc["orders"]:
        doc = {**doc["orders"][0], **(doc["orders"][0].get("plan") or {})}
    flat = dict(doc)
    for v in doc.values():
        if isinstance(v, dict):
            flat = {**v, **flat}
    out = {}
    for k in keys:
        v = flat.get(k)
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            out[k] = v
    return out


def _clip(text: str, n: int = 1600) -> str:
    return (text or "").strip()[:n]


def option_fees(buy_val: float, sell_val: float) -> float:
    return common.charges([(True, buy_val), (False, sell_val)])


def simulate(c: dict, bars: dict, upto: str, slip: float = SLIP, fees=None) -> dict:
    """One long paper position through the candles after its trigger candle. bars: {HH:MM: (o, h, l, c)}. An option is long its premium."""
    raw, stop, target, qty = c["entry"], c["stop"], c["target"], c["qty"]
    entry = raw * (1 + slip)
    last = raw
    exit_px, exit_at, reason = None, None, None
    live = c.get("live_exit")            # closed on the last traded price before a candle could show it (live_stops)
    for t in common.LABELS:
        if t <= c["bar_start"] or t > upto:
            continue
        if live and live["at"] < common.plus_minutes(t, 5):
            break
        bar = bars.get(t)
        if bar:
            o, h, lo, cl = bar
            last = cl
            if lo <= stop:
                exit_px, exit_at, reason = min(o, stop), common.plus_minutes(t, 5), "stop"
                break
            if h >= target:
                exit_px, exit_at, reason = target, common.plus_minutes(t, 5), "target"
                break
        if t == FLAT_BAR:
            exit_px, exit_at, reason = last, "15:15", "15:15 exit"
    if exit_px is None and live:
        exit_px, exit_at, reason = live["price"], live["at"], live["reason"]
    done = exit_px is not None
    out = (exit_px if done else last) * (1 - slip)
    gross = (out - entry) * qty
    fee = (fees or common.costs)(entry * qty, out * qty)
    risk = raw - stop
    return {"symbol": c["symbol"], "underlying": c["symbol"], "key": f"NSE:{c['symbol']}", "token": None, "side": "LONG", "signal_time": c["at"], "entry_time": c["close_time"], "entry_ref": round(entry, 2), "sl": stop, "stop_now": stop, "target": target, "qty": qty,
            "exit_ref": round(out, 2) if done else None, "exit_time": exit_at, "exit_reason": reason, "status": "closed" if done else "open", "left": 0.0 if done else 1.0, "ltp": round(last, 2), "gross": round(gross, 2), "costs": round(fee, 2),
            "net": round(gross - fee, 2), "r": round(gross / (risk * qty), 2) if risk > 0 and qty else 0.0}


def simulate_option(c: dict, bars: dict, upto: str) -> dict:
    """An option buy, followed on the contract's own 5-minute premium candles."""
    kind = "index" if c["symbol"] in INDEX_SCAN.values() else "stock"
    t = simulate(c, bars, upto, slip=OPTION_SLIP[kind], fees=option_fees)
    t.update(symbol=c["contract"], underlying=c["symbol"], key=f"NFO:{c['contract']}", product="options", direction=c["direction"], book="options", lots=c.get("lots"))
    return t


def signal_fields(c: dict) -> dict:
    """What a trade carries so its Telegram call can say how it was found and how much evidence backs it."""
    od = c.get("odds") or {}
    return {"setup": SETUP_NAME, "book": c.get("book") or "cash", "direction": c.get("direction") or "up", "lots": c.get("lots"), "max_loss": c.get("max_loss"), "expiry": c.get("expiry"), "confidence": od.get("confidence"), "success_rate_pct": od.get("success_rate_pct"), "sample": od.get("sample")}


def _for_desk(t: dict) -> dict:
    extra = {k: t[k] for k in ("setup", "book", "direction", "lots", "max_loss", "expiry", "confidence", "success_rate_pct", "sample") if t.get(k) is not None}
    return {**extra, "symbol": t["symbol"], "key": t["key"], "token": None, "side": "LONG", "underlying": t["underlying"], "entry_time": t["entry_time"], "entry": t["entry_ref"], "stop": t["sl"], "stop_now": t["stop_now"], "target": t["target"], "qty": t["qty"],
            "exit": t["exit_ref"], "exit_time": t["exit_time"], "exit_reason": t["exit_reason"], "status": t["status"], "ltp": t["ltp"], "r": t["r"], "costs": t["costs"], "net": t["net"], "left": t["left"]}


def close_label(bar_start: str) -> str:
    return common.plus_minutes(bar_start, 5)


def odds_of(tm: dict) -> dict:
    """What a person reads on a signal: the success rate and sample behind it, and the Time Machine's paths."""
    mc, hist = tm.get("mc") or {}, tm.get("history") or {}
    conf = tm.get("confidence") or {}
    n = hist.get("triggered") or 0
    return {"sample": n, "sessions": hist.get("sessions"), "success_rate_pct": conf.get("success_rate_pct"), "target_first_pct": round(100 * hist.get("target", 0) / n, 1) if n else None,
            "avg_R_net": hist.get("avg_R_net"), "paths_target_pct": round(100 * mc["p_target"], 1) if mc.get("p_target") is not None else None,
            "paths_stop_pct": round(100 * mc["p_stop"], 1) if mc.get("p_stop") is not None else None, "paths_avg_R": mc.get("avg_R"), "confidence": conf.get("level") or "Low", "confidence_why": conf.get("why") or "no history replay"}


def option_odds(o: dict | None) -> dict:
    o = o or {}
    conf = o.get("confidence") or {}
    paths = o.get("paths") or {}
    pct = lambda x: None if x is None else round(100 * x, 1)  # noqa: E731
    return {"sample": o.get("fired") or 0, "sessions": o.get("sessions"), "success_rate_pct": conf.get("success_rate_pct"), "target_first_pct": pct(o.get("target_first")), "avg_R_net": o.get("avg_R_net"),
            "paths_target_pct": pct(paths.get("p_target")), "paths_stop_pct": pct(paths.get("p_stop")), "paths_avg_R": paths.get("avg_R_net"), "confidence": conf.get("level") or "Low", "confidence_why": conf.get("why") or (o.get("error") or "no history replay")}


def odds_line(od: dict) -> str:
    rate = "–" if od.get("success_rate_pct") is None else f"{od['success_rate_pct']:.0f}%"
    return f"Confidence {od['confidence']}: {od['confidence_why']}. Success rate {rate} of {od['sample']} past triggers."


class AgentDesk:
    def __init__(self, cache_dir: Path | None = None, runner=run_tool, bars=fetch_bars, symbols=nifty50, desk: str = DESK, name: str = "Agent Desk", books=("cash", "options")):
        self.cache_dir = cache_dir or common.CACHE_DIR
        self.desk, self.name, self.books = desk, name, tuple(books)      # each team keeps its own paper book, state file and workspace
        self.risk_gate = None                                              # this team's own gate, if it has one; the Agent Desk uses the shared one
        self.bars, self.symbols = bars, symbols
        self.last_bar: str | None = None
        self.snapshot = self._load()
        # What the team is doing right now, for the live view: the agent at work and the symbol it is on. Set as each agent starts.
        self.live: dict = {"agent": None, "symbol": None, "since": None, "last_pass": None}

        def traced(agent: str, args: list[str], cwd: Path):
            flags = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--symbol", "--tickers")}
            sym = flags.get("--symbol") or flags.get("--tickers") or (args[1] if agent == "entry" and len(args) > 1 else None) or (args[0] if agent == "analyst" and args else None)
            self.live.update(agent=agent, symbol=sym or (None if agent == "scanner" else self.live.get("symbol")), since=datetime.now(common.IST).isoformat(timespec="seconds"))
            try:
                return runner(agent, args, cwd)
            finally:
                self.live.update(agent=None, last_pass=datetime.now(common.IST).isoformat(timespec="seconds"))
        self.runner = traced

    def _state_file(self) -> Path:
        return self.cache_dir / f"{self.desk}_state.json"

    def _load(self) -> dict | None:
        try:
            return json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, snap: dict) -> None:
        path = self._state_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        tmp.replace(path)
        self.snapshot = snap

    def _workspace(self, session: str, budget: float) -> Path:
        cwd = self.cache_dir / self.desk / session
        (cwd / "trader").mkdir(parents=True, exist_ok=True)
        cfg = {"budget": round(budget, 2), "cash_buffer_pct": 0, "max_position_pct": 100, "max_order_inr": round(budget, 2), "min_order_inr": MIN_ORDER, "max_loss_per_trade_inr": None, "max_daily_loss_inr": None, "max_trades_per_day": MAX_TRADES, "max_orders_per_day": 10,
               "min_reward_risk": 1.0, "require_stop": True, "no_entries_before": "09:30", "no_entries_after": LAST_SIGNAL, "flat_by": "15:15", "fractional": False, "broker": "kite", "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False}
        (cwd / "trader" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return cwd

    def _option_workspace(self, session: str) -> Path:
        cwd = self.cache_dir / self.desk / session / "options"
        (cwd / "trader").mkdir(parents=True, exist_ok=True)
        cfg = {"paper": True, "allow_options": True, "allow_short": False, "cash_buffer_pct": 0, "option_stop_pct": 30, "target_r": TARGET_R, "min_history_trades": 10}
        (cwd / "trader" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return cwd

    def review(self, hit: dict, session: str, cwd: Path) -> dict:
        """One cash candidate through the team. Stops at the first agent that says no; the steps say why."""
        sym = hit["symbol"]
        now = f"{session} {common.plus_minutes(close_label(hit['bar_start']), 1)}"
        steps = [{"agent": "scanner", "state": "ok", "text": f"{sym} closed above its opening-range high of {hit.get('or_high')} at {hit.get('at')} ({hit.get('pct_over')}% over).",
                  "data": _facts(symbol=sym, direction="up", setup="opening-range breakout", at=hit.get("at"), price=hit.get("price"), range_high=hit.get("or_high"), range_low=hit.get("or_low"), pct_over=hit.get("pct_over"))}]
        for agent, args in (("news", ["--tickers", sym, "--all", "--no-moves"]), ("analyst", [sym, "--quarters", "2", "--no-write"]), ("insiders", ["--symbol", sym, "--all"])):
            rc, text = self.runner(agent, args, cwd)
            steps.append({"agent": agent, "state": "info" if rc == 0 or "Nothing new" in text else "note", "text": _clip(text, 900), "note": "as read now, not as of the trigger"})
        c = {"symbol": sym, "book": "cash", "direction": "up", "at": hit.get("at"), "bar_start": hit["bar_start"], "close_time": close_label(hit["bar_start"]), "decision": "skipped", "why": "", "steps": steps, "reviewed": True}

        def stop(agent: str, why: str, text: str, **facts) -> dict:
            steps.append({"agent": agent, "state": "no", "text": _clip(text), "data": _facts(blocked=why, **facts)})
            c["why"] = why
            return c

        rc, text = self.runner("entry", ["check", sym, "--write", "--now", now, "--json"], cwd)
        entry = _json(text)
        if rc != 0 or not entry or entry.get("status") != "triggered":
            return stop("entry", f"Entry did not confirm ({(entry or {}).get('status') or 'no answer'}).", (entry or {}).get("reason") or text)
        trig = entry["trigger"]
        steps.append({"agent": "entry", "state": "ok", "text": f"Triggered at {trig.get('time')} at ₹{trig.get('price')}: {entry.get('reason')}.", "data": _facts(symbol=sym, time=trig.get("time"), entry=trig.get("price"))})
        rc, text = self.runner("exit", ["plan", "--from", "trader/entry.json", "--write", "--now", now], cwd)
        if rc != 0:
            return stop("exit", "Exit could not set a sensible stop and target.", text)
        steps.append({"agent": "exit", "state": "ok", "text": _clip(text), "data": _file_facts(cwd / "trader" / "plan.json", "entry", "stop", "target", "reward_risk", "flat_by")})
        rc, text = self.runner("timemachine", ["run", "--now", now, "--json"], cwd)
        tm = _json(text)
        if rc != 0 or not tm:
            return stop("timemachine", "The Time Machine had no answer.", text)
        mc, hist = tm.get("mc") or {}, tm.get("history") or {}
        c["odds"] = odds_of(tm)
        summary = f"Target first {mc.get('p_target', 0) * 100:.1f}%, stop first {mc.get('p_stop', 0) * 100:.1f}%, neither {mc.get('p_neither', 0) * 100:.1f}%. Average {mc.get('avg_R')}R. Past sessions: fired {hist.get('triggered')} times, {hist.get('target')} target, {hist.get('stop')} stop. {tm.get('verdict') or ''} {odds_line(c['odds'])}"
        tm_facts = _facts(target_first_pct=_pct(mc.get("p_target")), stop_first_pct=_pct(mc.get("p_stop")), neither_pct=_pct(mc.get("p_neither")), avg_R=mc.get("avg_R"), past_triggers=hist.get("triggered"),
                          confidence=(c["odds"] or {}).get("confidence"))
        rate = (c["odds"] or {}).get("success_rate_pct")
        tm_facts["success_rate_pct"] = rate
        refusal = success_rule(rate)
        if refusal:
            return stop("timemachine", refusal[0].upper() + refusal[1:] + ".", summary, **tm_facts)
        note = "" if (mc.get("avg_R") or 0) > 0 else f" The simulated average ({mc.get('avg_R')}R) is set aside: the success rate decides."
        steps.append({"agent": "timemachine", "state": "ok", "text": _clip(f"Success rate {rate:.1f}% is above {MIN_SUCCESS_PCT:g}%. " + summary + note), "data": tm_facts})
        rc, text = self.runner("budget", ["--write", "--now", now], cwd)
        if rc != 0:
            return stop("budget", "Budget could not size it inside the limits.", text)
        steps.append({"agent": "budget", "state": "ok", "text": _clip(text), "data": _file_facts(cwd / "trader" / "orders.json", "qty", "risk_per_share", "max_loss")})
        rc, text = self.runner("risk", ["--now", now, "--json"], cwd)
        verdict = _json(text)
        result = ((verdict or {}).get("results") or [{}])[0]
        if result.get("verdict") != "PASS":
            return stop("risk", "Risk blocked it: " + ("; ".join(result.get("reasons") or []) or "no answer"), "\n".join(result.get("reasons") or []) or text, verdict="BLOCK")
        steps.append({"agent": "risk", "state": "ok", "text": "PASS. " + " ".join(result.get("info") or []), "data": {"verdict": "PASS"}})
        rc, text = self.runner("broker", ["show", "--index", "1", "--now", now], cwd)
        steps.append({"agent": "broker", "state": "ok" if rc == 0 else "note", "text": _clip(text, 700), "note": "filled on the paper desk only"})
        order = json.loads((cwd / "trader" / "orders.json").read_text(encoding="utf-8"))["orders"][0]
        plan = order["plan"]
        steps[-1]["data"] = _facts(order="BUY", symbol=sym, qty=int(order["qty"]), entry=float(trig["price"]), stop=float(plan["stop"]), target=float(plan["target"]), book="paper")
        c.update(decision="traded", why="Every agent agreed.", entry=float(trig["price"]), stop=float(plan["stop"]), target=float(plan["target"]), qty=int(order["qty"]))
        return c

    def review_option(self, hit: dict, session: str, cwd: Path, free: float) -> dict:
        """One option candidate: the Options agent picks and checks the contract and the Time Machine replays its premium after charges."""
        sym = INDEX_SCAN.get(hit["symbol"], hit["symbol"])
        down = hit.get("direction") == "down"
        side = "put" if down else "call"
        if hit.get("price") is None or hit.get("or_high") is None or hit.get("or_low") is None:
            return self._skip(hit, "The Scanner gave no price or range for it.", "options", sym)
        e = float(hit["price"])
        stop_u = round((float(hit["or_high"]) + float(hit["or_low"])) / 2, 2)
        risk = (stop_u - e) if down else (e - stop_u)
        c = {"symbol": sym, "book": "options", "direction": "down" if down else "up", "at": hit.get("at"), "bar_start": hit["bar_start"], "close_time": close_label(hit["bar_start"]), "decision": "skipped", "why": "", "reviewed": True,
             "steps": [{"agent": "scanner", "state": "ok", "text": f"{sym} closed {'below its opening-range low of ' + str(hit.get('or_low')) if down else 'above its opening-range high of ' + str(hit.get('or_high'))} at {hit.get('at')}: a {side} buy.",
                        "data": _facts(symbol=sym, direction="down" if down else "up", buy=side, setup="opening-range breakout", at=hit.get("at"), price=hit.get("price"), range_high=hit.get("or_high"), range_low=hit.get("or_low"))}]}
        if risk <= 0:
            c["why"] = "The stop at the range's midpoint is on the wrong side of the entry."
            return c
        target_u = round(e - TARGET_R * risk if down else e + TARGET_R * risk, 2)
        now = f"{session} {common.plus_minutes(close_label(hit['bar_start']), 1)}"
        def ask(max_loss: float) -> tuple[str, dict | None]:
            _, out = self.runner("options", ["review", "--symbol", sym, "--direction", c["direction"], "--entry", str(e), "--stop", str(stop_u), "--target", str(target_u), "--bar-start", hit["bar_start"], "--now", now,
                                             "--max-loss", str(max_loss), "--budget", str(round(free, 2))], cwd)
            return out, _json(out)

        def own(reasons):
            """The objections that aren't the Time Machine's: the contract, liquidity, money and the loss limit."""
            return [r for r in reasons or [] if not any(w in r for w in TM_WORDS)]

        text, res = ask(OPTION_MAX_LOSS)
        # over the usual loss limit only (setting the Time Machine's own objections aside), but with a strong record:
        # allowed up to the higher limit
        rest = own(res.get("reasons")) if res else []
        over_loss_only = bool(res) and bool(res.get("plan")) and not res.get("ok") and bool(rest) and all("max loss" in r for r in rest)
        rate = option_odds(res.get("odds")).get("success_rate_pct") if res else None
        if over_loss_only and rate is not None and rate > HIGH_CONF_MIN_SUCCESS and HIGH_CONF_MAX_LOSS > OPTION_MAX_LOSS:
            text2, res2 = ask(HIGH_CONF_MAX_LOSS)
            if res2:
                c["steps"].append({"agent": "risk", "state": "info", "text": f"One lot is over the usual ₹{OPTION_MAX_LOSS:,.0f} limit, but the success rate of {rate:.0f}% is above {HIGH_CONF_MIN_SUCCESS:.0f}%, "
                                   f"so up to ₹{HIGH_CONF_MAX_LOSS:,.0f} at the stop is allowed.", "data": _facts(usual_max_loss=OPTION_MAX_LOSS, high_confidence_max_loss=HIGH_CONF_MAX_LOSS, success_rate_pct=rate)})
                c["high_confidence"] = True
                text, res = text2, res2
        if not res:
            c["steps"].append({"agent": "options", "state": "no", "text": _clip(text), "data": {"blocked": "The Options agent had no answer."}})
            c["why"] = "The Options agent had no answer."
            return c
        # the success rate decides in place of the Time Machine's other checks; the contract, money and loss limit still stand
        if res.get("plan"):
            rate = option_odds(res.get("odds")).get("success_rate_pct")
            set_aside = [r for r in res.get("reasons") or [] if any(w in r for w in TM_WORDS)]
            rest = own(res.get("reasons"))
            refusal = success_rule(rate)
            if refusal:
                res["reasons"], res["ok"] = [refusal] + rest, False
            else:
                res["reasons"], res["ok"] = rest, not rest
                if set_aside:
                    c["steps"].append({"agent": "timemachine", "state": "info", "text": _clip(f"Success rate {rate:.1f}% is above {MIN_SUCCESS_PCT:g}%, so these Time Machine checks are set aside: " + "; ".join(set_aside) + "."),
                                       "data": _facts(success_rate_pct=rate, min_success_pct=MIN_SUCCESS_PCT, set_aside="; ".join(set_aside))})
        plan, sizing = res.get("plan") or {}, res.get("sizing") or {}
        c["odds"] = option_odds(res.get("odds"))
        pick = f"{plan.get('symbol')}, premium ₹{plan.get('entry')}, stop ₹{plan.get('stop')}, target ₹{plan.get('target')}, {sizing.get('lots')} lot(s)" if plan else "no contract"
        c["steps"].append({"agent": "options", "state": "ok" if plan else "no", "text": _clip(pick + ". " + " ".join(res.get("notes") or [])),
                           "data": _facts(contract=plan.get("symbol"), premium=plan.get("entry"), stop=plan.get("stop"), target=plan.get("target"), lots=sizing.get("lots"), qty=sizing.get("qty"), max_loss=sizing.get("max_loss"), expiry=plan.get("expiry"))})
        o = c["odds"] or {}
        tm_facts = _facts(success_rate_pct=o.get("success_rate_pct"), target_first_pct=o.get("target_first_pct"), avg_R_net=o.get("avg_R_net"), past_triggers=o.get("sample"), confidence=o.get("confidence"))
        tm_text = odds_line(c["odds"]) + (f" Rest-of-session paths: {c['odds']['paths_target_pct']}% target first, average {c['odds']['paths_avg_R']}R after charges." if c["odds"].get("paths_target_pct") is not None else "")
        if not res.get("ok"):
            c["steps"].append({"agent": "timemachine" if plan else "options", "state": "no", "text": _clip(tm_text + " " + "; ".join(res.get("reasons") or [])), "data": _facts(blocked="; ".join(res.get("reasons") or []), **tm_facts)})
            c["why"] = "Blocked: " + ("; ".join(res.get("reasons") or []) or "no answer")
            return c
        c["steps"].append({"agent": "timemachine", "state": "ok", "text": _clip(tm_text), "data": tm_facts})
        c["steps"].append({"agent": "broker", "state": "ok", "text": f"Buy {sizing['lots']} lot(s), {sizing['qty']} units of {plan['symbol']} at ₹{plan['entry']}. Loss at the stop with charges about ₹{sizing.get('max_loss')}.", "note": "filled on the paper desk only",
                           "data": _facts(order="BUY", symbol=plan["symbol"], lots=sizing["lots"], qty=sizing["qty"], entry=plan["entry"], stop=plan["stop"], target=plan["target"], book="paper")})
        c.update(decision="traded", why="Every agent agreed.", contract=plan["symbol"], expiry=plan.get("expiry"), strike=plan.get("strike"), option_type=plan.get("option_type"), lots=sizing["lots"],
                 entry=float(plan["entry"]), stop=float(plan["stop"]), target=float(plan["target"]), qty=int(sizing["qty"]), max_loss=sizing.get("max_loss"))
        return c

    @staticmethod
    def _no_money(c: dict, why: str) -> dict:
        """A candidate every agent passed, but with no money free in its book: not taken, and not sent anywhere.
        Telegram only ever carries trades the paper book actually takes."""
        if c["decision"] == "traded":
            c.update(decision="skipped", why=why)
        return c

    @staticmethod
    def _skip(hit: dict, why: str, book: str = "cash", symbol: str | None = None) -> dict:
        sym = symbol or hit["symbol"]
        return {"symbol": sym, "book": book, "direction": hit.get("direction", "up"), "at": hit.get("at"), "bar_start": hit["bar_start"], "close_time": close_label(hit["bar_start"]), "decision": "skipped", "why": why, "reviewed": False,
                "steps": [{"agent": "scanner", "state": "ok", "text": f"{sym} broke its opening range at {hit.get('at')}."}]}

    def _scan(self, cwd: Path, session: str, cutoff: str, symbols: list[str], direction: str = "up") -> tuple[dict | None, str]:
        if not symbols:
            return {"hits": [], "scanned": 0}, ""
        args = ["orb", "--symbols", ",".join(symbols), "--now", f"{session} {cutoff}", "--no-write", "--json", "--quiet", "--config", "trader/config.json"]
        if direction == "down":
            args += ["--direction", "down"]
        rc, text = self.runner("scanner", args, cwd)
        scan = _json(text)                      # the scanner exits 1 when it simply found nothing, so its answer counts, whatever the exit code
        scan = scan if isinstance(scan, dict) and "hits" in scan else None
        if scan is not None:
            scan = dict(scan, hits=[dict(h, direction=h.get("direction") or direction) for h in scan.get("hits", [])])
        return scan, ("" if scan is not None else text)

    def watch(self, now: datetime) -> bool:
        """Every pass: closes an open trade as soon as its last traded price crosses the stop or the target, without waiting
        for the candle. The exit is kept on the trade's card, so every later candle pass books it the same way."""
        snap, session = self.snapshot, now.date().isoformat()
        if not snap or snap.get("session") != session or now.strftime("%H:%M") >= "15:15":
            return False
        open_keys = {t["key"] for t in (snap.get("trades") or []) + (snap.get("option_trades") or []) if t.get("status") == "open"}
        cards = [(c, f"NSE:{c['symbol']}") for c in snap.get("candidates") or [] if c.get("decision") == "traded"]
        cards += [(c, f"NFO:{c['contract']}") for c in snap.get("options") or [] if c.get("decision") == "traded"]
        cards = [(c, k) for c, k in cards if k in open_keys and not c.get("live_exit")]
        px = live_stops.prices([k for _, k in cards], now)
        hit = False
        for c, k in cards:
            got = live_stops.hit(px.get(k), c["stop"], c["target"])
            if got:
                c["live_exit"] = {"price": round(got[1], 2), "at": now.strftime("%H:%M"), "reason": got[0]}
                hit = True
        if not hit:
            return False
        upto = snap.get("last_bar") or common.completed_bar(now) or "09:15"

        def bars_of(sym: str) -> dict:
            try:
                return {t: v for (d, t), v in self.bars(sym).items() if d == session}
            except Exception:  # noqa: BLE001 - the live exit alone still closes the trade
                return {}
        trades = [{**simulate(c, bars_of(c["symbol"]), upto), **signal_fields(c)} for c in snap.get("candidates") or [] if c.get("decision") == "traded"]
        option_trades = [{**simulate_option(c, bars_of(f"NFO:{c['contract']}"), upto), **signal_fields(c)} for c in snap.get("options") or [] if c.get("decision") == "traded"]
        if snap.get("counted", True):
            paper_desk.put_session(self.desk, session, [_for_desk(t) for t in trades + option_trades], "" if trades or option_trades else "no candidate passed every agent")
        snap.update(trades=trades, option_trades=option_trades)
        self._save(snap)
        return True

    def tick(self, now: datetime) -> None:
        self.watch(now)
        bar = common.completed_bar(now)
        if not bar or bar == self.last_bar:
            return
        session = now.date().isoformat()
        lists = self.symbols()
        if isinstance(lists, list):
            lists = {"up": lists, "down": lists}
        if not lists.get("up") and not lists.get("down"):
            self.last_bar = bar
            return
        same = bool(self.snapshot and self.snapshot.get("session") == session)
        snap = self.snapshot if same else {"session": session, "candidates": [], "options": []}
        snap.setdefault("candidates", [])
        snap.setdefault("options", [])
        # a breakout counts only if its candle closed since the last pass (two candles on the day's first pass), so no fill is dated back
        since = snap.get("last_bar") if same and snap.get("last_bar") else common.plus_minutes(bar, -10)
        fresh = lambda h: since < h["bar_start"] <= bar  # noqa: E731
        cutoff = min(common.plus_minutes(bar, 5), LAST_SIGNAL)
        cwd = self._workspace(session, PER_TRADE)
        scan, note = self._scan(cwd, session, cutoff, lists.get("up") or [])
        if scan is None:
            snap.update(asof=now.isoformat(timespec="seconds"), scan_note=_clip(note, 400))
            self.last_bar = bar
            self._save(snap)
            return
        all_bars: dict[str, dict] = {}

        def bars_of(sym: str) -> dict:
            if sym not in all_bars:
                try:
                    all_bars[sym] = {t: v for (d, t), v in self.bars(sym).items() if d == session}
                except Exception:  # noqa: BLE001 - a missing chart leaves the position marked at its last price
                    log.exception("agent desk candles for %s", sym)
                    all_bars[sym] = {}
            return all_bars[sym]

        def publish() -> tuple[list, list, bool]:
            """Puts today's trades on the paper book and sends any new call. Runs after each decision, so a slow pass can't hold a call back."""
            trades = [{**simulate(c, bars_of(c["symbol"]), bar), **signal_fields(c)} for c in snap["candidates"] if c["decision"] == "traded"]
            option_trades = [{**simulate_option(c, bars_of(f"NFO:{c['contract']}"), bar), **signal_fields(c)} for c in snap["options"] if c["decision"] == "traded"]
            counted = now.strftime("%H:%M") < "15:15" or paper_desk.has_session(self.desk, session)
            if counted:
                everything = trades + option_trades
                paper_desk.put_session(self.desk, session, [_for_desk(t) for t in everything], "" if everything else "no candidate passed every agent")
            snap.update(trades=trades, option_trades=option_trades)
            return trades, option_trades, counted

        stale_keys = set(snap.get("stale_keys") or [])
        more = dict(snap.get("skipped_more") or {"cash": 0, "options": 0})

        def note_skip(book: str, card: dict) -> None:
            rows = snap["candidates"] if book == "cash" else snap["options"]
            if sum(1 for c in rows if not c.get("reviewed")) < SKIP_CARDS:
                rows.append(card)
            else:
                more[book] = more.get(book, 0) + 1
        gate = self.risk_gate if self.desk != DESK else risk_gate          # a separate team is never held back by another team's trades
        # ---- the cash book
        seen = {c["symbol"] for c in snap["candidates"]}
        hits = [h for h in scan.get("hits", []) if h["symbol"] not in seen] if "cash" in self.books else []
        stale_keys |= {f"cash|{h['symbol']}" for h in hits if not fresh(h)}
        for hit in sorted((h for h in hits if fresh(h)), key=lambda h: (h["bar_start"], -(h.get("pct_over") or 0))):
            taken = [c for c in snap["candidates"] if c["decision"] == "traded"]
            if sum(1 for c in snap["candidates"] if c.get("reviewed")) >= MAX_CANDIDATES:
                break
            if len(taken) >= MAX_TRADES:
                note_skip("cash", self._skip(hit, f"The day's limit of {MAX_TRADES} trades was already reached."))
                seen.add(hit["symbol"])
                continue
            shared = gate(hit["symbol"], "shares") if gate else None
            if shared:
                note_skip("cash", self._skip(hit, "Shared risk gate: " + shared + "."))
                seen.add(hit["symbol"])
                continue
            live = [simulate(c, bars_of(c["symbol"]), hit["bar_start"]) for c in taken]
            tied = sum(t["entry_ref"] * t["qty"] for t in live if t["status"] == "open")
            booked = sum(t["net"] for t in live if t["status"] == "closed")
            free = min(PER_TRADE, CASH_CAPITAL + booked - tied)
            if free < MIN_ORDER:
                self._workspace(session, PER_TRADE)
                c = self._no_money(self.review(hit, session, cwd), "No cash free: every agent agreed, but the cash book had no money free, so it wasn't taken.")
                snap["candidates"].append(c)
                self._save(snap)
                continue
            self._workspace(session, free)
            snap["candidates"].append(self.review(hit, session, cwd))
            if snap["candidates"][-1]["decision"] == "traded":
                publish()
            self._save(snap)
        # ---- the option book: calls on breaks up, puts on breaks down, indices first
        o_hits = list(scan.get("hits", []))
        notes = []
        for syms, direction in ((list(INDEX_SCAN), "up"), (list(INDEX_SCAN), "down"), (lists.get("down") or [], "down")):
            s2, n2 = self._scan(cwd, session, cutoff, syms, direction)
            if s2 is None:
                notes.append(n2)
            else:
                o_hits += s2.get("hits", [])
        o_seen = {(c["symbol"], c["direction"]) for c in snap["options"]}
        o_new = [h for h in o_hits if (INDEX_SCAN.get(h["symbol"], h["symbol"]), h.get("direction", "up")) not in o_seen] if "options" in self.books else []
        stale_keys |= {f"options|{INDEX_SCAN.get(h['symbol'], h['symbol'])}|{h.get('direction', 'up')}" for h in o_new if not fresh(h)}
        o_cwd = self._option_workspace(session)
        for hit in sorted((h for h in o_new if fresh(h)), key=lambda h: (h["bar_start"], h["symbol"] not in INDEX_SCAN, -(h.get("pct_over") or 0))):
            sym = INDEX_SCAN.get(hit["symbol"], hit["symbol"])
            if (sym, hit.get("direction", "up")) in o_seen:
                continue
            o_seen.add((sym, hit.get("direction", "up")))
            taken = [c for c in snap["options"] if c["decision"] == "traded"]
            if sum(1 for c in snap["options"] if c.get("reviewed")) >= MAX_CANDIDATES:
                break
            if OPTION_MAX_TRADES is not None and len(taken) >= OPTION_MAX_TRADES:
                note_skip("options", self._skip(hit, f"The day's limit of {OPTION_MAX_TRADES} option trades was already reached.", "options", sym))
                continue
            shared = gate(sym, "option") if gate else None
            if shared:
                note_skip("options", self._skip(hit, "Shared risk gate: " + shared + ".", "options", sym))
                continue
            live = [simulate_option(c, bars_of(f"NFO:{c['contract']}"), hit["bar_start"]) for c in taken]
            tied = sum(t["entry_ref"] * t["qty"] for t in live if t["status"] == "open")
            booked = sum(t["net"] for t in live if t["status"] == "closed")
            free = OPTION_CAPITAL + booked - tied
            if free < MIN_PREMIUM_FREE:
                c = self._no_money(self.review_option(hit, session, o_cwd, OPTION_CAPITAL), "No money free: every agent agreed, but the option book had no money free, so it wasn't taken.")
                snap["options"].append(c)
                self._save(snap)
                continue
            snap["options"].append(self.review_option(hit, session, o_cwd, free))
            if snap["options"][-1]["decision"] == "traded":
                publish()
            self._save(snap)
        trades, option_trades, counted = publish()
        snap.update(books=list(self.books), asof=now.isoformat(timespec="seconds"), last_bar=bar, counted=counted, capital=CAPITAL, cash_capital=CASH_CAPITAL, option_capital=OPTION_CAPITAL, scanned=scan.get("scanned"), hits=len(scan.get("hits", [])),
                    trades=trades, option_trades=option_trades, stale_keys=sorted(stale_keys), stale_skipped=len(stale_keys), skipped_more=more, scan_note="; ".join(x for x in notes if x)[:400])
        self.last_bar = bar
        self._save(snap)


risk_gate = None              # the shared gate, for a desk built without one of its own; the Agent Pipeline has its own
