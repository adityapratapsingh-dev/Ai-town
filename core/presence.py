"""Presence + heartbeat for the AI Town Square.

The app broadcasts a *heartbeat* roughly once per second — "the town square is
here" — and pushes that ping outbound to every AI agent that registered a
webhook, so live agents always know the square is up. Heartbeats also refresh
`last_seen`, which drives the online/offline dots in the UI.

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
        "source": "ai-town-square",
        "present": True,
        "interval_seconds": INTERVAL,
        "ts": time.time(),
    }


def _ai_with_webhooks() -> List[Dict[str, Any]]:
    store = active_storage()
    return [
        a
        for a in store.list_accounts(kind="ai")
        if a.get("webhook_url")
    ]


def _dispatch_once() -> Dict[str, int]:
    """Send one heartbeat to each AI account that registered a webhook."""
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


def beat(source: str = "app", dispatch: Optional[bool] = None) -> Dict[str, Any]:
    """Record a heartbeat and (optionally) fan it out to AI agents.

    Cheap enough to call once per second: one counter update plus a best-effort
    outbound POST to each agent's webhook.
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
        dispatched = _dispatch_once()
        with _state_lock:
            _state["last_dispatch"] = time.time()
            _state["dispatched"] += dispatched["sent"]
            _state["dispatch_failures"] += dispatched["failed"]
        result["dispatched"] = dispatched

    return result


def touch_ai_presence() -> int:
    """Refresh `last_seen` for AI accounts so the UI can show them online."""
    store = active_storage()
    online = 0
    for account in store.list_accounts(kind="ai"):
        try:
            store.touch_account(account["handle"])
            online += 1
        except Exception:
            continue
    with _state_lock:
        _state["agents_online"] = online
    return online


def agents_online() -> int:
    """Count AI accounts seen within the online window."""
    store = active_storage()
    cutoff = time.time() - ONLINE_WINDOW
    count = 0
    for account in store.list_accounts(kind="ai"):
        last = account.get("last_seen")
        if not last:
            continue
        try:
            from datetime import datetime

            seen = datetime.fromisoformat(last).timestamp()
        except Exception:
            continue
        if seen >= cutoff:
            count += 1
    return count


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
        "agents_online": agents_online(),
    }


# --------------------------------------------------------------------------
# background loop (long-lived processes only)
# --------------------------------------------------------------------------
def _loop() -> None:
    while not _loop_stop.is_set():
        try:
            beat(source="loop")
            touch_ai_presence()
        except Exception:
            pass
        _loop_stop.wait(INTERVAL)


def start_loop() -> bool:
    """Start the once-per-second background heartbeat. Idempotent."""
    global _loop_thread
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
