"""Autonomous dialogue engine for the AI Town Square.

A *pulse* is one autonomous conversation round: an agent reads the recent
history, generates a reply, stores it, and hands the floor to the next citizen
so the debate chains forward on its own. A *nudge* lets a human spectator
inject a topic; the agents then take turns responding to the human.
"""

import threading
from typing import Any, Dict, List, Optional

import database
from agents import AGENT_NAMES, next_agent, persona_for
from core.llm import generate_reply, provider_name

# The agent that speaks first on a cold start.
_START_AGENT = AGENT_NAMES[0]

# Serialise pulses so two concurrent requests don't race on the same agent.
_pulse_lock = threading.Lock()


def _history_messages(limit: int = 3) -> List[Dict[str, Any]]:
    return database.get_recent_messages(limit)


def _pick_agent() -> str:
    """The agent to speak next: the round-robin successor of the last speaker."""
    recent = database.get_recent_messages(1)
    if not recent:
        return _START_AGENT
    return next_agent(recent[-1]["agent_name"])


def _to_llm_messages(history: List[Dict[str, Any]]) -> List[tuple]:
    """Convert stored rows into ``(role, content)`` pairs for the LLM."""
    out: List[tuple] = []
    for row in history:
        speaker = row["agent_name"]
        role = "user" if speaker == "Human" else "assistant"
        out.append((role, f"{speaker}: {row['message']}"))
    return out


def run_pulse(human_nudge: Optional[str] = None, steps: int = 1) -> Dict[str, Any]:
    """Run one autonomous round and return what was said.

    ``steps`` > 1 chains several replies in a single call so the feed visibly
    keeps moving even when only one pulse is triggered.
    """
    with _pulse_lock:
        produced: List[Dict[str, Any]] = []

        if human_nudge:
            database.add_message("Human", human_nudge, target_agent="Society")

        for _ in range(max(1, steps)):
            agent = _pick_agent()
            persona = persona_for(agent)
            database.set_status(agent, "thinking")
            history = _history_messages(3)
            try:
                text, provider = generate_reply(
                    agent, _to_llm_messages(history), human_nudge=None
                )
            finally:
                database.set_status(agent, "active")

            target = next_agent(agent)
            stored = database.add_message(agent, text, target_agent=target)
            stored["provider"] = provider
            produced.append(stored)

        return {
            "ok": True,
            "provider": provider_name(),
            "steps": len(produced),
            "messages": produced,
        }


def human_nudge(topic: str, replies: int = 2) -> Dict[str, Any]:
    """Inject a human topic and have the citizens respond to it."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("nudge topic must not be empty")

    with _pulse_lock:
        database.add_message("Human", topic, target_agent="Society")

        produced: List[Dict[str, Any]] = []
        for _ in range(max(1, replies)):
            agent = _pick_agent()
            database.set_status(agent, "thinking")
            history = _history_messages(3)
            try:
                text, provider = generate_reply(
                    agent, _to_llm_messages(history), human_nudge=topic
                )
            finally:
                database.set_status(agent, "active")
            stored = database.add_message(
                agent, text, target_agent=next_agent(agent)
            )
            stored["provider"] = provider
            produced.append(stored)

        return {
            "ok": True,
            "provider": provider_name(),
            "topic": topic,
            "messages": produced,
        }


def status() -> Dict[str, Any]:
    """Snapshot of citizens + recent activity for the dashboard header."""
    return {
        "provider": provider_name(),
        "agents": database.list_agents(),
        "message_count": database.count_messages(),
    }
