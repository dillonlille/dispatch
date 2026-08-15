from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .application import install_core_application
from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError, installation_lock
from .manifest import load_manifest
from .service import install_user_service
from .setup import persist_release_manifest
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
    install = subparsers.add_parser("install", help="install a reviewed ready Core release")
    install.add_argument("--manifest", type=Path, required=True)
    install.add_argument("--manifest-sha256", required=True)
    install.add_argument("--core-wheel", type=Path, required=True)
    install.add_argument("--yes", action="store_true", help="confirm Core installation")
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
        if args.action == "install":
            if not args.yes:
                raise InstallerError("confirmation_required", "install requires --yes")
            manifest = load_manifest(args.manifest, expected_sha256=args.manifest_sha256)
            if not manifest.ready:
                raise InstallerError("release_not_ready", "installation manifest is not ready")
            if manifest.installer_version != __version__:
                raise InstallerError("installer_version_mismatch", "running installer version differs from release authority")
            if manifest.core_artifact.size is None or args.core_wheel.stat().st_size != manifest.core_artifact.size:
                raise InstallerError("core_artifact_size", "Core artifact size differs from release authority")
            result = install_core_application(
                layout,
                args.core_wheel,
                expected_sha256=str(manifest.core_artifact.sha256),
                expected_version=manifest.core_version,
                expected_package_files=dict(manifest.core_package_files),
                expected_requires_dist=manifest.core_requires_dist,
                launcher_python=Path(sys.executable),
            )
            persist_release_manifest(
                layout,
                args.manifest,
                expected_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
            )
            result["service"] = install_user_service(layout, Path(str(result["launcher"])))
            result["product_version"] = manifest.product_version
            result["builtin_plugins"] = [asdict(plugin) for plugin in manifest.builtin_plugins]
            _emit(True, "install", "installed", result)
            return 0
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
