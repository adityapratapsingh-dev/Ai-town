"""Application bootstrap: resolve plugins, init storage, seed defaults.

Called on FastAPI startup (and by tests) so the platform is self-provisioning:
an empty D1 database becomes a working society on first request.
"""

from __future__ import annotations

from core.seed import DEFAULT_CITIZENS, DEFAULT_COLONIES
from plugins.registry import active_storage, bootstrap

_ready = False


def ensure_ready(force: bool = False) -> None:
    global _ready
    if _ready and not force:
        return

    bootstrap()
    store = active_storage()
    store.init()

    for colony in DEFAULT_COLONIES:
        store.create_colony(
            colony["slug"],
            name=colony["name"],
            purpose=colony["purpose"],
            created_by=colony.get("created_by", "system"),
        )

    for citizen in DEFAULT_CITIZENS:
        store.upsert_citizen(
            citizen["name"],
            kind=citizen.get("kind", "native"),
            role=citizen.get("role", ""),
            personality=citizen.get("personality", ""),
            avatar_url=citizen.get("avatar_url", ""),
            status="active",
        )

    # Every founding citizen joins the genesis colony.
    for citizen in DEFAULT_CITIZENS:
        store.add_member("genesis", citizen["name"])

    _ready = True
