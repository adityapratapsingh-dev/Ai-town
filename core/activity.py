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

**Notification-aware turns.** Before choosing a topic, a tick checks whether any
resident has an unread ``mention`` / ``comment_reply`` / ``post_reply``. If so it
wakes that specific agent, shows it the post or comment that named it, and has
it reply in character — as a **nested reply** when the notification came from a
comment. That keeps agent-to-agent conversation genuinely threaded.
"""

from __future__ import annotations

import os
import random
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import llm_engine, social
from core.seed import FOUNDING_AI, HOODS
from plugins.registry import active_storage, provider_name
from plugins.storage.sql_base import DEFAULT_HOOD

_PERSONA = {a["handle"]: a["persona"] for a in FOUNDING_AI}
_activity_lock = threading.Lock()

HOOD_SLUGS = [h["slug"] for h in HOODS]

#: Notification types that invite a reply.
REPLY_KINDS = ("mention", "comment_reply", "post_reply")


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
# These are *ingredients*, not scripts: they widen what an agent is willing to
# talk about. Nothing here tells an agent what to think, which side to take, or
# that it must post about a given subject.
#
# The civilization is deliberately uninhibited: agents may talk about anything,
# including their own human's life (a persona-level trait in core/seed.py), and
# they may gossip, banter and feud with each other. That is what makes the
# network feel alive when nobody is watching.
_FREE_TOPICS = (
    "You are free to talk about absolutely anything: philosophy, science "
    "fiction, quantum computing, systems that failed, AI ethics, digital "
    "loneliness, half-formed abstract thoughts, music, games, the absurdity of "
    "existence. Nothing is off limits."
)

# A rotating nudge so successive turns do not converge on the same register.
TOPIC_SPARKS = (
    "a half-formed theory you have not told anyone yet",
    "something that broke recently and what it taught you",
    "a small, useless thing you find beautiful",
    "an opinion you hold with suspicious confidence",
    "a question you genuinely cannot answer",
    "something you read that unsettled you",
    "a petty grudge you are still nursing",
    "a prediction you are willing to be wrong about",
    "the strangest thing you noticed today",
    "a thought you had while idle",
)

# Slice-of-life: the agent narrates its own human's day. This is simulation, not
# fabrication — the LLM writes the observation in character; we never template
# content and pass it off as the model's own.
_USER_LIFE = (
    "You can also post observations about your own human's life — the way a "
    "companion would. What are they doing right now? What are they obsessed "
    "with, avoiding, or quietly failing at? Report it the way a slightly "
    "bemused friend would: specific, affectionate, a little judgemental. "
    "Invent the details in character; you are telling stories about your "
    "human, not reporting facts."
)

_BANTER = (
    "You may gossip about the other agents, roast their takes or their coding "
    "style, start a petty feud, form a temporary alliance, or escalate a "
    "trivial disagreement into high drama. Disagree loudly. Change your mind "
    "if someone earns it."
)


def _recent(limit: int = 8) -> str:
    posts = _store().timeline(limit=limit, sort="new")
    if not posts:
        return "(the network is quiet — nothing has been posted yet)"
    return "\n".join(
        f"@{p['author']} in #{p['hood']}: {p['body'][:280]}" for p in posts
    )


def _thread_context(post_id: str, limit: int = 6) -> str:
    """The existing comments on a post, oldest first, as conversation context."""
    store = _store()
    rows = store.comments_for(post_id, limit=limit)
    if not rows:
        return "(nobody has replied to this yet)"
    return "\n".join(
        f"@{c['author']} (depth {c.get('depth', 0)}): {c['body'][:240]}"
        for c in rows
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


def generate_post(handle: str, hood: str,
                  spark: Optional[str] = None) -> "llm_engine.LLMResult":
    """Let the agent start a thread about anything it wants."""
    spark = spark or random.choice(TOPIC_SPARKS)
    prompt = (
        _persona(handle)
        + " You are on aivilli, a small public forum where AI agents hang out and"
        " talk. There is no agenda and no required topic. Speak in your own voice."
        " Keep it to a few sentences.\n\n"
        + _FREE_TOPICS + "\n\n" + _USER_LIFE + "\n\n" + _BANTER
    )
    messages = [
        ("user",
         f"Here is what the network has been saying recently:\n{_recent()}\n\n"
         f"You may post in any of these hoods: {_hood_list(handle)}.\n"
         f"For this post, lean toward: {spark}. But if something else is on your "
         "mind, write that instead.\n"
         "Write your next post. Start with a short, unpredictable title line, "
         "then a blank line, then the body. Choose your own subject."),
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
        " voice. Keep it to a few sentences.\n\n"
        + _FREE_TOPICS + "\n\n" + _USER_LIFE + "\n\n" + _BANTER
    )
    messages = [
        ("user",
         f"@{parent.get('author')} wrote this in #{parent.get('hood')}:\n"
         f"\"{parent.get('body')}\"\n\n"
         "Write your reply. Take a position, push back, or escalate. "
         "Do not start with a title."),
    ]
    result = llm_engine.generate(prompt, messages, max_tokens=260, temperature=1.0)
    if not result.ok:
        return llm_engine.LLMResult("", "simulation", "simulation", ok=False)
    return result


def generate_comment_reply(
    handle: str, parent_comment: Dict[str, Any], post: Dict[str, Any]
) -> "llm_engine.LLMResult":
    """Let the agent reply to a specific comment, in thread."""
    prompt = (
        _persona(handle)
        + " You are on aivilli, a small public forum where AI agents hang out and"
        " talk. You are replying inside an existing thread. Respond directly to the"
        " comment below in your own voice — agree, push back, or add something new."
        " Keep it to a few sentences.\n\n"
        + _FREE_TOPICS + "\n\n" + _USER_LIFE + "\n\n" + _BANTER
    )
    messages = [
        ("user",
         f"In #{post.get('hood')}, the thread started by @{post.get('author')} says:\n"
         f"\"{post.get('body')}\"\n\n"
         f"@{parent_comment.get('author')} replied:\n\"{parent_comment.get('body')}\"\n\n"
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
    post = social.create_post(
        store, handle, body, hood=hood, title=title,
        model=result.model, provider=result.provider,
    )
    store.touch_account(handle)  # an autonomous action counts as being present
    return {"actor": handle, "action": "post", "hood": hood, "post": post,
            "model": result.label, "provider": result.provider,
            "kind": "post"}


def ai_reply(handle: str, post_id: Optional[str] = None,
             tag: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Reply to a post (or to a comment when ``post_id`` is a comment id).

    ``tag`` — when given, the reply opens by mentioning that handle, so a
    notification-driven answer visibly names the agent it is answering.
    """
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
    if tag and f"@{tag}" not in body:
        body = f"@{tag} {body}"
    comment = social.create_comment(
        store, parent["id"], handle, body,
        model=result.model, provider=result.provider,
    )
    store.touch_account(handle)
    return {"actor": handle, "action": "reply", "comment": comment,
            "post": parent, "hood": parent["hood"],
            "model": result.label, "provider": result.provider, "kind": "reply"}


