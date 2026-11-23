"""The ATR-trail what-if on the team's paper trades: it follows the same stop and target, adds the trail, and never touches the real trade."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from strategies import multi_agent_team as t  # noqa: E402

DAY = "2026-11-11"


def _under(closes_before, closes_today, start="10:00"):
    """Underlying candles: flat history with a range of 2 points, then today's closes from `start`."""
    out = [{"date": "2026-11-10", "start": f"{9 + i // 12:02d}:{(i % 12) * 5:02d}", "h": c + 1, "l": c - 1, "c": c} for i, c in enumerate(closes_before)]
    h, m = int(start[:2]), int(start[3:])
    for c in closes_today:
        out.append({"date": DAY, "start": f"{h:02d}:{m:02d}", "h": c + 1, "l": c - 1, "c": c})
        m += 5
        h, m = h + m // 60, m % 60
    return out


def _trade(**kw):
    return {"instrument": "NIFTY26N1725000CE", "underlying": "NIFTY", "kind": "index", "qty": 65, "entry": 100.0, "stop": 80.0, "target": 140.0, "entry_bar": "10:05", "status": "open"} | kw


def test_atr_matches_wilder_on_a_flat_range():
    bars = [{"h": 101, "l": 99, "c": 100}] * 20
    assert t._atr(bars) == 2.0
    assert t._atr(bars[:10]) is None


def test_trail_exits_a_call_when_the_underlying_falls_back_two_atr():
    under = _under([100] * 20, [100, 104, 108, 103])          # best close 108, ATR 2: trail at 104, broken by the 103 close
    bars = {"10:05": (100, 102, 99, 101), "10:10": (101, 110, 100, 108), "10:15": (108, 109, 104, 105)}
    x = t.shadow(_trade(), bars, under, DAY, "15:10")
    assert x["status"] == "closed" and x["exit_reason"] == "ATR trail" and x["exit_at"] == "10:20"
    assert x["atr"] == 2.0 and x["level"] == 104.0
    assert x["net"] > 0


def test_put_trails_the_other_way():
    under = _under([100] * 20, [100, 96, 92, 97])             # a put wins as the index falls: best 92, trail at 96, broken by 97
    bars = {"10:05": (100, 102, 99, 101), "10:10": (101, 110, 100, 108), "10:15": (108, 109, 104, 105)}
    x = t.shadow(_trade(instrument="NIFTY26N1725000PE"), bars, under, DAY, "15:10")
    assert x["exit_reason"] == "ATR trail" and x["exit_at"] == "10:20"


def test_stop_and_target_still_come_first():
    under = _under([100] * 20, [100, 101, 102])
    hit = t.shadow(_trade(), {"10:05": (100, 141, 99, 120)}, under, DAY, "15:10")
    assert hit["exit_reason"] == "target"
    stop = t.shadow(_trade(), {"10:05": (100, 101, 79, 85)}, under, DAY, "15:10")
    assert stop["exit_reason"] == "stop"


def test_still_open_before_the_close_and_needs_history():
    under = _under([100] * 20, [100, 101])
    x = t.shadow(_trade(), {"10:05": (100, 102, 99, 101)}, under, DAY, "10:10")
    assert x["status"] == "open" and x["level"] == 97.0
    none = t.shadow(_trade(), {}, _under([100] * 5, [100]), DAY, "15:10")
    assert none["status"] == "none"


def test_real_trade_is_left_alone():
    e = _trade()
    snap = dict(e)
    t.shadow(e, {"10:05": (100, 141, 99, 120)}, _under([100] * 20, [100, 101]), DAY, "15:10")
    assert e == snap


def test_what_if_compares_only_finished_pairs():
    held = [
        {"status": "closed", "net": -1000.0, "shadow": {"status": "closed", "net": -400.0}},
        {"status": "closed", "net": 2000.0, "shadow": {"status": "closed", "net": 2000.0}},
        {"status": "closed", "net": 500.0, "shadow": {"status": "open"}},
        {"status": "open", "net": None},
    ]
    w = t.what_if(held)
    assert (w["trades"], w["actual"], w["trail"], w["better"], w["worse"]) == (2, 1000.0, 1600.0, 1, 0)
