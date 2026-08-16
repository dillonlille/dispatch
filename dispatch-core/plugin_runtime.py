"""Runtime discovery for selected Dispatch plugins in the shared Python environment."""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, cast

ENTRY_POINT_GROUP = "dispatch.plugins"
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
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


def _configured_ids() -> list[str]:
    """Return the explicitly selected plugin IDs.

    Plugin source and installation locations are deliberately not configuration
    inputs. Editable installs in the process's shared environment are the only
    discovery source; DISPATCH_ACTIVE_PLUGINS is only a selection filter.
    """
    value = os.environ.get("DISPATCH_ACTIVE_PLUGINS", "")
    if not value:
        return []
    ids = value.split(",")
    if (
        len(ids) > 32
        or len(set(ids)) != len(ids)
        or any(_PLUGIN_ID.fullmatch(plugin_id) is None for plugin_id in ids)
    ):
        raise PluginRuntimeError("plugin_configuration_invalid", "active plugin configuration is invalid")
    return ids


def _environment_entry_points() -> list[importlib.metadata.EntryPoint]:
    try:
        try:
            selected = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except TypeError:  # pragma: no cover - compatibility with older Python metadata APIs
            available = importlib.metadata.entry_points()
            if hasattr(available, "select"):
                selected = available.select(group=ENTRY_POINT_GROUP)
            else:
                selected = [item for item in available if getattr(item, "group", None) == ENTRY_POINT_GROUP]
    except (OSError, ValueError, AttributeError) as exc:
        raise PluginRuntimeError("plugin_discovery_failed", "shared plugin metadata could not be read") from exc
    return list(cast(list[importlib.metadata.EntryPoint], selected))


def _distribution_details(entry_point: importlib.metadata.EntryPoint) -> tuple[str, str]:
    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        raise PluginRuntimeError("plugin_entry_point_invalid", "plugin entry point has no distribution metadata")
    try:
        name = str(distribution.metadata.get("Name") or "")
        version = str(distribution.version)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PluginRuntimeError("plugin_entry_point_invalid", "plugin distribution metadata is invalid") from exc
    if not name or not version:
        raise PluginRuntimeError("plugin_entry_point_invalid", "plugin distribution metadata is incomplete")
    return name, version


def discover_plugins() -> list[DiscoveredPlugin]:
    active_ids = _configured_ids()
    if not active_ids:
        return []

    entry_points = _environment_entry_points()
    discovered: list[DiscoveredPlugin] = []
    for plugin_id in active_ids:
        candidates = [entry_point for entry_point in entry_points if entry_point.name == plugin_id]
        if len(candidates) != 1:
            raise PluginRuntimeError(
                "plugin_entry_point_invalid",
                f"active plugin {plugin_id} must publish exactly one {ENTRY_POINT_GROUP} entry point",
            )
        entry_point = candidates[0]
        try:
            handler = entry_point.load()
        except Exception as exc:
            raise PluginRuntimeError("plugin_load_failed", f"active plugin {plugin_id} could not be loaded") from exc
        if not callable(handler):
            raise PluginRuntimeError("plugin_entry_point_invalid", f"active plugin {plugin_id} entry point is not callable")
        distribution, version = _distribution_details(entry_point)
        discovered.append(
            DiscoveredPlugin(
                id=plugin_id,
                distribution=distribution,
                version=version,
                handle=cast(Callable[[dict[str, Any]], dict[str, Any]], handler),
            )
        )
    return discovered


def list_plugins() -> list[dict[str, str]]:
    return [plugin.safe_data() for plugin in discover_plugins()]


def _json_bytes(value: Any, *, limit: int, message: str, code: str) -> bytes:
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise PluginRuntimeError(code, message) from exc
    if len(encoded) > limit:
        raise PluginRuntimeError(code, message)
    return encoded


def _validate_response(plugin_id: str, result: Any) -> dict[str, Any]:
    if type(result) is not dict or set(result) != _ENVELOPE_KEYS:
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    if (
        type(result.get("ok")) is not bool
        or not isinstance(result.get("action"), str)
        or not result["action"]
        or not isinstance(result.get("status"), str)
        or not result["status"]
        or not isinstance(result.get("data"), dict)
        or (result.get("freshness") is not None and not isinstance(result["freshness"], dict))
        or (result.get("delivery") is not None and not isinstance(result["delivery"], dict))
    ):
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    error = result.get("error")
    if result["ok"]:
        if error is not None:
            raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    elif (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error["code"], str)
        or _ERROR_CODE.fullmatch(error["code"]) is None
        or not isinstance(error["message"], str)
        or not 1 <= len(error["code"]) <= 64
        or not 1 <= len(error["message"]) <= 512
    ):
        raise PluginRuntimeError("plugin_response_invalid", f"plugin {plugin_id} returned an invalid response envelope")
    return result


def invoke_plugin(plugin_id: str, request: dict[str, Any]) -> dict[str, Any]:
    if _PLUGIN_ID.fullmatch(plugin_id) is None:
        raise PluginRuntimeError("plugin_id_invalid", "plugin id is invalid")
    if type(request) is not dict:
        raise PluginRuntimeError("plugin_request_invalid", "plugin request must be a JSON object")
    _json_bytes(request, limit=64 * 1024, message="plugin request is not valid bounded JSON", code="plugin_request_invalid")
    plugin = next((item for item in discover_plugins() if item.id == plugin_id), None)
    if plugin is None:
        raise PluginRuntimeError("plugin_not_active", f"plugin {plugin_id} is not active")
    try:
        result = plugin.handle(request)
    except Exception as exc:
        raise PluginRuntimeError("plugin_invocation_failed", f"plugin {plugin_id} invocation failed") from exc
    _json_bytes(result, limit=1024 * 1024, message=f"plugin {plugin_id} response is not valid bounded JSON", code="plugin_response_invalid")
    return _validate_response(plugin_id, result)


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
