"""F&O money flow for Claude Trader: futures open-interest build-up, volume surges and breadth across every F&O stock.

Plain code from Kite quotes, one pass a minute (a handful of quote calls), never a Claude call. It tells Claude where fresh
positions are being built, which a price chart alone can't show:

  price up,   OI up    long build-up    fresh buying
  price down, OI up    short build-up   fresh shorting
  price up,   OI down  short covering   shorts leaving; often runs out of fuel
  price down, OI down  long unwinding   longs leaving

Open interest is the near and next month futures added together, so the expiry-week rollover from one to the other isn't
read as positions closing. Paper only; reads, never orders."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

POLL_SECONDS = 55             # one pass a minute
BATCH = 100                   # instruments per quote call
OI_FLAT = 1.0                 # % change in OI below this is "no real change"
OI_HOUR = 6.0                 # early look: futures OI up this % more than the median F&O stock's in the last hour...
                              # (the wake-up bars are set by how often they fire, about 16 a day on 20 Nov, not by what followed)
PRICE_HOUR = 1.2              # ...with the price moving this % the same hour
VOL_SURGE = 6.0               # early look: a 5-minute candle trading this many times the last hour's median volume...
VOL_MIN_MOVE = 0.8            # ...while the price moves at least this % in that candle
VOL_BASE = 6                  # candles of history the volume median needs
HOUR = 12                     # 5-minute candles in an hour
TOP = 5

DIR: Path | None = None       # set by Claude Trader: where today's flow is kept, so a backend restart keeps the last hour
quote_source = None           # tests replace it: keys -> Kite quote dicts
history_source = None         # tests replace it: (token, from, to, interval, oi) -> Kite candles

_state: dict = {"day": None}
_lock = threading.RLock()


# ---------------------------------------------------------------- Kite

def _kite():
    from kiteconnect import KiteConnect
    from core import store
    d = store.load()
    if not d:
        return None
    k = KiteConnect(api_key=d["api_key"])
    k.set_access_token(d["access_token"])
    return k


def _quote(keys: list[str]) -> dict:
    if quote_source:
        return quote_source(keys)
    k = _kite()
    if not k:
        return {}
    out = {}
    for i in range(0, len(keys), BATCH):
        out.update(k.quote(keys[i:i + BATCH]) or {})
        time.sleep(0.35)                                     # Kite allows about one quote call a second
    return out


def _history(token: int, frm: date, to: date, interval: str = "5minute", oi: bool = True) -> list[dict]:
    if history_source:
        return history_source(token, frm, to, interval, oi)
    k = _kite()
    return k.historical_data(token, frm, to, interval, oi=oi) if k else []


def equity_tokens(names: list[str]) -> dict[str, int]:
    """{stock: its NSE instrument token}, for the volume backfill."""
    from routes.market import _instrument_list
    k = _kite()
    if not k:
        return {}
    wanted = set(names)
    return {r["tradingsymbol"]: r["instrument_token"] for r in _instrument_list(k, "NSE") if r.get("segment") == "NSE" and r["tradingsymbol"] in wanted}


def contracts(names: list[str], today: date) -> dict[str, list[dict]]:
    """{name: its nearest two futures (key, token)}, from Kite's instrument list."""
    from routes.market import _instrument_list
    k = _kite()
    if not k:
        return {}
    wanted, out = set(names), {}
    rows = [r for r in _instrument_list(k, "NFO") if r.get("instrument_type") == "FUT" and r.get("name") in wanted and str(r.get("expiry")) >= today.isoformat()]
    for r in sorted(rows, key=lambda r: str(r["expiry"])):
        got = out.setdefault(r["name"], [])
        if len(got) < 2:
            got.append({"key": f"NFO:{r['tradingsymbol']}", "token": r["instrument_token"]})
    return out


# ---------------------------------------------------------------- state

def _bucket(now: datetime) -> str:
    return now.replace(minute=now.minute - now.minute % 5).strftime("%H:%M")


