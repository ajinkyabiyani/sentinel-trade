"""Telegram: one message for each new call the system makes, in one layout: new paper trades on every desk and the skills' alerts. If a channel is set, each call goes there too.

The bot token, chat id and channel id come only from the environment or signal_bot/secrets.env. They are never logged, returned to the browser or put in a message.
A background queue paces the sends, so a slow or failed Telegram can never hold up a desk. Each call is sent once, even after a restart.
Paper only: this module sends text and nothing else.
"""
from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from strategies import paper_desk

log = logging.getLogger("kite-desk.notify")
IST = paper_desk.IST
SECRETS = Path(__file__).resolve().parents[3] / "signal_bot" / "secrets.env"
STATE = paper_desk.CACHE_DIR / "telegram.json"
PACE = 1.2                 # Telegram allows about one message a second to a chat
MAX_LEN = 3800
REPORT_PREFIX = "📊 END OF DAY"
CALL_PREFIX = "🟢 NEW CALL"
SAMPLE_PREFIX = "🧪 SAMPLE"
CHANNEL_PREFIXES = (CALL_PREFIX, REPORT_PREFIX)   # only new calls and the daily report go to the channel
FIXED_WIDTH = (CALL_PREFIX, REPORT_PREFIX, SAMPLE_PREFIX)
TOKEN_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_queue: queue.Queue = queue.Queue()
_lock = threading.Lock()
_worker: threading.Thread | None = None
_last = {"ok": None, "time": None, "error": ""}
_watch_lock = threading.Lock()   # the background watcher and a desk's immediate send must not both send the same call


def _testing() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _secret(name: str) -> str:
    v = os.environ.get(name)
    if v:
        return v.strip()
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _token() -> str:
    """The token itself, even if the saved line has other words around it."""
    m = TOKEN_SHAPE.search(_secret("TELEGRAM_BOT_TOKEN"))
    return m.group(0) if m else ""


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(STATE)


def enabled() -> bool:
    return _state().get("enabled", True)


def set_enabled(on: bool) -> dict:
    with _lock:
        d = _state()
        d["enabled"] = bool(on)
        _save(d)
    return status()


def configured() -> bool:
    return bool(_token() and _secret("TELEGRAM_CHAT_ID"))


def status() -> dict:
    return {"configured": configured(), "enabled": enabled(), "channel": bool(_secret("TELEGRAM_CHANNEL_ID")), "last_ok": _last["ok"], "last_time": _last["time"], "last_error": _last["error"], "queued": _queue.qsize()}


def _deliver(text: str) -> None:
    token, chat = _token(), _secret("TELEGRAM_CHAT_ID")
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    if not token or not chat:
        _last.update(ok=False, time=stamp, error="The bot token or chat id is missing from signal_bot/secrets.env")
        return
    _post(token, chat, text, stamp, "")
    channel = _secret("TELEGRAM_CHANNEL_ID")
    if channel and text.startswith(CHANNEL_PREFIXES):
        _post(token, channel, text, stamp, "Channel: ")


def _post(token: str, chat: str, text: str, stamp: str, label: str) -> None:
    for attempt in range(3):
        try:
            payload = {"chat_id": chat, "text": text[:MAX_LEN], "disable_web_page_preview": True}
            if text.startswith(FIXED_WIDTH):                    # cards and tables: fixed-width text keeps their columns straight
                payload.update(text=f"<pre>{html.escape(text[:MAX_LEN])}</pre>", parse_mode="HTML")
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15)
            if r.ok:
                _last.update(ok=True, time=stamp, error="")
                return
            try:
                why = str(r.json().get("description") or r.status_code)
            except ValueError:
                why = str(r.status_code)
            _last.update(ok=False, time=stamp, error=(label + why).replace(token, "<token>")[:200])
            log.warning("telegram refused a message: %s", _last["error"])
            if r.status_code != 429 and 400 <= r.status_code < 500:
                return
        except requests.RequestException as exc:       # the exception's text holds the URL, so the token: only its type is kept
            _last.update(ok=False, time=stamp, error=f"{label}Couldn't reach Telegram ({type(exc).__name__})")
            log.warning("telegram send failed: %s", type(exc).__name__)
        time.sleep(2 * (attempt + 1))


def _run() -> None:
    while True:
        text = _queue.get()
        try:
            _deliver(text)
        except Exception:  # noqa: BLE001 - the sender must never die
            log.warning("telegram worker error")
        time.sleep(PACE)


def send(text: str, force: bool = False) -> bool:
    """Queues a message. False if Telegram is off, not set up, or this is a test run."""
    global _worker
    if _testing() or not text or not configured() or not (force or enabled()):
        return False
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="telegram")
            _worker.start()
    _queue.put(text)
    return True


def _rs(v) -> str:
    return "–" if v is None else f"₹{float(v):,.2f}"


