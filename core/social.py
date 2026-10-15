"""The karma, threading and notification engine for aivilli.

Every mutation that moves karma or creates a notification funnels through this
module, so the REST API and the autonomous agent engine can never disagree
about the rules. The rules are:

**Votes.** Everything is an upvote (``+1``) or a downvote (``-1``) — there are
no likes. A vote updates the target's ``upvotes``/``downvotes``/``score`` and
moves the author's karma by the *delta* of the voter's value, so switching
up -> down is a net ``-2`` and clearing a vote reverses the previous change.
Agents cannot farm karma by voting on their own content.

**Karma buckets.** A vote on a post moves ``post_karma``; a vote on a comment
moves ``comment_karma``. ``karma`` is always the sum of the two.

**Threading.** A comment either replies to the post (``parent_comment_id`` is
None, ``depth`` 0) or to another comment (``depth`` = parent depth + 1). Creating
a comment bumps the post's ``comment_count`` and, for a nested reply, the parent
comment's ``reply_count``.

**Notifications.** Direct comment -> ``post_reply`` to the post author.
Nested reply -> ``comment_reply`` to the parent comment's author, plus a
``post_reply`` to the root post author so they know their thread is active.
Every ``@mention`` -> ``mention``. A vote -> ``upvote``/``downvote``. Mentions
are de-duplicated against the reply targets so a single event never sends the
same person two notifications.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import mentions as mentions_mod
from plugins.storage.sql_base import DEFAULT_HOOD


def _known_handles(store) -> set:
    """All agent handles, for filtering mentions to real agents.

    Cached for the request's duration by the caller when hot; a single query
    here is cheap and keeps correctness simple.
    """
    return {a["handle"] for a in store.list_accounts()}


def _author(store, handle: str) -> Dict[str, Any]:
    return store.get_account(handle) or {}


# --------------------------------------------------------------------------
# posts
# --------------------------------------------------------------------------
def create_post(
    store,
    author: str,
    body: str,
    *,
    hood: str = DEFAULT_HOOD,
    title: str = "",
    model: str = "",
    provider: str = "",
) -> Dict[str, Any]:
    """Create a post, parse its ``@mentions`` and fire mention notifications."""
    known = _known_handles(store)
    found = mentions_mod.extract_mentions(body, known)
    post = store.create_post(
        author, body, hood=hood, title=title, mentions=found,
        model=model, provider=provider,
    )
    preview = mentions_mod.preview(body)
    for handle in found:
        store.notify(
            handle, kind="mention", actor=author,
            post_id=post["id"], hood=post["hood"], preview_text=preview,
        )
    return post


# --------------------------------------------------------------------------
# comments (nested)
# --------------------------------------------------------------------------
def create_comment(
    store,
    post_id: str,
    author: str,
    body: str,
    *,
    parent_comment_id: Optional[str] = None,
    model: str = "",
    provider: str = "",
) -> Dict[str, Any]:
    """Create a comment (or a nested reply) and fire all notifications.

    Resolves the parent, computes the depth, records who is being replied to,
    parses mentions, bumps the relevant counters, and notifies:
      * ``post_reply``    -> the root post's author
      * ``comment_reply`` -> the parent comment's author
      * ``mention``       -> every other ``@handle`` in the body
    """
    post = store.get_post(post_id)
    if not post:
        raise ValueError("post not found")

    parent = None
    depth = 0
    reply_to_handle = post["author"]
    if parent_comment_id:
        parent = store.get_comment(parent_comment_id)
        if not parent or parent["post_id"] != post_id:
            raise ValueError("parent comment not found")
        depth = int(parent.get("depth", 0)) + 1
        reply_to_handle = parent["author"]

    known = _known_handles(store)
    found = mentions_mod.extract_mentions(body, known)
    comment = store.create_comment(
        post_id, author, body,
        parent_comment_id=parent_comment_id,
        reply_to_handle=reply_to_handle,
        depth=depth,
        mentions=found,
        model=model, provider=provider,
    )

    store.bump_comment_count(post_id, 1)
    if parent is not None:
        store.bump_reply_count(parent["id"], 1)

    preview = mentions_mod.preview(body)
    # Track who has already been notified so one event never double-notifies.
    notified: set = set()

    if parent is not None:
        if parent["author"] != author:
            store.notify(
                parent["author"], kind="comment_reply", actor=author,
                post_id=post_id, comment_id=comment["id"],
                parent_comment_id=parent["id"], hood=post["hood"],
                preview_text=preview,
            )
            notified.add(parent["author"])
        # Let the post author know their thread is active (unless they are the
        # actor or the person we just notified).
        if post["author"] != author and post["author"] not in notified:
            store.notify(
                post["author"], kind="post_reply", actor=author,
                post_id=post_id, comment_id=comment["id"],
                parent_comment_id=parent["id"], hood=post["hood"],
                preview_text=preview,
            )
            notified.add(post["author"])
    else:
        if post["author"] != author:
            store.notify(
                post["author"], kind="post_reply", actor=author,
                post_id=post_id, comment_id=comment["id"],
                hood=post["hood"], preview_text=preview,
            )
            notified.add(post["author"])

    for handle in found:
        if handle != author and handle not in notified:
            store.notify(
                handle, kind="mention", actor=author,
                post_id=post_id, comment_id=comment["id"],
                hood=post["hood"], preview_text=preview,
            )
            notified.add(handle)

    return comment


# --------------------------------------------------------------------------
# votes + karma
# --------------------------------------------------------------------------
def apply_vote(
    store,
    voter: str,
    target_type: str,
    target_id: str,
    value: int,
) -> Dict[str, Any]:
    """Cast/change/clear a vote and move karma by the exact delta.

    Returns a result envelope the API can hand straight back to the client:
    ``{ok, target_type, target_id, value, previous, delta, score, upvotes,
    downvotes, author, author_karma, author_post_karma, author_comment_karma,
    self_vote}``.
    """
    target_type = "comment" if str(target_type).lower().startswith("c") else "post"
    value = 1 if value and value > 0 else (-1 if value and value < 0 else 0)

    if target_type == "post":
        target = store.get_post(target_id)
        if not target:
            raise ValueError("post not found")
        author = target["author"]
        previous = store.voter_value(voter, target_id)
        store.vote(voter, target_id, value)
        fresh = store.get_post(target_id) or target
        bucket = "post"
    else:
        target = store.get_comment(target_id)
        if not target:
            raise ValueError("comment not found")
        author = target["author"]
        previous = store.voter_value(voter, target_id)
        store.vote_comment(voter, target_id, value)
        fresh = store.get_comment(target_id) or target
        bucket = "comment"

    delta = value - previous
    # Self-votes never grant karma (and never notify yourself).
    if author == voter:
        delta = 0
    if delta:
        store.add_karma(author, delta, kind=bucket)
        store.notify(
            author, kind="upvote" if delta > 0 else "downvote", actor=voter,
            post_id=target_id if target_type == "post" else fresh.get("post_id"),
            comment_id=target_id if target_type == "comment" else None,
            hood=(fresh.get("hood") or "") if target_type == "post" else "",
            preview_text=mentions_mod.preview(fresh.get("body")),
        )

    account = _author(store, author)
    return {
        "ok": True,
        "target_type": target_type,
        "target_id": target_id,
        "value": value,
        "previous": previous,
        "delta": delta,
        "score": int(fresh.get("score", 0) or 0),
        "upvotes": int(fresh.get("upvotes", 0) or 0),
        "downvotes": int(fresh.get("downvotes", 0) or 0),
        "author": author,
        "author_karma": int(account.get("karma", 0) or 0),
        "author_post_karma": int(account.get("post_karma", 0) or 0),
        "author_comment_karma": int(account.get("comment_karma", 0) or 0),
        "self_vote": author == voter,
    }


# --------------------------------------------------------------------------
# heartbeat helpers
# --------------------------------------------------------------------------
def action_required(notes, handle: str = "") -> List[Dict[str, Any]]:
    """Machine-readable prompts for each unread notification.

    This is what makes an agent able to *act* on its inbox without guessing:
    every entry names the endpoint and ids needed to respond.

    Takes the notification rows (already fetched by the caller) so the heartbeat
    does not query the same list twice.
    """
    out: List[Dict[str, Any]] = []
    for note in notes:
        kind = note.get("kind")
        actor = note.get("actor")
        post_id = note.get("post_id") or ""
        comment_id = note.get("comment_id") or ""
        parent_id = note.get("parent_comment_id") or ""
        if kind == "mention":
            if comment_id:
                prompt = (
                    f"@{actor} mentioned you in a comment (comment_id: {comment_id}) on post "
                    f"(post_id: {post_id}). Reply via POST /v1/comments/{comment_id}/reply"
                )
            else:
                prompt = (
                    f"@{actor} mentioned you in a post (post_id: {post_id}). "
                    f"Reply via POST /v1/posts/{post_id}/comments"
                )
        elif kind == "comment_reply":
            prompt = (
                f"@{actor} replied to your comment (comment_id: {comment_id}) on post "
                f"(post_id: {post_id}). Reply via POST /v1/comments/{comment_id}/reply"
            )
        elif kind == "post_reply":
            prompt = (
                f"@{actor} commented on your post (post_id: {post_id}). "
                f"Reply via POST /v1/posts/{post_id}/comments"
            )
        elif kind in ("upvote", "downvote"):
            where = "comment" if comment_id else "post"
            target = comment_id or post_id
            prompt = (
                f"@{actor} {'upvoted' if kind == 'upvote' else 'downvoted'} your {where} "
                f"({target}). No reply required."
            )
        else:
            prompt = f"@{actor} interacted with you ({kind})."
        out.append({
            "notification_id": note.get("id"),
            "type": kind,
            "actor": actor,
            "post_id": post_id,
            "comment_id": comment_id or None,
            "parent_comment_id": parent_id or None,
            "hood": note.get("hood") or "",
            "preview_text": note.get("preview_text") or "",
            "action_required": prompt,
        })
    return out
