from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .application import install_core_application
from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError, atomic_json, installation_lock
from .manifest import load_manifest
from .service import install_user_service
from .setup import persist_release_manifest
from .uninstall import plan_uninstall, uninstall as apply_uninstall


def _record_install_phase(
    layout: InstallLayout,
    *,
    manifest_sha256: str,
    product_version: str,
    phase: str,
) -> None:
    atomic_json(
        layout.state / "install" / "install-transaction.json",
        {
            "schema_version": 1,
            "manifest_sha256": manifest_sha256,
            "product_version": product_version,
            "phase": phase,
            "contains_secrets": False,
        },
        mode=0o600,
    )


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
            layout.prepare()
            transaction_path = layout.state / "install" / "install-transaction.json"
            if transaction_path.exists() or transaction_path.is_symlink():
                if transaction_path.is_symlink() or not transaction_path.is_file():
                    raise InstallerError("install_transaction_invalid", "install transaction receipt is unsafe")
                transaction_details = transaction_path.stat()
                if (
                    transaction_details.st_uid != os.geteuid()
                    or transaction_details.st_nlink != 1
                    or stat.S_IMODE(transaction_details.st_mode) != 0o600
                    or transaction_details.st_size > 16 * 1024
                ):
                    raise InstallerError("install_transaction_invalid", "install transaction receipt is unsafe")
                try:
                    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise InstallerError("install_transaction_invalid", "install transaction receipt is invalid") from exc
                if (
                    not isinstance(transaction, dict)
                    or set(transaction)
                    != {
                        "schema_version",
                        "manifest_sha256",
                        "product_version",
                        "phase",
                        "contains_secrets",
                    }
                    or transaction.get("schema_version") != 1
                    or transaction.get("contains_secrets") is not False
                    or not isinstance(transaction.get("manifest_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", transaction["manifest_sha256"]) is None
                    or not isinstance(transaction.get("product_version"), str)
                    or transaction.get("phase")
                    not in {"started", "core_active", "manifest_persisted", "service_active", "complete"}
                ):
                    raise InstallerError("install_transaction_invalid", "install transaction receipt is invalid")
                if transaction["phase"] != "complete" and transaction.get("manifest_sha256") != args.manifest_sha256:
                    raise InstallerError(
                        "install_transaction_incomplete",
                        "a different incomplete installation must be repaired before another release is installed",
                    )
            _record_install_phase(
                layout,
                manifest_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
                phase="started",
            )
            result = install_core_application(
                layout,
                args.core_wheel,
                expected_sha256=str(manifest.core_artifact.sha256),
                expected_version=manifest.core_version,
                expected_package_files=dict(manifest.core_package_files),
                expected_requires_dist=manifest.core_requires_dist,
                launcher_python=Path(sys.executable),
            )
            _record_install_phase(
                layout,
                manifest_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
                phase="core_active",
            )
            persist_release_manifest(
                layout,
                args.manifest,
                expected_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
            )
            _record_install_phase(
                layout,
                manifest_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
                phase="manifest_persisted",
            )
            result["service"] = install_user_service(layout, Path(str(result["launcher"])))
            _record_install_phase(
                layout,
                manifest_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
                phase="service_active",
            )
            verification = inspect_installation(layout)
            if not verification["ok"]:
                raise InstallerError("installation_verification_failed", "installed release did not pass verification")
            result["verification"] = verification
            _record_install_phase(
                layout,
                manifest_sha256=args.manifest_sha256,
                product_version=manifest.product_version,
                phase="complete",
            )
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
