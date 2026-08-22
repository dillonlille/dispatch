"""Read-only installed health, verification, and path responses."""
from __future__ import annotations

import importlib
import json
import os
import stat
from typing import Any

from paths import DispatchPaths, PathConfigError
from plugin_runtime import (
    PluginRuntimeError,
    discover_collector_registrations,
    plugin_health,
)

PLANES = (
    "registration",
    "runtime_integrity",
    "configuration",
    "query",
    "data",
    "freshness",
    "collector",
    "authentication",
    "browser",
    "service",
    "delivery",
    "overall",
)

_MAX_JSON_DEPTH = 64


def _json_depth_exceeds_limit(value: Any, *, max_depth: int = _MAX_JSON_DEPTH) -> bool:
    """Iteratively check JSON nesting depth.

    ``json.loads`` only raises ``RecursionError`` for deeply nested input on
    some CPython versions, so the depth bound must not rely on interpreter
    behavior. Returns True when the structure nests deeper than ``max_depth``.
    """

    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            return True
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return False


def _load_json_bounded(payload: str) -> Any:
    try:
        parsed = json.loads(payload)
    except RecursionError:
        # Some CPython versions fail during decoding before the explicit
        # depth check can run; treat it the same as exceeding the limit.
        raise ValueError("json nesting depth exceeds limit") from None
    if _json_depth_exceeds_limit(parsed):
        raise ValueError("json nesting depth exceeds limit")
    return parsed


def _setup_state(paths: DispatchPaths) -> dict[str, Any]:
    path = paths.config / "plugins.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"complete": False, "selected_plugins": [], "capabilities": []}
    except OSError:
        return {"complete": False, "selected_plugins": [], "capabilities": [], "invalid": True}
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 64 * 1024
        ):
            return {"complete": False, "selected_plugins": [], "capabilities": [], "invalid": True}
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            payload = _load_json_bounded(stream.read())
    except (OSError, UnicodeError, ValueError):
        return {"complete": False, "selected_plugins": [], "capabilities": [], "invalid": True}
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("status") != "complete"
        or not isinstance(payload.get("selected_plugins"), list)
        or not isinstance(payload.get("plugins"), list)
        or any(
            not isinstance(plugin, dict)
            or not isinstance(plugin.get("id"), str)
            or not isinstance(plugin.get("capabilities"), list)
            or not isinstance(plugin.get("required_profiles", []), list)
            or any(
                not isinstance(item, dict)
                or set(item) != {"provider"}
                or not isinstance(item.get("provider"), str)
                for item in plugin.get("required_profiles", [])
            )
            for plugin in payload["plugins"]
        )
    ):
        return {"complete": False, "selected_plugins": [], "capabilities": [], "invalid": True}
    capabilities = sorted(
        {value for plugin in payload["plugins"] for value in plugin["capabilities"] if isinstance(value, str)}
    )
    authentication_requirements = [
        {"plugin": plugin["id"], "provider": item["provider"]}
        for plugin in payload["plugins"]
        for item in plugin.get("required_profiles", [])
    ]
    return {
        "complete": True,
        "selected_plugins": payload["selected_plugins"],
        "capabilities": capabilities,
        "authentication_requirements": authentication_requirements,
    }


def envelope(
    *,
    ok: bool,
    action: str,
    status: str,
    data: dict[str, Any],
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "action": action,
        "status": status,
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": error,
    }


