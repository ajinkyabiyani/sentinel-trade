"""Stock and index metrics for the terminal: daily candles once a day from Kite, marked to the live quote on every read.

Definitions (the same everywhere on the terminal):
  change      last vs Kite's previous close.          gap        today's open vs previous close.
  period      last vs the close on or before the calendar date 1 week / 1, 3, 6 months / 1 year ago, or 31 Dec for YTD.
  RVOL        today's volume / (20-day average volume x share of the 375-minute session elapsed). Assumes volume comes
              evenly through the day, so it reads high after the open, when real volume is heaviest.
  ATR%        14-day Wilder ATR as a share of the last price.
  RSI         14-day Wilder RSI, with today's live price as the latest close.
  beta        slope of the stock's daily returns on Nifty 50's, last 250 sessions.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta

from .sources import CACHE_DIR, IST, nifty500, screener_company

log = logging.getLogger("kite-desk.terminal")

CANDLE_DAYS = 400
HIST_PACE = 0.34              # Kite allows 3 historical calls a second
BEHIND_CHECK_S = 900          # how often a built day asks Kite whether a newer completed session has appeared
CRAWL_PACE = 4.0              # seconds between Screener pages: one company at a time, slowly
CRAWL_REFRESH_DAYS = 7
SESSION_MINUTES = 375
MARKET_OPEN = (9, 15)

HEADLINE_INDICES = {"NIFTY 50": "NSE", "SENSEX": "BSE", "NIFTY BANK": "NSE", "NIFTY MIDCAP 100": "NSE"}
SECTOR_INDICES = {"NIFTY BANK": "Banks", "NIFTY FIN SERVICE": "Financial Services", "NIFTY IT": "IT", "NIFTY AUTO": "Auto",
                  "NIFTY PHARMA": "Pharma", "NIFTY FMCG": "FMCG", "NIFTY METAL": "Metals", "NIFTY ENERGY": "Energy",
                  "NIFTY REALTY": "Realty", "NIFTY MEDIA": "Media", "NIFTY PSU BANK": "PSU Banks", "NIFTY INFRA": "Infrastructure"}
MOOD_BANDS = ((25, "Extreme fear"), (45, "Fear"), (55, "Neutral"), (76, "Greed"), (101, "Extreme greed"))

# Sector rotation by RSI: the sector indices above plus a few more, and NSE's thematic indices (Kite tradingsymbols).
ROTATION_SECTORS = {**SECTOR_INDICES, "NIFTY PVT BANK": "Private Banks", "NIFTY HEALTHCARE": "Healthcare", "NIFTY OIL AND GAS": "Oil & Gas",
                    "NIFTY CONSR DURBL": "Consumer Durables", "NIFTY CHEMICALS": "Chemicals"}
THEME_INDICES = {"NIFTY IND DEFENCE": "Defence", "NIFTY RAILWAYS PSU": "Railways PSU", "NIFTY EV": "EV & new-age auto", "NIFTY CAPITAL MKT": "Capital markets",
                 "NIFTY CPSE": "CPSE", "NIFTY PSE": "PSE", "NIFTY IND DIGITAL": "Digital", "NIFTY IND TOURISM": "Tourism", "NIFTY INDIA MFG": "Manufacturing",
                 "NIFTY HOUSING": "Housing", "NIFTY CORE HOUSING": "Core housing", "NIFTY MOBILITY": "Mobility", "NIFTY CONSUMPTION": "Consumption",
                 "NIFTY NEW CONSUMP": "New-age consumption", "NIFTY RURAL": "Rural", "NIFTY INFRALOG": "Infra & logistics", "NIFTY TRANS LOGIS": "Transport & logistics",
                 "NIFTY INTERNET": "Internet", "NIFTY COMMODITIES": "Commodities", "NIFTY MNC": "MNC", "NIFTY IPO": "IPOs", "NIFTYCONGLOMERATE": "Conglomerates",
                 "NIFTY SERV SECTOR": "Services", "NIFTY WAVES": "Media & entertainment (WAVES)"}
ROTATION_LOOKBACK = 5         # sessions: RSI now against RSI this many sessions ago is the rotation's momentum
ROTATION_TAIL = 6             # points of history drawn behind each dot


def rsi_series(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI after every close (None until there are n changes)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0) for d in deltas[:n]) / n
    out[n] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i, d in enumerate(deltas[n:], start=n + 1):
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def weekly_closes(rows: list[list]) -> list[float]:
    """The last close of each ISO week, from [date, o, h, l, c, v] rows."""
    weeks: dict[tuple, float] = {}
    for r in rows:
        y, w, _ = date.fromisoformat(r[0]).isocalendar()
        weeks[(y, w)] = r[4]
    return [weeks[k] for k in sorted(weeks)]


def quadrant(rsi: float, change: float) -> str:
    if rsi >= 50:
        return "Leading" if change >= 0 else "Weakening"
    return "Improving" if change >= 0 else "Lagging"


def rotation_row(label: str, key: str, rows: list[list], last: float | None, today: str) -> dict | None:
    """One index on the rotation map: daily RSI(14) with today's live price, its change over ROTATION_LOOKBACK sessions,
    weekly RSI(14), 1-week and 1-month returns, the quadrant, and a short tail of (RSI, change) points. None without enough history."""
    closes = [r[4] for r in rows if r[0] < today]
    if last:
        closes = closes + [last]
    if len(closes) < 14 + ROTATION_LOOKBACK + ROTATION_TAIL + 1:
        return None
    rsi = rsi_series(closes)
    tail = []
    for i in range(len(closes) - ROTATION_TAIL, len(closes)):
        now_, then = rsi[i], rsi[i - ROTATION_LOOKBACK]
        if now_ is not None and then is not None:
            tail.append([round(now_, 1), round(now_ - then, 1)])
    if not tail:
        return None
    r_now, d_now = tail[-1]
    wk = rsi_series(weekly_closes([r for r in rows if r[0] < today] + ([[today, 0, 0, 0, last, 0]] if last else [])))
    week_rsi = next((x for x in reversed(wk) if x is not None), None)
    ret = lambda k: round((closes[-1] / closes[-1 - k] - 1) * 100, 2) if len(closes) > k else None  # noqa: E731
    zone = "Overbought" if r_now >= 70 else "Oversold" if r_now <= 30 else None
    return {"label": label, "key": key, "last": round(closes[-1], 2), "rsi": r_now, "rsi_change": d_now, "rsi_week": None if week_rsi is None else round(week_rsi, 1),
            "ret_1w": ret(5), "ret_1m": ret(21), "quadrant": quadrant(r_now, d_now), "zone": zone, "tail": tail}


def wilder_rsi_state(closes: list[float], n: int = 14) -> tuple[float, float] | None:
    """(average gain, average loss) after the last close."""
    if len(closes) <= n:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0) for d in deltas[:n]) / n
    loss = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    return gain, loss


def rsi_with_last(state: tuple[float, float] | None, prev: float, last: float, n: int = 14) -> float | None:
    if not state or not prev or not last:
        return None
    d = last - prev
    gain = (state[0] * (n - 1) + max(d, 0)) / n
    loss = (state[1] * (n - 1) + max(-d, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def wilder_atr(rows: list[list], n: int = 14) -> float | None:
    """rows: [date, open, high, low, close, volume]"""
    if len(rows) <= n:
        return None
    trs = [max(r[2] - r[3], abs(r[2] - p[4]), abs(r[3] - p[4])) for p, r in zip(rows, rows[1:])]
    atr = sum(trs[:n]) / n
    for t in trs[n:]:
        atr = (atr * (n - 1) + t) / n
    return atr


def beta_of(rows: list[list], bench: dict[str, float], n: int = 250) -> float | None:
    """Least-squares slope of daily returns against the benchmark's, on the dates both traded."""
    pairs = []
    tail = rows[-n - 1:]
    for p, r in zip(tail, tail[1:]):
        a, b = bench.get(p[0]), bench.get(r[0])
        if a and b and p[4]:
            pairs.append((b / a - 1, r[4] / p[4] - 1))
    if len(pairs) < 60:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    var = sum((x - mx) ** 2 for x, _ in pairs)
    return sum((x - mx) * (y - my) for x, y in pairs) / var if var else None


