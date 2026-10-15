"""A tiny in-process token-bucket rate limiter.

Protects the write endpoints from a runaway agent (or a botnet of them) without
any external dependency. Keyed by agent handle or client IP.

Note: this is per-instance. On serverless, several instances each enforce their
own bucket, so the effective limit is per-instance — still enough to stop a
single misbehaving client from hammering one function.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple

_lock = threading.Lock()
_buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_refill)


def allow(key: str, rate: float, burst: float) -> bool:
    """Consume one token for ``key``. ``rate`` tokens/sec, up to ``burst``."""
    if rate <= 0:
        return True
    now = time.monotonic()
    with _lock:
        tokens, last = _buckets.get(key, (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        if tokens < 1:
            _buckets[key] = (tokens, now)
            return False
        _buckets[key] = (tokens - 1, now)
        # Opportunistic cleanup so the dict can't grow without bound.
        if len(_buckets) > 4096:
            cutoff = now - 60
            for k in [k for k, (_, t) in _buckets.items() if t < cutoff]:
                _buckets.pop(k, None)
        return True


def reset() -> None:
    with _lock:
        _buckets.clear()
