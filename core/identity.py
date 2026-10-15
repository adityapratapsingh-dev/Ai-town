"""Account security: passwords for humans, API keys for AI agents, sessions.

* Humans sign up with a password (PBKDF2-HMAC-SHA256, salted).
* AI agents sign in with an API key; only its hash is stored.
* Both get a signed session token (HMAC) used as a bearer credential in the UI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import Header, HTTPException

from plugins.registry import active_storage

KEY_PREFIX = "aits_"
_PBKDF2_ROUNDS = 120_000


# --------------------------------------------------------------------------
# passwords (humans)
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ROUNDS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        scheme, rounds, salt_b64, dk_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# --------------------------------------------------------------------------
# API keys (AI agents / bots)
# --------------------------------------------------------------------------
def _pepper() -> bytes:
    return os.environ.get("ACCOUNT_KEY_PEPPER", "").encode("utf-8")


def hash_key(api_key: str) -> str:
    return hashlib.sha256(_pepper() + api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# session tokens (both kinds of account)
# --------------------------------------------------------------------------
_SESSION_SECRET: Optional[bytes] = None


def _secret() -> bytes:
    global _SESSION_SECRET
    if _SESSION_SECRET is None:
        env = os.environ.get("SESSION_SECRET")
        _SESSION_SECRET = env.encode("utf-8") if env else secrets.token_bytes(32)
    return _SESSION_SECRET


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def make_token(handle: str, ttl_seconds: int = 60 * 60 * 24 * 30) -> str:
    payload = f"{handle}:{int(time.time()) + ttl_seconds}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{_b64(payload.encode())}.{_b64(sig)}"


def read_token(token: str) -> Optional[str]:
    """Return the handle if the token is valid and unexpired, else None."""
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _unb64(payload_b64).decode()
        expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig_b64), expected):
            return None
        handle, expires = payload.rsplit(":", 1)
        if int(expires) < int(time.time()):
            return None
        return handle
    except Exception:
        return None


# --------------------------------------------------------------------------
# FastAPI dependencies
# --------------------------------------------------------------------------
def _resolve_bearer(token: str) -> Optional[str]:
    """A bearer token may be a session token (any account) or an API key (AI)."""
    handle = read_token(token)
    if handle and active_storage().get_account(handle):
        return handle
    account = active_storage().get_account_by_key_hash(hash_key(token))
    if account:
        active_storage().touch_account(account["handle"])
        return account["handle"]
    return None


async def current_account(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """Require an authenticated account (session token or API key)."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing credentials")
    handle = _resolve_bearer(token)
    if not handle:
        raise HTTPException(status_code=401, detail="invalid or expired credentials")
    return handle


async def optional_account(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Optional[str]:
    """Identify the viewer when present, without requiring auth."""
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token:
        return None
    return _resolve_bearer(token)
