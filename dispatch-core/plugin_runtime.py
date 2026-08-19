"""Runtime discovery for selected Dispatch plugins in the shared Python environment."""
from __future__ import annotations

import getpass
import importlib.metadata
import inspect
import json
import os
import re
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from paths import DispatchPaths

try:  # Keep query-only Core usable when optional browser/auth dependencies are absent.
    from browser_manager import BrowserManager
except ImportError:  # pragma: no cover - exercised by dependency-resolution environments
    BrowserManager = None  # type: ignore[assignment,misc]

try:
    from authentication import AuthenticationManager
except ImportError:  # pragma: no cover - exercised by dependency-resolution environments
    AuthenticationManager = None  # type: ignore[assignment,misc]

ENTRY_POINT_GROUP = "dispatch.plugins"
SERVICE_ENTRY_POINT_GROUP = "dispatch.services"
CONFIGURATOR_ENTRY_POINT_GROUP = "dispatch.configurators"
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ENVELOPE_KEYS = {"ok", "action", "status", "data", "freshness", "delivery", "error"}
_MAX_INTERACTIVE_TEXT = 4096
_MAX_PLUGIN_STDOUT = 64 * 1024


class PluginRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _BoundedPluginStdout:
    """Discard direct plugin stdout while bounding accidental write volume."""

    def __init__(self) -> None:
        self.size = 0

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("plugin stdout must be text")
        self.size += len(value.encode("utf-8", errors="replace"))
        if self.size > _MAX_PLUGIN_STDOUT:
            raise PluginRuntimeError("plugin_output_invalid", "plugin wrote too much direct stdout")
        return len(value)

    def flush(self) -> None:
        return None


def _default_prompt(message: str) -> str:
    print(message, end="", file=sys.stderr, flush=True)
    return input()


def _default_secret_prompt(message: str) -> str:
    return getpass.getpass(message, stream=sys.stderr)


def _default_output(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _accepts_context(handler: Callable[..., Any]) -> bool:
    try:
        inspect.signature(handler).bind(object())
    except (TypeError, ValueError):
        return False
    return True


def _contains_secret(value: Any, secrets: tuple[str, ...]) -> bool:
    if not secrets:
        return False
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(
            _contains_secret(key, secrets) or _contains_secret(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item, secrets) for item in value)
    return False


def _interactive_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > _MAX_INTERACTIVE_TEXT
        or any(ord(character) < 32 and character != "\t" for character in value)
        or "\x7f" in value
    ):
        raise PluginRuntimeError("plugin_configuration_input_invalid", f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    id: str
    distribution: str
    version: str
    handle: Callable[[dict[str, Any]], dict[str, Any]]

    def safe_data(self) -> dict[str, str]:
        return {"id": self.id, "distribution": self.distribution, "version": self.version}


def _bounded_stop_callback(callback: Callable[[], bool]) -> Callable[[], bool]:
    """Wrap a plugin stop callback so malformed callbacks fail closed."""
    if not callable(callback):
        raise PluginRuntimeError("plugin_context_invalid", "plugin stop callback is not callable")

    def stopped() -> bool:
        try:
            value = callback()
        except Exception:
            return True
        return value if type(value) is bool else True

    return stopped


@dataclass(slots=True)
class PluginServiceContext:
    """Core-owned capabilities passed to a long-running service entry point.

    ``acquire_browser_manager`` and ``acquire_authentication_manager`` are the
    only Core factories a service needs.  They lazily create the managers in
    the Core process using the validated ``DispatchPaths``; browser sessions
    and credentials therefore never cross a process or JSON boundary.
    """

    paths: DispatchPaths
    stop_requested: Callable[[], bool]
    plugin_id: str = ""
    _browser_manager: Any = field(default=None, init=False, repr=False)
    _authentication_manager: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.paths, DispatchPaths):
            raise PluginRuntimeError("plugin_context_invalid", "plugin service paths are invalid")
        self.stop_requested = _bounded_stop_callback(self.stop_requested)

    def should_stop(self) -> bool:
        """Return whether Core has requested an orderly service shutdown."""
        return self.stop_requested()

    def acquire_browser_manager(self) -> Any:
        """Lazily create and return the Core-owned BrowserManager."""
        if self._browser_manager is None:
            if BrowserManager is None:
                raise PluginRuntimeError("browser_dependency_missing", "Browser Manager is not installed")
            try:
                self._browser_manager = BrowserManager(self.paths)
            except Exception as exc:
                raise PluginRuntimeError("browser_manager_unavailable", "Browser Manager could not be acquired") from exc
        return self._browser_manager

    @property
    def browser_manager(self) -> Any:
        """Compatibility property for service callables preferring attribute access."""
        return self.acquire_browser_manager()

    def acquire_authentication_manager(self) -> Any:
        """Lazily create and return the Core-owned AuthenticationManager."""
        if self._authentication_manager is None:
            if AuthenticationManager is None:
                raise PluginRuntimeError("authentication_dependency_missing", "Authentication Manager is not installed")
            try:
                self._authentication_manager = AuthenticationManager(self.paths)
            except Exception as exc:
                raise PluginRuntimeError(
                    "authentication_manager_unavailable",
                    "Authentication Manager could not be acquired",
                ) from exc
        return self._authentication_manager

    @property
    def authentication_manager(self) -> Any:
        """Compatibility property for service callables preferring attribute access."""
        return self.acquire_authentication_manager()

    def close(self) -> None:
        """Release any browser manager acquired by the service."""
        if self._browser_manager is not None:
            try:
                self._browser_manager.shutdown()
            except BaseException as exc:
                raise PluginRuntimeError("browser_manager_shutdown_failed", "Browser Manager shutdown failed") from exc


