"""QA_Automation_Agent tests (2026-11-04). Pure functions, a tmp_path-only improvement-loop state, and read-only GET
smoke tests against the running server that skip when it is down. No Kite call, no order, no POST."""
import json
import urllib.error
import urllib.request
import pytest


# ---------------- paper_desk._size caps ----------------
def _book(open_=()):
    return {"capital": 100000.0, "closed": [], "open": list(open_), "kind": "darvas"}


@pytest.mark.parametrize("entry,stop,open_,want", [
    (100.0, 90.0, (), 100),                                       # risk cap: 1000 / 10
    (100.0, 99.0, (), 200),                                       # 20% position cap: 20000 / 100 (risk would be 1000)
    (100.0, 99.5, ({"symbol": "X", "entry": 100.0, "qty": 900},), 100),   # cash cap: (100000 - 90000) / 100
    (100.0, 100.0, (), 0),                                        # zero R
    (100.0, 101.0, (), 0),                                        # stop above entry
    (100.0, 99.0, ({"symbol": "X", "entry": 100.0, "qty": 1100},), 0),    # over-invested: negative cash -> 0, not < 0
])
def test_paper_desk_size_caps(entry, stop, open_, want):
    from strategies import paper_desk
    assert paper_desk._size(_book(open_), entry, stop) == want


# ---------------- improve_worker._stats ----------------
def test_improve_worker_stats_empty_and_none_r():
    from strategies import improve_worker
    e = improve_worker._stats([])
    assert e == {"trades": 0, "win_rate": None, "exp_r": None, "total_r": 0, "net": 0, "profit_factor": None}
    s = improve_worker._stats([{"r": None, "net": 5.0}, {"r": 2.0, "net": 10.0}, {"r": -1.0, "net": -4.0}])
    assert s["trades"] == 2 and s["win_rate"] == 50.0 and s["total_r"] == 1.0 and s["profit_factor"] == 2.0
    assert s["net"] == 11.0


# ---------------- improve.approve / rollback on a tmp PARAMS_FILE ----------------
@pytest.fixture
def imp(tmp_path, monkeypatch):
    from strategies import improve
    res = {"orb": {"built": "2026-11-03T16:30:00", "code": {"status": "pass"},
                   "quant": {"proposal": {"param": "TARGET_R", "from": 3.0, "to": 2.5, "oos": 0.21, "base_oos": 0.12}}}}
    monkeypatch.setattr(improve, "CACHE", tmp_path)
    monkeypatch.setattr(improve, "PARAMS_FILE", tmp_path / "strategy_params.json")
    monkeypatch.setattr(improve, "result", lambda k: res.get(k))
    monkeypatch.setattr(improve, "apply_overrides", lambda: None)   # never touch the live module globals
    return improve, res, tmp_path


def test_improve_approve_schedules_once(imp):
    improve, res, tmp = imp
    v = improve.approve("orb")
    st = json.loads((tmp / "strategy_params.json").read_text(encoding="utf-8"))
    hist = [h for h in st["history"] if h["strategy"] == "orb"]
    assert len(hist) == 1 and hist[0]["param"] == "TARGET_R" and hist[0]["to"] == 2.5
    if hist[0]["status"] == "scheduled":
        assert len(st["pending"]) == 1 and st["pending"][0]["effective"] >= hist[0]["approved_at"][:10]
        with pytest.raises(ValueError, match="already scheduled"):
            improve.approve("orb")
    else:   # approved before 09:10 on a weekday: applied immediately
        assert hist[0]["status"] == "live" and st["applied"]["orb"]["TARGET_R"] == 2.5
    assert "history" in v and sorted(p.name for p in tmp.iterdir()) == ["strategy_params.json"]


def test_improve_approve_refuses_unvetted(imp):
    improve, res, tmp = imp
    res["orb"]["code"]["status"] = "fail"
    with pytest.raises(ValueError, match="code agent"):
        improve.approve("orb")
    res["orb"]["quant"]["proposal"] = None
    with pytest.raises(ValueError, match="no proposal"):
        improve.approve("orb")
    assert not (tmp / "strategy_params.json").exists()


def test_improve_rollback_cancel_and_reverse(imp):
    improve, res, tmp = imp
    pf = tmp / "strategy_params.json"
    sched = {"id": "orb-A-1", "strategy": "orb", "param": "A", "from": 1, "to": 2, "effective": "2099-01-01", "status": "scheduled"}
    live = {"id": "orb-B-1", "strategy": "orb", "param": "B", "from": 3, "to": 4, "effective": "2026-01-01", "status": "live"}
    pf.write_text(json.dumps({"applied": {"orb": {"B": 4}}, "pending": [dict(sched)], "history": [sched, live],
                              "dismissed": []}), encoding="utf-8")
    improve.rollback("orb-A-1")
    st = json.loads(pf.read_text(encoding="utf-8"))
    assert st["pending"] == [] and next(h for h in st["history"] if h["id"] == "orb-A-1")["status"] == "cancelled"
    improve.rollback("orb-B-1")
    st = json.loads(pf.read_text(encoding="utf-8"))
    rb = [h for h in st["history"] if h.get("rollback_of") == "orb-B-1"]
    assert len(rb) == 1 and rb[0]["from"] == 4 and rb[0]["to"] == 3
    assert next(h for h in st["history"] if h["id"] == "orb-B-1")["status"] == "rolled back"
    for cid in ("orb-A-1", "orb-B-1", "nope"):     # cancelled / rolled back / unknown cannot be rolled back again
        with pytest.raises(ValueError):
            improve.rollback(cid)


# ---------------- read-only GET smoke tests (skip when the server is down) ----------------
@pytest.mark.parametrize("path,key", [("/api/improve", "strategies"), ("/api/portfolio", "desks")])
def test_server_get_smoke(path, key):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5) as r:
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"server not reachable: {e}")
    assert isinstance(body, dict) and key in body
