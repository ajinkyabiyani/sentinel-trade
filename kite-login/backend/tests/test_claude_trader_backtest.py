"""The Claude Trader backtest's plumbing: expiries, contract names, the chain built from Dhan's rolling strikes, following a trade."""
from datetime import date

from strategies import claude_trader as ct
from strategies import claude_trader_backtest as bt
from strategies import dhan_data


def test_expiries_weekly_nifty_monthly_rest():
    assert bt.expiries("NIFTY", date(2026, 10, 1))[:2] == [date(2026, 10, 6), date(2026, 10, 13)]
    assert bt.expiries("NIFTY", date(2026, 10, 6))[0] == date(2026, 10, 6)          # expiry day: that day's contract
    assert bt.expiries("BANKNIFTY", date(2026, 10, 1))[0] == date(2026, 10, 27)
    assert bt.expiries("RELIANCE", date(2026, 10, 28))[0] > date(2026, 10, 28)


def test_contract_names_follow_kite():
    assert bt.tsym("NIFTY", date(2026, 10, 6), 22600.0, "CE") == "NIFTY26O0622600CE"
    assert bt.tsym("NIFTY", date(2026, 10, 27), 22600.0, "PE") == "NIFTY26OCT22600PE"
    assert bt.tsym("RELIANCE", date(2026, 10, 27), 1172.5, "CE") == "RELIANCE26OCT1172.5CE"


def _fake_rolling(spot_path):
    """Strikes 100 apart; ATM follows spot; premium = 50 + (spot - strike)/2 for calls."""
    def rolling(name, day, offset, call, code=1, monthly=None, interval=1):
        out = {}
        for lb, spot in spot_path.items():
            atm = round(spot / 100) * 100
            k = atm + 100 * offset
            p = max(5.0, 50 + ((spot - k) if call else (k - spot)) / 2)
            out[lb] = {"o": p, "h": p + 2, "l": p - 2, "c": p, "iv": 15.0, "oi": 1000 * (10 - abs(offset)), "v": 100, "spot": spot, "strike": float(k)}
        return out
    return rolling


def test_chain_and_held_contract(monkeypatch):
    path = {"09:27": 22000.0, "09:28": 22010.0, "09:29": 22020.0, "09:30": 22160.0, "09:31": 22250.0}
    monkeypatch.setattr(dhan_data, "rolling", _fake_rolling(path))
    D = bt.Day("2026-10-01", [], {"NIFTY": 75}, {})
    ch = D.chain("NIFTY", "09:30")
    assert ch["atm"] == 22000.0 and ch["lot_size"] == 75 and ch["days_to_expiry"] == 5
    leg = next(r for r in ch["rows"] if r["strike"] == 22000.0)["ce"]
    assert leg["tradingsymbol"] == "NIFTY26O0622000CE" and leg["bid"] < leg["ltp"] < leg["ask"]
    # the ATM shifts to 22200 later, and the held 22000 call is still found one strike below
    bars = D.contract_bars("NIFTY26O0622000CE")
    assert bars["09:31"][3] == 50 + 250 / 2
    assert D.premium("NIFTY26O0622000CE", "09:30") == 50 + 20 / 2


def test_minute_walk_stop_target_and_close():
    D = bt.Day("2026-10-01", [], {}, {})
    base = {"status": "open", "instrument": "X", "look_at": "09:30", "entry": 100.0, "stop": 90.0, "target": 110.0, "qty": 75, "slip": 0.003}
    D._bars["X"] = {"09:29": (100, 130, 50, 100), "09:30": (100, 104, 95, 103), "09:31": (103, 111, 101, 109)}
    e = dict(base)
    D.advance([e], "09:31")
    assert e["status"] == "open" and e["ltp"] == 103          # the minute before the look never counts
    D.advance([e], "09:35")
    assert e["status"] == "closed" and e["exit_reason"] == "target" and e["exit_at"] == "09:32"
    D._bars["X"] = {"09:30": (100, 112, 89, 100)}
    e = dict(base)
    D.advance([e], "09:35")
    assert e["exit_reason"] == "stop"                          # both in one minute: the stop is assumed first
    D._bars["X"] = {"09:30": (100, 101, 99, 100), "15:14": (100, 101, 99, 104)}
    e = dict(base)
    D.advance([e], ct.FLAT_AT)
    assert e["exit_reason"] == "15:15 exit" and e["exit_at"] == ct.FLAT_AT


def test_check_works_on_replayed_chain(monkeypatch):
    path = {lb: 22000.0 for lb in ("09:27", "09:28", "09:29")}
    monkeypatch.setattr(dhan_data, "rolling", _fake_rolling(path))
    D = bt.Day("2026-10-01", [], {"NIFTY": 75}, {})
    chain = ct.trim_chain(D.chain("NIFTY", "09:30"), 5)
    ctx = {"underlyings": [{"underlying": "NIFTY", "kind": "index", "option_chain": chain}]}
    d = {"action": "BUY", "underlying": "NIFTY", "instrument": "NIFTY26O0622000CE", "stop_loss": 40, "target": 70, "quantity": 75}
    from datetime import datetime
    now = datetime(2026, 10, 1, 9, 30, tzinfo=bt.common.IST)
    res = ct.check(d, ctx, now, lambda key: D.premium(key.split(":", 1)[1], "09:30"), [])
    assert res["ok"], res["why"]
    assert res["position"]["entry"] == round(50 * 1.003, 2)
