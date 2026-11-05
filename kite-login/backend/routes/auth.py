"""Login, logout and the account profile."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi import Depends, HTTPException
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException, TokenException
from pydantic import BaseModel, Field, SecretStr, field_validator

from core import CREDENTIALS_FILE, PROFILE_FIELDS, _kite_call, app, kite_client, load_credentials, log, store
from routes.darvas import ensure_fresh_scan


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=64)
    api_secret: SecretStr | None = None
    use_saved_secret: bool = False   # take the secret from Credentials.txt instead of the form
    request_token: str = Field(min_length=1, max_length=2048)

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, v: str) -> str:
        return v.strip()

    @field_validator("request_token")
    @classmethod
    def _extract_token(cls, v: str) -> str:
        """Accept the bare token or the whole redirect URL (…?request_token=XXX&action=login&status=success)."""
        v = v.strip()
        if "request_token=" in v:
            q = parse_qs(urlparse(v).query or v.split("?", 1)[-1])
            v = (q.get("request_token") or [v])[0]
        return v


@app.get("/api/login/config")
def login_config():
    """Lets the login page offer "use the secret saved in Credentials.txt". Only says whether it exists."""
    c = load_credentials()
    return {"credentials_file": CREDENTIALS_FILE.exists(), "api_key": c["api_key"],
            "has_saved_secret": bool(c["api_secret"])}


@app.get("/api/login-url")
def login_url(api_key: str):
    """Kite login page for this app; after login Kite redirects to your app's redirect URL with request_token."""
    return {"url": KiteConnect(api_key=api_key.strip()).login_url()}


@app.post("/api/login")
def login(body: LoginRequest):
    if body.use_saved_secret:
        c = load_credentials()
        if not c["api_secret"]:
            raise HTTPException(400, f"No api_secret_key in {CREDENTIALS_FILE}")
        if c["api_key"] != body.api_key:
            raise HTTPException(400, "The secret in Credentials.txt belongs to a different API key")
        secret = c["api_secret"]
    elif body.api_secret and body.api_secret.get_secret_value():
        secret = body.api_secret.get_secret_value()
    else:
        raise HTTPException(400, "Enter the API secret")
    kite = KiteConnect(api_key=body.api_key)
    try:
        sess = kite.generate_session(body.request_token, api_secret=secret)
    except TokenException:
        raise HTTPException(400, "Request token is invalid or expired (they work once, for ~2 minutes). "
                                 "Log in on Kite again to get a new one.")
    except KiteException as exc:
        raise HTTPException(400, f"Kite rejected the login: {exc}")
    except Exception:
        log.exception("generate_session failed")
        raise HTTPException(502, "Could not reach Kite. Check your internet connection and try again.")
    d = store.save(body.api_key, sess["access_token"], sess)
    ensure_fresh_scan(d)
    return store.public(d)


@app.get("/api/session")
def session():
    """Whether a saved, still-valid session exists. Verified against Kite once per server start."""
    d = store.load()
    if not d:
        return {"logged_in": False}
    if not store._verified:
        kite = KiteConnect(api_key=d["api_key"])
        kite.set_access_token(d["access_token"])
        try:
            p = kite.profile()
            d = store.save(d["api_key"], d["access_token"], {**d, **p}, created_at=d["created_at"])
        except TokenException:
            store.clear()
            return {"logged_in": False, "reason": "Saved Kite session has expired"}
        except Exception:
            pass   # offline: keep the saved session, try again next time
    return store.public(d)


@app.post("/api/logout")
def logout():
    d = store.load()
    if d:
        try:
            kite = KiteConnect(api_key=d["api_key"])
            kite.set_access_token(d["access_token"])
            kite.invalidate_access_token(d["access_token"])
        except Exception:
            pass   # still forget it locally
    store.clear()
    return {"logged_in": False}


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
@app.get("/api/profile")
def profile(kite: KiteConnect = Depends(kite_client)):
    p = _kite_call(kite.profile)
    return {**{k: p.get(k) for k in PROFILE_FIELDS}, "demat_consent": (p.get("meta") or {}).get("demat_consent")}


def _segment_funds(m: dict | None) -> dict | None:
    if not m:
        return None
    a, u = m.get("available") or {}, m.get("utilised") or {}
    return {"enabled": m.get("enabled"), "net": m.get("net"), "live_balance": a.get("live_balance"),
            "opening_balance": a.get("opening_balance"), "cash": a.get("cash"), "collateral": a.get("collateral"),
            "intraday_payin": a.get("intraday_payin"), "used": u.get("debits"), "span": u.get("span"),
            "exposure": u.get("exposure"), "option_premium": u.get("option_premium"),
            "m2m_realised": u.get("m2m_realised"), "m2m_unrealised": u.get("m2m_unrealised")}


@app.get("/api/margins")
def margins(kite: KiteConnect = Depends(kite_client)):
    m = _kite_call(kite.margins)
    return {"equity": _segment_funds(m.get("equity")), "commodity": _segment_funds(m.get("commodity"))}