def _file(day: str) -> Path | None:
    return DIR / f"flow_{day}.json" if DIR else None


def _fresh(day: str) -> None:
    """Today's state, loaded from disk after a restart."""
    if _state.get("day") == day:
        return
    _state.clear()
    _state.update(day=day, contracts={}, prev_oi={}, prev_loading=False, fut={}, cash={}, last_poll=0.0)
    f = _file(day)
    if f and f.exists():
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            _state.update(fut=saved.get("fut") or {}, cash=saved.get("cash") or {}, prev_oi=saved.get("prev_oi") or {})
        except (OSError, ValueError):
            log.warning("flow file unreadable; starting today's flow afresh")


def _save() -> None:
    f = _file(_state.get("day") or "")
    if not f or not _state.get("day"):
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fut": _state.get("fut") or {}, "cash": _state.get("cash") or {}, "prev_oi": _state.get("prev_oi") or {}}), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        log.warning("flow file not saved")


def _day_of(r: dict) -> str:
    return str(r["date"])[:10]


def _hhmm(r: dict) -> str:
    return str(r["date"])[11:16]


def _carry(rows: list[dict], day: str) -> list[tuple[str, int]]:
    """One contract's OI through today, oldest first, starting from its last reading before today. A thin next-month
    contract has no candle (and no OI reading) in many 5-minute slots; its OI there is the last one seen, not zero."""
    before = [r for r in rows if _day_of(r) < day and r.get("oi")]
    out = [("00:00", int(before[-1]["oi"]))] if before else []
    out += [(_hhmm(r), int(r["oi"])) for r in rows if _day_of(r) == day and r.get("oi")]
    return out


def _opening_oi(rows: list[dict], day: str) -> int:
    """A contract's OI at today's first 5-minute candle. Kite's 5-minute history carries a stale OI on yesterday's last
    candles (the exchange's closing OI arrives after the close); today's first reading matches the official close."""
    first = [r for r in rows if _day_of(r) == day and r.get("oi")]
    if first:
        return int(first[0]["oi"])
    before = [r for r in rows if _day_of(r) < day and r.get("oi")]
    return int(before[-1]["oi"]) if before else 0


def _oi_at(carried: list[tuple[str, int]], b: str) -> int:
    got = [v for k, v in carried if k <= b]
    return got[-1] if got else 0


def _backfill(cons: dict[str, list[dict]], tokens: dict[str, int], today: date) -> None:
    """In the background, once per backend start: today's 5-minute candles the live tape missed (futures price and OI,
    stock price and volume) and yesterday's closing OI, from Kite's history. Live buckets are never overwritten."""
    day = today.isoformat()
    pause = 0 if history_source else 0.35                    # Kite allows about three history calls a second
    for name, legs in cons.items():
        try:
            per = []
            for leg in legs:
                per.append(_history(leg["token"], today - timedelta(days=7), today))
                time.sleep(pause)
        except Exception:  # noqa: BLE001 - that name just has a shorter tape
            continue
        if not per or not per[0]:
            continue
        prev = [r for r in per[0] if _day_of(r) < day]
        pc = prev[-1]["close"] if prev else None
        oi_at = [_carry(rows, day) for rows in per]
        prev_oi = sum(_opening_oi(rows, day) for rows in per)
        with _lock:
            if _state.get("day") != day:
                return
            if prev_oi:
                _state["prev_oi"][name] = prev_oi
            series = _state["fut"].setdefault(name, {})
            for r in per[0]:
                b = _hhmm(r)
                if _day_of(r) == day and b not in series:
                    series[b] = {"p": r["close"], "oi": sum(_oi_at(o, b) for o in oi_at), "pc": pc}
    for name, tok in tokens.items():
        try:
            rows = _history(tok, today - timedelta(days=7), today, "5minute", False)
            time.sleep(pause)
        except Exception:  # noqa: BLE001
            continue
        prev = [r for r in rows if _day_of(r) < day]
        pc, cum = (prev[-1]["close"] if prev else None), 0
        with _lock:
            if _state.get("day") != day:
                return
            series = _state["cash"].setdefault(name, {})
            for r in rows:
                if _day_of(r) != day:
                    continue
                cum += int(r.get("volume") or 0)
                b = _hhmm(r)
                if b not in series:
                    series[b] = {"p": r["close"], "v": cum, "pc": pc}
    with _lock:
        if _state.get("day") == day:
            _state["prev_loading"] = False
            _save()