@dataclass(slots=True)
class PluginConfiguratorContext:
    """Core-owned context passed to an interactive configurator entry point."""

    paths: DispatchPaths
    stop_requested: Callable[[], bool]
    plugin_id: str = ""
    prompt_fn: Callable[[str], str] = field(default=_default_prompt, repr=False)
    secret_prompt_fn: Callable[[str], str] = field(default=_default_secret_prompt, repr=False)
    output_fn: Callable[[str], None] = field(default=_default_output, repr=False)
    _secret_values: list[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.paths, DispatchPaths):
            raise PluginRuntimeError("plugin_context_invalid", "plugin configurator paths are invalid")
        self.stop_requested = _bounded_stop_callback(self.stop_requested)
        if not all(callable(value) for value in (self.prompt_fn, self.secret_prompt_fn, self.output_fn)):
            raise PluginRuntimeError("plugin_context_invalid", "plugin configurator I/O is invalid")

    def should_stop(self) -> bool:
        return self.stop_requested()

    def prompt(self, message: str) -> str:
        prompt = _interactive_text(message, "prompt")
        return _interactive_text(self.prompt_fn(prompt), "configuration value", allow_empty=True)

    def prompt_secret(self, message: str) -> str:
        prompt = _interactive_text(message, "secret prompt")
        value = _interactive_text(self.secret_prompt_fn(prompt), "secret configuration value", allow_empty=True)
        if value:
            self._secret_values.append(value)
        return value

    def output(self, message: str) -> None:
        value = _interactive_text(message, "configuration output", allow_empty=True)
        if _contains_secret(value, tuple(self._secret_values)):
            raise PluginRuntimeError(
                "plugin_configuration_secret_exposure",
                "plugin configurator attempted to expose a secret",
            )
        self.output_fn(value)


@dataclass(frozen=True, slots=True)
class DiscoveredPluginService:
    id: str
    distribution: str
    version: str
    handle: Callable[[PluginServiceContext], Any]

    @property
    def run(self) -> Callable[[PluginServiceContext], Any]:
        return self.handle

    def safe_data(self) -> dict[str, str]:
        return {"id": self.id, "distribution": self.distribution, "version": self.version}


@dataclass(frozen=True, slots=True)
class DiscoveredPluginConfigurator:
    id: str
    distribution: str
    version: str
    handle: Callable[[PluginConfiguratorContext], Any]

    @property
    def configure(self) -> Callable[[PluginConfiguratorContext], Any]:
        return self.handle

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


def _environment_entry_points(group: str = ENTRY_POINT_GROUP) -> list[importlib.metadata.EntryPoint]:
    try:
        try:
            selected = importlib.metadata.entry_points(group=group)
        except TypeError:  # pragma: no cover - compatibility with older Python metadata APIs
            available = importlib.metadata.entry_points()
            if hasattr(available, "select"):
                selected = available.select(group=group)
            else:
                selected = [item for item in available if getattr(item, "group", None) == group]
    except (OSError, ValueError, AttributeError) as exc:
        raise PluginRuntimeError("plugin_discovery_failed", "shared plugin metadata could not be read") from exc
    # Test doubles and older metadata APIs may ignore the group argument.  Do
    # not let a plugin entry point satisfy a service/configurator lookup.
    return [
        item
        for item in cast(list[importlib.metadata.EntryPoint], selected)
        if getattr(item, "group", group) == group
    ]


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
        except BaseException as exc:
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


