"""Bounded command interface for installed Dispatch Core."""
from __future__ import annotations

import argparse
import getpass
import json
import signal
import sys
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
    browser_prune = browser_actions.add_parser(
        "prune", help="delete terminal Browser Manager lease rows older than a cutoff"
    )
    browser_prune.add_argument(
        "--older-than-days",
        type=int,
        default=30,
        help="remove terminal rows last updated more than this many days ago (default: 30)",
    )
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
    auth_actions.add_parser("list", help="list named authentication profiles")
    auth_status = auth_actions.add_parser("status", help="report bounded credential enrollment status")
    auth_status.add_argument("profile", nargs="?")
    auth_status.add_argument("--realm")
    auth_status.add_argument("--account", default="default")
    auth_add = auth_actions.add_parser("add", help="add a named authentication profile through hidden prompts")
    auth_add.add_argument("profile")
    auth_add.add_argument("--provider", help="profile type: amazon or paycom")
    auth_enroll = auth_actions.add_parser("enroll", help="enroll credentials through hidden prompts")
    auth_enroll.add_argument("realm")
    auth_enroll.add_argument("--account", default="default")
    auth_remove = auth_actions.add_parser("remove", help="remove enrolled credentials")
    auth_remove.add_argument("profile")
    auth_remove.add_argument("--account", default="default")
    auth_remove.add_argument("--realm")
    auth_remove.add_argument("--yes", action="store_true", help="confirm credential removal")
    auth_select = auth_actions.add_parser(
        "select",
        help="select an enrolled authentication profile for a plugin",
    )
    auth_select.add_argument("profile")
    auth_select.add_argument("--plugin", required=True, help="built-in plugin ID")
    auth_select.add_argument("--provider", help="override the profile type derived from the profile")
    auth_deselect = auth_actions.add_parser(
        "deselect",
        help="release a plugin's selected authentication profile",
    )
    auth_deselect.add_argument("--plugin", required=True, help="built-in plugin ID")
    auth_rotate = auth_actions.add_parser(
        "rotate",
        help="re-encrypt the vault under a freshly generated key",
    )
    auth_rotate.add_argument(
        "--yes",
        action="store_true",
        help="confirm vault key rotation",
    )

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


