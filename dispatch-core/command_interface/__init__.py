"""Bounded command interface for installed Dispatch Core."""
from __future__ import annotations

import argparse
import getpass
import json
import signal
from datetime import datetime
from typing import Any, NoReturn, Sequence

from collection_manager import (
    CollectionManager,
    CollectionManagerError,
    CollectionRequest,
    CollectionStoreError,
    CollectionTaskStore,
)
from collection_manager.queue import utc_now
from collection_manager.supervisor import (
    CollectionService,
    CollectionWorkerSupervisor,
    ProductionManagerFactory,
)
from health import envelope, resolved
from paths import DispatchPaths, PathConfigError
from plugin_runtime import (
    PluginRuntimeError,
    configure_plugin,
    discover_collector_registrations,
    invoke_plugin,
    list_plugins,
    serve_plugin,
)


class CommandInterfaceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CommandInterfaceError("arguments_invalid", message)


def parser(*, prog: str = "dispatch-core") -> argparse.ArgumentParser:
    value = _JsonArgumentParser(prog=prog)
    subcommands = value.add_subparsers(dest="action", required=True)
    subcommands.add_parser("health", help="report installed Core path readiness")
    subcommands.add_parser("verify", help="verify the installed Core package and path contract")
    subcommands.add_parser("browser-doctor", help="report non-mutating Browser Manager dependency readiness")
    browser = subcommands.add_parser("browser", help="inspect the managed Browser Manager runtime")
    browser_actions = browser.add_subparsers(dest="browser_action", required=True)
    browser_actions.add_parser("status", help="report bounded runtime and provider status")
    browser_actions.add_parser("doctor", help="diagnose managed browser readiness")
    browser_actions.add_parser("verify", help="verify the active managed browser generation")
    browser_actions.add_parser("reconcile", help="reconcile interrupted Browser Manager leases")
    browser_actions.add_parser("providers", help="list implemented and reserved provider contracts")
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
    plugin_serve = plugin_actions.add_parser("serve", help="run one active plugin service in the foreground")
    plugin_serve.add_argument("plugin_id")
    plugin_configure = plugin_actions.add_parser("configure", help="run one active plugin configurator interactively")
    plugin_configure.add_argument("plugin_id")

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
    collection_submit = collection_actions.add_parser("submit", help="queue one bounded collector request")
    collection_submit.add_argument("collector_id")
    collection_submit.add_argument("--account", "--account-alias", dest="account_alias", default="default")
    collection_submit.add_argument("--parameters", default="{}", help="bounded JSON object passed to the collector")
    collection_submit.add_argument("--max-attempts", type=int, default=3)
    collection_submit.add_argument("--not-before")
    collection_submit.add_argument("--idempotency-key")
    collection_actions.add_parser("worker-once", help="run one bounded worker process")
    collection_actions.add_parser("reconcile", help="clean orphaned workers and reconcile interrupted tasks")
    collection_cancel = collection_actions.add_parser("cancel", help="persist a collection cancellation request")
    collection_cancel.add_argument("task_id")
    collection_resume = collection_actions.add_parser("resume", help="request resume in the owning worker")
    collection_resume.add_argument("task_id")
    return value