def poll(now: datetime, names: list[str], force: bool = False, indices=()) -> None:
    """One pass: every F&O stock's futures (price, OI, volume) and cash (price, volume, previous close) into today's 5-minute buckets."""
    day = now.date().isoformat()
    with _lock:
        _fresh(day)
        if not force and time.time() - _state["last_poll"] < POLL_SECONDS:
            return
        _state["last_poll"] = time.time()
        if not _state["contracts"]:
            _state["contracts"] = contracts(list(names) + list(indices), now.date())
        cons = _state["contracts"]
        if cons and not _state.get("backfilled"):
            _state["backfilled"] = _state["prev_loading"] = True
            threading.Thread(target=_backfill, args=(cons, equity_tokens(names), now.date()), daemon=True, name="flow-prev-oi").start()
    keys = [leg["key"] for legs in cons.values() for leg in legs] + [f"NSE:{n}" for n in names]
    try:
        q = _quote(keys)
    except Exception:  # noqa: BLE001 - the next pass tries again
        log.warning("flow quotes unavailable")
        return
    b = _bucket(now)
    with _lock:
        for name, legs in cons.items():
            got = [q.get(leg["key"]) for leg in legs]
            if not got or not got[0] or not got[0].get("last_price"):
                continue
            oi = sum(int((g or {}).get("oi") or 0) for g in got)
            _state["fut"].setdefault(name, {})[b] = {"p": got[0]["last_price"], "oi": oi, "pc": (got[0].get("ohlc") or {}).get("close")}
        for n in names:
            c = q.get(f"NSE:{n}")
            if c and c.get("last_price"):
                _state["cash"].setdefault(n, {})[b] = {"p": c["last_price"], "v": int(c.get("volume") or 0), "pc": (c.get("ohlc") or {}).get("close")}
        _save()


# ---------------------------------------------------------------- reading it

def _closed(series: dict, now: datetime) -> list[tuple[str, dict]]:
    """A name's buckets that have closed, oldest first (the one still forming is left out)."""
    b = _bucket(now)
    return [(k, series[k]) for k in sorted(series) if k < b]


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hour_ago(rows: list[tuple[str, dict]]) -> dict | None:
    """The bucket an hour before the last one, if the tape reaches back that far."""
    if not rows:
        return None
    want = _mins(rows[-1][0]) - 60
    old = [v for k, v in rows if _mins(k) <= want]
    return old[-1] if old else None


def classify(price_pct: float | None, oi_pct: float | None) -> str:
    if price_pct is None or oi_pct is None or abs(oi_pct) < OI_FLAT:
        return "no real change in OI"
    if oi_pct > 0:
        return "long build-up" if price_pct > 0 else "short build-up"
    return "short covering" if price_pct > 0 else "long unwinding"


def _pct(a, b) -> float | None:
    return round((a / b - 1) * 100, 2) if a and b else None


