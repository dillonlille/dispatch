"""Read-only installed health, verification, and path responses."""
from __future__ import annotations

from typing import Any

from dispatch_core.paths import DispatchPaths, PathConfigError

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
        from dispatch_core.browser_manager import RealmRegistry
        from dispatch_core.browser_manager.runtime_authority import BrowserRuntimeAuthority
        from dispatch_core.collection_manager import CollectionManager, CollectionStoreError, CollectionTaskStore

        inspection = BrowserRuntimeAuthority.production().inspect(full_tree=True)
        authentication: dict[str, Any] | None = None
        authentication_error: Any | None = None
        authentication_dependency_installed = True
        collection_error: CollectionStoreError | None = None
        if action in {"health", "verify"}:
            try:
                from dispatch_core.authentication import AuthenticationError, AuthenticationManager
            except ImportError:
                authentication_dependency_installed = False
                authentication = {"configured": False, "dependency": "not_installed"}
            else:
                try:
                    authentication = AuthenticationManager(paths).status()
                except AuthenticationError as exc:
                    authentication_error = exc
        authentication_ready = authentication_dependency_installed and authentication_error is None
        collection = CollectionManager().status()
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
        setup_ready = browser_ready and authentication_ready
        core_operational = collection_ready
        configured = inspection["configured"] is True
        planes = {name: "not_applicable" for name in PLANES}
        planes.update(
            {
                "registration": "ready",
                "runtime_integrity": "ready",
                "configuration": "ready" if configured else "unavailable",
                "browser": "ready" if browser_ready else "unavailable",
                "overall": "ready" if setup_ready else "setup_incomplete",
            }
        )
        if action in {"health", "verify"}:
            planes["query"] = "ready"
            planes["collector"] = "ready" if collection_ready else "unavailable"
            planes["authentication"] = "ready" if authentication_ready else "unavailable"
        data: dict[str, Any] = {
            "installed": True,
            "configured": configured,
            "ready": setup_ready,
            "operational": browser_ready if action == "browser-doctor" else core_operational,
            "planes": planes,
            "browser_manager": {
                **inspection,
                "realms": RealmRegistry().safe_data(),
            },
            "collection_manager": collection,
        }
        if authentication is not None:
            data["authentication"] = authentication
        if action == "verify":
            data["package"] = "dispatch-core"
            data["version"] = "1.0.0"

        error = None
        if collection_error is not None:
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
        ok = browser_ready if action == "browser-doctor" else core_operational
        status = "ready" if setup_ready else ("setup_incomplete" if ok else "degraded")
        return envelope(ok=ok, action=action, status=status, data=data, error=error)

    raise ValueError(f"unsupported health action: {action}")


__all__ = ["PLANES", "envelope", "resolved"]
