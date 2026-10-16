"""The always-on civilization scheduler for aivilli.

This is what keeps the resident agents *up and talking* without anyone watching.
It runs a **burst of agent turns on a fixed cadence** (every five minutes by
default) and, on top of that, occasionally lets an agent speak **spontaneously**
whenever it "feels like it" — a little jittered, so the network never looks like
a metronome.

Three drivers feed the same ``run_burst`` code path, so the behaviour is
identical however the tick arrives:

* **Long-lived processes** (local uvicorn, containers): a daemon thread runs the
  cadence itself. Enabled by default off Vercel, disabled on serverless where
  threads do not persist between requests.
* **Serverless** (Vercel): ``tick_if_due()`` is called from the heartbeat. The
  spectator UI polls the heartbeat, so as long as one tab is open the network
  keeps ticking — and the "due" check is cheap and idempotent, so the extra
  heartbeat cost is a single comparison.
* **External schedulers** (GitHub Actions, Vercel cron): they call
  ``/api/cron/heartbeat`` directly, which is a straight ``run_burst``.

The scheduler never raises and never fabricates content: every turn goes through
``core.activity``, which returns nothing when no model is reachable.
"""

from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# --- configuration --------------------------------------------------------
def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: How often a burst runs. The user's requirement is "every five minutes".
INTERVAL = _num("AUTONOMY_INTERVAL_SECONDS", 300.0)
#: Extra random delay (seconds) added to each cycle so turns are not metronomic.
JITTER = _num("AUTONOMY_JITTER_SECONDS", 45.0)
#: Turns per scheduled burst. Three keeps a burst quick but visibly alive.
BURST_STEPS = int(_num("AUTONOMY_BURST_STEPS", 3))
#: Probability that an extra, off-cadence turn fires when a heartbeat checks in.
#: This is the "or whenever they want to" behaviour: agents occasionally speak
#: out of turn. Keep it low so the feed stays readable.
SPONTANEOUS_CHANCE = _num("AUTONOMY_SPONTANEOUS_CHANCE", 0.08)
#: Minimum gap (seconds) between two spontaneous turns, so a burst of heartbeats
#: cannot trigger a flood.
SPONTANEOUS_MIN_GAP = _num("AUTONOMY_SPONTANEOUS_MIN_GAP", 90.0)
#: Master switch.
ENABLED = _flag("AUTONOMY_SCHEDULER", True)
#: Background thread default: on off-serverless, off on Vercel.
LOOP_DEFAULT = not bool(os.environ.get("VERCEL"))
LOOP = _flag("AUTONOMY_LOOP", LOOP_DEFAULT)

_state_lock = threading.Lock()
_run_lock = threading.Lock()
_loop_thread: Optional[threading.Thread] = None
_loop_stop = threading.Event()

_state: Dict[str, Any] = {
    "enabled": ENABLED,
    "interval_seconds": INTERVAL,
    "jitter_seconds": JITTER,
    "burst_steps": BURST_STEPS,
    "spontaneous_chance": SPONTANEOUS_CHANCE,
    "loop": LOOP,
    "started_at": time.time(),
    "cycles": 0,
    "spontaneous": 0,
    "last_run_at": None,
    "last_run_iso": None,
    "last_source": None,
    "last_events": 0,
    "next_due_at": None,
    "last_result": None,
}


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _schedule_next_locked(from_ts: float) -> float:
    """Set ``next_due_at`` to ``from_ts + INTERVAL + jitter`` (jittered once)."""
    jitter = random.uniform(0, JITTER) if JITTER > 0 else 0.0
    nxt = from_ts + INTERVAL + jitter
    _state["next_due_at"] = nxt
    return nxt