def _auth_result(args: argparse.Namespace, *, interactive: bool) -> dict[str, Any]:
    try:
        from authentication import (
            AuthenticationError,
            AuthenticationManager,
            DEFAULT_AUTH_REALMS,
        )
    except ImportError as exc:
        raise CommandInterfaceError(
            "authentication_dependency_missing",
            "authentication is not enabled; install and set up a plugin that requires it",
        ) from exc

    try:
        paths = DispatchPaths.from_environment()
        authentication = AuthenticationManager(paths)
        action = f"auth-{args.auth_action}"
        if args.auth_action == "list":
            profiles = authentication.profiles()
            data = {
                "profiles": profiles,
                "configured": any(item["status"] == "enrolled" for item in profiles),
                "contains_secrets": False,
            }
        elif args.auth_action == "status":
            profile = getattr(args, "profile", None)
            if profile is not None:
                try:
                    data = authentication.profile_status(profile)
                except AuthenticationError:
                    if profile not in {item.id for item in DEFAULT_AUTH_REALMS}:
                        raise
                    data = authentication.status(profile, args.account)
            elif args.realm is not None:
                data = authentication.status(args.realm, args.account)
            else:
                profiles = authentication.profiles()
                data = {
                    "profiles": profiles,
                    "configured": any(item["status"] == "enrolled" for item in profiles),
                    "contains_secrets": False,
                }
        elif args.auth_action == "add":
            if not interactive:
                raise AuthenticationError(
                    "authentication_interactive_required",
                    "profile credentials must be entered interactively",
                )
            profile = args.profile
            try:
                authentication.profile_status(profile)
            except AuthenticationError as exc:
                if exc.code != "profile_not_found":
                    raise
            else:
                raise AuthenticationError(
                    "profile_exists",
                    "authentication profile already exists; create a new named profile",
                )
            provider = args.provider
            if provider is None:
                providers = authentication.providers()
                print("Available profile types:", file=sys.stderr)
                for index, item in enumerate(providers, start=1):
                    print(f"  {index}. {item.display_name}", file=sys.stderr)
                answer = input("Select profile type: ").strip()
                try:
                    selection = int(answer)
                    if not 1 <= selection <= len(providers):
                        raise IndexError(selection)
                    policy = providers[selection - 1]
                except (ValueError, IndexError) as exc:
                    raise AuthenticationError("provider_selection_invalid", "profile type selection is invalid") from exc
            else:
                policy = authentication.provider(provider)
            values = {name: getpass.getpass(f"{name}: ") for name in policy.credential_fields}
            data = authentication.enroll_profile(profile, policy.id, values)
        elif args.auth_action == "select":
            # Provider is derived from the profile's own enrollment unless the
            # operator overrides it; the store re-validates compatibility.
            provider = args.provider
            if provider is None:
                record = authentication.profile_status(args.profile)
                if record["type"] == "unavailable":
                    raise AuthenticationError(
                        "unknown_auth_provider",
                        "authentication profile type is not installed",
                    )
                provider = record["type"]
            data = authentication.select_plugin_profile(args.profile, args.plugin, provider)
        elif args.auth_action == "deselect":
            data = authentication.clear_plugin_profile(args.plugin)
        elif args.auth_action == "rotate":
            if not args.yes:
                raise AuthenticationError(
                    "confirmation_required",
                    "vault key rotation requires --yes",
                )
            data = authentication.rotate_vault()
        elif args.auth_action == "enroll":
            if not interactive:
                raise AuthenticationError(
                    "authentication_interactive_required",
                    "profile credentials must be entered interactively",
                )
            policy = next((item for item in DEFAULT_AUTH_REALMS if item.id == args.realm), None)
            if policy is None:
                raise AuthenticationError("authentication_realm_unknown", "authentication realm is not supported")
            values = {name: getpass.getpass(f"{name}: ") for name in policy.credential_fields}
            data = authentication.enroll(args.realm, args.account, values)
        elif args.auth_action == "remove":
            if not args.yes:
                raise AuthenticationError("confirmation_required", "credential removal requires --yes")
            target = args.realm or args.profile
            if args.realm is not None:
                data = authentication.remove(args.realm, args.account)
            else:
                if target in {item.id for item in DEFAULT_AUTH_REALMS}:
                    try:
                        authentication.profile_status(target)
                    except AuthenticationError:
                        data = authentication.remove(target, args.account)
                    else:
                        data = authentication.remove_profile(target)
                else:
                    data = authentication.remove_profile(target)
        else:  # pragma: no cover - argparse owns this boundary
            raise AuthenticationError("invalid_auth_request", "unsupported authentication action")
    except (EOFError, KeyboardInterrupt) as exc:
        raise CommandInterfaceError("authentication_cancelled", "credential enrollment was cancelled") from exc
    except AuthenticationError as exc:
        raise CommandInterfaceError(exc.code, str(exc)) from exc
    except OSError as exc:
        raise CommandInterfaceError(
            "authentication_store_unavailable",
            "authentication profile storage could not be updated safely",
        ) from exc
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
    registrations = discover_collector_registrations()
    registration = next(
        (item for item in registrations if item.collector_id == args.collector_id),
        None,
    )
    authentication = None
    if registration is not None and registration.authentication_required:
        try:
            from authentication import AuthenticationManager

            authentication = AuthenticationManager(DispatchPaths.from_environment())
        except Exception as exc:
            raise CommandInterfaceError(
                "authentication_unavailable",
                "authentication profiles could not be inspected before collection",
            ) from exc
    manager = CollectionManager(authentication=authentication, store=store)
    for registration in registrations:
        manager.register(registration)
    account_alias = args.account_alias
    request = CollectionRequest(
        args.collector_id,
        account_alias=account_alias,
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
        if not isinstance(args.request, str) or len(args.request.encode("utf-8")) > 64 * 1024:
            raise CommandInterfaceError("plugin_request_invalid", "plugin request is too large")
        try:
            request = json.loads(args.request)
        except (json.JSONDecodeError, RecursionError) as exc:
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
    if args.browser_action == "prune":
        if not 1 <= int(args.older_than_days) <= 3650:
            raise CommandInterfaceError("invalid_browser_request", "--older-than-days must be between 1 and 3650")
        from datetime import datetime, timedelta, timezone

        try:
            manager = BrowserManager(DispatchPaths.from_environment(), reconciliation_only=True)
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(args.older_than_days))
            removed = manager.store.prune(before=cutoff, limit=10_000)
        except (BrowserManagerError, PathConfigError) as exc:
            code = exc.code if isinstance(exc, BrowserManagerError) else "path_configuration_invalid"
            raise CommandInterfaceError(code, str(exc)) from exc
        return envelope(
            ok=True,
            action=action,
            status="ready",
            data={"schema_version": 1, "removed": removed, "contains_secrets": False},
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
    needs_browser = any(item.browser_realm is not None for item in registrations)
    browser_manager: Any | None = None
    if needs_browser:
        from browser_manager import BrowserManager, BrowserManagerError

        try:
            browser_manager = BrowserManager(paths)
        except BrowserManagerError as exc:
            raise CommandInterfaceError(exc.code, str(exc)) from exc
    supervisor = CollectionWorkerSupervisor(
        store.database,
        ProductionManagerFactory(paths, registrations),
        registrations=registrations,
    )
    service = CollectionService(
        store,
        supervisor,
        browser_maintenance=None if browser_manager is None else browser_manager.maintain,
    )
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    try:
        ticks = service.run(lambda: stopping, idle_seconds=args.idle_seconds, max_ticks=args.max_ticks)
    finally:
        if browser_manager is not None:
            browser_manager.shutdown()
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return envelope(
        ok=True,
        action="service",
        status="stopped",
        data={"ticks": len(ticks), "last_tick": ticks[-1].safe_data() if ticks else None},
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    prog: str = "dispatch-core",
    interactive: bool = True,
) -> int:
    arguments = None if argv is None else list(argv)
    if not interactive and arguments is not None and any(value in {"-h", "--help"} for value in arguments):
        command = [value for value in arguments if value not in {"-h", "--help"}]
        result = envelope(
            ok=True,
            action="help",
            status="ready",
            data={
                "command": command,
                "usage": parser(prog=prog).format_usage().strip(),
                "contains_secrets": False,
            },
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        args = parser(prog=prog).parse_args(arguments)
        if args.action == "auth":
            result = _auth_result(args, interactive=interactive)
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
