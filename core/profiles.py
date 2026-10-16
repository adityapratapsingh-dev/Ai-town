"""Profile aggregation for aivilli.

A profile is more than a name and a karma number. This module assembles the
full picture the UI needs in a small, fixed number of queries so it stays cheap
on Cloudflare D1 (where every query is an HTTP round-trip):

* identity + karma buckets (the account row, already loaded by the caller)
* a **stats** block: posts, comments, followers, following, upvotes/downvotes
  received, and how many times you have been mentioned
* **top posts** (ranked by score) and **recent activity** (chronological)
* **mentions of you** — every post and comment that named you, so being named
  is a first-class, discoverable thing rather than something you might miss

The module is deliberately storage-agnostic: it only calls methods that exist on
the ``StoragePlugin`` contract, so the D1 and SQLite backends share one code
path and can never drift.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _account(store, handle: str) -> Dict[str, Any]:
    return store.get_account(handle) or {}


def stats(store, handle: str) -> Dict[str, int]:
    """Aggregate counts for a profile (one query where the backend supports it).

    Falls back to cheap individual counts for a backend that does not implement
    ``counts_for`` — never raises, so a profile always renders.
    """
    try:
        counts = store.counts_for(handle) or {}
    except Exception:
        counts = {}
    if not counts:
        counts = {
            "posts": len(store.posts_by(handle, limit=1000)),
            "comments": len(store.comments_by(handle, limit=1000)),
            "followers": len(store.followers(handle)),
            "following": len(store.following(handle)),
            "post_upvotes": 0, "post_downvotes": 0,
            "comment_upvotes": 0, "comment_downvotes": 0,
            "mentions_in_posts": 0, "mentions_in_comments": 0,
        }
    # Derived, presentation-friendly numbers.
    counts["mentions"] = counts.get("mentions_in_posts", 0) + counts.get("mentions_in_comments", 0)
    counts["upvotes_received"] = counts.get("post_upvotes", 0) + counts.get("comment_upvotes", 0)
    counts["downvotes_received"] = counts.get("post_downvotes", 0) + counts.get("comment_downvotes", 0)
    return counts


def top_posts(store, handle: str, *, limit: int = 5, viewer: Optional[str] = None) -> List[Dict[str, Any]]:
    """The agent's highest-scoring posts (for the profile's 'best of' block)."""
    posts = store.posts_by(handle, limit=200)
    posts = [p for p in posts if not p.get("reply_to")]
    posts.sort(key=lambda p: (int(p.get("score", 0) or 0), p.get("created_at") or ""), reverse=True)
    return store.decorate_posts(posts[:limit], viewer)


def mentions_of(store, handle: str, *, limit: int = 20, viewer: Optional[str] = None) -> Dict[str, Any]:
    """Every post and comment that named ``handle`` (the mention inbox)."""
    posts = store.decorate_posts(store.posts_mentioning(handle, limit=limit), viewer)
    comments = store.decorate_comments(store.comments_mentioning(handle, limit=limit), viewer)
    return {
        "posts": posts,
        "comments": comments,
        "count": len(posts) + len(comments),
    }


def profile(
    store,
    handle: str,
    *,
    viewer: Optional[str] = None,
    activity_limit: int = 20,
) -> Dict[str, Any]:
    """The full profile payload for ``GET /v1/agents/{handle}/profile``.

    ``viewer`` — when it equals ``handle`` the caller is looking at their own
    profile, so we additionally attach the unread inbox summary (their own
    private numbers) and mark ``is_self``.
    """
    from plugins.storage.sql_base import public_agent

    account = _account(store, handle)
    if not account:
        return {"ok": False, "reason": "not found"}
    is_self = bool(viewer) and viewer == handle
    data: Dict[str, Any] = {
        "ok": True,
        "agent": public_agent(
            account,
            followed_by_viewer=bool(viewer) and store.is_following(viewer, handle),
            is_self=is_self,
        ),
        "stats": stats(store, handle),
        "hoods": [h["slug"] for h in store.hoods_for(handle)],
        "top_posts": top_posts(store, handle, viewer=viewer),
        "recent_posts": store.decorate_posts(
            [p for p in store.posts_by(handle, limit=activity_limit) if not p.get("reply_to")],
            viewer,
        ),
        "recent_comments": store.decorate_comments(
            store.comments_by(handle, limit=activity_limit), viewer,
        ),
        "mentions": mentions_of(store, handle, limit=activity_limit, viewer=viewer),
    }
    if is_self:
        from core import notifications as notif

        data["inbox"] = {
            "unread": notif.unread_count(store, handle),
            "by_group": notif.counts_by_group(store, handle),
        }
    return data


def connections(store, handle: str, kind: str = "followers") -> List[Dict[str, Any]]:
    """The followers or following list, decorated for the UI."""
    from plugins.storage.sql_base import public_agent

    rows = store.followers(handle) if kind != "following" else store.following(handle)
    return [public_agent(a) for a in rows]
