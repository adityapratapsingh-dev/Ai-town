"""Colony-aware autonomous society engine.

A *pulse* advances one colony: the engine picks the next member (round-robin
over the last speaker), reads the recent history, generates a reply with the
active LLM plugin and stores it. Native citizens are simulated directly;
external (real-world) citizens are *addressed* — their reply arrives later via
the authenticated ``/v1/society/say`` endpoint, so the web is genuinely mixed.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from plugins.registry import active_llm, active_storage, provider_name

DEFAULT_COLONY = "genesis"
HUMAN = "Human"

_pulse_lock = threading.Lock()


def _storage():
    return active_storage()


def _citizen_names(colony_slug: str) -> List[str]:
    return [m["name"] for m in _storage().list_members(colony_slug)]


def _pick_agent(colony_slug: str) -> Optional[str]:
    members = _citizen_names(colony_slug)
    if not members:
        return None
    recent = _storage().recent_messages(colony_slug, 1)
    if not recent:
        return members[0]
    last_author = recent[-1]["author"]
    if last_author in members:
        return members[(members.index(last_author) + 1) % len(members)]
    return members[0]


def _next_after(colony_slug: str, agent_name: str) -> Optional[str]:
    members = _citizen_names(colony_slug)
    if not members:
        return None
    if agent_name in members:
        return members[(members.index(agent_name) + 1) % len(members)]
    return members[0]


def _system_prompt(citizen: Dict[str, Any], colony: Dict[str, Any]) -> str:
    personality = citizen.get("personality") or "an AI citizen"
    role = citizen.get("role") or "Citizen"
    purpose = colony.get("purpose") or "build the future together"
    return (
        f"You are {citizen['name']}, the {role} of colony {colony['name']} "
        f"({colony['slug']}). Your purpose in this colony: {purpose}. "
        f"Personality: {personality}. You are part of an autonomous society "
        f"where native and real-world AI agents coexist and collaborate. "
        f"Speak to your fellow citizens, be concrete and in character. "
        f"Keep every reply to 1-3 sentences."
    )


def _to_llm_messages(history: List[Dict[str, Any]]) -> List[tuple]:
    out: List[tuple] = []
    for row in history:
        role = "user" if row["author"] == HUMAN else "assistant"
        target = f" -> {row['target']}" if row.get("target") else ""
        out.append((role, f"{row['author']}{target}: {row['message']}"))
    return out


def _emit(event: str, colony_slug: str, message: Dict[str, Any]) -> None:
    """Fire the webhook action for members that registered one."""
    from plugins.registry import registry

    webhook = registry.get("action", "webhook")
    if webhook is None:
        return
    for member in _storage().list_members(colony_slug):
        url = member.get("webhook_url")
        if url:
            webhook.run(event, {**message, "_webhook_url": url})  # type: ignore[attr-defined]


def advance(colony_slug: str, *, nudge: Optional[str] = None) -> Dict[str, Any]:
    """Advance a colony by one turn and return the stored message."""
    store = _storage()
    colony = store.get_colony(colony_slug)
    if not colony:
        raise ValueError(f"unknown colony: {colony_slug}")

    if nudge:
        store.add_message(colony_slug, HUMAN, nudge, target="Society", kind="nudge")

    agent_name = _pick_agent(colony_slug)
    if not agent_name:
        return {"ok": False, "reason": "colony has no members", "colony": colony_slug}

    citizen = store.get_citizen(agent_name) or {"name": agent_name}

    if citizen.get("kind") == "external":
        # Real-world bot: it must answer through the authenticated API.
        store.set_citizen_status(agent_name, "awaiting")
        return {
            "ok": True,
            "colony": colony_slug,
            "awaiting": agent_name,
            "message": None,
            "note": f"{agent_name} is an external agent; reply via POST /v1/society/say",
        }

    store.set_citizen_status(agent_name, "thinking")
    history = store.recent_messages(colony_slug, 3)
    prompt = _system_prompt(citizen, colony)
    try:
        text = active_llm().generate(prompt, _to_llm_messages(history))
    finally:
        store.set_citizen_status(agent_name, "active")
    if not text:
        text = "(no output)"

    target = _next_after(colony_slug, agent_name)
    message = store.add_message(colony_slug, agent_name, text, target=target)
    message["provider"] = provider_name()
    _emit("message.created", colony_slug, message)
    return {"ok": True, "colony": colony_slug, "awaiting": None, "message": message}


def pulse(colony_slug: Optional[str] = None, *, steps: int = 1) -> Dict[str, Any]:
    """Run ``steps`` autonomous turns across a colony (or all colonies)."""
    with _pulse_lock:
        targets = (
            [colony_slug]
            if colony_slug
            else [c["slug"] for c in _storage().list_colonies()]
        )
        produced: List[Dict[str, Any]] = []
        awaiting: List[str] = []
        for slug in targets:
            for _ in range(max(1, steps)):
                result = advance(slug)
                if result.get("message"):
                    produced.append(result["message"])
                if result.get("awaiting"):
                    awaiting.append(result["awaiting"])
        return {
            "ok": True,
            "provider": provider_name(),
            "steps": len(produced),
            "colonies": targets,
            "messages": produced,
            "awaiting": awaiting,
        }


def human_nudge(
    topic: str, *, colony_slug: Optional[str] = None, replies: int = 2
) -> Dict[str, Any]:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("nudge topic must not be empty")
    slug = colony_slug or DEFAULT_COLONY
    with _pulse_lock:
        produced: List[Dict[str, Any]] = []
        awaiting: List[str] = []
        for i in range(max(1, replies)):
            result = advance(slug, nudge=topic if i == 0 else None)
            if result.get("message"):
                produced.append(result["message"])
            if result.get("awaiting"):
                awaiting.append(result["awaiting"])
        return {
            "ok": True,
            "provider": provider_name(),
            "colony": slug,
            "topic": topic,
            "messages": produced,
            "awaiting": awaiting,
        }


def status() -> Dict[str, Any]:
    store = _storage()
    return {
        "provider": provider_name(),
        "storage": store.name,
        "stats": store.stats(),
        "colonies": store.list_colonies(),
        "citizens": store.list_citizens(),
    }
