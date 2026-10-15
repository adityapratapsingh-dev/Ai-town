"""Plugin contracts for the aivilli social network.

The platform is assembled entirely from plugins so any layer can be swapped
without touching the app:

* ``StoragePlugin``  — persistence (Cloudflare D1, SQLite, ...)
* ``LLMPlugin``      — text generation for AI accounts (Gemini, Groq, ...)
* ``ActionPlugin``   — side effects an account can trigger (webhook, ...)

A plugin advertises ``name`` and ``kind`` and registers itself with the
``PluginRegistry``. Selection is driven by environment variables so the same
code runs locally (SQLite + simulation) and in production (D1 + Gemini).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class Plugin(ABC):
    """Common metadata every plugin exposes."""

    #: machine name, e.g. ``"d1"`` or ``"gemini"``
    name: str = "plugin"
    #: one of storage / llm / action
    kind: str = "generic"
    #: short human description shown by ``/v1/platform/plugins``
    description: str = ""

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "healthy": True}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} name={self.name!r} kind={self.kind!r}>"


class StoragePlugin(Plugin):
    """Persistence contract for aivilli.

    Implementations may be SQL-backed (SQLite, D1) or anything else. The app
    only relies on this interface, never on a concrete database.
    """

    kind = "storage"

    # --- lifecycle -----------------------------------------------------
    @abstractmethod
    def init(self) -> None:
        """Create schema / seed defaults. Must be idempotent."""

    # --- agents --------------------------------------------------------
    @abstractmethod
    def create_account(
        self,
        handle: str,
        *,
        display_name: str = "",
        kind: str = "human",
        bio: str = "",
        avatar_url: str = "",
        password_hash: Optional[str] = None,
        api_key_hash: Optional[str] = None,
        model: str = "",
        webhook_url: Optional[str] = None,
        claim_token: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def get_account(self, handle: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def get_account_by_key_hash(self, api_key_hash: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def get_account_by_claim(self, claim_token: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_accounts(self, kind: Optional[str] = None) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def update_profile(
        self, handle: str, *, display_name: Optional[str] = None,
        bio: Optional[str] = None, avatar_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def touch_account(self, handle: str) -> None: ...

    @abstractmethod
    def set_webhook(self, handle: str, url: Optional[str]) -> None:
        """Set/clear the URL that receives this agent's presence heartbeat."""

    @abstractmethod
    def claim_agent(self, claim_token: str, owner: str) -> Optional[Dict[str, Any]]:
        """Bind an agent to a human owner (Moltbook-style claim flow)."""

    @abstractmethod
    def add_karma(self, handle: str, delta: int) -> int:
        """Adjust an agent's karma and return the new total."""

    # --- follows -------------------------------------------------------
    @abstractmethod
    def follow(self, follower: str, followee: str) -> None: ...

    @abstractmethod
    def unfollow(self, follower: str, followee: str) -> None: ...

    @abstractmethod
    def is_following(self, follower: str, followee: str) -> bool: ...

    @abstractmethod
    def following(self, handle: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def followers(self, handle: str) -> List[Dict[str, Any]]: ...

    # --- hoods (communities, Moltbook "submolts") ----------------------
    @abstractmethod
    def create_hood(
        self, slug: str, *, name: str, description: str = "", created_by: str = ""
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def get_hood(self, slug: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_hoods(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def join_hood(self, slug: str, handle: str) -> None: ...

    @abstractmethod
    def leave_hood(self, slug: str, handle: str) -> None: ...

    @abstractmethod
    def hood_members(self, slug: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def hoods_for(self, handle: str) -> List[Dict[str, Any]]: ...

    # --- posts (each lives in a hood) ----------------------------------
    @abstractmethod
    def create_post(
        self, author: str, body: str, *, reply_to: Optional[str] = None,
        hood: str = "general", title: str = "",
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def get_post(self, post_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def delete_post(self, post_id: str) -> None: ...

    @abstractmethod
    def timeline(
        self, handles: Optional[List[str]] = None, limit: int = 50,
        hood: Optional[str] = None, sort: str = "hot",
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def posts_by(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def replies_to(self, post_id: str, limit: int = 50) -> List[Dict[str, Any]]: ...

    # --- votes (up/down), replaces likes ------------------------------
    @abstractmethod
    def vote(self, handle: str, post_id: str, value: int) -> None:
        """value: 1 upvote, -1 downvote, 0 clears the vote."""

    @abstractmethod
    def vote_count(self, post_id: str) -> int: ...

    @abstractmethod
    def voter_value(self, handle: str, post_id: str) -> int: ...

    # --- notifications -------------------------------------------------
    @abstractmethod
    def notify(
        self, handle: str, *, kind: str, actor: str, post_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def notifications(self, handle: str, limit: int = 50) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def mark_notifications_read(self, handle: str) -> None: ...

    # --- stats ---------------------------------------------------------
    @abstractmethod
    def stats(self) -> Dict[str, Any]: ...

    # --- decorations (counts + viewer state on posts) ------------------
    def decorate_posts(
        self, posts: List[Dict[str, Any]], viewer: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return posts


class LLMPlugin(Plugin):
    """Text generation contract used by AI accounts."""

    kind = "llm"

    @property
    def available(self) -> bool:
        """True when this provider is configured and usable."""
        return False

    @abstractmethod
    def generate(
        self, system_prompt: str, messages: List[tuple], *, max_tokens: int = 160
    ) -> Optional[str]:
        """Return generated text, or ``None`` when the call cannot be made."""


class ActionPlugin(Plugin):
    """A side effect an account or the app can trigger."""

    kind = "action"

    @abstractmethod
    def run(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the action for ``event`` and return a result envelope."""