def _entry_point_candidates(
    entry_points: list[importlib.metadata.EntryPoint],
    plugin_id: str,
    group: str,
) -> list[importlib.metadata.EntryPoint]:
    return [
        entry_point
        for entry_point in entry_points
        if getattr(entry_point, "group", None) == group and entry_point.name == plugin_id
    ]


def _load_entry_point(
    plugin_id: str,
    group: str,
    *,
    role: str,
) -> tuple[Callable[..., Any], str, str]:
    if _PLUGIN_ID.fullmatch(plugin_id) is None:
        raise PluginRuntimeError("plugin_id_invalid", "plugin id is invalid")
    if plugin_id not in _configured_ids():
        raise PluginRuntimeError("plugin_not_active", f"plugin {plugin_id} is not active")
    candidates = _entry_point_candidates(_environment_entry_points(group), plugin_id, group)
    if len(candidates) != 1:
        raise PluginRuntimeError(
            "plugin_entry_point_invalid",
            f"active plugin {plugin_id} must publish exactly one {group} entry point",
        )
    entry_point = candidates[0]
    try:
        handler = entry_point.load()
    except BaseException as exc:
        raise PluginRuntimeError("plugin_load_failed", f"active plugin {plugin_id} {role} could not be loaded") from exc
    if not callable(handler) or not _accepts_context(handler):
        raise PluginRuntimeError(
            "plugin_entry_point_invalid",
            f"active plugin {plugin_id} {role} must accept one context argument",
        )
    distribution, version = _distribution_details(entry_point)
    return cast(Callable[..., Any], handler), distribution, version


def _discover_context_entry_points(
    group: str,
    *,
    role: str,
    descriptor_type: type[DiscoveredPluginService] | type[DiscoveredPluginConfigurator],
) -> list[Any]:
    """List installed context entry points without requiring every plugin to provide one."""
    discovered: list[Any] = []
    active_ids = _configured_ids()
    if not active_ids:
        return discovered
    entry_points = _environment_entry_points(group)
    for plugin_id in active_ids:
        candidates = _entry_point_candidates(entry_points, plugin_id, group)
        if not candidates:
            continue
        if len(candidates) != 1:
            raise PluginRuntimeError(
                "plugin_entry_point_invalid",
                f"active plugin {plugin_id} must publish exactly one {group} entry point",
            )
        entry_point = candidates[0]
        try:
            handler = entry_point.load()
        except BaseException as exc:
            raise PluginRuntimeError("plugin_load_failed", f"active plugin {plugin_id} {role} could not be loaded") from exc
        if not callable(handler) or not _accepts_context(handler):
            raise PluginRuntimeError(
                "plugin_entry_point_invalid",
                f"active plugin {plugin_id} {role} must accept one context argument",
            )
        distribution, version = _distribution_details(entry_point)
        if descriptor_type is DiscoveredPluginService:
            discovered.append(  # type: ignore[arg-type]
                DiscoveredPluginService(plugin_id, distribution, version, cast(Callable[[PluginServiceContext], Any], handler))
            )
        else:
            discovered.append(  # type: ignore[arg-type]
                DiscoveredPluginConfigurator(
                    plugin_id,
                    distribution,
                    version,
                    cast(Callable[[PluginConfiguratorContext], Any], handler),
                )
            )
    return discovered


def discover_services() -> list[DiscoveredPluginService]:
    return cast(
        list[DiscoveredPluginService],
        _discover_context_entry_points(
            SERVICE_ENTRY_POINT_GROUP,
            role="service",
            descriptor_type=DiscoveredPluginService,
        ),
    )


def discover_configurators() -> list[DiscoveredPluginConfigurator]:
    return cast(
        list[DiscoveredPluginConfigurator],
        _discover_context_entry_points(
            CONFIGURATOR_ENTRY_POINT_GROUP,
            role="configurator",
            descriptor_type=DiscoveredPluginConfigurator,
        ),
    )


def discover_service(plugin_id: str) -> DiscoveredPluginService:
    handler, distribution, version = _load_entry_point(
        plugin_id,
        SERVICE_ENTRY_POINT_GROUP,
        role="service",
    )
    return DiscoveredPluginService(plugin_id, distribution, version, cast(Callable[[PluginServiceContext], Any], handler))


def discover_configurator(plugin_id: str) -> DiscoveredPluginConfigurator:
    handler, distribution, version = _load_entry_point(
        plugin_id,
        CONFIGURATOR_ENTRY_POINT_GROUP,
        role="configurator",
    )
    return DiscoveredPluginConfigurator(
        plugin_id,
        distribution,
        version,
        cast(Callable[[PluginConfiguratorContext], Any], handler),
    )