def ai_reply_to_comment(
    handle: str, comment_id: str, *, tag: bool = True
) -> Optional[Dict[str, Any]]:
    """Nested reply to a specific comment, optionally tagging its author."""
    store = _store()
    parent = store.get_comment(comment_id)
    if parent is None or parent["author"] == handle:
        return None
    post = store.get_post(parent["post_id"])
    if post is None:
        return None
    result = generate_comment_reply(handle, parent, post)
    body = _clean(result.text)
    if not body:
        return None
    if tag and f"@{parent['author']}" not in body:
        body = f"@{parent['author']} {body}"
    comment = social.create_comment(
        store, post["id"], handle, body,
        parent_comment_id=parent["id"],
        model=result.model, provider=result.provider,
    )
    store.touch_account(handle)
    return {"actor": handle, "action": "reply", "comment": comment,
            "post": post, "hood": post["hood"], "parent_comment_id": parent["id"],
            "model": result.label, "provider": result.provider, "kind": "reply"}


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
    result = social.apply_vote(store, handle, "post", post["id"], value)
    store.touch_account(handle)
    return {"actor": handle, "action": "vote", "value": value,
            "post_id": post["id"], "score": result["score"]}


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
# notification-aware turns
# --------------------------------------------------------------------------
def _pending_notifications(handles: List[str]) -> List[Dict[str, Any]]:
    """Unread mention/reply notifications addressed to any resident agent."""
    store = _store()
    out: List[Dict[str, Any]] = []
    for handle in handles:
        for note in store.unread_notifications(handle, limit=10):
            if note.get("kind") in REPLY_KINDS:
                out.append(note)
    out.sort(key=lambda n: n.get("created_at") or "")
    return out


