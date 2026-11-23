"""Gap Momentum desk: a full paper day against a fake Kite (no network, no real orders)."""
import datetime as dt

import pytest

from strategies.gap_momentum import broker as gm_broker, desk
from strategies.gap_momentum.config import Config

TODAY = dt.date(2026, 11, 18)
EXP = "2026-11-24"
STOCKS = [f"STK{i}" for i in range(15)]
LOT = 250


def _weekdays_before(day, n):
    out, d = [], day - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


class Clock:
    def __init__(self):
        self.t = dt.datetime.combine(TODAY, dt.time(9, 0))

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)


class FakeKite:
    """STK0 gaps up 1 ATR. Options start at ₹10 and drift up ('trend') or fall 0.4% a minute ('crash')."""
    def __init__(self, clock, path, prev_close=1000.0, nifty_trend=0.004):
        self.clock, self.path, self.prev_close, self.nifty_trend = clock, path, prev_close, nifty_trend
        exp = dt.date.fromisoformat(EXP)
        self.ins = []
        for s in STOCKS:
            self.ins.append(dict(instrument_token=len(self.ins) + 1, tradingsymbol=f"{s}FUT", name=s, segment="NFO-FUT", expiry=exp, strike=0.0, instrument_type="FUT", lot_size=LOT))
            for k in range(960, 1041, 20):
                for t in ("CE", "PE"):
                    self.ins.append(dict(instrument_token=len(self.ins) + 1, tradingsymbol=f"{s}{k}{t}", name=s, segment="NFO-OPT", expiry=exp, strike=float(k), instrument_type=t, lot_size=LOT))

        self.name = {i["tradingsymbol"]: i["name"] for i in self.ins}

    def historical_data(self, token, start, end, interval):
        """Nifty 50: 40 sessions, rising by nifty_trend a day. A stock future: 10 sessions at 1000 with a 20-point range (ATR 20)."""
        if token == 256265:
            days = _weekdays_before(TODAY, 40)
            return [dict(date=dt.datetime.combine(d, dt.time()), close=24000 * (1 + self.nifty_trend) ** n) for n, d in enumerate(days)]
        return [dict(date=dt.datetime.combine(d, dt.time()), open=1000, high=1010, low=990, close=1000) for d in _weekdays_before(TODAY, 10)]

    def instruments(self, ex):
        return self.ins

    def _ltp(self, ts):
        if ts.endswith("FUT"):
            return 1020.0 if ts.startswith("STK0") else 1000.0
        mins = (self.clock.t - dt.datetime.combine(TODAY, dt.time(9, 15))).total_seconds() / 60
        return round(max(0.05, 10 * (1 + 0.002 * mins if self.path == "trend" else 1 - 0.004 * mins)), 2)

    def quote(self, keys):
        out = {}
        for k in keys:
            ts = k.split(":", 1)[1]
            px = self._ltp(ts)
            rank = STOCKS.index(self.name[ts])
            out[k] = dict(last_price=px, last_trade_time=self.clock.t, volume=LOT * 100 * (15 - rank), average_price=px, ohlc=dict(open=px, close=self.prev_close if ts.endswith("FUT") else px),
                          depth=dict(buy=[dict(price=round(px * 0.995, 2))], sell=[dict(price=round(px * 1.005, 2))]))
        return out


@pytest.fixture()
def day(tmp_path, monkeypatch):
    state = tmp_path / "state"
    clk = Clock()
    sent = []
    monkeypatch.setattr(desk, "CFG", Config(state_dir=str(state)))
    monkeypatch.setattr(desk, "now", clk.now)
    monkeypatch.setattr(desk, "_sleep", clk.sleep)
    monkeypatch.setattr(desk, "_engine", None)
    monkeypatch.setattr(desk.notify, "send_once", lambda mid, text, now=None: sent.append((mid, text)) or True)

    def run(path, until=dt.time(15, 20), prev_close=1000.0, nifty_trend=0.004):
        kite = FakeKite(clk, path, prev_close, nifty_trend)
        monkeypatch.setattr(desk, "_kite_factory", lambda: kite)
        while clk.t.time() < until:
            desk.tick(clk.t)
            clk.sleep(5)
        return desk.view(TODAY.isoformat())
    return run, sent, clk, state


