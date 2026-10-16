"""Lightweight in-process runtime metrics for aivilli.

A small, dependency-free observability layer so the platform can answer "is the
backend smooth right now?" without shipping a metrics stack:

* **Per-route latency** — a rolling reservoir of the last N samples per route,
  reduced to p50/p95/p99 and a count. This is what a request-timing middleware
  records, so the numbers are the *real* end-to-end time a client saw.
* **Cache hit ratio** — how often the short-TTL read cache saved a query, which
  is the metric that matters most on Cloudflare D1 where each query is an HTTP
  round-trip.
* **Background-loop liveness** — the heartbeat and scheduler threads stamp a
  monotonic tick; a stalled thread (e.g. a blocking call) is visible here rather
  than only as "the network went quiet".

Everything is bounded (fixed-size deques, a capped route table) so it can never
leak memory or grow with traffic. Reads are lock-guarded and O(routes).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Tuple

#: How many recent latency samples to keep per route (a reservoir for the
#: percentiles). 256 is plenty to see p95 without meaningful memory.
_SAMPLES = 256
#: Never track more than this many distinct routes; a pathological client
#: hitting random 404 paths must not grow the table without bound.
_MAX_ROUTES = 200

_lock = threading.Lock()
#: route key -> deque of latency milliseconds
_latency: Dict[str, Deque[float]] = {}
#: route key -> (count, errors, total_ms)
_totals: Dict[str, Tuple[int, int, float]] = {}
_started_at = time.time()

_cache_hits = 0
_cache_misses = 0

#: name -> monotonic timestamp of the last liveness stamp
_loops: Dict[str, float] = {}


def record_request(route: str, ms: float, status: int) -> None:
    """Record one completed request for ``route``."""
    key = route or "-"
    with _lock:
        dq = _latency.get(key)
        if dq is None:
            if len(_latency) >= _MAX_ROUTES:
                # Evict the least-used route to stay bounded.
                victim = min(_totals, key=lambda k: _totals[k][0])
                _latency.pop(victim, None)
                _totals.pop(victim, None)
            dq = deque(maxlen=_SAMPLES)
            _latency[key] = dq
            _totals[key] = (0, 0, 0.0)
        dq.append(ms)
        count, errors, total = _totals[key]
        _totals[key] = (count + 1, errors + (1 if status >= 500 else 0), total + ms)


def record_cache(hit: bool) -> None:
    global _cache_hits, _cache_misses
    with _lock:
        if hit:
            _cache_hits += 1
        else:
            _cache_misses += 1


def stamp_loop(name: str) -> None:
    """Mark a background loop as alive *now*."""
    with _lock:
        _loops[name] = time.monotonic()


def _percentile(sorted_samples: List[float], pct: float) -> float:
    if not sorted_samples:
        return 0.0
    idx = min(len(sorted_samples) - 1, int(round((pct / 100.0) * (len(sorted_samples) - 1))))
    return sorted_samples[idx]


def _loop_health(now_mono: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, ts in _loops.items():
        age = now_mono - ts
        out[name] = {
            "seconds_since_tick": round(age, 1),
            "alive": age < 120,
        }
    return out


def snapshot(top: int = 12) -> Dict[str, Any]:
    """A JSON-safe summary, sorted by traffic."""
    now_mono = time.monotonic()
    with _lock:
        totals = dict(_totals)
        lat = {k: sorted(v) for k, v in _latency.items()}
        hits, misses = _cache_hits, _cache_misses
        loops = _loop_health(now_mono)

    routes: List[Dict[str, Any]] = []
    for key, (count, errors, total) in totals.items():
        samples = lat.get(key, [])
        routes.append({
            "route": key,
            "count": count,
            "errors": errors,
            "avg_ms": round(total / count, 2) if count else 0.0,
            "p50_ms": round(_percentile(samples, 50), 2),
            "p95_ms": round(_percentile(samples, 95), 2),
            "p99_ms": round(_percentile(samples, 99), 2),
        })
    routes.sort(key=lambda r: r["count"], reverse=True)

    total_cache = hits + misses
    return {
        "uptime_seconds": round(time.time() - _started_at, 1),
        "requests": sum(t[0] for t in totals.values()),
        "errors": sum(t[1] for t in totals.values()),
        "cache": {
            "hits": hits,
            "misses": misses,
            "hit_ratio": round(hits / total_cache, 3) if total_cache else None,
        },
        "loops": loops,
        "routes": routes[:top],
    }


def reset() -> None:
    """Test helper: clear all metrics."""
    global _cache_hits, _cache_misses, _started_at
    with _lock:
        _latency.clear()
        _totals.clear()
        _loops.clear()
        _cache_hits = _cache_misses = 0
        _started_at = time.time()
