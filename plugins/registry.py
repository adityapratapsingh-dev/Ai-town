"""Plugin registry and selection.

Plugins register themselves here; the platform resolves the active storage and
LLM implementations from environment variables (with sane local defaults).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Type

from plugins.base import ActionPlugin, LLMPlugin, Plugin, StoragePlugin


class PluginRegistry:
    def __init__(self) -> None:
        self._by_kind: Dict[str, Dict[str, Plugin]] = {
            "storage": {},
            "llm": {},
            "action": {},
        }

    def register(self, plugin: Plugin) -> Plugin:
        self._by_kind.setdefault(plugin.kind, {})[plugin.name] = plugin
        return plugin

    def register_class(self, cls: Type[Plugin]) -> Plugin:
        return self.register(cls())

    def get(self, kind: str, name: str) -> Optional[Plugin]:
        return self._by_kind.get(kind, {}).get(name)

    def of_kind(self, kind: str) -> List[Plugin]:
        return list(self._by_kind.get(kind, {}).values())

    def describe(self) -> List[Dict[str, object]]:
        out: List[Dict[str, object]] = []
        for kind in ("storage", "llm", "action"):
            for plugin in self.of_kind(kind):
                info = plugin.health()
                info["description"] = plugin.description
                out.append(info)
        return out


registry = PluginRegistry()


# --------------------------------------------------------------------------
# Active-plugin resolution
# --------------------------------------------------------------------------
def _build_storage(name: str) -> StoragePlugin:
    from plugins.storage.d1 import D1StoragePlugin
    from plugins.storage.sqlite import SQLiteStoragePlugin

    if name == "d1":
        return D1StoragePlugin()
    return SQLiteStoragePlugin()


def _build_llm_plugins() -> List[LLMPlugin]:
    from plugins.llm.gemini import GeminiLLMPlugin
    from plugins.llm.groq import GroqLLMPlugin
    from plugins.llm.simulation import SimulationLLMPlugin

    return [GeminiLLMPlugin(), GroqLLMPlugin(), SimulationLLMPlugin()]


def _build_action_plugins() -> List[ActionPlugin]:
    from plugins.action.webhook import WebhookActionPlugin

    return [WebhookActionPlugin()]


def bootstrap() -> PluginRegistry:
    """Register every known plugin and mark the active ones.

    Selection rules (all overridable by env):
      * ``STORAGE_BACKEND``  -> ``d1`` when Cloudflare creds are present, else
        ``sqlite`` (``AIVILLI_DB``, default ``/tmp/aivilli.db``).
    """
    storage_name = os.environ.get("STORAGE_BACKEND")
    if not storage_name:
        storage_name = (
            "d1"
            if os.environ.get("CLOUDFLARE_API_TOKEN")
            and os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            and os.environ.get("CLOUDFLARE_D1_DATABASE_ID")
            else "sqlite"
        )

    storage = _build_storage(storage_name)
    registry.register(storage)

    # Register the alternative backend too so it shows up in /plugins.
    if storage_name == "sqlite":
        registry.register(_build_storage("d1"))
    else:
        registry.register(_build_storage("sqlite"))

    for plugin in _build_llm_plugins():
        registry.register(plugin)
    for plugin in _build_action_plugins():
        registry.register(plugin)

    os.environ["_AI_ACTIVE_STORAGE"] = storage.name
    return registry


# Records why the preferred storage was abandoned, surfaced via /health.
_storage_error: Optional[str] = None


def storage_error() -> Optional[str]:
    return _storage_error


def active_storage() -> StoragePlugin:
    name = os.environ.get("_AI_ACTIVE_STORAGE", "sqlite")
    plugin = registry.get("storage", name)
    if plugin is None:  # pragma: no cover - defensive
        plugin = registry.get("storage", "sqlite")
    assert plugin is not None
    return plugin  # type: ignore[return-value]


def init_storage_or_fallback() -> StoragePlugin:
    """Bring up the active storage, falling back to SQLite if it can't start.

    Serverless cold starts can't afford a hard dependency on a remote database:
    if D1 is unreachable the app must still serve, so we downgrade to the local
    SQLite file and record the reason.

    IMPORTANT: on serverless the local file lives in ``/tmp``, which is
    *ephemeral and per-instance* — writes made against it are lost on the next
    cold start and are invisible to other instances. That is exactly how
    "accounts register but disappear" happens, so a fallback is flagged as
    **non-durable** and surfaced everywhere (`/health`, `/v1/stats`, the UI)
    rather than being allowed to fail silently.
    """
    global _storage_error
    store = active_storage()
    try:
        store.init()
        _storage_error = None
        return store
    except Exception as exc:  # network/permission/schema failure
        _storage_error = f"{store.name}: {type(exc).__name__}: {exc}"[:300]
        fallback = registry.get("storage", "sqlite")
        if fallback is not None and store.name != "sqlite":
            fallback.init()
            os.environ["_AI_ACTIVE_STORAGE"] = "sqlite"
            return fallback  # type: ignore[return-value]
        raise


def is_durable() -> bool:
    """True when the active storage persists across instances/restarts.

    SQLite is durable only off-serverless; on Vercel it lives in /tmp and is
    ephemeral, so writes silently vanish. Used to warn the user.
    """
    store = active_storage()
    if store.name != "sqlite":
        return True
    return not bool(os.environ.get("VERCEL"))



def active_llm() -> LLMPlugin:
    """First configured LLM plugin, falling back to the simulation provider."""
    for plugin in registry.of_kind("llm"):
        if isinstance(plugin, LLMPlugin) and plugin.available:
            return plugin
    return registry.get("llm", "simulation")  # type: ignore[return-value]


def provider_name() -> str:
    return active_llm().name
