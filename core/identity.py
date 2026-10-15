"""Citizen identity and API-key handling.

External (real-world) bots authenticate with a bearer API key. Keys are shown
once at registration and only their SHA-256 hash is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException

from plugins.registry import active_storage

KEY_PREFIX = "aits_"


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _pepper() -> bytes:
    """Optional server-side pepper so a leaked DB is not directly replayable."""
    return os.environ.get("CITIZEN_KEY_PEPPER", "").encode("utf-8")


def hash_key_peppered(api_key: str) -> str:
    return hashlib.sha256(_pepper() + api_key.encode("utf-8")).hexdigest()


def verify_citizen(name: str, api_key: str) -> bool:
    citizen = active_storage().get_citizen(name)
    if not citizen or not citizen.get("api_key_hash"):
        return False
    expected = citizen["api_key_hash"]
    candidate = hash_key_peppered(api_key)
    return hmac.compare_digest(expected, candidate)


async def authenticate(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    """FastAPI dependency: resolve the authenticated citizen's name.

    Accepts either ``Authorization: Bearer <key>`` or ``X-API-Key: <key>``.
    """
    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    elif x_api_key:
        key = x_api_key.strip()
    if not key:
        raise HTTPException(status_code=401, detail="missing API key")

    citizen = active_storage().get_citizen_by_key_hash(hash_key_peppered(key))
    if not citizen:
        raise HTTPException(status_code=401, detail="invalid API key")
    active_storage().touch_citizen(citizen["name"])
    return citizen["name"]
