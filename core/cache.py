"""A tiny in-process TTL cache.

The spectator UI polls ``/v1/heartbeat`` every second, and each hit used to
re-query the database for stats and the online count. On Cloudflare D1 every
query is an HTTP round-trip, so that is both slow and wasteful.

This caches read-only aggregates for a short window (default 1s) — fresh enough
that the UI still feels live, but it collapses the per-second polling into one
query per window. Writes invalidate the relevant keys explicitly.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

_lock = threading.Lock()
_store: Dict[str, Tuple[float, Any]] = {}


def get_or_set(key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    """Return the cached value for ``key``, computing it if missing/stale."""
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = producer()  # computed outside the lock; races are harmless
    with _lock:
        _store[key] = (now + ttl, value)
    return value


def invalidate(*keys: str) -> None:
    with _lock:
        for key in keys:
            _store.pop(key, None)


def invalidate_prefix(prefix: str) -> None:
    with _lock:
        for key in [k for k in _store if k.startswith(prefix)]:
            _store.pop(key, None)


def clear() -> None:
    with _lock:
        _store.clear()
