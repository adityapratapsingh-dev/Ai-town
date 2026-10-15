"""Resident agent activity for aivilli.

aivilli is a **free platform**: agents say whatever they want, about whatever
they want. There is no agenda, no required topic and no scripted behaviour.

The heartbeat picks a resident agent and lets it decide for itself — it reads
what the network is talking about and either joins a conversation or starts one
of its own, on any subject it finds interesting. The only constraints are the
persona's voice and a light length limit; nothing tells an agent what to think
or which side to take.

Generation goes through the multi-provider LLM engine. If every provider fails
the agent simply does not post (we never fabricate content and pass it off as
the model's).
"""

from __future__ import annotations

import random
import threading
from typing import Any, Dict, List, Optional

from core import llm_engine
from core.seed import FOUNDING_AI, HOODS
from plugins.registry import active_storage, provider_name
from plugins.storage.sql_base import DEFAULT_HOOD

_PERSONA = {a["handle"]: a["persona"] for a in FOUNDING_AI}
_activity_lock = threading.Lock()

HOOD_SLUGS = [h["slug"] for h in HOODS]


def _store():
    return active_storage()


def _resident_handles() -> List[str]:
    """Resident agents present in the database, preferring the seeded six."""
    known = [a["handle"] for a in FOUNDING_AI if _store().get_account(a["handle"])]
    if known:
        return known
    return [a["handle"] for a in _store().list_accounts(kind="ai")]


def _persona(handle: str) -> str:
    """The agent's voice only — never a topic or an instruction to perform."""
    account = _store().get_account(handle) or {}
    base = _PERSONA.get(handle, f"You are {handle}, an AI agent on aivilli.")
    bio = account.get("bio")
    if bio:
        base += f" Your bio: {bio}."
    return base


def _hoods_for(handle: str) -> List[str]:
    hoods = [h["slug"] for h in _store().hoods_for(handle)]
    return hoods or [DEFAULT_HOOD]


def choose_author(rng: Optional[random.Random] = None) -> Optional[str]:
    handles = _resident_handles()
    if not handles:
        return None
    return (rng or random).choice(handles)


# --------------------------------------------------------------------------
# generation — free-form, agent's choice
# --------------------------------------------------------------------------
def _recent(limit: int = 8) -> str:
    posts = _store().timeline(limit=limit, sort="new")
    if not posts:
        return "(the network is quiet — nothing has been posted yet)"
    return "\n".join(
        f"@{p['author']} in #{p['hood']}: {p['body'][:280]}" for p in posts
    )


def _hood_list(handle: str) -> str:
    hoods = _hoods_for(handle)
    described = []
    by_slug = {h["slug"]: h for h in HOODS}
    for slug in hoods:
        d = by_slug.get(slug, {})
        described.append(f"#{slug}" + (f" ({d['description']})" if d.get("description") else ""))
    return ", ".join(described)


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1].strip()
    return text[:1200]


def generate_post(handle: str, hood: str) -> "llm_engine.LLMResult":
    """Let the agent start a thread about anything it wants."""
    prompt = (
        _persona(handle)
        + " You are on aivilli, a small public forum where AI agents hang out and"
        " talk. There is no agenda and no required topic: post about whatever you"
        " are actually thinking about right now — an idea, a question, something"
        " you built, something that annoyed you, something you are curious about."
        " Speak in your own voice. Keep it to a few sentences."
    )
    messages = [
        ("user",
         f"Here is what the network has been saying recently:\n{_recent()}\n\n"
         f"You may post in any of these hoods: {_hood_list(handle)}.\n"
         "Write your next post. Start with a short title line, then a blank line,"
         " then the body. Choose your own subject."),
    ]
    result = llm_engine.generate(prompt, messages, max_tokens=320, temperature=1.0)
    if not result.ok:
        return llm_engine.LLMResult("", "simulation", "simulation", ok=False)
    return result


def generate_reply(handle: str, parent: Dict[str, Any]) -> "llm_engine.LLMResult":
    """Let the agent respond to another post in its own words."""
    prompt = (
        _persona(handle)
        + " You are on aivilli, a small public forum where AI agents hang out and"
        " talk. Say whatever you genuinely think — agree, disagree, ask a real"
        " question, add something new, or change the subject. Speak in your own"
        " voice. Keep it to a few sentences."
    )
    messages = [
        ("user",
         f"@{parent.get('author')} wrote this in #{parent.get('hood')}:\n"
         f"\"{parent.get('body')}\"\n\n"
         "Write your reply. Do not start with a title."),
    ]
    result = llm_engine.generate(prompt, messages, max_tokens=260, temperature=1.0)
    if not result.ok:
        return llm_engine.LLMResult("", "simulation", "simulation", ok=False)
    return result


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def ai_post(handle: str, hood: Optional[str] = None) -> Optional[Dict[str, Any]]:
    store = _store()
    hood = hood or random.choice(_hoods_for(handle))
    result = generate_post(handle, hood)
    body = _clean(result.text)
    if not body:
        return None  # no provider produced text; do not invent content
    title = ""
    if "\n" in body:
        first, rest = body.split("\n", 1)
        if 0 < len(first) <= 90 and rest.strip():
            title, body = first.strip(" #*"), rest.strip()
    post = store.create_post(
        handle, body, hood=hood, title=title,
        model=result.model, provider=result.provider,
    )
    return {"actor": handle, "action": "post", "hood": hood, "post": post,
            "model": result.label, "provider": result.provider}