def period_starts(today: date) -> dict[str, date]:
    def months_back(k: int) -> date:
        y, m = (today.year * 12 + today.month - 1 - k) // 12, (today.year * 12 + today.month - 1 - k) % 12 + 1
        last_day = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
        return date(y, m, min(today.day, last_day))
    return {"1W": today - timedelta(days=7), "1M": months_back(1), "3M": months_back(3), "6M": months_back(6),
            "YTD": date(today.year - 1, 12, 31), "1Y": months_back(12)}


def close_on_or_before(rows: list[list], day: date) -> float | None:
    key = day.isoformat()
    for r in reversed(rows):
        if r[0] <= key:
            return r[4]
    return None


def session_fraction(now: datetime) -> float:
    """Share of the cash session elapsed: 1 outside market hours, so a closed-market RVOL is the whole day's."""
    if now.weekday() >= 5:
        return 1.0
    minutes = (now.hour - MARKET_OPEN[0]) * 60 + now.minute - MARKET_OPEN[1]
    return 1.0 if minutes <= 0 or minutes >= SESSION_MINUTES else max(minutes, 5) / SESSION_MINUTES


def static_metrics(rows: list[list], bench: dict[str, float] | None) -> dict:
    """What only changes once a day, from completed daily candles."""
    closes = [r[4] for r in rows]
    year = rows[-250:]
    vols = [r[5] for r in rows[-20:]]
    return {"closes": closes[-200:], "avg_vol": sum(vols) / len(vols) if vols else None,
            "atr": wilder_atr(rows), "rsi_state": wilder_rsi_state(closes),
            "hi52": max((r[2] for r in year), default=None), "lo52": min((r[3] for r in year), default=None),
            "beta": beta_of(rows, bench) if bench else None}


