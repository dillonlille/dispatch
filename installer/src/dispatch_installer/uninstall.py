"""Installation-aware user-scope uninstall with a non-mutating safety plan."""
from __future__ import annotations

import os
import shutil

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .layout import (
    InstallLayout,
    InstallerError,
    assert_user_owned_directory,
    atomic_json,
    installation_lock,
    read_installation,
    read_json,
)
from .repository import canonical_record_has_remote_authority, local_checkout_matches_record
from .service import (
    legacy_service_unit_is_owned,
    remove_legacy_user_service,
    remove_user_service,
    service_unit_is_owned,
)
from .user_command import inspect_user_command, remove_user_command

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
AuthorityVerifier = Callable[[dict[str, object]], bool]
AuthorityIdentity = tuple[str, str, str]


def _uninstall_receipt_is_valid(layout: InstallLayout) -> bool:
    try:
        payload = read_json(layout.state / "uninstall.json", maximum=16 * 1024)
    except InstallerError:
        return False
    return (
        set(payload) == {"schema_version", "status", "dispatch_home", "contains_secrets"}
        and payload.get("schema_version") == 1
        and payload.get("status") == "uninstalled"
        and payload.get("dispatch_home") == str(layout.dispatch_home)
        and payload.get("contains_secrets") is False
    )


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _verified_authority(
    layout: InstallLayout,
    verify_authority: AuthorityVerifier,
) -> AuthorityIdentity | None:
    if not layout.installation_record.exists() or layout.installation_record.is_symlink():
        return None
    try:
        record = read_installation(layout)
        if record is None or not local_checkout_matches_record(layout.clone, record):
            return None
        if not verify_authority(record):
            return None
    except (InstallerError, OSError):
        return None
    return (str(record["channel"]), str(record["ref"]), str(record["commit"]))


def _uninstall_blockers(
    layout: InstallLayout,
    authority: AuthorityIdentity | None,
) -> list[str]:
    blockers: list[str] = []
    if not layout.dispatch_home.exists() and not layout.dispatch_home.is_symlink():
        return blockers
    record_present = layout.installation_record.exists() or layout.installation_record.is_symlink()
    if record_present:
        try:
            record = read_installation(layout)
        except InstallerError as exc:
            blockers.append(str(exc))
        else:
            identity = None if record is None else (
                str(record["channel"]),
                str(record["ref"]),
                str(record["commit"]),
            )
            if (
                record is None
                or not local_checkout_matches_record(layout.clone, record)
                or identity != authority
            ):
                blockers.append("Dispatch installation provenance does not match canonical repository authority")
    elif not _uninstall_receipt_is_valid(layout) and not legacy_service_unit_is_owned(layout):
        blockers.append("Dispatch installation provenance is missing")
    for directory, label in (
        (layout.command_path.parent, "launcher directory"),
        (layout.service_directory, "service directory"),
    ):
        try:
            assert_user_owned_directory(directory, label)
        except InstallerError as exc:
            blockers.append(str(exc))

    managed_directories = (
        layout.clone,
        layout.venv,
        layout.cache,
        layout.run,
        layout.dispatch_home / ".install-tmp",
    )
    for path in managed_directories:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            blockers.append(f"managed path is unsafe: {path}")
        elif path.exists():
            details = path.stat(follow_symlinks=False)
            if details.st_uid != os.geteuid():
                blockers.append(f"managed path is not user-owned: {path}")

    if layout.dispatch_home.exists():
        details = layout.dispatch_home.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or details.st_mode & 0o077:
            blockers.append(f"DISPATCH_HOME is not private and user-owned: {layout.dispatch_home}")

    if layout.installation_record.is_symlink() or (
        layout.installation_record.exists() and not layout.installation_record.is_file()
    ):
        blockers.append(f"installation record is unsafe: {layout.installation_record}")
    if layout.lock_path.is_symlink() or (layout.lock_path.exists() and not layout.lock_path.is_file()):
        blockers.append(f"installation lock is unsafe: {layout.lock_path}")

    if layout.command_path.exists() or layout.command_path.is_symlink():
        if inspect_user_command(layout).get("status") != "ready":
            blockers.append(f"launcher is not Dispatch-owned: {layout.command_path}")

    if layout.service_path.exists() or layout.service_path.is_symlink():
        if not service_unit_is_owned(layout):
            blockers.append(f"service unit is not Dispatch-owned: {layout.service_path}")

    legacy_service = layout.service_directory / "dispatch-core.service"
    if legacy_service.exists() or legacy_service.is_symlink():
        if not legacy_service_unit_is_owned(layout):
            blockers.append(f"legacy service unit is not Dispatch-owned: {legacy_service}")

    return sorted(set(blockers))


