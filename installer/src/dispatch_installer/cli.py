from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError, installation_lock
from .manifest import load_manifest
from .uninstall import plan_uninstall, uninstall as apply_uninstall


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dispatch-installer")
    parser.add_argument("--dispatch-home", help="absolute per-user Dispatch root; defaults to $HOME/.dispatch")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("layout", help="print the resolved installation layout without changing it")
    prepare = subparsers.add_parser("prepare", help="create the private per-user directory layout")
    prepare.add_argument("--yes", action="store_true", help="confirm directory creation")

    subparsers.add_parser("doctor", help="inspect installation state without changing it")
    subparsers.add_parser("verify", help="require a complete verified installation without changing it")
    plan = subparsers.add_parser("plan", help="validate a digest-pinned installation manifest")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--manifest-sha256", required=True)
    uninstall = subparsers.add_parser("uninstall", help="plan or apply a receipt-bound user-scope uninstall")
    uninstall.add_argument("--plan", action="store_true", help="show the uninstall plan without changing files")
    uninstall.add_argument("--purge", action="store_true", help="also remove configuration and durable data")
    uninstall.add_argument("--yes", action="store_true", help="confirm the uninstall operation")
    return parser


def _emit(ok: bool, action: str, status: str, data: dict, error: dict | None = None) -> None:
    print(
        json.dumps(
            {
                "ok": ok,
                "action": action,
                "status": status,
                "data": data,
                "error": error,
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        layout = InstallLayout.from_environment(dispatch_home=args.dispatch_home)
        if args.action == "layout":
            _emit(True, "layout", "ready", {"layout": layout.as_dict()})
            return 0
        if args.action == "prepare":
            if not args.yes:
                raise InstallerError("confirmation_required", "prepare requires --yes")
            with installation_lock(layout, prepare_layout=True):
                prepared = layout.as_dict()
            _emit(True, "prepare", "ready", {"layout": prepared})
            return 0

        if args.action in {"doctor", "verify"}:
            result = inspect_installation(layout)
            _emit(result["ok"], args.action, result["status"], result)
            return 0 if result["ok"] else 1
        if args.action == "plan":
            manifest = load_manifest(args.manifest, expected_sha256=args.manifest_sha256)
            status = "ready" if manifest.ready else "blocked"
            _emit(manifest.ready, "plan", status, {"manifest": asdict(manifest)})
            return 0 if manifest.ready else 2
        if args.action == "uninstall":
            if args.plan and args.yes:
                raise InstallerError("uninstall_arguments", "--plan and --yes cannot be combined")
            if args.plan:
                result = plan_uninstall(layout, purge=args.purge)
                blocked = bool(result["blockers"])
                _emit(not blocked, "uninstall", str(result["status"]), result)
                return 2 if blocked else 0
            if not args.yes:
                raise InstallerError("confirmation_required", "uninstall requires --yes or --plan")
            result = apply_uninstall(layout, purge=args.purge)
            blocked = bool(result["blockers"])
            _emit(not blocked, "uninstall", str(result["status"]), result)
            return 2 if blocked else 0
        raise InstallerError("action_unknown", "unknown installer action")
    except InstallerError as exc:
        _emit(False, args.action, "error", {}, {"code": exc.code, "message": str(exc)[:512]})
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit(False, args.action, "error", {}, {"code": "installer_failed", "message": str(exc)[:512]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