def test_trend_day_holds_to_1515_and_sends_entry_and_exit(day):
    run, sent, _, _ = day
    v = run("trend")
    assert v["regime"] == "UP"
    assert v["signals"][0]["symbol"] == "STK0" and v["signals"][0]["side"] == "CE"
    (t,) = v["day_trades"]
    assert t["symbol"] == "STK0" and t["side"] == "CE" and t["strike"] == 1020.0
    assert t["reason"] == "TIME EXIT" and t["exit_time"][11:16] >= "15:15"
    assert t["lots"] == 3 and t["qty"] == 3 * LOT                       # (3000 − 120) ÷ (30% × 10.05 × 250) = 3.8 → 3 lots
    assert t["trigger"] == pytest.approx(t["entry"] * 0.7, abs=0.06)
    assert t["max_loss"] <= 3000
    assert t["pnl"] == pytest.approx(t["gross"] - t["charges"])
    assert [m for m, _ in sent] == [f"gap_momentum|20261118|{t['tradingsymbol']}|open", f"gap_momentum|20261118|{t['tradingsymbol']}|exit"]
    entry, exit_ = sent[0][1], sent[1][1]
    assert "BUY CALL" in entry and "STK0 1020 CE" in entry and "Stop (SL)" in entry and "Hold to 15:15" in entry and "1R level" in entry
    assert "15:15 exit" in exit_ and "Booked P&L" in exit_


def test_crash_day_stops_out_within_budget(day):
    run, sent, _, _ = day
    v = run("crash")
    (t,) = v["day_trades"]
    assert t["reason"] == "STOP"
    assert t["exit_time"][11:16] < "15:15"
    assert t["pnl"] >= -3000 * 1.15
    assert "Stop hit" in sent[-1][1]


def test_one_trade_a_day_and_restart_keeps_position(day, monkeypatch):
    run, sent, clk, _ = day
    run("trend", until=dt.time(10, 0))
    assert desk._engine.pos is not None
    monkeypatch.setattr(desk, "_engine", None)                             # app restart mid-day
    v = run("trend")
    assert len(v["day_trades"]) == 1 and v["day_trades"][0]["reason"] == "TIME EXIT"
    assert len([m for m, _ in sent if m.endswith("|open")]) == 1


def test_sideways_market_books_nothing_and_sends_nothing(day):
    run, sent, _, _ = day
    v = run("trend", nifty_trend=0.0)
    assert v["regime"] == "SIDEWAYS" and not v["day_trades"] and not sent


def test_paper_broker_has_no_way_to_place_real_orders():
    assert not hasattr(gm_broker, "KiteBroker")
    assert not any(hasattr(gm_broker.PaperBroker, m) for m in ("_place", "place_order"))


def test_candle_and_quote_close_disagreeing_means_no_trade(day):
    run, sent, _, _ = day
    v = run("trend", prev_close=1040.0)                                    # the quote says yesterday closed at 1040, the candles say 1000
    assert not v["day_trades"] and not sent
    assert any("don't match Kite's previous close" in e["msg"] for e in v["events"])


def test_universe_is_ranked_by_option_turnover(day):
    run, _, _, _ = day
    v = run("trend", until=dt.time(9, 10))
    assert [u["symbol"] for u in v["universe"]] == STOCKS                  # STK0 traded the most options
    assert v["universe"][0]["prev_close"] == 1000 and v["universe"][0]["atr"] == 20


def test_a_finished_day_stays_finished_after_restart(day, monkeypatch):
    run, sent, clk, _ = day
    clk.t = dt.datetime.combine(TODAY, dt.time(16, 0))                    # app starts after the entry window
    run("trend", until=dt.time(16, 1))
    monkeypatch.setattr(desk, "_engine", None)                             # restart
    v = run("trend", until=dt.time(16, 2))
    msgs = [e["msg"] for e in v["events"]]
    assert v["done"] and len(msgs) == 1 and msgs[0].startswith("Started after 09:13")
