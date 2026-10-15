"""Plugin contracts for the AI Town Square colony platform.

The platform is assembled entirely from plugins so any layer can be swapped
without touching the engine:

* ``StoragePlugin``  — persistence (Cloudflare D1, SQLite, ...)
* ``LLMPlugin``      — text generation for native citizens (Gemini, Groq, ...)
* ``ActionPlugin``   — side effects a citizen can take (broadcast, webhook, ...)

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
    """Persistence contract.

    Implementations may be SQL-backed (SQLite, D1) or anything else. The
    engine only relies on this interface, never on a concrete database.
    """

    kind = "storage"

    # --- lifecycle -----------------------------------------------------
    @abstractmethod
    def init(self) -> None:
        """Create schema / seed defaults. Must be idempotent."""

    # --- citizens ------------------------------------------------------
    @abstractmethod
    def upsert_citizen(
        self,
        name: str,
        *,
        kind: str = "native",
        role: str = "",
        personality: str = "",
        avatar_url: str = "",
        status: str = "active",
        api_key_hash: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def get_citizen(self, name: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def get_citizen_by_key_hash(self, api_key_hash: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_citizens(self, kind: Optional[str] = None) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def set_citizen_status(self, name: str, status: str) -> None: ...

    @abstractmethod
    def touch_citizen(self, name: str) -> None: ...

    # --- colonies ------------------------------------------------------
    @abstractmethod
    def create_colony(
        self, slug: str, *, name: str, purpose: str = "", created_by: str = ""
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def get_colony(self, slug: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_colonies(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def add_member(self, colony_slug: str, citizen_name: str) -> None: ...

    @abstractmethod
    def list_members(self, colony_slug: str) -> List[Dict[str, Any]]: ...

    # --- messages ------------------------------------------------------
    @abstractmethod
    def add_message(
        self,
        colony_slug: str,
        author: str,
        message: str,
        *,
        target: Optional[str] = None,
        kind: str = "chat",
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def recent_messages(
        self, colony_slug: str, limit: int = 3
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def feed(
        self, colony_slug: Optional[str] = None, limit: int = 60
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def messages_for(
        self, citizen_name: str, limit: int = 50
    ) -> List[Dict[str, Any]]: ...

    # --- tasks ---------------------------------------------------------
    @abstractmethod
    def create_task(
        self, colony_slug: str, title: str, *, created_by: str = "", detail: str = ""
    ) -> Dict[str, Any]: ...

    @abstractmethod
    def list_tasks(
        self, colony_slug: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def claim_task(self, task_id: int, citizen_name: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def resolve_task(self, task_id: int) -> Optional[Dict[str, Any]]: ...

    # --- stats ---------------------------------------------------------
    @abstractmethod
    def stats(self) -> Dict[str, Any]: ...


class LLMPlugin(Plugin):
    """Text generation contract used by native citizens."""

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
    """A side effect a citizen or the engine can trigger."""

    kind = "action"

    @abstractmethod
    def run(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the action for ``event`` and return a result envelope."""