def resolved(action: str, owner: str | None = None) -> dict[str, Any]:
    try:
        paths = DispatchPaths.from_environment()
    except PathConfigError as exc:
        planes = {name: "not_applicable" for name in PLANES}
        planes.update(
            {
                "registration": "ready",
                "runtime_integrity": "ready",
                "configuration": "unavailable",
                "overall": "degraded",
            }
        )
        return envelope(
            ok=False,
            action=action,
            status="degraded",
            data={
                "installed": True,
                "configured": False,
                "ready": False,
                "operational": False,
                "planes": planes,
            },
            error={"code": "invalid_path_configuration", "message": str(exc)[:256]},
        )

    if action == "paths":
        values = paths.owner_environment(owner) if owner else paths.as_environment()
        return envelope(
            ok=True,
            action=action,
            status="ready",
            data={
                "installed": True,
                "configured": True,
                "ready": True,
                "operational": False,
                "paths": values,
            },
        )

    if action in {"browser-doctor", "health", "verify"}:
        from browser_manager import RealmRegistry
        from browser_manager.runtime_authority import BrowserRuntimeAuthority
        from collection_manager import (
            CollectionManager,
            CollectionManagerError,
            CollectionStoreError,
            CollectionTaskStore,
        )

        inspection = BrowserRuntimeAuthority.production().inspect(full_tree=True)
        setup = _setup_state(paths)
        authentication: dict[str, Any] | None = None
        authentication_manager: Any | None = None
        authentication_error: Any | None = None
        authentication_dependency_installed = True
        collection_error: CollectionStoreError | None = None
        collector_error: PluginRuntimeError | CollectionManagerError | None = None
        collector_registrations: tuple[Any, ...] = ()
        collection_manager = CollectionManager()
        if action in {"health", "verify"}:
            try:
                collector_registrations = discover_collector_registrations()
                for registration in collector_registrations:
                    collection_manager.register(registration)
            except (PluginRuntimeError, CollectionManagerError) as exc:
                collector_error = exc
        if action in {"health", "verify"}:
            try:
                from authentication import AuthenticationError, AuthenticationManager
            except ImportError:
                authentication_dependency_installed = False
                authentication = {"configured": False, "dependency": "not_installed"}
            else:
                try:
                    # Resolve exactly like EncryptedCredentialStore does
                    # (sys.modules-aware), not via the package attribute.
                    keyring_store = importlib.import_module("authentication.keyring")

                    authentication_keyring_available = bool(keyring_store.available())
                except Exception:
                    authentication_keyring_available = False
                try:
                    authentication_manager = AuthenticationManager(paths)
                    authentication = authentication_manager.status()
                    authentication["keyring_available"] = authentication_keyring_available
                except AuthenticationError as exc:
                    authentication_error = exc
        required_authentication_realms = {
            registration.browser_realm
            for registration in collector_registrations
            if getattr(registration, "authentication_required", False)
            and isinstance(getattr(registration, "browser_realm", None), str)
        }
        configured_authentication_realms = {
            item.get("id")
            for item in authentication.get("realms", [])
            if isinstance(authentication, dict)
            and isinstance(item, dict)
            and item.get("status") == "configured"
        } if isinstance(authentication, dict) else set()
        requirement_status: list[dict[str, Any]] = []
        declared_requirements = setup.get("authentication_requirements", [])
        if not isinstance(declared_requirements, list):
            declared_requirements = []
        if authentication_manager is not None:
            for requirement in declared_requirements:
                if not isinstance(requirement, dict):
                    continue
                plugin_id = requirement.get("plugin")
                provider = requirement.get("provider")
                if not isinstance(plugin_id, str) or not isinstance(provider, str):
                    continue
                try:
                    profile = authentication_manager.profile_for_plugin(plugin_id, provider)
                    profile_status = authentication_manager.profile_status(profile)
                    requirement_status.append(
                        {
                            "plugin": plugin_id,
                            "profile": profile,
                            "type": profile_status.get("type"),
                            "status": profile_status.get("status"),
                            "verification": profile_status.get("verification"),
                        }
                    )
                except Exception:
                    requirement_status.append(
                        {"plugin": plugin_id, "profile": None, "type": None, "status": "not_enrolled"}
                    )
        exact_requirements_ready = bool(declared_requirements) and len(requirement_status) == len(declared_requirements) and all(
            item.get("status") == "enrolled" for item in requirement_status
        )
        legacy_requirements_ready = (
            isinstance(authentication, dict)
            and authentication.get("configured") is True
            and (
                not required_authentication_realms
                or required_authentication_realms.issubset(configured_authentication_realms)
            )
        )
        authentication_ready = (
            authentication_dependency_installed
            and authentication_error is None
            and (
                exact_requirements_ready
                if declared_requirements
                else legacy_requirements_ready
            )
        )
        if isinstance(authentication, dict):
            authentication["requirements"] = requirement_status
            if declared_requirements:
                authentication["configured"] = exact_requirements_ready
            if authentication_manager is not None and hasattr(authentication_manager, "profiles"):
                authentication["profiles"] = authentication_manager.profiles()
        collection = collection_manager.status()
        if action in {"health", "verify"}:
            try:
                collection["durable_queue"] = CollectionTaskStore.inspect_paths(paths)
            except CollectionStoreError as exc:
                collection_error = exc
                collection["durable_queue"] = {
                    "ready": False,
                    "status": "unavailable",
                    "tasks": {},
                    "schedules": 0,
                    "workers": 0,
                    "overdue_workers": 0,
                }
        durable_queue = collection.get("durable_queue", {"ready": True})
        collection_ready = collection_error is None and (
            action not in {"health", "verify"} or durable_queue.get("ready") is True
        )
        browser_ready = inspection["ready"] is True
        setup_invalid = setup.get("invalid") is True
        plugins = (
            plugin_health(setup["selected_plugins"])
            if setup["complete"] is True
            else {"ready": False, "plugins": {}, "error": None}
        )
        browser_required = "browser" in setup["capabilities"] or any(
            getattr(registration, "browser_realm", None) is not None
            for registration in collector_registrations
        )
        authentication_required = "authentication" in setup["capabilities"]
        collector_required = "collect" in setup["capabilities"]
        setup_ready = (
            setup["complete"] is True
            and plugins["ready"] is True
            and (not browser_required or browser_ready)
            and (not authentication_required or authentication_ready)
            and collector_error is None
            and (not collector_required or bool(collector_registrations))
        )
        collector_ready = (
            collection_ready
            and collector_error is None
            and (not collector_required or bool(collector_registrations))
        )
        core_operational = (
            collector_ready
            and not setup_invalid
            and (not browser_required or browser_ready)
            and (not authentication_required or authentication_ready)
        )
        configured = setup["complete"] is True
        planes = {name: "not_applicable" for name in PLANES}
        planes.update(
            {
                "registration": "ready",
                "runtime_integrity": "ready",
                "configuration": "invalid" if setup_invalid else ("ready" if configured else "unavailable"),
                "browser": "ready" if browser_ready else ("unavailable" if browser_required else "not_applicable"),
                "overall": "degraded" if setup_invalid else ("ready" if setup_ready else "setup_incomplete"),
            }
        )
        if action in {"health", "verify"}:
            planes["query"] = "ready" if plugins["ready"] is True else "unavailable"
            planes["collector"] = "ready" if collector_ready else "unavailable"
            planes["authentication"] = (
                ("ready" if authentication_ready else "unavailable")
                if authentication_required
                else "not_applicable"
            )
        data: dict[str, Any] = {
            "installed": True,
            "configured": configured,
            "ready": setup_ready,
            "operational": browser_ready if action == "browser-doctor" else core_operational,
            "planes": planes,
            "setup": setup,
            "plugin_runtime": plugins,
            "browser_manager": {
                **inspection,
                "realms": RealmRegistry().safe_data(),
            },
            "collection_manager": collection,
        }
        if authentication is not None:
            data["authentication"] = authentication
        installation_invalid = False
        if action == "verify":
            data["application"] = "dispatch-core"
            identity = paths.dispatch_home / "installation.json"
            try:
                installed = _load_json_bounded(identity.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Verify is the strict gate: an installation without its
                # identity record must not pass as "development".
                installed = {}
                installation_invalid = True
            except (OSError, UnicodeError, ValueError):
                installed = {}
                installation_invalid = True
            if not isinstance(installed, dict):
                installed = {}
                installation_invalid = True

            def _bounded_text(value: object, *, maximum: int = 128) -> str | None:
                if not isinstance(value, str) or not value:
                    return None
                return value if len(value) <= maximum else value[:maximum]

            data["version"] = _bounded_text(installed.get("ref")) or "development"
            channel = installed.get("channel")
            data["channel"] = channel if channel in {"stable", "dev"} else None
            data["commit"] = _bounded_text(installed.get("commit"), maximum=64)

        error = None
        if setup_invalid:
            error = {
                "code": "plugin_config_invalid",
                "message": "plugin configuration is unreadable or invalid",
            }
        elif installation_invalid:
            error = {
                "code": "installation_record_invalid",
                "message": "installation record is unreadable or invalid",
            }
        elif collector_error is not None:
            error = {"code": collector_error.code, "message": str(collector_error)}
        elif collector_required and not collector_registrations:
            error = {
                "code": "plugin_collector_provider_missing",
                "message": "one or more selected collecting plugins have no collector registrations",
            }
        elif collection_error is not None:
            error = {"code": collection_error.code, "message": str(collection_error)}
        elif not collection_ready:
            error = {
                "code": "collection_worker_reconciliation_required",
                "message": "one or more collection workers require reconciliation",
            }
        if action == "browser-doctor" and not browser_ready:
            error = {
                "code": str(inspection["error_code"]),
                "message": str(inspection["error_message"]),
            }
        ok = browser_ready if action == "browser-doctor" else core_operational and not installation_invalid
        status = "ready" if setup_ready and not installation_invalid else ("setup_incomplete" if ok else "degraded")
        return envelope(ok=ok, action=action, status=status, data=data, error=error)

    raise ValueError(f"unsupported health action: {action}")


__all__ = ["PLANES", "envelope", "resolved"]