def oi_rows(now: datetime) -> list[dict]:
    """Every name's futures today and over the last hour: price %, OI % and what that combination means."""
    with _lock:
        if _state.get("day") != now.date().isoformat():
            return []
        out = []
        for name, series in _state["fut"].items():
            rows = _closed(series, now)
            if not rows:
                continue
            last, ago = rows[-1][1], _hour_ago(rows)
            prev = _state["prev_oi"].get(name)
            r = {"name": name, "price_pct": _pct(last["p"], last.get("pc")), "oi_pct": _pct(last["oi"], prev),
                 "price_60m_pct": _pct(last["p"], ago["p"]) if ago else None, "oi_60m_pct": _pct(last["oi"], ago["oi"]) if ago else None}
            out.append(r)
    # Every F&O name builds OI through the day (expiry-week rollover, and Kite's intraday OI runs ahead of the official
    # close), so a name's change only means something against the median name's: label each by its excess over that.
    day_med, hour_med = _median([r["oi_pct"] for r in out]), _median([r["oi_60m_pct"] for r in out])
    for r in out:
        r["oi_vs_fno_pct"] = None if r["oi_pct"] is None else round(r["oi_pct"] - day_med, 2)
        r["oi_60m_vs_fno_pct"] = None if r["oi_60m_pct"] is None else round(r["oi_60m_pct"] - hour_med, 2)
        r["today"] = classify(r["price_pct"], r["oi_vs_fno_pct"])
        r["last_hour"] = classify(r["price_60m_pct"], r["oi_60m_vs_fno_pct"])
    return out


