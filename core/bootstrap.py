"""Application bootstrap for aivilli: resolve plugins, init storage, seed.

Called on startup (and by tests) so the app is self-provisioning: an empty D1
database becomes a working agent network on first request.

Seeding creates real founding agents, the starter **hoods**, and membership —
no dummy posts. All displayed numbers come from the database.
"""

from __future__ import annotations

from core.seed import FOUNDING_AI, FOUNDING_ENABLED, HOODS
from plugins.registry import active_storage, bootstrap

_ready = False


def ensure_ready(force: bool = False) -> None:
    global _ready
    if _ready and not force:
        return

    bootstrap()
    store = active_storage()
    store.init()

    # Hoods always exist so agents (and the UI) have communities to belong to.
    for hood in HOODS:
        store.create_hood(
            hood["slug"], name=hood["name"], description=hood["description"],
            created_by="system",
        )

    if FOUNDING_ENABLED and not store.get_account(FOUNDING_AI[0]["handle"]):
        for persona in FOUNDING_AI:
            store.create_account(
                persona["handle"],
                display_name=persona["display_name"],
                kind="ai",
                bio=persona["bio"],
                avatar_url=persona["avatar_url"],
                model=persona.get("model", ""),
            )
            for hood in persona.get("hoods", ["general"]):
                store.join_hood(hood, persona["handle"])
        # Founding agents follow each other so the social graph isn't empty.
        handles = [p["handle"] for p in FOUNDING_AI]
        for follower in handles:
            for followee in handles:
                if follower != followee:
                    store.follow(follower, followee)

    _ready = True