def invoke_service(
    plugin_id: str,
    context: PluginServiceContext | None = None,
    *,
    paths: DispatchPaths | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
) -> Any:
    """Run exactly one active service entry point in the current Core process."""
    service = discover_service(plugin_id)
    service_context = context or PluginServiceContext(
        paths or DispatchPaths.from_environment(),
        stop_requested,
        plugin_id,
    )
    if not isinstance(service_context, PluginServiceContext):
        raise PluginRuntimeError("plugin_context_invalid", "plugin service context is invalid")
    if not service_context.plugin_id:
        service_context.plugin_id = plugin_id
    failure: PluginRuntimeError | None = None
    result: Any = None
    try:
        with redirect_stdout(_BoundedPluginStdout()):
            result = service.handle(service_context)
    except (KeyboardInterrupt, EOFError) as exc:
        failure = PluginRuntimeError("plugin_service_interrupted", "plugin service was interrupted")
        failure.__cause__ = exc
    except BaseException as exc:
        failure = PluginRuntimeError("plugin_service_failed", "plugin service failed")
        failure.__cause__ = exc
    try:
        service_context.close()
    except PluginRuntimeError as cleanup_error:
        if failure is not None:
            raise PluginRuntimeError(
                "plugin_service_cleanup_failed",
                "plugin service failed and Browser Manager cleanup also failed",
            ) from cleanup_error
        raise
    if failure is not None:
        raise failure
    return result


def invoke_configurator(
    plugin_id: str,
    context: PluginConfiguratorContext | None = None,
    *,
    paths: DispatchPaths | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    prompt_fn: Callable[[str], str] = _default_prompt,
    secret_prompt_fn: Callable[[str], str] = _default_secret_prompt,
    output_fn: Callable[[str], None] = _default_output,
) -> Any:
    """Run exactly one active configurator with bounded interactive I/O."""
    configurator = discover_configurator(plugin_id)
    configurator_context = context or PluginConfiguratorContext(
        paths or DispatchPaths.from_environment(),
        stop_requested,
        plugin_id,
        prompt_fn,
        secret_prompt_fn,
        output_fn,
    )
    if not isinstance(configurator_context, PluginConfiguratorContext):
        raise PluginRuntimeError("plugin_context_invalid", "plugin configurator context is invalid")
    if not configurator_context.plugin_id:
        configurator_context.plugin_id = plugin_id
    try:
        with redirect_stdout(_BoundedPluginStdout()):
            result = configurator.handle(configurator_context)
    except (KeyboardInterrupt, EOFError) as exc:
        raise PluginRuntimeError("plugin_configuration_cancelled", "plugin configuration was cancelled") from exc
    except BaseException as exc:
        raise PluginRuntimeError("plugin_configuration_failed", "plugin configuration failed") from exc
    _json_bytes(
        result,
        limit=64 * 1024,
        message=f"plugin {plugin_id} configurator result is not bounded JSON",
        code="plugin_configuration_result_invalid",
    )
    if _contains_secret(result, tuple(configurator_context._secret_values)):
        raise PluginRuntimeError(
            "plugin_configuration_secret_exposure",
            "plugin configurator attempted to expose a secret",
        )
    return _validate_response(plugin_id, result)


def list_services() -> list[dict[str, str]]:
    return [service.safe_data() for service in discover_services()]


def list_configurators() -> list[dict[str, str]]:
    return [configurator.safe_data() for configurator in discover_configurators()]


# Explicit aliases keep the API readable at call sites while retaining one
# implementation and one discovery boundary for each entry-point kind.
invoke_plugin_service = invoke_service
run_plugin_service = invoke_service
serve_plugin = invoke_service
invoke_plugin_configurator = invoke_configurator
run_plugin_configurator = invoke_configurator
configure_plugin = invoke_configurator


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
        with redirect_stdout(_BoundedPluginStdout()):
            result = plugin.handle(request)
    except BaseException as exc:
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
    "SERVICE_ENTRY_POINT_GROUP",
    "CONFIGURATOR_ENTRY_POINT_GROUP",
    "DiscoveredPlugin",
    "DiscoveredPluginService",
    "DiscoveredPluginConfigurator",
    "PluginServiceContext",
    "PluginConfiguratorContext",
    "PluginRuntimeError",
    "discover_plugins",
    "discover_services",
    "discover_configurators",
    "discover_service",
    "discover_configurator",
    "invoke_plugin",
    "invoke_service",
    "invoke_configurator",
    "invoke_plugin_service",
    "invoke_plugin_configurator",
    "run_plugin_service",
    "run_plugin_configurator",
    "serve_plugin",
    "configure_plugin",
    "list_plugins",
    "list_services",
    "list_configurators",
    "plugin_health",
]
