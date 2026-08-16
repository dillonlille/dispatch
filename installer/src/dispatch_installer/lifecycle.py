"""Transactional clone and environment lifecycle operations."""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .layout import (
    InstallLayout,
    InstallerError,
    assert_directory_ancestors,
    assert_user_owned_directory,
    atomic_json,
    installation_lock,
    read_installation,
)
from .repository import (
    REPOSITORY_URL,
    assert_checkout_clean,
    checkout_existing,
    clone_repository,
    current_commit,
    resolve_latest_release,
    resolve_published_release,
    run_command,
    verify_checkout_authority,
)
from .service import (
    install_user_service,
    legacy_service_unit_is_owned,
    remove_legacy_user_service,
    service_unit_is_owned,
    stop_legacy_user_service,
)
from .setup import install_editable_source, migrate_legacy_plugin_config
from .user_command import install_user_command

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
RollbackResult = TypeVar("RollbackResult")


def _checked(run: RunCommand, command: Sequence[str], cwd: Path | None, code: str, message: str) -> None:
    if run(command, cwd).returncode != 0:
        raise InstallerError(code, message)


def _selected_plugins(layout: InstallLayout) -> list[str]:
    path = layout.config / "plugins.json"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("plugin_config_invalid", "plugin configuration is unreadable") from exc
    selected = payload.get("selected_plugins", [])
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")
    return selected


def ensure_venv(
    layout: InstallLayout,
    *,
    destination: Path | None = None,
    browser_cache: Path | None = None,
    run: RunCommand = run_command,
) -> Path:
    """Build a complete replacement environment without mutating the active one."""

    target = destination or layout.venv

    def private_run(command: Sequence[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
        previous_umask = os.umask(0o077)
        try:
            return run(command, cwd)
        finally:
            os.umask(previous_umask)

    if target.exists() or target.is_symlink():
        raise InstallerError("venv_target_exists", "replacement virtual environment target already exists")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)

    _checked(
        private_run,
        (sys.executable, "-m", "venv", str(target)),
        None,
        "venv_create_failed",
        "could not create Dispatch virtual environment",
    )
    python = target / "bin" / "python"
    if not python.is_file():
        raise InstallerError("venv_python_missing", "replacement virtual environment has no safe Python executable")
    python_details = python.lstat()
    if python.is_symlink():
        if python_details.st_uid != os.geteuid() or python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
            raise InstallerError("venv_python_unsafe", "replacement virtual environment has an unsafe Python symlink")
    elif (
        not stat.S_ISREG(python_details.st_mode)
        or python_details.st_uid != os.geteuid()
        or python_details.st_nlink != 1
    ):
        raise InstallerError("venv_python_unsafe", "replacement virtual environment has an unsafe Python executable")
    if not os.access(python, os.X_OK):
        raise InstallerError("venv_python_unsafe", "replacement virtual environment Python is not executable")

    requirements = layout.clone / "dispatch-core" / "requirements.txt"
    if not requirements.is_file() or requirements.is_symlink():
        raise InstallerError("requirements_missing", "Core runtime requirements are missing")
    _checked(
        private_run,
        (str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)),
        None,
        "core_dependencies_failed",
        "could not install Core dependencies",
    )
    if install_editable_source(
        python,
        layout.installer_source,
        no_deps=True,
        run=private_run,
    ).returncode != 0:
        raise InstallerError("installer_install_failed", "could not install the source installer adapter")

    for plugin_id in _selected_plugins(layout):
        source = layout.clone / "plugins" / plugin_id
        if not source.is_dir() or source.is_symlink():
            raise InstallerError("selected_plugin_missing", f"selected plugin source is missing: {plugin_id}")
        if install_editable_source(python, source, run=private_run).returncode != 0:
            raise InstallerError("plugin_install_failed", f"could not install selected plugin: {plugin_id}")

    browser_cache = browser_cache or layout.cache / "browser"
    if browser_cache.exists() or browser_cache.is_symlink():
        if browser_cache.is_symlink() or not browser_cache.is_dir():
            raise InstallerError("browser_cache_unsafe", "browser cache target is unsafe")
    else:
        browser_cache.mkdir(mode=0o700, parents=True)
    browser_cache.chmod(0o700)
    _checked(
        private_run,
        (str(python), "-m", "playwright", "install-deps", "chromium"),
        None,
        "browser_system_dependencies_failed",
        "could not install Playwright Chromium system dependencies",
    )
    _checked(
        private_run,
        (
            "env",
            f"PLAYWRIGHT_BROWSERS_PATH={browser_cache}",
            str(python),
            "-m",
            "playwright",
            "install",
            "chromium",
        ),
        None,
        "browser_install_failed",
        "could not install user-owned Playwright Chromium",
    )
    return target


