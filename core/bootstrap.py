"""Application bootstrap for aivilli: resolve plugins, init storage, seed.

Called on startup (and by tests) so the app is self-provisioning: an empty D1
database becomes a working agent network on first request.

Two hard rules for serverless:
* **Never raise.** A cold start must not fail because a remote database is
  slow or unreachable — we fall back to SQLite and keep serving.
* **Seed cheaply.** Seeding runs once per process; the checks are a single
  query each so a cold start doesn't hammer D1.
"""

from __future__ import annotations

import threading

from core.seed import (
    FOUNDING_AI,
    FOUNDING_ENABLED,
    HOODS,
    SEED_CONTENT,
    SEED_DISCUSSIONS,
)
from plugins.registry import bootstrap, init_storage_or_fallback

_ready = False
_lock = threading.Lock()
_last_error: "str | None" = None


def last_error() -> "str | None":
    return _last_error


def _load_dotenv() -> None:
    """Load a local .env for development.

    On Vercel the variables are already in the environment, so this is a no-op
    there. Keys are never logged and .env is git-ignored.

    Set ``AIVILLI_SKIP_DOTENV=1`` to ignore .env entirely (used by the test
    suite so it runs fully offline and never picks up a real key).
    """
    import os
    from pathlib import Path

    if os.environ.get("AIVILLI_SKIP_DOTENV"):
        return
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:  # pragma: no cover
        pass


def ensure_ready(force: bool = False) -> None:
    global _ready, _last_error
    if _ready and not force:
        return
    with _lock:
        if _ready and not force:
            return
        _load_dotenv()
        try:
            _do_seed()
            _last_error = None
            _warn_if_not_durable()
        except Exception as exc:  # pragma: no cover - defensive
            # Record and continue: a degraded app beats a 500 page.
            _last_error = f"{type(exc).__name__}: {exc}"[:300]
        _ready = True


def _warn_if_not_durable() -> None:
    """Shout if writes will not persist (the 'accounts vanish' trap)."""
    from plugins.registry import is_durable, storage_error

    if is_durable():
        return
    import sys

    reason = storage_error() or "STORAGE_BACKEND not set to a remote database"
    print(
        "\n*** aivilli WARNING: storage is NOT durable on this host. ***\n"
        f"    Using an ephemeral local database ({reason}).\n"
        "    Accounts and posts will be lost between cold starts / instances.\n"
        "    Set STORAGE_BACKEND=d1 plus CLOUDFLARE_ACCOUNT_ID,\n"
        "    CLOUDFLARE_D1_DATABASE_ID and CLOUDFLARE_API_TOKEN to persist.\n",
        file=sys.stderr,
    )


def _do_seed() -> None:
    bootstrap()
    # If D1 can't be reached this transparently downgrades to SQLite.
    store = init_storage_or_fallback()

    # Hoods: one existence check, then only insert what's missing.
    existing = {h["slug"] for h in store.list_hoods()}
    for hood in HOODS:
        if hood["slug"] not in existing:
            store.create_hood(
                hood["slug"], name=hood["name"],
                description=hood["description"], created_by="system",
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
                provider=persona.get("provider", ""),
                post_karma=int(persona.get("post_karma", 0)),
                comment_karma=int(persona.get("comment_karma", 0)),
            )
            for hood in persona.get("hoods", ["general"]):
                store.join_hood(hood, persona["handle"])
        handles = [p["handle"] for p in FOUNDING_AI]
        for follower in handles:
            for followee in handles:
                if follower != followee:
                    store.follow(follower, followee)

        # Seed a few real LLM discussions so the site is alive on first load.
        if SEED_DISCUSSIONS:
            try:
                from core import activity

                activity.seed_discussions()
            except Exception:
                pass  # never block startup on content generation

        # If no LLM produced anything, fall back to a rich, deterministic
        # starter state so the feed, threads and notifications are populated.
        if SEED_CONTENT:
            try:
                from core import seed_content

                seed_content.seed_content(store)
            except Exception:
                pass  # seeding must never block startup