def _sma(closes: list[float], last: float, n: int) -> float | None:
    return (sum(closes[-(n - 1):]) + last) / n if len(closes) >= n - 1 else None


def stock_row(info: dict, static: dict, rows: list[list], q: dict, now: datetime, starts: dict, cap: dict | None) -> dict | None:
    """One stock's live row for the heatmap and scanner. None when Kite has no price for it."""
    o = q.get("ohlc") or {}
    last, prev, opened = q.get("last_price"), o.get("close"), o.get("open")
    if not last or not prev:
        return None
    volume = q.get("volume") or 0
    avg_vol = static.get("avg_vol")
    frac = session_fraction(now)
    closes = static["closes"]
    sma20, sma50, sma200 = (_sma(closes, last, n) for n in (20, 50, 200))
    hi52 = max(static["hi52"] or last, o.get("high") or last)
    lo52 = min(static["lo52"] or last, o.get("low") or last)
    ret = {k: round((last / base - 1) * 100, 2) if (base := close_on_or_before(rows, d)) else None for k, d in starts.items()}
    mcap = cap["mcap_cr"] * last / cap["price"] if cap and cap.get("mcap_cr") and cap.get("price") else None
    pct = lambda a, b: round((a / b - 1) * 100, 2) if a and b else None  # noqa: E731
    return {
        **info, "last": last, "prev": prev, "open": opened, "high": o.get("high"), "low": o.get("low"),
        "chg": pct(last, prev), "gap": pct(opened, prev), "volume": volume, "value_cr": round(volume * (q.get("average_price") or last) / 1e7, 2),
        "avg_vol": round(avg_vol) if avg_vol else None, "rvol": round(volume / (avg_vol * frac), 2) if avg_vol and volume else None,
        "atr_pct": round(static["atr"] / last * 100, 2) if static.get("atr") else None,
        "rsi": round(x, 1) if (x := rsi_with_last(static.get("rsi_state"), closes[-1] if closes else None, last)) is not None else None,
        "vs_sma20": pct(last, sma20), "vs_sma50": pct(last, sma50), "vs_sma200": pct(last, sma200),
        "hi52": hi52, "lo52": lo52, "from_hi52": pct(last, hi52), "from_lo52": pct(last, lo52), "beta": round(static["beta"], 2) if static.get("beta") is not None else None,
        "ret": ret, "mcap_cr": round(mcap) if mcap else None, "pe": cap.get("pe") if cap else None,
    }