def plan_uninstall(
    layout: InstallLayout,
    *,
    purge: bool = False,
    verify_authority: AuthorityVerifier = canonical_record_has_remote_authority,
) -> dict[str, object]:
    external = [
        layout.command_path,
        layout.service_path,
        layout.service_directory / "dispatch-core.service",
    ]
    remove = [
        layout.clone,
        layout.venv,
        layout.cache,
        layout.run,
        layout.installation_record,
        layout.lock_path,
        layout.dispatch_home / ".install-tmp",
        *external,
    ]
    preserve_names = ["config", "secrets", "data", "state", "logs"]
    preserve = [getattr(layout, name) for name in preserve_names]
    if purge:
        remove = [layout.dispatch_home, *external]
        preserve = []
    present = [path for path in remove if path.exists() or path.is_symlink()]
    authority = _verified_authority(layout, verify_authority)
    blockers = _uninstall_blockers(layout, authority)
    return {
        "schema_version": 1,
        "status": "blocked" if blockers else ("planned" if present else "already-absent"),
        "mode": "purge" if purge else "keep-data",
        "remove": sorted(str(path) for path in present),
        "preserve": sorted(str(path) for path in preserve if path.exists()),
        "system_dependencies": "preserved-shared",
        "hermes": "untouched",
        "blockers": blockers,
    }


def uninstall(
    layout: InstallLayout,
    *,
    purge: bool = False,
    run: RunCommand = _run,
    verify_authority: AuthorityVerifier = canonical_record_has_remote_authority,
) -> dict[str, object]:
    if not layout.dispatch_home.exists() and not layout.dispatch_home.is_symlink():
        return plan_uninstall(layout, purge=purge, verify_authority=verify_authority)
    if layout.dispatch_home.is_symlink() or not layout.dispatch_home.is_dir():
        raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME is unsafe")

    authority = _verified_authority(layout, verify_authority)
    blockers = _uninstall_blockers(layout, authority)
    if blockers:
        raise InstallerError("uninstall_blocked", "; ".join(blockers))

    with installation_lock(layout):
        locked_blockers = _uninstall_blockers(layout, authority)
        if locked_blockers:
            raise InstallerError("uninstall_blocked", "; ".join(locked_blockers))
        if layout.service_path.exists() or layout.service_path.is_symlink():
            remove_user_service(layout, run=run)
        remove_legacy_user_service(layout, run=run)
        if layout.command_path.exists() or layout.command_path.is_symlink():
            remove_user_command(layout)
        if purge:
            shutil.rmtree(layout.dispatch_home)
            return {
                "schema_version": 1,
                "status": "purged",
                "mode": "purge",
                "remove": [str(layout.dispatch_home)],
                "preserve": [],
                "system_dependencies": "preserved-shared",
                "hermes": "untouched",
                "blockers": [],
            }
        for path in (layout.clone, layout.venv, layout.cache, layout.run, layout.dispatch_home / ".install-tmp"):
            if path.exists():
                shutil.rmtree(path)
        layout.installation_record.unlink(missing_ok=True)
        atomic_json(
            layout.state / "uninstall.json",
            {
                "schema_version": 1,
                "status": "uninstalled",
                "dispatch_home": str(layout.dispatch_home),
                "contains_secrets": False,
            },
        )
        layout.lock_path.unlink(missing_ok=True)
    result = plan_uninstall(layout, purge=False, verify_authority=verify_authority)
    result["status"] = "uninstalled"
    return result


__all__ = ["plan_uninstall", "uninstall"]
