"""Test setup: the backend modules import from backend/ (tests live outside it so saving a test never restarts uvicorn)."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
