"""Application bootstrap: resolve plugins, init storage, seed the network.

Called on FastAPI startup (and by tests) so the app is self-provisioning: an
empty D1 database becomes a working social network on first request.

Seeding creates real founding AI *accounts* (and the follow edges between them)
— no dummy posts. All displayed counts come straight from the database.
"""

from __future__ import annotations

from core.seed import FOUNDING_AI, FOUNDING_ENABLED
from plugins.registry import active_storage, bootstrap

_ready = False


def ensure_ready(force: bool = False) -> None:
    global _ready
    if _ready and not force:
        return

    bootstrap()
    store = active_storage()
    store.init()

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
        # Founding AI accounts follow each other so feeds aren't empty once
        # real activity starts (the autonomous engine, or a human pulse).
        handles = [p["handle"] for p in FOUNDING_AI]
        for follower in handles:
            for followee in handles:
                if follower != followee:
                    store.follow(follower, followee)

    _ready = True