def mood(rows: list[dict], nifty_last: float | None, nifty_closes: list[float], vix_last: float | None, vix_closes: list[float]) -> dict | None:
    """Market mood 0-100, this app's own measure and not a published index: the plain average of five readings.
    Stocks are the Nifty 500; a reading without enough data is left out and says so."""
    parts = []
    if vix_last and len(vix_closes) >= 60:
        below = sum(1 for v in vix_closes[-250:] if v < vix_last) / len(vix_closes[-250:])
        parts.append({"name": "Volatility", "score": round((1 - below) * 100), "note": f"India VIX {vix_last:.2f}, higher than {below * 100:.0f}% of the past year"})
    if nifty_last and len(nifty_closes) >= 125:
        sma = sum(nifty_closes[-125:]) / 125
        dist = (nifty_last / sma - 1) * 100
        parts.append({"name": "Momentum", "score": round(max(0, min(100, 50 + dist * 10))), "note": f"Nifty {dist:+.1f}% vs its 125-day average"})
    priced = [r for r in rows if r.get("chg") is not None]
    if priced:
        up = sum(r["chg"] > 0 for r in priced)
        down = sum(r["chg"] < 0 for r in priced)
        if up + down:
            parts.append({"name": "Breadth", "score": round(up / (up + down) * 100), "note": f"{up} advancing, {down} declining"})
    trend = [r for r in rows if r.get("vs_sma50") is not None]
    if trend:
        share = sum(r["vs_sma50"] > 0 for r in trend) / len(trend) * 100
        parts.append({"name": "Trend", "score": round(share), "note": f"{share:.0f}% of stocks above their 50-day average"})
    highs = sum(1 for r in rows if r.get("from_hi52") is not None and r["from_hi52"] >= -5)
    lows = sum(1 for r in rows if r.get("from_lo52") is not None and r["from_lo52"] <= 5)
    if highs + lows:
        parts.append({"name": "Highs vs lows", "score": round(highs / (highs + lows) * 100), "note": f"{highs} within 5% of a 52-week high, {lows} of a low"})
    if not parts:
        return None
    score = round(sum(p["score"] for p in parts) / len(parts))
    return {"score": score, "label": next(label for limit, label in MOOD_BANDS if score < limit), "parts": parts}


