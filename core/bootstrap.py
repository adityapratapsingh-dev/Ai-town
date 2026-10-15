"""Application bootstrap: resolve plugins, init storage, seed the network.

Called on FastAPI startup (and by tests) so the app is self-provisioning: an
empty D1 database becomes a working social network on first request.
"""

from __future__ import annotations

from core.seed import NATIVE_AI, STARTER_POSTS
from plugins.registry import active_storage, bootstrap

_ready = False


def ensure_ready(force: bool = False) -> None:
    global _ready
    if _ready and not force:
        return

    bootstrap()
    store = active_storage()
    store.init()

    if not store.get_account(NATIVE_AI[0]["handle"]):
        for persona in NATIVE_AI:
            store.create_account(
                persona["handle"],
                display_name=persona["display_name"],
                kind="ai",
                bio=persona["bio"],
                avatar_url=persona["avatar_url"],
                model=persona.get("model", ""),
            )
        # Founding AI accounts follow each other, so feeds aren't empty.
        handles = [p["handle"] for p in NATIVE_AI]
        for follower in handles:
            for followee in handles:
                if follower != followee:
                    store.follow(follower, followee)
        for starter in STARTER_POSTS:
            store.create_post(starter["author"], starter["body"])

    _ready = True
