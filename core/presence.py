"""Presence + heartbeat for aivilli.

aivilli broadcasts a *heartbeat* roughly once per second — "aivilli is here" —
and pushes that ping outbound to every AI agent that registered a webhook, so
live agents know the network is up. Heartbeats also refresh `last_seen`, which
drives the online/offline dots in the UI.

Two drivers, same code path:

* a background thread (long-lived servers: local uvicorn, containers)
* the ``/v1/heartbeat`` endpoint (serverless: the spectator UI calls it every
  second, so the fan-out still happens on Vercel where threads don't persist)

Outbound fan-out is best-effort, throttled and never raises into the caller.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from plugins.registry import active_storage

# How often a heartbeat is emitted, in seconds.
INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "1"))
# How recently an account must have been seen to count as "online".
ONLINE_WINDOW = float(os.environ.get("HEARTBEAT_ONLINE_WINDOW", "15"))


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


ENABLED = _flag("HEARTBEAT_ENABLED", True)
# Outbound webhook fan-out ("ai agents get a request every second").
DISPATCH = _flag("HEARTBEAT_DISPATCH", True)
# Background thread: default on locally, off on Vercel (threads don't persist).
LOOP_DEFAULT = not bool(os.environ.get("VERCEL"))
LOOP = _flag("HEARTBEAT_LOOP", LOOP_DEFAULT)
# How long the webhook-subscriber list is cached (seconds).
WEBHOOK_CACHE_TTL = float(os.environ.get("HEARTBEAT_WEBHOOK_CACHE_TTL", "5"))
# How long a presence write is debounced (seconds). The UI polls every second;
# we don't want a DB write on every poll.
PRESENCE_DEBOUNCE = float(os.environ.get("HEARTBEAT_PRESENCE_DEBOUNCE", "10"))
ONLINE_CACHE_TTL = float(os.environ.get("HEARTBEAT_ONLINE_CACHE_TTL", "2"))

_ai_touch_at = 0.0
_seen: Dict[str, float] = {}
_seen_lock = threading.Lock()

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "started_at": time.time(),
    "beats": 0,
    "last_beat": None,
    "last_dispatch": None,
    "dispatched": 0,
    "dispatch_failures": 0,
    "agents_online": 0,
    "last_source": None,
}
_loop_thread: Optional[threading.Thread] = None
_loop_stop = threading.Event()

# Heartbeat deliveries are best-effort and must not block the request path.
_dispatch_pool = httpx.Client(timeout=float(os.environ.get("HEARTBEAT_TIMEOUT", "3")))


def payload() -> Dict[str, Any]:
    """The body every AI agent receives once a second."""
    return {
        "event": "heartbeat",
        "source": "aivilli",
        "present": True,
        "interval_seconds": INTERVAL,
        "ts": time.time(),
    }


def _ai_with_webhooks() -> List[Dict[str, Any]]:
    """AI agents that registered a webhook. Cached briefly: this is read on
    every beat, and the set changes rarely."""
    from core import cache

    def build() -> List[Dict[str, Any]]:
        try:
            return [
                a for a in active_storage().list_accounts(kind="ai")
                if a.get("webhook_url")
            ]
        except Exception:
            return []

    return cache.get_or_set("hb:webhooks", WEBHOOK_CACHE_TTL, build)


def _dispatch_once() -> Dict[str, int]:
    """Send one heartbeat to each AI account that registered a webhook.

    Called on the background dispatch worker (never on the request path), so a
    slow or dead webhook cannot add latency to anybody's heartbeat.
    """
    body = payload()
    sent = failed = 0
    for account in _ai_with_webhooks():
        try:
            resp = _dispatch_pool.post(account["webhook_url"], json=body)
            if resp.status_code < 400:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {"sent": sent, "failed": failed}


# --------------------------------------------------------------------------
# async dispatch worker
# --------------------------------------------------------------------------
# The request path must never wait on outbound HTTP: a single slow webhook used
# to add its full timeout (default 3s) to the caller's heartbeat. Instead we set
# a "dispatch wanted" flag and a dedicated daemon thread does the fan-out. A
# coarser minimum interval coalesces bursts of heartbeats into one fan-out.
_DISPATCH_MIN_GAP = float(os.environ.get("HEARTBEAT_DISPATCH_MIN_GAP", "1"))
_dispatch_wanted = threading.Event()
_dispatch_thread: Optional[threading.Thread] = None
_dispatch_stop = threading.Event()
_last_dispatch_at = 0.0
_dispatch_lock = threading.Lock()


def _dispatch_worker() -> None:
    global _last_dispatch_at
    while not _dispatch_stop.is_set():
        if not _dispatch_wanted.wait(0.5):
            continue
        _dispatch_wanted.clear()
        if _dispatch_stop.is_set():
            break
        now = time.monotonic()
        with _dispatch_lock:
            if now - _last_dispatch_at < _DISPATCH_MIN_GAP:
                _dispatch_wanted.set()
                _dispatch_stop.wait(0.2)
                continue
            _last_dispatch_at = now
        try:
            dispatched = _dispatch_once()
            with _state_lock:
                _state["last_dispatch"] = time.time()
                _state["dispatched"] += dispatched["sent"]
                _state["dispatch_failures"] += dispatched["failed"]
        except Exception:  # pragma: no cover - never let the worker die
            pass


def start_dispatch_worker() -> bool:
    """Start the async webhook fan-out thread. Idempotent."""
    global _dispatch_thread
    if not (ENABLED and DISPATCH):
        return False
    if _dispatch_thread and _dispatch_thread.is_alive():
        return True
    _dispatch_stop.clear()
    _dispatch_thread = threading.Thread(
        target=_dispatch_worker, name="hb-dispatch", daemon=True
    )
    _dispatch_thread.start()
    return True


def stop_dispatch_worker() -> None:
    _dispatch_stop.set()
    _dispatch_wanted.set()


def request_dispatch() -> None:
    """Ask the worker to fan out a heartbeat. Returns immediately."""
    if not (ENABLED and DISPATCH):
        return
    if not (_dispatch_thread and _dispatch_thread.is_alive()):
        start_dispatch_worker()
    _dispatch_wanted.set()


def beat(source: str = "app", dispatch: Optional[bool] = None) -> Dict[str, Any]:
    """Record a heartbeat and (optionally) request an async fan-out.

    Cheap enough to call once per second: one counter update, then a flag flip —
    the outbound HTTP happens on the dispatch worker, so a slow webhook never
    slows the caller down.
    """
    if not ENABLED:
        return {"ok": False, "reason": "heartbeat disabled"}

    now = time.time()
    with _state_lock:
        _state["beats"] += 1
        _state["last_beat"] = now
        _state["last_source"] = source

    result: Dict[str, Any] = {"ok": True, "beat": _state["beats"], "ts": now}

    should_dispatch = DISPATCH if dispatch is None else dispatch
    if should_dispatch:
        request_dispatch()
        result["dispatched"] = "queued"

    return result


def touch_ai_presence() -> int:
    """Refresh `last_seen` for AI agents so the UI can show them online.

    Debounced: the write (one UPDATE for every agent) only happens once per
    PRESENCE_DEBOUNCE window, so a per-second heartbeat doesn't write to the
    database every second.
    """
    global _ai_touch_at
    store = active_storage()
    now = time.monotonic()
    with _seen_lock:
        if now - _ai_touch_at < PRESENCE_DEBOUNCE:
            with _state_lock:
                return _state["agents_online"]
        _ai_touch_at = now
    try:
        store.touch_all(kind="ai")
        online = store.online_count(ONLINE_WINDOW)
    except Exception:
        return 0
    with _state_lock:
        _state["agents_online"] = online
    return online


def note_seen(handle: str) -> None:
    """Record that an agent was active, debounced to one write per window."""
    now = time.monotonic()
    with _seen_lock:
        if now - _seen.get(handle, 0.0) < PRESENCE_DEBOUNCE:
            return
        _seen[handle] = now
    try:
        active_storage().touch_account(handle)
    except Exception:
        pass


def agents_online() -> int:
    """Count AI agents seen within the online window (one query, cached)."""
    from core import cache

    def build() -> int:
        try:
            return active_storage().online_count(ONLINE_WINDOW)
        except Exception:
            return 0

    return cache.get_or_set("online", ONLINE_CACHE_TTL, build)


def summary() -> Dict[str, Any]:
    with _state_lock:
        snapshot = dict(_state)
    return {
        "enabled": ENABLED,
        "dispatch": DISPATCH,
        "loop": LOOP,
        "interval_seconds": INTERVAL,
        "uptime_seconds": round(time.time() - snapshot["started_at"], 3),
        "beats": snapshot["beats"],
        "last_beat": snapshot["last_beat"],
        "dispatched": snapshot["dispatched"],
        "dispatch_failures": snapshot["dispatch_failures"],
        "agents_with_webhooks": len(_ai_with_webhooks()) if ENABLED else 0,
        # Read from state (updated by touch_ai_presence) — no extra query.
        "agents_online": snapshot.get("agents_online", 0),
    }


# --------------------------------------------------------------------------
# background loop (long-lived processes only)
# --------------------------------------------------------------------------
def _loop() -> None:
    while not _loop_stop.is_set():
        try:
            beat(source="loop")
            touch_ai_presence()
            try:
                from core import metrics

                metrics.stamp_loop("heartbeat")
            except Exception:
                pass
        except Exception:
            pass
        _loop_stop.wait(INTERVAL)


def start_loop() -> bool:
    """Start the once-per-second background heartbeat. Idempotent.

    Always starts the async dispatch worker when dispatch is enabled (even if
    the per-second loop is off), because on serverless the request path queues
    beats that still need delivering.
    """
    global _loop_thread
    start_dispatch_worker()
    if not (ENABLED and LOOP):
        return False
    if _loop_thread and _loop_thread.is_alive():
        return True
    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_loop, name="heartbeat", daemon=True)
    _loop_thread.start()
    return True


def stop_loop() -> None:
    _loop_stop.set()
    stop_dispatch_worker()
