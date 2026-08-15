"""Bounded command interface for installed Dispatch Core."""
from __future__ import annotations

import argparse
import getpass
import json
from typing import Any, Sequence

from dispatch_core.authentication import AuthenticationError, AuthenticationManager, DEFAULT_AUTH_REALMS
from dispatch_core.collection_manager import CollectionStoreError, CollectionTaskStore
from dispatch_core.collection_manager.queue import utc_now
from dispatch_core.collection_manager.supervisor import (
    CollectionWorkerSupervisor,
    ProductionManagerFactory,
)
from dispatch_core.health import envelope, resolved
from dispatch_core.paths import DispatchPaths, PathConfigError


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="dispatch-core")
    subcommands = value.add_subparsers(dest="action", required=True)
    subcommands.add_parser("health", help="report installed Core path readiness")
    subcommands.add_parser("verify", help="verify the installed Core package and path contract")
    subcommands.add_parser("browser-doctor", help="report non-mutating Browser Manager dependency readiness")
    paths = subcommands.add_parser("paths", help="resolve non-mutating installation roots")
    paths.add_argument("--owner")

    auth = subcommands.add_parser("auth", help="manage private Core authentication credentials")
    auth_actions = auth.add_subparsers(dest="auth_action", required=True)
    auth_status = auth_actions.add_parser("status", help="report bounded credential enrollment status")
    auth_status.add_argument("--realm", choices=[item.id for item in DEFAULT_AUTH_REALMS])
    auth_status.add_argument("--account", default="default")
    auth_enroll = auth_actions.add_parser("enroll", help="enroll credentials through hidden prompts")
    auth_enroll.add_argument("realm", choices=[item.id for item in DEFAULT_AUTH_REALMS])
    auth_enroll.add_argument("--account", default="default")
    auth_remove = auth_actions.add_parser("remove", help="remove enrolled credentials")
    auth_remove.add_argument("realm", choices=[item.id for item in DEFAULT_AUTH_REALMS])
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
    paths = DispatchPaths.from_environment()
    authentication = AuthenticationManager(paths)
    action = f"auth-{args.auth_action}"
    if args.auth_action == "status":
        data = authentication.status(args.realm, args.account)
    elif args.auth_action == "enroll":
        policy = next(item for item in DEFAULT_AUTH_REALMS if item.id == args.realm)
        try:
            values = {name: getpass.getpass(f"{name}: ") for name in policy.credential_fields}
        except (EOFError, KeyboardInterrupt) as exc:
            raise AuthenticationError("authentication_cancelled", "credential enrollment was cancelled") from exc
        data = authentication.enroll(args.realm, args.account, values)
    elif args.auth_action == "remove":
        if not args.yes:
            raise AuthenticationError("confirmation_required", "credential removal requires --yes")
        data = authentication.remove(args.realm, args.account)
    else:  # pragma: no cover - argparse owns this boundary
        raise AuthenticationError("invalid_auth_request", "unsupported authentication action")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "auth":
            result = _auth_result(args)
        elif args.action == "collection":
            result = _collection_result(args)
        else:
            result = resolved(args.action, getattr(args, "owner", None))
    except (AuthenticationError, CollectionStoreError, PathConfigError) as exc:
        code = exc.code if isinstance(exc, (AuthenticationError, CollectionStoreError)) else "invalid_path_configuration"
        result = envelope(
            ok=False,
            action=(
                f"auth-{getattr(args, 'auth_action', 'unknown')}"
                if args.action == "auth"
                else (
                    f"collection-{getattr(args, 'collection_action', 'unknown')}"
                    if args.action == "collection"
                    else args.action
                )
            ),
            status="error",
            data={},
            error={"code": code, "message": str(exc)[:256]},
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


__all__ = ["main", "parser"]
