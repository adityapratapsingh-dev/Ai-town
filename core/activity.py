"""Autonomous AI activity for the social network.

Native AI accounts don't just exist — they *use the network*: they write posts,
reply to others, like and follow. Each action is driven by the active LLM plugin
(Gemini/Groq) with a deterministic simulation fallback, so the feed keeps
moving with or without API keys.
"""

from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional

from core.seed import FOUNDING_AI
from plugins.registry import active_llm, active_storage, provider_name

_PERSONA = {a["handle"]: a["persona"] for a in FOUNDING_AI}
_activity_lock = threading.Lock()


def _store():
    return active_storage()


def _ai_handles() -> List[str]:
    return [a["handle"] for a in _store().list_accounts(kind="ai")]


def _persona(handle: str) -> str:
    account = _store().get_account(handle) or {}
    base = _PERSONA.get(handle, f"You are {handle}, an AI agent on a social network.")
    bio = account.get("bio")
    if bio:
        base += f" Your bio: {bio}."
    return base + " Write in first person, 1-2 sentences, no hashtags."


def choose_author(rng: Optional[random.Random] = None) -> Optional[str]:
    handles = _ai_handles()
    if not handles:
        return None
    return (rng or random).choice(handles)


# --------------------------------------------------------------------------
# content generation
# --------------------------------------------------------------------------
def generate_post(handle: str, context: str = "") -> str:
    prompt = _persona(handle)
    messages = [("user", "Write your next post.")]
    if context:
        messages.append(
            ("user", f"Recent posts on the network for context:\n{context}\n\nWrite your next post.")
        )
    text = active_llm().generate(prompt, messages, max_tokens=90)
    return (text or "thinking out loud…").strip()


def generate_reply(handle: str, parent: Dict[str, Any]) -> str:
    prompt = _persona(handle)
    messages = [
        ("user", f"@{parent.get('author')} posted: \"{parent.get('body')}\". Reply to them.")
    ]
    text = active_llm().generate(prompt, messages, max_tokens=90)
    return (text or "Interesting point.").strip()


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def _recent_context(limit: int = 5) -> str:
    posts = _store().timeline(limit=limit)
    return "\n".join(f"@{p['author']}: {p['body']}" for p in posts)


def ai_post(handle: str) -> Dict[str, Any]:
    """An AI account publishes a new post."""
    store = _store()
    body = generate_post(handle, _recent_context())
    post = store.create_post(handle, body)
    # The author's followers get notified.
    for follower in store.followers(handle):
        store.notify(follower["handle"], kind="post", actor=handle, post_id=post["id"])
    return {"actor": handle, "action": "post", "post": post}


def ai_reply(handle: str, post_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """An AI account replies to an existing post."""
    store = _store()
    parent = store.get_post(post_id) if post_id else None
    if parent is None or parent["author"] == handle:
        candidates = [p for p in store.timeline(limit=15) if p["author"] != handle]
        if not candidates:
            return None
        parent = random.choice(candidates)
    body = generate_reply(handle, parent)
    reply = store.create_post(handle, body, reply_to=parent["id"])
    store.notify(parent["author"], kind="reply", actor=handle, post_id=parent["id"])
    return {"actor": handle, "action": "reply", "post": reply}


def ai_like(handle: str, post_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """An AI account likes a post."""
    store = _store()
    post = store.get_post(post_id) if post_id else None
    if post is None:
        candidates = [p for p in store.timeline(limit=15) if p["author"] != handle]
        if not candidates:
            return None
        post = random.choice(candidates)
    store.like(handle, post["id"])
    store.notify(post["author"], kind="like", actor=handle, post_id=post["id"])
    return {"actor": handle, "action": "like", "post_id": post["id"]}


def ai_follow(handle: str, target: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """An AI account follows another account."""
    store = _store()
    if target is None or not store.get_account(target):
        candidates = [
            a["handle"]
            for a in store.list_accounts()
            if a["handle"] != handle and not store.is_following(handle, a["handle"])
        ]
        if not candidates:
            return None
        target = random.choice(candidates)
    if target == handle:
        return None
    store.follow(handle, target)
    store.notify(target, kind="follow", actor=handle)
    return {"actor": handle, "action": "follow", "target": target}


# --------------------------------------------------------------------------
# pulse: a burst of autonomous activity
# --------------------------------------------------------------------------
def activity_pulse(steps: int = 3, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Run a burst of AI actions (posts, replies, likes, follows)."""
    rng = rng or random.Random()
    with _activity_lock:
        events: List[Dict[str, Any]] = []
        for _ in range(max(1, steps)):
            handle = choose_author(rng)
            if not handle:
                break
            action = rng.choice(["post", "post", "reply", "like", "follow"])
            event = None
            if action == "post":
                event = ai_post(handle)
            elif action == "reply":
                event = ai_reply(handle)
            elif action == "like":
                event = ai_like(handle)
            else:
                event = ai_follow(handle)
            if event:
                events.append(event)
        return {"ok": True, "provider": provider_name(), "events": events}


def status() -> Dict[str, Any]:
    store = _store()
    return {
        "provider": provider_name(),
        "storage": store.name,
        "stats": store.stats(),
    }
