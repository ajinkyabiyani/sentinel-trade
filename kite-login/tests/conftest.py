"""Test setup: the backend modules import from backend/ (tests live outside it so saving a test never restarts uvicorn)."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_prices(monkeypatch):
    """The desks' real-time stop check asks Kite for last prices; tests never reach Kite unless they set a price."""
    from strategies import live_stops
    monkeypatch.setattr(live_stops, "source", lambda keys: {})
