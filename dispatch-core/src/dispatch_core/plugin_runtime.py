"""Minimal runtime discovery for installer-approved Dispatch plugins."""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

ENTRY_POINT_GROUP = "dispatch.plugins"
_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENVELOPE_KEYS = {"ok", "action", "status", "data", "freshness", "delivery", "error"}


class PluginRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    id: str
    distribution: str
    version: str
    handle: Callable[[dict[str, Any]], dict[str, Any]]

    def safe_data(self) -> dict[str, str]:
        return {"id": self.id, "distribution": self.distribution, "version": self.version}


def _configured_plugins() -> list[tuple[str, Path]]:
    ids_value = os.environ.get("DISPATCH_ACTIVE_PLUGINS", "")
    paths_value = os.environ.get("DISPATCH_PLUGIN_PATHS", "")
    if not ids_value and not paths_value:
        return []
    ids = ids_value.split(",") if ids_value else []
    paths = paths_value.split(os.pathsep) if paths_value else []
    if (
        len(ids) != len(paths)
        or len(ids) > 32
        or len(set(ids)) != len(ids)
        or any(_ID.fullmatch(plugin_id) is None for plugin_id in ids)
    ):
        raise PluginRuntimeError("plugin_configuration_invalid", "active plugin configuration is invalid")
    configured: list[tuple[str, Path]] = []
    for plugin_id, value in zip(ids, paths):
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise PluginRuntimeError("plugin_configuration_invalid", "active plugin path is missing or unsafe")
        configured.append((plugin_id, path))
    return configured


def discover_plugins() -> list[DiscoveredPlugin]:
    discovered: list[DiscoveredPlugin] = []
    for plugin_id, path in _configured_plugins():
        candidates: list[tuple[importlib.metadata.Distribution, importlib.metadata.EntryPoint]] = []
        try:
            distributions = list(importlib.metadata.distributions(path=[str(path)]))
        except (OSError, ValueError) as exc:
            raise PluginRuntimeError("plugin_discovery_failed", "active plugin metadata could not be read") from exc
        for distribution in distributions:
            for entry_point in distribution.entry_points:
                if entry_point.group == ENTRY_POINT_GROUP and entry_point.name == plugin_id:
                    candidates.append((distribution, entry_point))
        if len(candidates) != 1:
            raise PluginRuntimeError(
                "plugin_entry_point_invalid",
                f"active plugin {plugin_id} must publish exactly one {ENTRY_POINT_GROUP} entry point",
            )
        distribution, entry_point = candidates[0]
        try:
            handler = entry_point.load()
        except (AttributeError, ImportError, ModuleNotFoundError, TypeError, ValueError) as exc:
            raise PluginRuntimeError("plugin_load_failed", f"active plugin {plugin_id} could not be loaded") from exc
        if not callable(handler):
            raise PluginRuntimeError("plugin_entry_point_invalid", f"active plugin {plugin_id} entry point is not callable")
        discovered.append(
            DiscoveredPlugin(
                id=plugin_id,
                distribution=str(distribution.metadata["Name"] or ""),
                version=str(distribution.version),
                handle=cast(Callable[[dict[str, Any]], dict[str, Any]], handler),
            )
        )
    return discovered


def list_plugins() -> list[dict[str, str]]:
    return [plugin.safe_data() for plugin in discover_plugins()]


def invoke_plugin(plugin_id: str, request: dict[str, Any]) -> dict[str, Any]:
    if _ID.fullmatch(plugin_id) is None:
        raise PluginRuntimeError("plugin_id_invalid", "plugin id is invalid")
    if type(request) is not dict:
        raise PluginRuntimeError("plugin_request_invalid", "plugin request must be a JSON object")
    try:
        encoded_request = json.dumps(request, sort_keys=True).encode()
    except (TypeError, ValueError) as exc:
        raise PluginRuntimeError("plugin_request_invalid", "plugin request is not valid JSON") from exc
    if len(encoded_request) > 64 * 1024:
        raise PluginRuntimeError("plugin_request_invalid", "plugin request exceeds 64 KiB")
    plugin = next((item for item in discover_plugins() if item.id == plugin_id), None)
    if plugin is None:
        raise PluginRuntimeError("plugin_not_active", f"plugin {plugin_id} is not active")
    try:
        result = plugin.handle(request)
    except Exception as exc:
        raise PluginRuntimeError("plugin_invocation_failed", f"plugin {plugin_id} invocation failed") from exc
    if type(result) is not dict or set(result) != _ENVELOPE_KEYS:
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    if type(result.get("ok")) is not bool or not isinstance(result.get("status"), str):
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    try:
        encoded_result = json.dumps(result, sort_keys=True).encode()
    except (TypeError, ValueError) as exc:
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} response is not valid JSON") from exc
    if len(encoded_result) > 1024 * 1024:
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} response exceeds 1 MiB")
    return result


def plugin_health(plugin_ids: list[str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    ready = True
    try:
        discovered = {plugin.id for plugin in discover_plugins()}
        if discovered != set(plugin_ids):
            raise PluginRuntimeError("plugin_selection_mismatch", "discovered plugins differ from setup selection")
        for plugin_id in plugin_ids:
            result = invoke_plugin(plugin_id, {"action": "health"})
            results[plugin_id] = result
            ready = ready and result["ok"] is True and result["status"] == "ready"
    except PluginRuntimeError as exc:
        return {
            "ready": False,
            "plugins": results,
            "error": {"code": exc.code, "message": str(exc)[:256]},
        }
    return {"ready": ready, "plugins": results, "error": None}


__all__ = [
    "ENTRY_POINT_GROUP",
    "DiscoveredPlugin",
    "PluginRuntimeError",
    "discover_plugins",
    "invoke_plugin",
    "list_plugins",
    "plugin_health",
]