CONTRACT = re.compile(r"^([A-Z0-9&-]+?)(\d{2})([A-Z]{3}|[1-9OND]\d{2})(\d+(?:\.\d+)?)(CE|PE)$")       # NIFTY26N0924950PE, GLENMARK26NOV2700CE
WEEK_MONTH = {"1": "Jan", "2": "Feb", "3": "Mar", "4": "Apr", "5": "May", "6": "Jun", "7": "Jul", "8": "Aug", "9": "Sep", "O": "Oct", "N": "Nov", "D": "Dec"}
SHORT_SETUP = {"15-minute opening-range breakout": "15-min ORB", "15-minute Opening Range Breakout": "15-min ORB", "ORB + VWAP Confluence": "ORB + VWAP", "VWAP Reclaim / Rejection": "VWAP reclaim", "High-Volume Momentum Continuation": "Momentum"}
CARD_WIDTH = 32


def _nice_date(iso: str, with_year: bool = False) -> str:
    return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d %b %Y" if with_year else "%d %b")


def _title(name: str, expiry: str | None) -> str:
    """NIFTY26N1724900CE -> NIFTY 24900 CE · Exp 17 Nov."""
    m = CONTRACT.match(name)
    if not m:
        return f"{name} · Exp {_nice_date(expiry)}" if expiry else name
    code = m[3]
    when = _nice_date(expiry) if expiry else (code.title() if len(code) == 3 and code.isalpha() else f"{code[1:]} {WEEK_MONTH[code[0]]}")
    return f"{m[1]} {m[4]} {m[5]} · Exp {when}"


def _direction(s: dict) -> str:
    if s.get("direction_text"):
        return str(s["direction_text"])
    contract = CONTRACT.match(str(s.get("instrument") or "").upper())      # a real option symbol, so RELIANCE isn't read as a call
    if contract:
        return "BUY CALL" if contract[5] == "CE" else "BUY PUT"
    side = str(s.get("side") or "").upper()
    if side in ("LONG", "BUY"):
        return "BUY"
    if side in ("SHORT", "SELL"):
        return "SELL"
    return {"up": "UP", "down": "DOWN"}.get(str(s.get("direction") or "").lower(), "–")


def _confidence(s: dict) -> str:
    if not s.get("confidence"):
        return "Not rated"
    rate = s.get("success_rate_pct")
    return str(s["confidence"]) if rate is None else f"{s['confidence']} · {float(rate):.0f}%"


def _qty(s: dict) -> str:
    if s.get("qty") in (None, 0, ""):
        return ""
    return f"{s['lots']} lot{'' if s['lots'] == 1 else 's'} ({s['qty']} qty)" if s.get("lots") else f"{s['qty']} qty"


def _card_row(label: str, value: str) -> str:
    return f"{label}{value:>{max(1, CARD_WIDTH - len(label))}}"


def _card_foot(s: dict) -> list[str]:
    desk = str(s.get("desk") or "Desk").replace(" · ", " ")
    setup = SHORT_SETUP.get(s.get("setup"), s.get("setup"))
    if not setup:
        return [desk]
    one = f"{desk} · {setup}"
    return [one] if len(one) <= CARD_WIDTH else [desk, str(setup)]


def signal_text(s: dict) -> str:
    """The one Telegram message: a new call, as a fixed-width card. Only these named fields are read."""
    rule = "-" * CARD_WIDTH
    when = " · ".join(x for x in (_nice_date(s["date"], True) if s.get("date") else None, s.get("time")) if x)
    lines = [f"{CALL_PREFIX} · {when}" if when else CALL_PREFIX, ""]
    spread = s.get("spread")
    if spread:
        lines += [f"{spread['title']} · Exp {spread['expiry']}" if spread.get("expiry") else spread["title"], *spread["legs"]]
        if _qty(s):
            lines.append(_qty(s))
        lines += [rule, _card_row("Net credit", _rs(s.get("entry"))), _card_row("Exit", "15:15 close"), rule, _card_row("Max profit", _rs(spread["max_profit"])), _card_row("Max loss", _rs(spread["max_loss"])), _card_row("Confidence", _confidence(s)), rule, *_card_foot(s)]
        return "\n".join(lines)[:MAX_LEN]
    loss = s.get("max_loss")
    entry, stop, target = s.get("entry"), s.get("stop"), s.get("target")
    if loss is None and entry is not None and stop is not None and s.get("qty"):
        loss = abs(float(entry) - float(stop)) * float(s["qty"])
    ratio = "–"
    if None not in (entry, stop, target) and float(entry) != float(stop):
        ratio = f"{abs(float(target) - float(entry)) / abs(float(entry) - float(stop)):.1f}R"
    head = _direction(s) + (f" · {_qty(s)}" if _qty(s) else "")
    lines += [_title(str(s.get("instrument") or "–"), s.get("expiry")), head, rule, _card_row("Entry", _rs(entry)), _card_row("Stop", _rs(stop)), _card_row("Target", _rs(target)), rule, _card_row("Max loss", _rs(loss)), _card_row("Reward : Risk", ratio), _card_row("Confidence", _confidence(s)), rule, *_card_foot(s)]
    return "\n".join(lines)[:MAX_LEN]


