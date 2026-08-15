"""Bounded command interface for installed Dispatch Core."""
from __future__ import annotations

import argparse
import getpass
import json
import signal
from typing import Any, Sequence

from dispatch_core.collection_manager import CollectionStoreError, CollectionTaskStore
from dispatch_core.collection_manager.queue import utc_now
from dispatch_core.collection_manager.supervisor import (
    CollectionService,
    CollectionWorkerSupervisor,
    ProductionManagerFactory,
)
from dispatch_core.health import envelope, resolved
from dispatch_core.paths import DispatchPaths, PathConfigError
from dispatch_core.plugin_runtime import PluginRuntimeError, invoke_plugin, list_plugins


class CommandInterfaceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parser(*, prog: str = "dispatch-core") -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog=prog)
    subcommands = value.add_subparsers(dest="action", required=True)
    subcommands.add_parser("health", help="report installed Core path readiness")
    subcommands.add_parser("verify", help="verify the installed Core package and path contract")
    subcommands.add_parser("browser-doctor", help="report non-mutating Browser Manager dependency readiness")
    service = subcommands.add_parser("service", help="run the foreground Core collection service")
    service.add_argument("--idle-seconds", type=float, default=1.0)
    service.add_argument("--max-ticks", type=int)
    paths = subcommands.add_parser("paths", help="resolve non-mutating installation roots")
    paths.add_argument("--owner")

    plugin = subcommands.add_parser("plugin", help="list or invoke installer-approved plugins")
    plugin_actions = plugin.add_subparsers(dest="plugin_action", required=True)
    plugin_actions.add_parser("list", help="list active discoverable plugins")
    plugin_health = plugin_actions.add_parser("health", help="run a plugin's non-mutating health action")
    plugin_health.add_argument("plugin_id")
    plugin_invoke = plugin_actions.add_parser("invoke", help="send one bounded JSON request to a plugin")
    plugin_invoke.add_argument("plugin_id")
    plugin_invoke.add_argument("--request", required=True, help="JSON object accepted by the plugin")

    auth = subcommands.add_parser("auth", help="manage private Core authentication credentials")
    auth_actions = auth.add_subparsers(dest="auth_action", required=True)
    auth_status = auth_actions.add_parser("status", help="report bounded credential enrollment status")
    auth_status.add_argument("--realm")
    auth_status.add_argument("--account", default="default")
    auth_enroll = auth_actions.add_parser("enroll", help="enroll credentials through hidden prompts")
    auth_enroll.add_argument("realm")
    auth_enroll.add_argument("--account", default="default")
    auth_remove = auth_actions.add_parser("remove", help="remove enrolled credentials")
    auth_remove.add_argument("realm")
    auth_remove.add_argument("--account", default="default")
    auth_remove.add_argument("--yes", action="store_true", help="confirm credential removal")

    collection = subcommands.add_parser("collection", help="operate the durable Collection Manager worker")
    collection_actions = collection.add_subparsers(dest="collection_action", required=True)
    collection_actions.add_parser("status", help="inspect durable queue state without creating it")
    collection_actions.add_parser("worker-once", help="run one bounded worker process")
    collection_actions.add_parser("reconcile", help="clean orphaned workers and reconcile interrupted tasks")
    collection_cancel = collection_actions.add_parser("cancel", help="persist a collection cancellation request")
    collection_cancel.add_argument("task_id")
    collection_resume = collection_actions.add_parser("resume", help="request resume in the owning worker")
    collection_resume.add_argument("task_id")
    return value