def _auth_result(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from authentication import AuthenticationError, AuthenticationManager, DEFAULT_AUTH_REALMS
    except ImportError as exc:
        raise CommandInterfaceError(
            "authentication_dependency_missing",
            "authentication is not enabled; install and set up a plugin that requires it",
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


def _parse_collection_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise CommandInterfaceError("collection_request_invalid", "not-before timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise CommandInterfaceError("collection_request_invalid", "not-before timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommandInterfaceError("collection_request_invalid", "not-before timestamp must include a timezone")
    return parsed


def _collection_submission(args: argparse.Namespace, store: CollectionTaskStore) -> dict[str, object]:
    if not isinstance(args.parameters, str) or len(args.parameters.encode("utf-8")) > 64 * 1024:
        raise CommandInterfaceError("collection_request_invalid", "collection parameters are too large")
    try:
        parameters = json.loads(args.parameters)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise CommandInterfaceError("collection_request_invalid", "collection parameters must be valid JSON") from exc
    if type(parameters) is not dict:
        raise CommandInterfaceError("collection_request_invalid", "collection parameters must be a JSON object")
    manager = CollectionManager(store=store)
    for registration in discover_collector_registrations():
        manager.register(registration)
    request = CollectionRequest(
        args.collector_id,
        account_alias=args.account_alias,
        parameters=parameters,
    )
    return manager.enqueue(
        request,
        max_attempts=args.max_attempts,
        not_before=_parse_collection_time(args.not_before),
        idempotency_key=args.idempotency_key,
    ).safe_data()


def _collection_result(args: argparse.Namespace) -> dict[str, Any]:
    paths = DispatchPaths.from_environment()
    action = f"collection-{args.collection_action}"
    if args.collection_action == "status":
        data = CollectionTaskStore.inspect_paths(paths)
    else:
        store = CollectionTaskStore.from_paths(paths)
        if args.collection_action == "submit":
            data = _collection_submission(args, store)
        elif args.collection_action == "cancel":
            data = store.request_cancel(args.task_id, utc_now()).safe_data()
        elif args.collection_action == "resume":
            data = store.request_resume(args.task_id, utc_now()).safe_data()
        else:
            registrations = discover_collector_registrations()
            supervisor = CollectionWorkerSupervisor(
                store.database,
                ProductionManagerFactory(paths, registrations),
                registrations=registrations,
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
    elif args.plugin_action == "invoke":
        try:
            request = json.loads(args.request)
        except json.JSONDecodeError as exc:
            raise CommandInterfaceError("plugin_request_invalid", "plugin request must be valid JSON") from exc
        if type(request) is not dict:
            raise CommandInterfaceError("plugin_request_invalid", "plugin request must be a JSON object")
    elif args.plugin_action == "configure":
        paths = DispatchPaths.from_environment()
        response = configure_plugin(args.plugin_id, paths=paths)
        return envelope(
            ok=response["ok"],
            action=action,
            status=response["status"],
            data={"plugin": args.plugin_id, "response": response},
            error=response["error"],
        )
    else:  # serve
        paths = DispatchPaths.from_environment()
        stopping = False

        def request_stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        previous_term = signal.signal(signal.SIGTERM, request_stop)
        previous_int = signal.signal(signal.SIGINT, request_stop)
        try:
            serve_plugin(args.plugin_id, paths=paths, stop_requested=lambda: stopping)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
        return envelope(
            ok=True,
            action=action,
            status="stopped",
            data={"plugin": args.plugin_id},
        )
    response = invoke_plugin(args.plugin_id, request)
    return envelope(
        ok=response["ok"],
        action=action,
        status=response["status"],
        data={"plugin": args.plugin_id, "response": response},
        error=response["error"],
    )


def _browser_result(args: argparse.Namespace) -> dict[str, Any]:
    from browser_manager import BrowserManager, BrowserManagerError, BrowserProviderRegistry
    from browser_manager.runtime_authority import BrowserRuntimeAuthority

    action = f"browser-{args.browser_action}"
    providers = BrowserProviderRegistry().safe_data()
    if args.browser_action == "providers":
        return envelope(
            ok=True,
            action=action,
            status="ready",
            data={"schema_version": 1, "providers": providers, "contains_secrets": False},
        )
    if args.browser_action == "reconcile":
        try:
            manager = BrowserManager(DispatchPaths.from_environment(), reconciliation_only=True)
            outcomes = manager.reconcile()
        except (BrowserManagerError, PathConfigError) as exc:
            code = exc.code if isinstance(exc, BrowserManagerError) else "path_configuration_invalid"
            raise CommandInterfaceError(code, str(exc)) from exc
        return envelope(
            ok=True,
            action=action,
            status="ready",
            data={
                "schema_version": 1,
                "outcomes": outcomes,
                "leases": manager.status(),
                "contains_secrets": False,
            },
        )
    try:
        inspection = BrowserRuntimeAuthority.production().inspect(full_tree=True)
    except BrowserManagerError as exc:
        raise CommandInterfaceError(exc.code, str(exc)) from exc
    ready = bool(inspection["ready"])
    data = {
        "schema_version": 1,
        "runtime": inspection,
        "providers": providers,
        "contains_secrets": False,
    }
    if args.browser_action == "status":
        return envelope(ok=True, action=action, status="ready" if ready else "not_ready", data=data)
    error = None if ready else {
        "code": str(inspection["error_code"]),
        "message": str(inspection["error_message"]),
    }
    return envelope(ok=ready, action=action, status="ready" if ready else "not_ready", data=data, error=error)


def _service_result(args: argparse.Namespace) -> dict[str, Any]:
    paths = DispatchPaths.from_environment()
    registrations = discover_collector_registrations()
    store = CollectionTaskStore.from_paths(paths)
    supervisor = CollectionWorkerSupervisor(
        store.database,
        ProductionManagerFactory(paths, registrations),
        registrations=registrations,
    )
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
    try:
        args = parser(prog=prog).parse_args(argv)
        if args.action == "auth":
            result = _auth_result(args)
        elif args.action == "collection":
            result = _collection_result(args)
        elif args.action == "plugin":
            result = _plugin_result(args)
        elif args.action == "browser":
            result = _browser_result(args)
        elif args.action == "service":
            result = _service_result(args)
        else:
            result = resolved(args.action, getattr(args, "owner", None))
    except (CommandInterfaceError, CollectionManagerError, CollectionStoreError, PathConfigError, PluginRuntimeError) as exc:
        parsed = locals().get("args")
        action_name = str(getattr(parsed, "action", "unknown"))
        code = (
            exc.code
            if isinstance(exc, (CommandInterfaceError, CollectionManagerError, CollectionStoreError, PluginRuntimeError))
            else "invalid_path_configuration"
        )
        if action_name == "auth":
            rendered_action = f"auth-{getattr(parsed, 'auth_action', 'unknown')}"
        elif action_name == "collection":
            rendered_action = f"collection-{getattr(parsed, 'collection_action', 'unknown')}"
        elif action_name == "plugin":
            rendered_action = f"plugin-{getattr(parsed, 'plugin_action', 'unknown')}"
        else:
            rendered_action = action_name
        result = envelope(
            ok=False,
            action=rendered_action,
            status="error",
            data={},
            error={"code": code, "message": str(exc)[:256]},
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


__all__ = ["main", "parser"]