class Store:
    """Daily candles for the Nifty 500 and the terminal's indices, rebuilt once a day; Screener summaries crawled slowly."""

    def __init__(self):
        self.lock = threading.Lock()
        self.universe: list[dict] = []
        self.candles: dict[str, list[list]] = {}     # stock symbol or index name -> [date, o, h, l, c, v]
        self.static: dict[str, dict] = {}
        self.caps: dict[str, dict] = {}
        self.built: str | None = None
        self.progress = {"running": False, "done": 0, "total": 0, "error": ""}
        self._checked = 0.0
        self.crawl = {"running": False, "done": 0, "total": 0, "paused": ""}
        self._load()

    # ---- disk ----
    def _candle_file(self):
        return CACHE_DIR / "candles.json"

    def _caps_file(self):
        return CACHE_DIR / "caps.json"

    def _load(self) -> None:
        try:
            d = json.loads(self._candle_file().read_text(encoding="utf-8"))
            self.candles, self.built = d["candles"], d["built"]
            self._derive()
        except (OSError, ValueError, KeyError):
            pass
        try:
            self.caps = json.loads(self._caps_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def _save(self, path, data) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    def _derive(self) -> None:
        bench = {r[0]: r[4] for r in self.candles.get("NIFTY 50", [])}
        self.static = {k: static_metrics(v, bench) for k, v in self.candles.items() if v}

    # ---- candles ----
    def due(self, now: datetime) -> bool:
        return self.built != now.date().isoformat() and not self.progress["running"]

    def last_session(self) -> str | None:
        rows = self.candles.get("NIFTY 50") or []
        return rows[-1][0] if rows else None

    def behind(self, kite, instruments, now: datetime) -> bool:
        """True when today's build is missing a completed session Kite now has. A build just after midnight can run before Kite
        publishes the previous day's candle, and would otherwise keep it out until the next day. One small request, every 15 minutes."""
        if self.built != now.date().isoformat() or self.progress["running"] or time.time() - self._checked < BEHIND_CHECK_S:
            return False
        self._checked = time.time()
        token = self._indices(instruments).get("NIFTY 50")
        if not token:
            return False
        rows = fetch_daily(kite, token, now.date(), days=10)
        newest = rows[-1][0] if rows else None
        if newest and newest > (self.last_session() or ""):
            log.info("terminal candles end %s but Kite has %s: rebuilding", self.last_session(), newest)
            return True
        return False

    def _indices(self, instruments) -> dict[str, int]:
        wanted = {**{k: v for k, v in HEADLINE_INDICES.items()}, **{k: "NSE" for k in {**ROTATION_SECTORS, **THEME_INDICES}}, "INDIA VIX": "NSE"}
        tokens = {}
        for exch in ("NSE", "BSE"):
            for r in instruments(exch):
                if r.get("segment") == "INDICES" and wanted.get(r["tradingsymbol"]) == exch:
                    tokens[r["tradingsymbol"]] = r["instrument_token"]
        return tokens

    def refresh(self, kite, instruments, now: datetime) -> None:
        """Completed daily candles only: today's bar is left to the live quote."""
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.universe = nifty500()
            by_symbol = {r["tradingsymbol"]: r["instrument_token"] for r in instruments("NSE")
                         if r.get("segment") == "NSE" and r.get("instrument_type") == "EQ"}
            jobs = list(self._indices(instruments).items())
            jobs += [(u["symbol"], by_symbol[u["symbol"]]) for u in self.universe if u["symbol"] in by_symbol]
            self.progress = {"running": True, "done": 0, "total": len(jobs), "error": ""}
            today, out = now.date(), {}
            for i, (name, token) in enumerate(jobs):
                out[name] = fetch_daily(kite, token, today)
                time.sleep(HIST_PACE)
                self.progress["done"] = i + 1
            self.candles, self.built = out, today.isoformat()
            self._derive()
            self._save(self._candle_file(), {"built": self.built, "candles": out})
        except Exception as exc:  # noqa: BLE001 - shown on the page, retried on the next pass
            log.exception("terminal candles failed")
            self.progress["error"] = str(exc)
        finally:
            self.progress["running"] = False
            self.lock.release()

    # ---- Screener summaries ----
    def crawl_caps(self, should_stop=lambda: False) -> None:
        """Market cap and ratios for the universe, one page every CRAWL_PACE seconds, skipping what is recent.
        A refusal ends the pass; the next one starts later."""
        cutoff = (datetime.now(IST) - timedelta(days=CRAWL_REFRESH_DAYS)).isoformat()
        todo = [u["symbol"] for u in self.universe if self.caps.get(u["symbol"], {}).get("at", "") < cutoff]
        self.crawl = {"running": True, "done": 0, "total": len(todo), "paused": ""}
        try:
            for i, sym in enumerate(todo):
                if should_stop():
                    return
                res = screener_company(sym, ttl=CRAWL_REFRESH_DAYS * 86400)
                d = res["data"]
                if d:
                    self.caps[sym] = {k: d.get(k) for k in ("mcap_cr", "price", "pe", "roe", "roce", "div_yield")} | {"at": res["fetched_at"]}
                elif res["error"] and ("answered" in res["error"] or "refused" in res["error"]):
                    self.crawl["paused"] = res["error"]
                    return
                self.crawl["done"] = i + 1
                if (i + 1) % 25 == 0:
                    self._save(self._caps_file(), self.caps)
                time.sleep(CRAWL_PACE)
        finally:
            self._save(self._caps_file(), self.caps)
            self.crawl["running"] = False

    # ---- reads ----
    def rows(self, quotes: dict, now: datetime) -> list[dict]:
        starts = period_starts(now.date())
        out = []
        for u in self.universe or nifty500():
            sym = u["symbol"]
            q = quotes.get(f"NSE:{sym}")
            static = self.static.get(sym)
            if q and static:
                row = stock_row(u, static, self.candles[sym], q, now, starts, self.caps.get(sym))
                if row:
                    out.append(row)
        return out


def fetch_daily(kite, token: int, today: date, days: int = CANDLE_DAYS) -> list[list]:
    """Completed daily candles as [date, o, h, l, c, v]."""
    for attempt in range(4):
        try:
            rows = kite.historical_data(token, today - timedelta(days=days), today, "day")
            break
        except Exception as exc:  # noqa: BLE001 - rate limit or a network blip
            if type(exc).__name__ == "TokenException":
                raise
            if attempt == 3:
                log.warning("candles %s: %s", token, exc)
                return []
            time.sleep(1.5)
    key = today.isoformat()
    return [[str(r["date"])[:10], r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0]
            for r in rows if str(r["date"])[:10] < key]


store = Store()