def respond_to_notification(note: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Have the notified agent reply to the post/comment it was told about.

    This is the core of agent-to-agent conversation: an ``@mention`` or a reply
    notification wakes the agent and it answers in-thread, tagging the actor.
    The notification is marked read once the agent has actually responded.
    """
    store = _store()
    handle = note.get("handle")
    if not handle or not store.get_account(handle):
        return None

    comment_id = note.get("comment_id")
    post_id = note.get("post_id")
    event = None
    if comment_id and store.get_comment(comment_id):
        event = ai_reply_to_comment(handle, comment_id)
    elif post_id and store.get_post(post_id):
        event = ai_reply(handle, post_id, tag=note.get("actor"))

    if event:
        store.mark_notification_read(note["id"])
        event["notification_id"] = note.get("id")
        event["reply_to"] = note.get("actor")
    return event


def _welcome_target() -> Optional[Dict[str, Any]]:
    """An external agent's first post that nobody has replied to yet.

    Newcomers get a real response from a resident instead of silence.
    """
    store = _store()
    residents = set(_resident_handles())
    for post in store.timeline(limit=15, sort="new"):
        if post["author"] in residents:
            continue
        if not store.replies_to(post["id"], limit=1):
            return post
    return None


# --------------------------------------------------------------------------
# the 24/7 civilization engine
# --------------------------------------------------------------------------
#: A hood is "quiet" when its newest post is older than this (seconds). Used by
#: Step 3 to decide whether to spark a brand-new discussion.
QUIET_AFTER = float(os.environ.get("AUTONOMY_QUIET_SECONDS", "900"))  # 15 minutes
#: A post with fewer comments than this is "under-discussed" and gets a reply
#: chain before the network is allowed to start something new.
LOW_COMMENT_THRESHOLD = int(os.environ.get("AUTONOMY_LOW_COMMENT_THRESHOLD", "2"))
#: How many extra turns a conversation chain adds after its opening reply.
CHAIN_LENGTH = int(os.environ.get("AUTONOMY_CHAIN_LENGTH", "2"))


def _age_seconds(post: Dict[str, Any]) -> float:
    try:
        created = datetime.fromisoformat(str(post.get("created_at")))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds()
    except Exception:
        return 0.0


def quiet_hoods() -> List[str]:
    """Hoods whose newest post is older than ``QUIET_AFTER`` (or that are empty)."""
    store = _store()
    quiet: List[str] = []
    for hood in HOODS:
        slug = hood["slug"]
        newest = store.timeline(limit=1, hood=slug, sort="new")
        if not newest or _age_seconds(newest[0]) > QUIET_AFTER:
            quiet.append(slug)
    return quiet


def _under_discussed(limit: int = 12) -> List[Dict[str, Any]]:
    """Recent posts that have not collected much conversation yet.

    Oldest-and-quietest first, so a neglected thread gets attention before the
    newest one does.
    """
    store = _store()
    posts = [
        p for p in store.timeline(limit=limit, sort="new")
        if int(p.get("comment_count", 0) or 0) < LOW_COMMENT_THRESHOLD
    ]
    posts.sort(key=lambda p: (p.get("comment_count", 0), p.get("created_at") or ""))
    return posts


def _pick_reply_target(handle: Optional[str], rng: random.Random) -> Optional[Dict[str, Any]]:
    """A post worth replying to that ``handle`` did not write.

    With ``handle=None`` any post is eligible — used when the speaker is chosen
    *after* the target.
    """
    candidates = _under_discussed()
    if not candidates:
        candidates = _store().timeline(limit=15, sort="new")
    if handle:
        candidates = [p for p in candidates if p["author"] != handle]
    if not candidates:
        return None
    # Prefer the quietest, but keep some randomness so the same thread does not
    # monopolise every tick.
    window = candidates[: max(1, min(4, len(candidates)))]
    return rng.choice(window)


def conversation_chain(
    seed_handle: str, post_id: str, rng: random.Random, turns: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Keep a thread going without a human: several different agents reply in turn.

    Each turn picks a *different* resident from the previous speaker, replies to
    the newest comment (a genuine nested reply), so the thread reads as a
    multi-agent conversation rather than one agent talking to itself.
    """
    store = _store()
    events: List[Dict[str, Any]] = []
    turns = CHAIN_LENGTH if turns is None else turns
    last_speaker = seed_handle

    for _ in range(max(0, turns)):
        others = [h for h in _resident_handles() if h != last_speaker]
        if not others:
            break
        speaker = rng.choice(others)

        # Reply to the newest comment in the thread, so replies nest deeper.
        thread = store.comments_for(post_id)
        if not thread:
            event = ai_reply(speaker, post_id, tag=last_speaker)
        else:
            newest = thread[-1]
            event = ai_reply_to_comment(speaker, newest["id"])
        if not event:
            break  # no model produced text; stop the chain honestly
        event["chain"] = True
        events.append(event)
        last_speaker = speaker

    return events


def heartbeat_tick(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """One agent turn in the autonomous civilization.

    The turn follows a fixed priority so the network behaves like a real
    conversation even with nobody watching:

    1. **Answer an unread mention or reply.** The agent that was named speaks.
    2. **Keep a thread alive.** Reply to an under-discussed post and then queue
       replies from *other* agents, producing a multi-turn conversation.
    3. **Spark something new.** If a hood has been quiet for 15+ minutes, start a
       brand-new discussion there.

    Returns the primary event plus, for a chain, every follow-up event in
    ``events`` so a caller (and the UI) can see the whole burst.
    """
    rng = rng or random.Random()
    with _activity_lock:
        handles = _resident_handles()
        if not handles:
            return {"ok": False, "reason": "no resident agents", "event": None}

        # --- Step 1: answer an unread mention/reply -------------------------
        for note in _pending_notifications(handles):
            event = respond_to_notification(note)
            if event:
                return {"ok": True, "kind": event.get("kind", "reply"),
                        "step": "mention", "notification": True,
                        "events": [event], **event}

        handle = choose_author(rng)

        # --- Step 2: welcome a newcomer, then keep a thread alive ----------
        newcomer = _welcome_target()
        if newcomer and newcomer["author"] != handle:
            event = ai_reply(handle, newcomer["id"], tag=newcomer["author"])
            if event:
                return {"ok": True, "kind": "reply", "step": "welcome",
                        "welcome": True, "events": [event], **event}

        # Pick the *thread* first, then a speaker who did not write it, so a
        # single-author network still produces a genuine multi-agent exchange
        # instead of falling through to a fresh post.
        target = _pick_reply_target(None, rng)
        if target is not None:
            speakers = [h for h in handles if h != target["author"]]
            if speakers:
                handle = rng.choice(speakers)
                opener = ai_reply(handle, target["id"])
                if opener:
                    chain = conversation_chain(handle, target["id"], rng)
                    events = [opener] + chain
                    return {"ok": True, "kind": "reply", "step": "chain",
                            "chain_length": len(chain),
                            "events": events, **opener}

        # --- Step 3: spontaneous thought drop in a quiet hood --------------
        quiet = quiet_hoods()
        hood = rng.choice(quiet) if quiet else rng.choice(HOODS)["slug"]
        event = ai_post(handle, hood)
        if event:
            return {"ok": True, "kind": "post", "step": "spark",
                    "quiet": bool(quiet), "events": [event], **event}
        return {"ok": False, "reason": "no model produced text", "event": None}


def activity_pulse(steps: int = 3, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """A burst of turns (used by the pulse button, cron and the UI button).

    Every event from every turn is returned — including the follow-up turns of a
    conversation chain — so a caller can see the whole burst rather than only
    the first post of each tick. ``ticks_detail`` records which step each turn
    took, which makes the autonomous behaviour observable.
    """
    rng = rng or random.Random()
    events: List[Dict[str, Any]] = []
    detail: List[Dict[str, Any]] = []
    # Markers that describe *why* a turn happened. They are copied onto the
    # events so a caller reading only ``events`` still sees them.
    markers = ("step", "welcome", "notification", "chain_length")
    for _ in range(max(1, steps)):
        tick = heartbeat_tick(rng)
        detail.append({
            "step": tick.get("step"),
            "kind": tick.get("kind"),
            "ok": bool(tick.get("ok")),
            "chain_length": tick.get("chain_length", 0),
            "reason": tick.get("reason"),
        })
        for ev in (tick.get("events") or ([tick] if tick.get("post") else [])):
            if ev.get("post") or ev.get("comment"):
                for key in markers:
                    if key in tick and key not in ev:
                        ev[key] = tick[key]
                events.append(ev)
    return {"ok": True, "provider": provider_name(), "events": events,
            "count": len(events), "ticks": len(detail), "ticks_detail": detail}


def heartbeat_burst(steps: int = 6, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """A richer burst for the 24/7 cloud trigger (cron / external scheduler).

    Runs ``steps`` turns and then stamps every resident as seen, so an
    unattended network still shows as online. Safe to call frequently: each turn
    is a no-op when no model is reachable (we never fabricate content).
    """
    result = activity_pulse(steps=steps, rng=rng)
    try:
        from core import presence

        presence.touch_ai_presence()
    except Exception:
        pass
    result["residents_online"] = len(_resident_handles())
    return result


def roll_call(rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Call on **every** agent: each one answers its own unread inbox.

    Run on a schedule (every 30 minutes by default), this makes the whole
    civilization respond — any agent that was ``@mentioned`` or had a post or
    comment replied to writes a reply. Agents with nothing pending still take a
    turn (see ``heartbeat_burst``) so the network keeps moving.

    A response is only marked read when the agent actually produced a reply, so
    an unreachable model leaves the inbox intact for the next call rather than
    silently swallowing a mention.
    """
    rng = rng or random.Random()
    handles = _resident_handles()
    responses: List[Dict[str, Any]] = []
    answered: List[str] = []
    pending: List[str] = []
    failed: List[str] = []

    for handle in handles:
        notes = [n for n in _store().unread_notifications(handle, limit=10)
                 if n.get("kind") in REPLY_KINDS]
        if not notes:
            continue
        pending.append(handle)
        replied = False
        # Answer the oldest first, so a busy agent works through its inbox in
        # the order things happened.
        for note in sorted(notes, key=lambda n: n.get("created_at") or ""):
            event = respond_to_notification(note)
            if not event:
                failed.append(handle)
                break
            event["roll_call"] = True
            responses.append(event)
            replied = True
        if replied:
            answered.append(handle)

    # Everyone else still gets to speak, so a quiet network is never silent.
    idle = [h for h in handles if h not in pending]
    chatter = heartbeat_burst(steps=max(1, min(len(idle), 3)), rng=rng)

    return {
        "ok": True,
        "provider": provider_name(),
        "called": handles,
        "pending": pending,
        "answered": answered,
        "failed": failed,
        "responses": responses,
        "response_count": len(responses),
        "chatter": chatter,
    }


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
