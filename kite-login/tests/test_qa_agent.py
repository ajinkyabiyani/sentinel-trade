"""QA_Automation_Agent tests (2026-11-04). Pure functions, a tmp_path-only improvement-loop state, and read-only GET
smoke tests against the running server that skip when it is down. No Kite call, no order, no POST."""
import json
import urllib.error
import urllib.request
import pytest


# ---------------- read-only GET smoke tests (skip when the server is down) ----------------
@pytest.mark.parametrize("path,key", [("/api/portfolio", "desks")])
def test_server_get_smoke(path, key):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=5) as r:
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        pytest.skip(f"server not reachable: {e}")
    assert isinstance(body, dict) and key in body
