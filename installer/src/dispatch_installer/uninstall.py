"""Installation-aware user-scope uninstall with a non-mutating safety plan."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .browser_lock import (
    acquire_browser_generation_lock,
    assert_no_unresolved_browser_leases,
    release_browser_generation_lock,
)
from .layout import (
    InstallLayout,
    InstallerError,
    assert_installation_root_current,
    assert_user_owned_directory,
    atomic_json,
    installation_lock,
    pinned_installation_path,
    read_installation,
    read_json,
)
from .repository import canonical_record_has_remote_authority, local_checkout_matches_record
from .service import (
    legacy_service_unit_is_owned,
    plugin_service_ids,
    plugin_service_path,
    plugin_service_receipt_path,
    plugin_service_unit_is_owned,
    remove_legacy_user_service,
    remove_plugin_service,
    remove_user_service,
    restore_plugin_service_states,
    restore_systemd_service_state,
    service_unit_is_owned,
    status_plugin_service,
    systemd_service_state,
)
from .user_command import inspect_user_command, remove_user_command

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
AuthorityVerifier = Callable[[dict[str, object]], bool]
AuthorityIdentity = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    data: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class _DirectoryStage:
    original: Path
    staged: Path


def _snapshot_file(path: Path, *, maximum: int = 256 * 1024) -> _FileSnapshot | None:
    path = pinned_installation_path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise InstallerError("uninstall_file_unsafe", f"managed file is unsafe: {path}")
    details = path.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or details.st_nlink != 1 or details.st_size > maximum:
        raise InstallerError("uninstall_file_unsafe", f"managed file is unsafe: {path}")
    return _FileSnapshot(path.read_bytes(), stat.S_IMODE(details.st_mode))


def _restore_file(path: Path, snapshot: _FileSnapshot | None) -> None:
    path = pinned_installation_path(path)
    if snapshot is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise InstallerError("uninstall_rollback_failed", f"cannot remove unsafe rollback file: {path}")
            path.unlink()
        return
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, snapshot.mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(snapshot.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(snapshot.mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _directory_stage(path: Path) -> _DirectoryStage | None:
    path = pinned_installation_path(path)
    if not path.exists() and not path.is_symlink():
        return None
    details = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not path.is_dir()
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o022
    ):
        raise InstallerError("uninstall_path_unsafe", f"managed directory is unsafe: {path}")
    return _DirectoryStage(path, path.parent / f".{path.name}.uninstall-{uuid.uuid4().hex}")


def _stage_directory(stage: _DirectoryStage) -> None:
    os.replace(stage.original, stage.staged)


def _restore_directory(stage: _DirectoryStage) -> None:
    if not stage.staged.exists() and not stage.staged.is_symlink():
        return
    if stage.original.exists() or stage.original.is_symlink():
        raise InstallerError("uninstall_rollback_failed", f"rollback target already exists: {stage.original}")
    os.replace(stage.staged, stage.original)


def _discard_directory(stage: _DirectoryStage) -> None:
    if stage.staged.exists() and stage.staged.is_dir() and not stage.staged.is_symlink():
        shutil.rmtree(stage.staged)


def _browser_installation_record_is_valid(layout: InstallLayout) -> bool:
    try:
        payload = read_json(layout.browser_installation_record, maximum=16 * 1024)
    except InstallerError:
        return False
    expected = {
        "schema_version",
        "status",
        "playwright_version",
        "browser_family",
        "chromium_revision",
        "chromium_version",
        "cache",
        "contains_secrets",
    }
    return (
        set(payload) == expected
        and payload.get("schema_version") == 1
        and payload.get("status") == "active"
        and isinstance(payload.get("playwright_version"), str)
        and bool(payload.get("playwright_version"))
        and payload.get("browser_family") == "chromium"
        and isinstance(payload.get("chromium_revision"), str)
        and bool(payload.get("chromium_revision"))
        and (
            payload.get("chromium_version") is None
            or (isinstance(payload.get("chromium_version"), str) and bool(payload.get("chromium_version")))
        )
        and payload.get("cache") == str(layout.browser_cache)
        and payload.get("contains_secrets") is False
    )


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
    *,
    purge: bool = False,
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
    else:
        receipt_valid = _uninstall_receipt_is_valid(layout)
        if receipt_valid:
            managed_after_receipt = (
                layout.clone,
                layout.venv,
                layout.cache,
                layout.run,
                layout.dispatch_home / ".install-tmp",
                layout.command_path,
                layout.service_path,
            )
            if any(path.exists() or path.is_symlink() for path in managed_after_receipt):
                blockers.append("stale uninstall receipt cannot authorize removal of fresh managed assets")
        elif not legacy_service_unit_is_owned(layout):
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
            if details.st_uid != os.geteuid() or details.st_mode & 0o022:
                blockers.append(f"managed path is writable by group or other: {path}")

    if layout.dispatch_home.exists():
        details = layout.dispatch_home.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or details.st_mode & 0o077:
            blockers.append(f"DISPATCH_HOME is not private and user-owned: {layout.dispatch_home}")

    if layout.installation_record.is_symlink() or (
        layout.installation_record.exists() and not layout.installation_record.is_file()
    ):
        blockers.append(f"installation record is unsafe: {layout.installation_record}")
    try:
        assert_user_owned_directory(layout.browser_installation_record.parent, "browser installation state")
    except InstallerError as exc:
        blockers.append(str(exc))
    if layout.browser_installation_record.is_symlink() or (
        layout.browser_installation_record.exists() and not layout.browser_installation_record.is_file()
    ):
        blockers.append(f"browser installation record is unsafe: {layout.browser_installation_record}")
    elif layout.browser_installation_record.exists() and not _browser_installation_record_is_valid(layout):
        blockers.append(f"browser installation record is invalid: {layout.browser_installation_record}")
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

    for plugin_id in _plugin_service_ids(layout):
        unit = plugin_service_path(layout, plugin_id)
        receipt = plugin_service_receipt_path(layout, plugin_id)
        status = status_plugin_service(layout, plugin_id)
        if status.get("status") == "unsafe":
            blockers.append(f"plugin service projection is unsafe: {plugin_id}")
        if unit.exists() or unit.is_symlink():
            if not plugin_service_unit_is_owned(layout, plugin_id):
                blockers.append(f"plugin service unit is not Dispatch-owned: {unit}")
        if receipt.is_symlink() or (receipt.exists() and not receipt.is_file()):
            blockers.append(f"plugin service receipt is unsafe: {receipt}")

    if purge:
        for path in _external_private_roots(layout):
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_dir():
                blockers.append(f"purge root is unsafe: {path}")
                continue
            details = path.stat(follow_symlinks=False)
            if details.st_uid != os.geteuid() or details.st_mode & 0o077:
                blockers.append(f"purge root is not private and user-owned: {path}")

    return sorted(set(blockers))


def _unlink_browser_installation_record(layout: InstallLayout) -> None:
    path = layout.browser_installation_record
    if not path.exists() and not path.is_symlink():
        return
    if not _browser_installation_record_is_valid(layout):
        raise InstallerError("browser_record_unsafe", "browser installation record is invalid")
    assert_user_owned_directory(path.parent, "browser installation state")
    details = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise InstallerError("browser_record_unsafe", "browser installation record is unsafe")
    path.unlink()


def _external_private_roots(layout: InstallLayout) -> list[Path]:
    roots = (layout.config, layout.secrets, layout.data, layout.state, layout.cache, layout.logs, layout.run)
    return [path for path in roots if path != layout.dispatch_home and layout.dispatch_home not in path.parents]


def _plugin_service_ids(layout: InstallLayout) -> list[str]:
    return sorted(plugin_service_ids(layout))


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
        *(plugin_service_path(layout, plugin_id) for plugin_id in _plugin_service_ids(layout)),
    ]
    plugin_receipts = [plugin_service_receipt_path(layout, plugin_id) for plugin_id in _plugin_service_ids(layout)]
    remove = [
        layout.clone,
        layout.venv,
        layout.cache,
        layout.run,
        layout.installation_record,
        layout.browser_installation_record,
        layout.dispatch_home / ".install-tmp",
        *plugin_receipts,
        *external,
    ]
    preserve_names = ["config", "secrets", "data", "state", "logs"]
    preserve = [getattr(layout, name) for name in preserve_names]
    if purge:
        remove = [layout.dispatch_home, *_external_private_roots(layout), *external]
        preserve = []
    present = [path for path in remove if path.exists() or path.is_symlink()]
    authority = _verified_authority(layout, verify_authority)
    blockers = _uninstall_blockers(layout, authority, purge=purge)
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
    blockers = _uninstall_blockers(layout, authority, purge=purge)
    if blockers:
        raise InstallerError("uninstall_blocked", "; ".join(blockers))

    with installation_lock(layout, prepare=False):
        assert_installation_root_current(layout)
        locked_blockers = _uninstall_blockers(layout, authority, purge=purge)
        if locked_blockers:
            raise InstallerError("uninstall_blocked", "; ".join(locked_blockers))
        service_present = layout.service_path.exists() or layout.service_path.is_symlink()
        main_service_state = (
            systemd_service_state("dispatch.service", run=run)
            if service_present
            else {"active": False, "enabled": False}
        )
        plugin_ids = _plugin_service_ids(layout)
        plugin_states: list[dict[str, object]] = []
        for plugin_id in plugin_ids:
            status = status_plugin_service(layout, plugin_id, run=run)
            if status.get("status") == "unsafe":
                raise InstallerError("plugin_service_unsafe", "plugin service state is unsafe")
            plugin_states.append(
                {
                    "plugin_id": plugin_id,
                    "active": status.get("active") is True,
                    "enabled": status.get("enabled") is True,
                }
            )
        files = {
            layout.command_path: _snapshot_file(layout.command_path),
            layout.service_path: _snapshot_file(layout.service_path),
            layout.service_directory / "dispatch-core.service": _snapshot_file(
                layout.service_directory / "dispatch-core.service"
            ),
            layout.state / "install" / "service.json": _snapshot_file(
                layout.state / "install" / "service.json"
            ),
            layout.installation_record: _snapshot_file(layout.installation_record),
            layout.browser_installation_record: _snapshot_file(layout.browser_installation_record),
            layout.state / "uninstall.json": _snapshot_file(layout.state / "uninstall.json"),
        }
        for plugin_id in plugin_ids:
            files[plugin_service_path(layout, plugin_id)] = _snapshot_file(
                plugin_service_path(layout, plugin_id)
            )
            files[plugin_service_receipt_path(layout, plugin_id)] = _snapshot_file(
                plugin_service_receipt_path(layout, plugin_id)
            )
        legacy_present = files[layout.service_directory / "dispatch-core.service"] is not None
        generation_lock: int | None = None
        service_stopped = False
        try:
            if service_present:
                service_stopped = True
                stopped = run(("systemctl", "--user", "stop", "dispatch.service"), None)
                if stopped.returncode != 0:
                    raise InstallerError("service_stop_failed", "Dispatch service could not be stopped before uninstall")
            assert_installation_root_current(layout)
            generation_lock = acquire_browser_generation_lock(layout)
            assert_no_unresolved_browser_leases(layout)
        except BaseException:
            release_browser_generation_lock(generation_lock)
            generation_lock = None
            if service_stopped:
                try:
                    restore_systemd_service_state(
                        "dispatch.service",
                        main_service_state,
                        run=run,
                    )
                except BaseException as restart_exc:
                    raise InstallerError(
                        "service_rollback_failed",
                        "uninstall was blocked and the Dispatch service could not be restored",
                    ) from restart_exc

            raise
        stages: list[_DirectoryStage] = []
        committed = False
        result: dict[str, object] | None = None
        try:
            assert_installation_root_current(layout)
            if service_present:
                remove_user_service(layout, run=run)
            remove_legacy_user_service(layout, run=run)
            for plugin_id in plugin_ids:
                remove_plugin_service(layout, plugin_id, run=run)
            if layout.command_path.exists() or layout.command_path.is_symlink():
                remove_user_command(layout)

            if purge:
                targets = [*_external_private_roots(layout), layout.dispatch_home]
            else:
                targets = [layout.clone, layout.venv, layout.cache, layout.run, layout.dispatch_home / ".install-tmp"]
            for target in targets:
                assert_installation_root_current(layout)
                staged = _directory_stage(target)
                if staged is not None:
                    stages.append(staged)
                    _stage_directory(staged)

            if purge:
                removed = [layout.dispatch_home, *_external_private_roots(layout)]
                result = {
                    "schema_version": 1,
                    "status": "purged",
                    "mode": "purge",
                    "remove": sorted(str(path) for path in removed),
                    "preserve": [],
                    "system_dependencies": "preserved-shared",
                    "hermes": "untouched",
                    "blockers": [],
                }
            else:
                pinned_installation_path(layout.installation_record).unlink(missing_ok=True)
                _unlink_browser_installation_record(layout)
                atomic_json(
                    pinned_installation_path(layout.state / "uninstall.json"),
                    {
                        "schema_version": 1,
                        "status": "uninstalled",
                        "dispatch_home": str(layout.dispatch_home),
                        "contains_secrets": False,
                    },
                )
            committed = True
        except BaseException as mutation_error:
            rollback_failure: BaseException | None = None
            for stage in reversed(stages):
                try:
                    _restore_directory(stage)
                except BaseException as exc:
                    rollback_failure = rollback_failure or exc
            for path, snapshot in files.items():
                try:
                    _restore_file(path, snapshot)
                except BaseException as exc:
                    rollback_failure = rollback_failure or exc
            try:
                reloaded = run(("systemctl", "--user", "daemon-reload"), None)
                if reloaded.returncode != 0:
                    raise InstallerError("service_rollback_failed", "systemd reload failed during uninstall rollback")
                if service_present:
                    restore_systemd_service_state(
                        "dispatch.service",
                        main_service_state,
                        run=run,
                    )
                elif legacy_present:
                    restarted = run(("systemctl", "--user", "enable", "--now", "dispatch-core.service"), None)
                    if restarted.returncode != 0:
                        raise InstallerError("service_rollback_failed", "legacy service restart failed during rollback")
                restore_plugin_service_states(layout, plugin_states, run=run)
            except BaseException as exc:
                rollback_failure = rollback_failure or exc
            if rollback_failure is not None:
                raise InstallerError(
                    "uninstall_rollback_failed",
                    "uninstall failed and the previous installation could not be fully restored",
                ) from mutation_error
            raise
        finally:
            release_browser_generation_lock(generation_lock)
        if committed:
            cleanup_failure: BaseException | None = None
            for stage in stages:
                try:
                    _discard_directory(stage)
                except BaseException as exc:
                    cleanup_failure = cleanup_failure or exc
            if cleanup_failure is not None:
                raise InstallerError(
                    "uninstall_cleanup_failed",
                    "uninstall committed but staged managed files could not be fully removed; manual cleanup is required",
                ) from cleanup_failure
        if result is not None:
            return result
    result = plan_uninstall(layout, purge=False, verify_authority=verify_authority)
    result["status"] = "uninstalled"
    return result


__all__ = ["plan_uninstall", "uninstall"]