def _auth_result(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from dispatch_core.authentication import AuthenticationError, AuthenticationManager, DEFAULT_AUTH_REALMS
    except ImportError as exc:
        raise CommandInterfaceError(
            "authentication_dependency_missing",
            "authentication capability dependencies are not installed; run dispatch setup",
        ) from exc

    try:
        paths = DispatchPaths.from_environment()
        authentication = AuthenticationManager(paths)
        action = f"auth-{args.auth_action}"
        if args.auth_action == "status":
            data = authentication.status(args.realm, args.account)
        elif args.auth_action == "enroll":
            policy = next((item for item in DEFAULT_AUTH_REALMS if item.id == args.realm), None)
            if policy is None:
                raise AuthenticationError("authentication_realm_unknown", "authentication realm is not supported")
            values = {name: getpass.getpass(f"{name}: ") for name in policy.credential_fields}
            data = authentication.enroll(args.realm, args.account, values)
        elif args.auth_action == "remove":
            if not args.yes:
                raise AuthenticationError("confirmation_required", "credential removal requires --yes")
            data = authentication.remove(args.realm, args.account)
        else:  # pragma: no cover - argparse owns this boundary
            raise AuthenticationError("invalid_auth_request", "unsupported authentication action")
    except (EOFError, KeyboardInterrupt) as exc:
        raise CommandInterfaceError("authentication_cancelled", "credential enrollment was cancelled") from exc
    except AuthenticationError as exc:
        raise CommandInterfaceError(exc.code, str(exc)) from exc
    return envelope(ok=True, action=action, status="ready", data=data)


def _collection_result(args: argparse.Namespace) -> dict[str, Any]:
    paths = DispatchPaths.from_environment()
    action = f"collection-{args.collection_action}"
    if args.collection_action == "status":
        data = CollectionTaskStore.inspect_paths(paths)
    else:
        store = CollectionTaskStore.from_paths(paths)
        if args.collection_action == "cancel":
            data = store.request_cancel(args.task_id, utc_now()).safe_data()
        elif args.collection_action == "resume":
            data = store.request_resume(args.task_id, utc_now()).safe_data()
        else:
            supervisor = CollectionWorkerSupervisor(
                store.database,
                ProductionManagerFactory(paths),
            )
            if args.collection_action == "worker-once":
                data = supervisor.run_once().safe_data()
            elif args.collection_action == "reconcile":
                orphaned = supervisor.reconcile_orphans()
                expired = store.reconcile(utc_now())
                data = {
                    "orphaned": len(orphaned),
                    "expired": len(expired),
                }
            else:  # pragma: no cover - argparse owns this boundary
                raise CollectionStoreError("invalid_collection_request", "unsupported collection action")
    return envelope(ok=True, action=action, status="ready", data=data)


def _plugin_result(args: argparse.Namespace) -> dict[str, Any]:
    action = f"plugin-{args.plugin_action}"
    if args.plugin_action == "list":
        return envelope(ok=True, action=action, status="ready", data={"plugins": list_plugins()})
    if args.plugin_action == "health":
        request = {"action": "health"}
    else:
        try:
            request = json.loads(args.request)
        except json.JSONDecodeError as exc:
            raise CommandInterfaceError("plugin_request_invalid", "plugin request must be valid JSON") from exc
        if type(request) is not dict:
            raise CommandInterfaceError("plugin_request_invalid", "plugin request must be a JSON object")
    response = invoke_plugin(args.plugin_id, request)
    return envelope(
        ok=response["ok"],
        action=action,
        status=response["status"],
        data={"plugin": args.plugin_id, "response": response},
        error=response["error"],
    )


def _service_result(args: argparse.Namespace) -> dict[str, Any]:
    paths = DispatchPaths.from_environment()
    store = CollectionTaskStore.from_paths(paths)
    supervisor = CollectionWorkerSupervisor(store.database, ProductionManagerFactory(paths))
    service = CollectionService(store, supervisor)
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        ticks = service.run(lambda: stopping, idle_seconds=args.idle_seconds, max_ticks=args.max_ticks)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return envelope(
        ok=True,
        action="service",
        status="stopped",
        data={"ticks": len(ticks), "last_tick": ticks[-1].safe_data() if ticks else None},
    )


def main(argv: Sequence[str] | None = None, *, prog: str = "dispatch-core") -> int:
    args = parser(prog=prog).parse_args(argv)
    try:
        if args.action == "auth":
            result = _auth_result(args)
        elif args.action == "collection":
            result = _collection_result(args)
        elif args.action == "plugin":
            result = _plugin_result(args)
        elif args.action == "service":
            result = _service_result(args)
        else:
            result = resolved(args.action, getattr(args, "owner", None))
    except (CommandInterfaceError, CollectionStoreError, PathConfigError, PluginRuntimeError) as exc:
        code = (
            exc.code
            if isinstance(exc, (CommandInterfaceError, CollectionStoreError, PluginRuntimeError))
            else "invalid_path_configuration"
        )
        result = envelope(
            ok=False,
            action=(
                f"auth-{getattr(args, 'auth_action', 'unknown')}"
                if args.action == "auth"
                else (
                    f"collection-{getattr(args, 'collection_action', 'unknown')}"
                    if args.action == "collection"
                    else (
                        f"plugin-{getattr(args, 'plugin_action', 'unknown')}"
                        if args.action == "plugin"
                        else args.action
                    )
                )
            ),
            status="error",
            data={},
            error={"code": code, "message": str(exc)[:256]},
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


__all__ = ["main", "parser"]