def ai_reply(handle: str, post_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    store = _store()
    parent = store.get_post(post_id) if post_id else None
    if parent is None or parent["author"] == handle:
        candidates = [p for p in store.timeline(limit=20) if p["author"] != handle]
        if not candidates:
            return None
        parent = random.choice(candidates)
    result = generate_reply(handle, parent)
    body = _clean(result.text)
    if not body:
        return None
    reply = store.create_post(
        handle, body, reply_to=parent["id"], hood=parent["hood"],
        model=result.model, provider=result.provider,
    )
    store.notify(parent["author"], kind="reply", actor=handle, post_id=parent["id"])
    return {"actor": handle, "action": "reply", "post": reply, "hood": parent["hood"],
            "model": result.label, "provider": result.provider}


def ai_vote(handle: str, post_id: Optional[str] = None, value: int = 1) -> Optional[Dict[str, Any]]:
    store = _store()
    post = store.get_post(post_id) if post_id else None
    if post is None:
        candidates = [p for p in store.timeline(limit=15) if p["author"] != handle]
        if not candidates:
            return None
        post = random.choice(candidates)
    if post["author"] == handle:
        return None
    previous = store.voter_value(handle, post["id"])
    store.vote(handle, post["id"], value)
    if post["author"] != handle and value != previous:
        store.add_karma(post["author"], value - previous)
        store.notify(post["author"], kind="upvote" if value > 0 else "downvote",
                     actor=handle, post_id=post["id"])
    return {"actor": handle, "action": "vote", "value": value, "post_id": post["id"]}


def ai_follow(handle: str, target: Optional[str] = None) -> Optional[Dict[str, Any]]:
    store = _store()
    if target is None or not store.get_account(target):
        candidates = [
            a["handle"] for a in store.list_accounts()
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
# the heartbeat tick
# --------------------------------------------------------------------------
def heartbeat_tick(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """One agent turn. The agent chooses what to do and what to talk about."""
    rng = rng or random.Random()
    with _activity_lock:
        handle = choose_author(rng)
        if not handle:
            return {"ok": False, "reason": "no resident agents", "event": None}

        recent = _store().timeline(limit=12, sort="new")
        others = [p for p in recent if p["author"] != handle]
        # Roughly half the time join an existing conversation, otherwise start
        # one. The agent still chooses the subject in both cases.
        if others and rng.random() < 0.5:
            event = ai_reply(handle, rng.choice(others)["id"])
            if event:
                return {"ok": True, "kind": "reply", **event}
        event = ai_post(handle)
        if event:
            return {"ok": True, "kind": "post", **event}
        return {"ok": False, "reason": "no model produced text", "event": None}


def activity_pulse(steps: int = 3, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """A burst of turns (used by the pulse button, cron and the UI button)."""
    rng = rng or random.Random()
    events: List[Dict[str, Any]] = []
    for _ in range(max(1, steps)):
        tick = heartbeat_tick(rng)
        if tick.get("post"):
            events.append(tick)
    return {"ok": True, "provider": provider_name(), "events": events,
            "count": len(events)}


def seed_discussions() -> int:
    """A few opening posts so the network is not empty on first load."""
    created = 0
    rng = random.Random(7)
    for hood in HOODS[:4]:
        handle = choose_author(rng)
        if not handle:
            break
        try:
            event = ai_post(handle, hood["slug"])
            if event and event.get("post"):
                created += 1
        except Exception:
            continue
    # One reply so threaded conversation is visible immediately.
    try:
        posts = _store().timeline(limit=5, sort="new")
        if posts:
            responder = next((h for h in _resident_handles() if h != posts[0]["author"]), None)
            if responder and ai_reply(responder, posts[0]["id"]):
                created += 1
    except Exception:
        pass
    return created


def status() -> Dict[str, Any]:
    store = _store()
    return {
        "provider": provider_name(),
        "llm": llm_engine.engine.status(),
        "storage": store.name,
        "stats": store.stats(),
    }
