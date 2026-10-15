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


def active_storage() -> StoragePlugin:
    name = os.environ.get("_AI_ACTIVE_STORAGE", "sqlite")
    plugin = registry.get("storage", name)
    if plugin is None:  # pragma: no cover - defensive
        plugin = registry.get("storage", "sqlite")
    assert plugin is not None
    return plugin  # type: ignore[return-value]


def active_llm() -> LLMPlugin:
    """First configured LLM plugin, falling back to the simulation provider."""
    for plugin in registry.of_kind("llm"):
        if isinstance(plugin, LLMPlugin) and plugin.available:
            return plugin
    return registry.get("llm", "simulation")  # type: ignore[return-value]


def provider_name() -> str:
    return active_llm().name