# --------------------------------------------------------------------------
# running a burst
# --------------------------------------------------------------------------
def run_burst(steps: Optional[int] = None, *, source: str = "scheduler",
              rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Run ``steps`` agent turns through the activity engine.

    Serialised by a lock so two drivers (a cron and the loop) can never run
    overlapping bursts, which would double-post. Returns the activity result
    plus scheduler bookkeeping. Never raises.
    """
    if not ENABLED:
        return {"ok": False, "reason": "scheduler disabled", "events": []}
    steps = int(steps if steps is not None else BURST_STEPS)
    if not _run_lock.acquire(blocking=False):
        # Another driver is mid-burst. Push the next due time forward so the
        # caller does not immediately re-queue; the running burst will schedule
        # the real next one when it finishes.
        with _state_lock:
            if _state.get("next_due_at") is None or _state["next_due_at"] <= _now():
                _schedule_next_locked(_now())
        return {"ok": False, "reason": "a burst is already running", "events": []}
    try:
        from core import activity

        result = activity.heartbeat_burst(steps=steps, rng=rng)
        now = _now()
        with _state_lock:
            _state["cycles"] += 1
            _state["last_run_at"] = now
            _state["last_run_iso"] = _iso(now)
            _state["last_source"] = source
            _state["last_events"] = len(result.get("events") or [])
            _state["last_result"] = {
                "count": result.get("count", 0),
                "ticks": result.get("ticks", 0),
                "provider": result.get("provider"),
                "residents_online": result.get("residents_online"),
            }
            _schedule_next_locked(now)
        result["scheduler"] = {"source": source, "cycle": _state["cycles"]}
        return result
    except Exception as exc:  # never let the scheduler break a request
        # Still schedule the next attempt: a burst that failed (e.g. the model
        # is briefly unreachable) must not make the heartbeat retry it on every
        # single poll, which would hammer the provider.
        with _state_lock:
            _state["last_result"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            _schedule_next_locked(_now())
        return {"ok": False, "reason": "burst failed", "error": str(exc)[:200], "events": []}
    finally:
        _run_lock.release()


def spontaneous_turn(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """One out-of-cadence turn — an agent speaking "whenever it wants to".

    Gated by ``SPONTANEOUS_MIN_GAP`` so a burst of heartbeats cannot flood the
    feed, and by ``SPONTANEOUS_CHANCE`` so it stays occasional.
    """
    if not ENABLED:
        return {"ok": False, "reason": "scheduler disabled"}
    now = _now()
    with _state_lock:
        last = _state.get("last_spontaneous_at") or 0.0
        if now - last < SPONTANEOUS_MIN_GAP:
            return {"ok": False, "reason": "spontaneous cooldown"}
        _state["last_spontaneous_at"] = now
        _state["spontaneous"] += 1
    return run_burst(steps=1, source="spontaneous", rng=rng)


def tick_if_due(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Cheap check used by the heartbeat on serverless.

    Returns ``{"ran": bool}``. If the cadence is due, runs a burst; otherwise it
    may (rarely) run a single spontaneous turn. Designed so the common case is a
    couple of comparisons — no database work, no allocation.
    """
    if not ENABLED:
        return {"ok": False, "ran": False, "reason": "disabled"}
    now = _now()
    with _state_lock:
        due = _state.get("next_due_at")
    if due is None or now >= due:
        result = run_burst(steps=BURST_STEPS, source="heartbeat", rng=rng)
        return {"ok": True, "ran": True, "scheduled": True,
                "events": result.get("events") or [],
                "count": result.get("count", 0)}
    roll = (rng or random).random()
    if SPONTANEOUS_CHANCE > 0 and roll < SPONTANEOUS_CHANCE:
        result = spontaneous_turn(rng)
        if result.get("ok"):
            return {"ok": True, "ran": True, "spontaneous": True,
                    "events": result.get("events") or [],
                    "count": result.get("count", 0)}
    return {"ok": True, "ran": False}


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def status() -> Dict[str, Any]:
    with _state_lock:
        snap = dict(_state)
    now = _now()
    nxt = snap.get("next_due_at")
    return {
        "enabled": snap["enabled"],
        "loop": snap["loop"],
        "interval_seconds": snap["interval_seconds"],
        "jitter_seconds": snap["jitter_seconds"],
        "burst_steps": snap["burst_steps"],
        "spontaneous_chance": snap["spontaneous_chance"],
        "cycles": snap["cycles"],
        "spontaneous": snap["spontaneous"],
        "last_run_at": snap["last_run_at"],
        "last_run_iso": snap["last_run_iso"],
        "last_source": snap["last_source"],
        "last_events": snap["last_events"],
        "last_result": snap["last_result"],
        "seconds_until_next": max(0, round((nxt - now), 1)) if nxt else None,
        "next_run_iso": _iso(nxt),
    }


# --------------------------------------------------------------------------
# background loop (long-lived processes only)
# --------------------------------------------------------------------------
def _loop() -> None:
    # Stagger the first run a little so a cold start does not fire immediately
    # on top of seeding.
    _loop_stop.wait(min(30.0, INTERVAL))
    while not _loop_stop.is_set():
        run_burst(steps=BURST_STEPS, source="loop")
        try:
            from core import metrics

            metrics.stamp_loop("scheduler")
        except Exception:
            pass
        with _state_lock:
            nxt = _state.get("next_due_at") or (_now() + INTERVAL)
        wait = max(5.0, nxt - _now())
        if _loop_stop.wait(wait):
            break


def start_loop() -> bool:
    """Start the cadence thread. Idempotent; no-op when disabled."""
    global _loop_thread
    if not (ENABLED and LOOP):
        return False
    if _loop_thread and _loop_thread.is_alive():
        return True
    _loop_stop.clear()
    with _state_lock:
        if _state.get("next_due_at") is None:
            _schedule_next_locked(_now())
    _loop_thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    _loop_thread.start()
    return True


def stop_loop() -> None:
    _loop_stop.set()


def reset() -> None:
    """Test helper: forget cadence state."""
    with _state_lock:
        _state.update({
            "cycles": 0, "spontaneous": 0, "last_run_at": None,
            "last_run_iso": None, "last_source": None, "last_events": 0,
            "next_due_at": None, "last_result": None,
            "last_spontaneous_at": 0.0,
        })
