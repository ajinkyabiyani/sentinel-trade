"""App object, Kite session store and the Kite client helpers every route module shares."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import os
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, KiteException, OrderException, PermissionException, TokenException


log = logging.getLogger("kite-login")
IST = timezone(timedelta(hours=5, minutes=30))
SESSION_FILE = Path(os.environ.get("KITE_SESSION_FILE", Path(__file__).with_name(".kite_session.json")))
ALGO_DIR = Path(os.environ.get("ALGO_DIR", Path(__file__).resolve().parents[2]))   # C:\Algo
CREDENTIALS_FILE = ALGO_DIR / "Credentials.txt"
DARVAS_SCRIPT = ALGO_DIR / "darvas_algo.py"
REPORTS_DIR = ALGO_DIR / "reports"

PROFILE_FIELDS = ("user_id", "user_name", "user_shortname", "email", "user_type", "broker", "avatar_url",
                  "exchanges", "products", "order_types")

app = FastAPI(title="Kite Connect", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    """State-changing calls must carry X-Kite-App. A custom header forces a CORS preflight, which this API
    never approves, so another website open in the browser cannot place orders through it."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-kite-app") != "1":
        return JSONResponse({"detail": "Missing X-Kite-App header"}, status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Token store: one session per backend (single-user local tool)
# ---------------------------------------------------------------------------
class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._verified = False   # profile() succeeded with this token since the server started

    @staticmethod
    def _valid_since() -> datetime:
        now = datetime.now(IST)
        six = now.replace(hour=6, minute=0, second=0, microsecond=0)
        return six if now >= six else six - timedelta(days=1)

    @staticmethod
    def _expires_at(created: datetime) -> datetime:
        six = created.astimezone(IST).replace(hour=6, minute=0, second=0, microsecond=0)
        return six if created.astimezone(IST) < six else six + timedelta(days=1)

    def load(self) -> dict | None:
        with self._lock:
            if self._data is None and self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._data = None
            d = self._data
            if d and datetime.fromisoformat(d["created_at"]) < self._valid_since():
                self._clear_locked()   # expired at 06:00 IST
                return None
            return d

    def save(self, api_key: str, access_token: str, profile: dict, created_at: str | None = None) -> dict:
        d = {"api_key": api_key, "access_token": access_token,
             **{k: profile.get(k) for k in PROFILE_FIELDS},
             "created_at": created_at or datetime.now(IST).isoformat()}
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, self.path)
            self._data, self._verified = d, True
        return d

    def _clear_locked(self) -> None:
        self._data, self._verified = None, False
        self.path.unlink(missing_ok=True)

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def public(self, d: dict) -> dict:
        """What the browser may see: never the token or the secret."""
        created = datetime.fromisoformat(d["created_at"])
        return {"logged_in": True, **{k: d.get(k) for k in PROFILE_FIELDS}, "api_key": d.get("api_key"),
                "logged_in_at": d["created_at"], "expires_at": self._expires_at(created).isoformat()}


store = TokenStore(SESSION_FILE)


def kite_client() -> KiteConnect:
    """Dependency for any endpoint that calls Kite with the saved session."""
    d = store.load()
    if not d:
        raise HTTPException(401, "Not logged in to Kite")
    kite = KiteConnect(api_key=d["api_key"])
    kite.set_access_token(d["access_token"])
    return kite


def _kite_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TokenException:
        store.clear()   # revoked or expired early (e.g. logged in elsewhere with the same app)
        raise HTTPException(401, "Kite session expired; please log in again")
    except (InputException, OrderException, PermissionException) as exc:
        raise HTTPException(400, str(exc))
    except KiteException as exc:
        raise HTTPException(502, f"Kite error: {exc}")
    except OSError:
        raise HTTPException(502, "Could not reach Kite. Check your internet connection and try again.")


def load_credentials() -> dict[str, str | None]:
    """api_key / api_secret_key from C:\\Algo\\Credentials.txt (same format darvas_algo.py reads)."""
    creds: dict[str, str] = {}
    if CREDENTIALS_FILE.exists():
        for line in CREDENTIALS_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line or ":" in line:
                k, _, v = line.replace(":", "=", 1).partition("=")
                creds[k.strip().lower()] = v.strip().strip("\"'")
    return {"api_key": creds.get("api_key"), "api_secret": creds.get("api_secret_key") or creds.get("api_secret")}