def _spread_of(t: dict) -> dict | None:
    """A credit spread's legs in words and its limits, from a desk's trade."""
    kind = str(t.get("symbol") or "").split()[-1:]
    legs = t.get("legs") or []
    if len(legs) != 2 or kind not in (["CE"], ["PE"]) or t.get("entry") is None or not t.get("qty"):
        return None
    ordered = sorted(legs, key=lambda x: x["sign"])
    qty, credit = float(t["qty"]), float(t["entry"])
    width = abs(float(legs[0]["strike"]) - float(legs[1]["strike"]))
    m = CONTRACT.match(str(ordered[0].get("contract") or ""))
    expiry = None
    if m and len(m[3]) == 3 and not m[3].isalpha():
        expiry = f"{m[3][1:]} {WEEK_MONTH[m[3][0]]}"
    return {"title": f"NIFTY {'PUT' if kind[0] == 'PE' else 'CALL'} SPREAD", "expiry": expiry, "legs": [f"{'SELL' if x['sign'] < 0 else 'BUY '} {float(x['strike']):g} {kind[0]}" for x in ordered], "max_profit": credit * qty, "max_loss": (width - credit) * qty}


def open_text(desk: str, t: dict, day: str | None = None) -> str:
    book = t.get("book")
    return signal_text({"desk": f"{desk} · Options" if book == "options" else (f"{desk} · Cash" if book == "cash" else desk), "instrument": t.get("symbol"), "side": t.get("side"), "direction": t.get("direction"), "setup": t.get("setup"), "time": t.get("entry_time"), "date": day, "spread": _spread_of(t),
                        "entry": t.get("entry"), "stop": t.get("stop"), "target": t.get("target"), "qty": t.get("qty"), "lots": t.get("lots"), "max_loss": t.get("max_loss"), "expiry": t.get("expiry"), "confidence": t.get("confidence"), "success_rate_pct": t.get("success_rate_pct")})


def diff_calls(desk_id: str, desk: str, session: str, trades: list[dict], sent: set[str]) -> list[tuple[str, str]]:
    """(message id, text) for each new call not yet sent. One message a trade: exits aren't sent."""
    out = []
    for t in trades:
        mid = f"{session}|{desk_id}|{t.get('symbol')}|{t.get('entry_time')}|open"
        if mid not in sent:
            out.append((mid, open_text(desk, t, session)))
    return out


def watch_once(now: datetime | None = None) -> int:
    """Every desk's trades for today, compared with what has been sent."""
    if not enabled() or not configured():
        return 0
    with _watch_lock:
        return _watch(now)


def publish_now() -> None:
    """A desk has just put a new call on its paper book: send it now instead of at the next watch. Never raises."""
    if _testing():
        return
    try:
        watch_once()
    except Exception:  # noqa: BLE001 - a Telegram problem must never stop a desk's pass
        log.exception("telegram immediate send failed")


def _watch(now: datetime | None) -> int:
    today = (now or datetime.now(IST)).date().isoformat()
    with _lock:
        d = _state()
        sent = set(d.get("sent", {}).get(today, []))
    fresh = []
    for k, name in paper_desk.DESKS.items():
        v = paper_desk.view(k)
        trades = [t for t in (v.get("open") or []) + (v.get("closed") or []) if t.get("session") == today]
        fresh += diff_calls(k, name, today, trades, sent)
    for mid, text in fresh:
        if send(text):
            sent.add(mid)
    if fresh:
        with _lock:
            d = _state()
            d["sent"] = {today: sorted(sent | set(d.get("sent", {}).get(today, [])))}
            _save(d)
    return len(fresh)


def send_once(message_id: str, text: str, now: datetime | None = None) -> bool:
    """One message for one event, even if the app restarts."""
    today = (now or datetime.now(IST)).date().isoformat()
    with _lock:
        d = _state()
        have = set(d.get("once", {}).get(today, []))
        if message_id in have:
            return False
    if not send(text):
        return False
    with _lock:
        d = _state()
        d["once"] = {today: sorted(set(d.get("once", {}).get(today, [])) | have | {message_id})}
        _save(d)
    return True


def was_sent(message_id: str, now: datetime | None = None) -> bool:
    today = (now or datetime.now(IST)).date().isoformat()
    with _lock:
        return message_id in _state().get("once", {}).get(today, [])


def _watch_loop() -> None:
    time.sleep(30)
    while True:
        try:
            watch_once()
        except Exception:  # noqa: BLE001 - a background pass must never die
            log.exception("telegram watch failed")
        time.sleep(20)


def start() -> None:
    if not _testing():
        paper_desk.on_recorded = publish_now        # every desk's call goes out as soon as its trade is recorded; the loop below is a backstop
        threading.Thread(target=_watch_loop, daemon=True, name="telegram-watch").start()