def _median(xs) -> float:
    xs = sorted(x for x in xs if x is not None)
    return xs[(len(xs) - 1) // 2] if xs else 0.0


def market_oi_hour(rows: list[dict]) -> float:
    """The median F&O name's futures OI change over the last hour: the whole market's build-up, which a name must beat."""
    return _median([r["oi_60m_pct"] for r in rows])


def of(name: str, now: datetime) -> dict | None:
    """One name's futures flow, for its own read."""
    return next((r for r in oi_rows(now) if r["name"] == name), None)


def breadth(now: datetime) -> dict | None:
    """F&O stocks up and down on the day, now and an hour ago."""
    with _lock:
        if _state.get("day") != now.date().isoformat():
            return None
        nowc, agoc = [], []
        for series in _state["cash"].values():
            rows = _closed(series, now)
            if not rows:
                continue
            last, ago = rows[-1][1], _hour_ago(rows)
            if last.get("pc"):
                nowc.append(last["p"] - last["pc"])
            if ago and ago.get("pc"):
                agoc.append(ago["p"] - ago["pc"])

    def count(xs):
        up, down = sum(x > 0 for x in xs), sum(x < 0 for x in xs)
        return {"up": up, "down": down, "total": len(xs), "up_share_pct": round(up / len(xs) * 100, 1) if xs else None}
    if not nowc:
        return None
    return {"now": count(nowc), "an_hour_ago": count(agoc) if agoc else None}


def _volumes(series: dict, now: datetime) -> list[tuple[str, int, float, float]]:
    """(bucket, volume traded in it, its last price, the previous bucket's last price) for consecutive closed buckets."""
    rows = _closed(series, now)
    out = []
    for (k0, a), (k1, b) in zip(rows, rows[1:]):
        if _mins(k1) - _mins(k0) == 5 and b["v"] >= a["v"]:
            out.append((k1, b["v"] - a["v"], b["p"], a["p"]))
    return out


def surges(now: datetime, minutes: int = 30) -> list[dict]:
    """5-minute candles that traded VOL_SURGE times the median of the hour before them, with a real price move."""
    with _lock:
        cash = dict(_state["cash"]) if _state.get("day") == now.date().isoformat() else {}
    out = []
    for name, series in cash.items():
        vols = _volumes(series, now)
        for i in range(len(vols)):
            k, v, p, p0 = vols[i]
            if _mins(_bucket(now)) - _mins(k) > minutes + 5 or k <= "09:15":
                continue
            base = sorted(x[1] for x in vols[max(0, i - HOUR):i] if x[0] > "09:15")
            if len(base) < VOL_BASE:
                continue
            med = base[len(base) // 2]
            move = _pct(p, p0) or 0.0
            if med > 0 and v >= VOL_SURGE * med and abs(move) >= VOL_MIN_MOVE:
                out.append({"name": name, "at": k, "volume_x": round(v / med, 1), "move_pct": move})
    out.sort(key=lambda r: (r["at"], -r["volume_x"]), reverse=True)
    return out


def board(now: datetime) -> dict:
    """What Claude reads: breadth, who is building positions today and in the last hour, and recent volume surges."""
    rows = oi_rows(now)

    def top(kind, key, n=TOP, field="today"):
        pick = [r for r in rows if r[field] == kind and r[key] is not None]
        pick.sort(key=lambda r: -abs(r[key]))
        return [{k: r[k] for k in ("name", "price_pct", "oi_vs_fno_pct", "price_60m_pct", "oi_60m_vs_fno_pct")} for r in pick[:n]]
    with _lock:
        loaded = bool(_state.get("prev_oi"))
    return {
        "breadth_fno_stocks": breadth(now),
        "futures_oi_fno_median_pct": {"since_open": _median([r["oi_pct"] for r in rows]), "last_hour": market_oi_hour(rows)},
        "futures_oi_today": {"long_buildup": top("long build-up", "oi_vs_fno_pct"), "short_buildup": top("short build-up", "oi_vs_fno_pct"),
                             "short_covering": top("short covering", "oi_vs_fno_pct", 3), "long_unwinding": top("long unwinding", "oi_vs_fno_pct", 3)} if loaded else "today's opening OI still loading",
        "futures_oi_last_hour": {"long_buildup": top("long build-up", "oi_60m_vs_fno_pct", field="last_hour"), "short_buildup": top("short build-up", "oi_60m_vs_fno_pct", field="last_hour")},
        "volume_surges_last_30m": surges(now)[:8],
        "note": "OI is near plus next month futures. Every F&O name builds OI through the day, so oi_vs_fno_pct is a name's OI change since the open "
                "minus the median F&O name's, and oi_60m_vs_fno_pct the same for the last hour; the build-up labels use those. Price and OI rising together is fresh buying; price falling with OI rising is fresh shorting; "
                "price rising with OI falling is short covering, which often fades. A volume surge with a price move is someone acting now.",
    }


# ---------------------------------------------------------------- early looks

def events(now: datetime, seen: set, state: dict) -> list[tuple[str, str]]:
    """Fresh build-up in the last hour (OI up OI_HOUR% with the price moving PRICE_HOUR%), and volume surges on the last closed candle.
    A build-up can fire again the same way once its last-hour OI change cools below half the threshold."""
    out = []
    armed, fired = state.setdefault("armed", {}), state.setdefault("fired", {})
    rows = oi_rows(now)
    market = market_oi_hour(rows)                            # oi_rows already holds the excess; kept for the message
    for r in rows:
        oi60, p60 = r["oi_60m_pct"], r["price_60m_pct"]
        if oi60 is None or p60 is None:
            continue
        excess = oi60 - market
        for d in ("long", "short"):
            if excess < OI_HOUR / 2:
                armed[f"{r['name']}|{d}"] = True
        if excess < OI_HOUR or abs(p60) < PRICE_HOUR:
            continue
        d = "long" if p60 > 0 else "short"
        side = f"{r['name']}|{d}"
        if not armed.get(side, True):
            continue
        key = f"{r['name']}|oi|{d}|{fired.get(side, 0) + 1}"
        if key in seen:
            continue
        armed[side], fired[side] = False, fired.get(side, 0) + 1
        day = f" (today: price {r['price_pct']:+.2f}%, OI {r['oi_vs_fno_pct']:+.1f}% vs the F&O median since the open)" if r["price_pct"] is not None and r["oi_vs_fno_pct"] is not None else ""
        out.append((key, f"{r['name']}: futures OI {oi60:+.1f}% (F&O median {market:+.1f}%) and price {p60:+.2f}% in the last hour, fresh {d} build-up{day}"))
    last = (datetime.combine(now.date(), datetime.min.time()) + timedelta(minutes=_mins(_bucket(now)) - 5)).strftime("%H:%M")
    for s in surges(now, minutes=5):
        if s["at"] != last:
            continue
        key = f"{s['name']}|vol|{s['at']}"
        if key not in seen:
            out.append((key, f"{s['name']} traded {s['volume_x']:g}x its usual 5-minute volume at {s['at']} while moving {s['move_pct']:+.2f}%"))
    return out