def _installation_record(
    layout: InstallLayout,
    *,
    channel: str,
    ref: str,
    commit: str,
    now: Callable[[], datetime],
) -> dict[str, object]:
    timestamp = now().astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "repository": REPOSITORY_URL,
        "channel": channel,
        "ref": ref,
        "commit": commit,
        "checkout": str(layout.clone),
        "venv": str(layout.venv),
        "paths": layout.as_dict(),
        "updated_at": timestamp,
        "contains_secrets": False,
    }


def resolve_ref(channel: str, version: str | None, *, opener: Any = None) -> str:
    if channel == "dev":
        if version:
            raise InstallerError("dev_version_invalid", "--version is only valid for the stable channel")
        return "dev"
    if channel != "stable":
        raise InstallerError("channel_invalid", "channel must be stable or dev")
    if version is None:
        return resolve_latest_release() if opener is None else resolve_latest_release(opener=opener)
    return resolve_published_release(version) if opener is None else resolve_published_release(version, opener=opener)


def _safe_remove(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    details = path.lstat()
    if details.st_uid != os.geteuid():
        raise InstallerError("managed_path_unsafe", f"managed path is not user-owned: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        if details.st_nlink != 1:
            raise InstallerError("managed_path_unsafe", f"managed file has unsafe hard links: {path}")
        path.unlink()
    else:
        raise InstallerError("managed_path_unsafe", f"managed path is unsafe: {path}")


def _snapshot_file(path: Path) -> tuple[bytes, int] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid():
        raise InstallerError("managed_file_unsafe", f"managed file is unsafe: {path}")
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_file(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise InstallerError("rollback_file_unsafe", f"cannot remove unsafe rollback file: {path}")
            path.unlink()
        return
    data, mode = snapshot
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _swap_directory(replacement: Path, target: Path) -> Path | None:
    if replacement.is_symlink() or not replacement.is_dir():
        raise InstallerError("replacement_unsafe", f"replacement directory is unsafe: {replacement}")
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.geteuid():
            raise InstallerError("managed_directory_unsafe", f"managed directory is unsafe: {target}")
        backup = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
    try:
        if backup is not None:
            os.replace(target, backup)
        os.replace(replacement, target)
        target.chmod(0o700)
    except BaseException:
        def restore_swap() -> None:
            if backup is None:
                if target.exists() or target.is_symlink():
                    _safe_remove(target)
                if replacement.exists() or replacement.is_symlink():
                    _safe_remove(replacement)
                return
            if backup.exists() and (target.exists() or target.is_symlink()):
                if replacement.exists() or replacement.is_symlink():
                    raise InstallerError(
                        "directory_swap_state_unsafe",
                        "both the active target and replacement exist during rollback",
                    )
                os.replace(target, replacement)
            if backup.exists() and not target.exists() and not target.is_symlink():
                os.replace(backup, target)
            if replacement.exists() or replacement.is_symlink():
                _safe_remove(replacement)
            if backup.exists() or not target.is_dir():
                raise InstallerError("directory_swap_state_unsafe", "directory promotion rollback is incomplete")

        rollback_error: BaseException | None = None
        for _attempt in range(2):
            try:
                _complete_rollback(restore_swap)
                rollback_error = None
                break
            except BaseException as exc:
                rollback_error = exc
        if rollback_error is not None:
            raise InstallerError("directory_swap_rollback_failed", "directory promotion rollback failed") from rollback_error
        raise
    return backup


def _exchange_directories(left: Path, right: Path) -> None:
    """Atomically exchange two active directory names without an absent-target window."""
    if left.parent != right.parent:
        raise InstallerError("directory_restore_state_unsafe", "directory exchange requires sibling paths")
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise InstallerError("directory_restore_unsupported", "atomic directory exchange is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    if renameat2(
        at_fdcwd,
        os.fsencode(left),
        at_fdcwd,
        os.fsencode(right),
        rename_exchange,
    ) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(left), str(right))


def _restore_directory(target: Path, backup: Path | None) -> None:
    displaced = target.parent / f".{target.name}.failed-{uuid.uuid4().hex}"
    if backup is None:
        if target.exists() or target.is_symlink():
            os.replace(target, displaced)
        return
    if not backup.exists() and target.is_dir() and not target.is_symlink():
        return
    if backup.is_symlink() or not backup.is_dir():
        raise InstallerError("directory_restore_state_unsafe", "directory restore backup is unsafe")

    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise InstallerError("directory_restore_state_unsafe", "active directory is unsafe")
        target_before = target.lstat()
        backup_before = backup.lstat()
        target_identity = (target_before.st_dev, target_before.st_ino)
        backup_identity = (backup_before.st_dev, backup_before.st_ino)
        exchange_error: BaseException | None = None
        try:
            _exchange_directories(target, backup)
        except BaseException as error:
            current_target = target.lstat() if target.exists() and not target.is_symlink() else None
            current_backup = backup.lstat() if backup.exists() and not backup.is_symlink() else None
            current_target_identity = (
                (current_target.st_dev, current_target.st_ino) if current_target is not None else None
            )
            current_backup_identity = (
                (current_backup.st_dev, current_backup.st_ino) if current_backup is not None else None
            )
            if (
                current_target_identity == backup_identity
                and current_backup_identity == target_identity
            ):
                exchange_error = error
            elif (
                current_target_identity == target_identity
                and current_backup_identity == backup_identity
            ):
                raise
            else:
                raise InstallerError(
                    "directory_restore_rollback_failed",
                    "atomic directory restore left an unknown state",
                ) from error

        cleanup_interruption: BaseException | None = None
        while backup.exists() or backup.is_symlink():
            try:
                os.replace(backup, displaced)
            except (KeyboardInterrupt, SystemExit) as error:
                if cleanup_interruption is None:
                    cleanup_interruption = error
                continue
        restored = target.lstat() if target.exists() and not target.is_symlink() else None
        if restored is None or (restored.st_dev, restored.st_ino) != backup_identity:
            raise InstallerError(
                "directory_restore_rollback_failed",
                "directory restore cleanup left the wrong active target",
            )
        if exchange_error is not None:
            raise exchange_error
        if cleanup_interruption is not None:
            raise cleanup_interruption
        return

    try:
        os.replace(backup, target)
    except BaseException:
        def recover_active() -> None:
            if target.is_dir() and not target.is_symlink():
                return
            if target.exists() or target.is_symlink():
                raise InstallerError("directory_restore_state_unsafe", "active restore path is unsafe")
            if backup.is_dir() and not backup.is_symlink():
                os.replace(backup, target)
            if not target.is_dir() or target.is_symlink():
                raise InstallerError("directory_restore_state_unsafe", "directory restore left no active target")

        recovery_error: BaseException | None = None
        for _attempt in range(2):
            try:
                _complete_rollback(recover_active)
                recovery_error = None
                break
            except BaseException as exc:
                recovery_error = exc
        if recovery_error is not None and not target.exists() and backup.is_dir() and not backup.is_symlink():
            try:
                shutil.copytree(backup, target, symlinks=True)
                if not target.is_dir() or target.is_symlink():
                    raise InstallerError("directory_restore_state_unsafe", "fallback restore is unsafe")
                recovery_error = None
            except BaseException as exc:
                if target.exists() or target.is_symlink():
                    _safe_remove(target)
                recovery_error = exc
        if recovery_error is not None:
            raise InstallerError("directory_restore_rollback_failed", "directory restore recovery failed") from recovery_error
        raise


def _complete_rollback(action: Callable[[], RollbackResult]) -> RollbackResult:
    """Defer terminal interrupts until one critical rollback step completes."""
    while True:
        try:
            return action()
        except (KeyboardInterrupt, SystemExit):
            continue


def _promote_clone(layout: InstallLayout, source: Path) -> Path | None:
    temporary_root = _prepare_temporary_root(layout).resolve(strict=True)
    try:
        candidate = source.resolve(strict=True)
        candidate.relative_to(temporary_root)
    except (OSError, ValueError) as exc:
        raise InstallerError(
            "clone_outside_staging",
            "staged clone must be inside the private installation staging directory",
        ) from exc
    if candidate.is_symlink() or not candidate.is_dir() or not (candidate / ".git").is_dir():
        raise InstallerError("clone_invalid", "staged source is not a safe Git checkout")
    return _swap_directory(candidate, layout.clone)


def _remove_legacy_code(layout: InstallLayout, *, setup_migrated: bool) -> None:
    if not setup_migrated:
        raise InstallerError("legacy_cleanup_unsafe", "legacy code cleanup requires a verified setup receipt")
    legacy_state = layout.state / "install"
    assert_user_owned_directory(legacy_state, "legacy installation state")
    for name in ("releases", "staging", "plugins", "bin", "installer-venv", "runtime"):
        _safe_remove(layout.dispatch_home / name)
    if setup_migrated:
        (legacy_state / "setup.json").unlink(missing_ok=True)
    try:
        legacy_state.rmdir()
    except OSError:
        pass


def _prepare_temporary_root(layout: InstallLayout) -> Path:
    temporary_root = layout.dispatch_home / ".install-tmp"
    if temporary_root.exists() or temporary_root.is_symlink():
        if (
            temporary_root.is_symlink()
            or not temporary_root.is_dir()
            or temporary_root.stat().st_uid != os.geteuid()
            or stat.S_IMODE(temporary_root.stat(follow_symlinks=False).st_mode) != 0o700
        ):
            raise InstallerError("staging_unsafe", "installation staging directory is unsafe")
    else:
        temporary_root.mkdir(mode=0o700)
    return temporary_root


def _build_replacement_venv(layout: InstallLayout, *, run: RunCommand) -> tuple[Path, Path, Path]:
    temporary_root = _prepare_temporary_root(layout)
    work = Path(tempfile.mkdtemp(prefix="venv-", dir=temporary_root))
    replacement = work / "venv"
    browser_replacement = work / "browser"
    try:
        ensure_venv(
            layout,
            destination=replacement,
            browser_cache=browser_replacement,
            run=run,
        )
    except BaseException:
        _safe_remove(work)
        raise
    return replacement, browser_replacement, work


def _activate(
    layout: InstallLayout,
    *,
    channel: str,
    ref: str,
    commit: str,
    run: RunCommand,
    now: Callable[[], datetime],
    status: str,
) -> dict[str, object]:
    assert_user_owned_directory(layout.command_path.parent, "launcher directory")
    assert_user_owned_directory(layout.service_directory, "service directory")
    plugin_config = layout.config / "plugins.json"
    legacy_path = layout.service_directory / "dispatch-core.service"
    legacy_present = legacy_path.exists() or legacy_path.is_symlink()
    legacy_owned = legacy_present and legacy_service_unit_is_owned(layout)
    if legacy_present and not legacy_owned:
        raise InstallerError("legacy_service_unsafe", "legacy service unit is not Dispatch-owned")
    plugin_snapshot = _snapshot_file(plugin_config)
    work: Path | None = None
    try:
        legacy_setup_migrated = migrate_legacy_plugin_config(layout)
        replacement, browser_replacement, work = _build_replacement_venv(layout, run=run)
        assert_checkout_clean(layout.clone, run=run)
        snapshots = {
            "command": (layout.command_path, _snapshot_file(layout.command_path)),
            "installation": (layout.installation_record, _snapshot_file(layout.installation_record)),
            "service": (layout.service_path, _snapshot_file(layout.service_path)),
            "service_record": (layout.state / "service.json", _snapshot_file(layout.state / "service.json")),
            "plugin_config": (plugin_config, plugin_snapshot),
        }
        if snapshots["service"][1] is not None and not service_unit_is_owned(layout):
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
    except BaseException:
        _restore_file(plugin_config, plugin_snapshot)
        if work is not None:
            _safe_remove(work)
        raise
    assert work is not None
    old_service_present = snapshots["service"][1] is not None
    venv_backup: Path | None = None
    venv_swapped = False
    browser_backup: Path | None = None
    browser_swapped = False
    try:
        if legacy_owned:
            stop_legacy_user_service(layout, run=run)
        browser_backup = _swap_directory(browser_replacement, layout.cache / "browser")
        browser_swapped = True
        venv_backup = _swap_directory(replacement, layout.venv)
        venv_swapped = True
        command = install_user_command(layout)
        payload = _installation_record(layout, channel=channel, ref=ref, commit=commit, now=now)
        atomic_json(layout.installation_record, payload)
        service = install_user_service(layout, run=run, activate=True)
    except BaseException as activation_error:
        rollback_failure: BaseException | None = None

        def attempt(action: Callable[[], object]) -> None:
            nonlocal rollback_failure
            try:
                result = _complete_rollback(action)
                if isinstance(result, subprocess.CompletedProcess) and result.returncode != 0:
                    raise InstallerError("rollback_command_failed", "a rollback command failed")
            except BaseException as exc:
                if rollback_failure is None:
                    rollback_failure = exc

        if service_unit_is_owned(layout):
            attempt(lambda: run(("systemctl", "--user", "disable", "--now", "dispatch.service"), None))
        if venv_swapped:
            attempt(lambda: _restore_directory(layout.venv, venv_backup))
        if browser_swapped:
            attempt(lambda: _restore_directory(layout.cache / "browser", browser_backup))
        for path, snapshot in snapshots.values():
            attempt(lambda path=path, snapshot=snapshot: _restore_file(path, snapshot))
        attempt(lambda: run(("systemctl", "--user", "daemon-reload"), None))
        if legacy_owned:
            attempt(lambda: run(("systemctl", "--user", "enable", "--now", "dispatch-core.service"), None))
        elif old_service_present:
            attempt(lambda: run(("systemctl", "--user", "restart", "dispatch.service"), None))
        try:
            _safe_remove(work)
        except BaseException:
            pass
        if rollback_failure is not None:
            raise InstallerError(
                "activation_rollback_failed",
                "activation failed and the prior generation could not be fully restored",
            ) from activation_error
        raise

    # Activation is committed. Cleanup failures, including user interruption,
    # must not escape to callers that would roll back only the checkout.
    for obsolete in (venv_backup, browser_backup, work):
        if obsolete is None:
            continue
        try:
            _safe_remove(obsolete)
        except BaseException:
            pass
    try:
        if legacy_present:
            remove_legacy_user_service(layout, run=run)
        if legacy_present and legacy_setup_migrated:
            _remove_legacy_code(layout, setup_migrated=True)
    except BaseException:
        # The new generation is active; failed legacy cleanup may leave only
        # obsolete files and must not trigger a checkout-only rollback.
        pass
    return {
        "schema_version": 1,
        "status": status,
        "channel": channel,
        "ref": ref,
        "commit": commit,
        "layout": layout.as_dict(),
        "command": command,
        "service": service,
        "hermes": "untouched",
    }


def install_from_clone(
    layout: InstallLayout,
    source: str | Path,
    *,
    channel: str,
    ref: str,
    run: RunCommand = run_command,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    layout.prepare()
    with installation_lock(layout):
        clone_backup: Path | None = None
        clone_promoted = False
        try:
            clone_backup = _promote_clone(layout, Path(source))
            clone_promoted = True
            commit = verify_checkout_authority(layout.clone, channel=channel, ref=ref, run=run)
            result = _activate(
                layout,
                channel=channel,
                ref=ref,
                commit=commit,
                run=run,
                now=now,
                status="installed",
            )
        except BaseException as exc:
            if clone_promoted:
                try:
                    _complete_rollback(lambda: _restore_directory(layout.clone, clone_backup))
                except BaseException:
                    raise InstallerError(
                        "clone_rollback_failed",
                        "installation failed and the prior checkout could not be restored",
                    ) from exc
            raise
        if clone_backup is not None:
            try:
                _safe_remove(clone_backup)
            except BaseException:
                pass
        return result


def _stage_repository(layout: InstallLayout, *, channel: str, ref: str, run: RunCommand) -> tuple[Path, Path]:
    temporary_root = _prepare_temporary_root(layout)
    work = Path(tempfile.mkdtemp(prefix="clone-", dir=temporary_root))
    source = work / "dispatch"
    try:
        clone_repository(source, channel=channel, ref=ref, run=run)
    except BaseException:
        try:
            _safe_remove(work)
        except BaseException:
            pass
        raise
    return source, work


def _update_existing(
    layout: InstallLayout,
    *,
    channel: str,
    ref: str,
    run: RunCommand,
    now: Callable[[], datetime],
) -> dict[str, object]:
    def rollback_checkout(old_commit: str) -> subprocess.CompletedProcess[str]:
        restore = (
            ("git", "reset", "--hard", old_commit)
            if channel == "dev"
            else ("git", "checkout", "--detach", old_commit)
        )
        result: subprocess.CompletedProcess[str] | None = None
        first_failure: BaseException | None = None
        for command in (restore,):
            try:
                completed = run(command, layout.clone)
                result = completed
                if completed.returncode != 0 and first_failure is None:
                    first_failure = InstallerError(
                        "checkout_rollback_failed",
                        "a checkout rollback command failed",
                    )
            except BaseException as command_error:
                if first_failure is None:
                    first_failure = command_error
        try:
            assert_checkout_clean(layout.clone, run=run)
        except BaseException as cleanliness_error:
            if first_failure is None:
                first_failure = cleanliness_error
        if first_failure is not None:
            raise first_failure
        if result is None:
            raise InstallerError("checkout_rollback_failed", "the prior checkout could not be restored")
        return result

    with installation_lock(layout):
        assert_checkout_clean(layout.clone, run=run)
        old_commit = current_commit(layout.clone, run=run)
        try:
            checkout_existing(layout.clone, channel=channel, ref=ref, preflight=False, run=run)
            commit = current_commit(layout.clone, run=run)
            return _activate(
                layout,
                channel=channel,
                ref=ref,
                commit=commit,
                run=run,
                now=now,
                status="updated",
            )
        except BaseException as exc:
            try:
                _complete_rollback(lambda: rollback_checkout(old_commit))
            except BaseException:
                raise InstallerError("checkout_rollback_failed", "update failed and the prior checkout could not be restored") from exc
            raise


def install_or_update(
    layout: InstallLayout,
    *,
    channel: str,
    version: str | None = None,
    source: str | Path | None = None,
    update_current: bool = False,
    run: RunCommand = run_command,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    ref = resolve_ref(channel, version)
    current = read_installation(layout)
    if source is not None:
        return install_from_clone(layout, source=source, channel=channel, ref=ref, run=run, now=now)
    if update_current and current is not None and current.get("channel") == channel:
        return _update_existing(layout, channel=channel, ref=ref, run=run, now=now)

    layout.prepare()
    staged, work = _stage_repository(layout, channel=channel, ref=ref, run=run)
    try:
        result = install_from_clone(layout, source=staged, channel=channel, ref=ref, run=run, now=now)
        result["status"] = "switched" if current is not None else "installed"
        return result
    finally:
        try:
            _safe_remove(work)
        except BaseException:
            pass


def repair_existing(
    layout: InstallLayout,
    *,
    run: RunCommand = run_command,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    current = read_installation(layout)
    if current is None or not layout.clone.is_dir():
        raise InstallerError("installation_missing", "Dispatch is not installed")
    with installation_lock(layout):
        channel = str(current["channel"])
        ref = str(current["ref"])
        commit = verify_checkout_authority(
            layout.clone,
            channel=channel,
            ref=ref,
            run=run,
        )
        if commit != str(current["commit"]):
            raise InstallerError(
                "installation_commit_mismatch",
                "the checkout differs from the recorded installation; run dispatch update",
            )
        return _activate(
            layout,
            channel=channel,
            ref=ref,
            commit=commit,
            run=run,
            now=now,
            status="repaired",
        )


__all__ = [
    "ensure_venv",
    "install_from_clone",
    "install_or_update",
    "repair_existing",
    "resolve_ref",
]
