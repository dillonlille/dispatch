"""Transactional clone and environment lifecycle operations."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .browser_lock import (
    acquire_browser_generation_lock,
    assert_no_unresolved_browser_leases,
    release_browser_generation_lock,
)
from .layout import (
    InstallLayout,
    InstallerError,
    assert_directory_ancestors,
    assert_installation_root_current,
    assert_user_owned_directory,
    atomic_json,
    ensure_private_directory,
    installation_lock,
    pinned_installation_path,
    read_installation,
)
from .repository import (
    DEVELOPMENT_BRANCH,
    REPOSITORY_URL,
    assert_checkout_clean,
    clone_repository,
    resolve_latest_release,
    resolve_published_release,
    run_command,
    verify_checkout_authority,
)
from .service import (
    install_user_service,
    legacy_service_unit_is_owned,
    remove_legacy_user_service,
    restore_plugin_service_states,
    restore_systemd_service_state,
    service_unit_is_owned,
    stop_legacy_user_service,
    stop_plugin_services_for_activation,
    systemd_service_state,
)
from .setup import (
    BUILD_BACKEND_REQUIREMENT,
    assert_source_project_safe,
    install_source_distribution,
    migrate_legacy_plugin_config,
    plugin_dependencies,
    reconcile_plugin_services,
)
from .user_command import install_user_command

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
RollbackResult = TypeVar("RollbackResult")


@dataclass(slots=True)
class _SwapState:
    backup: Path | None = None
    active: bool = False


def _checked(run: RunCommand, command: Sequence[str], cwd: Path | None, code: str, message: str) -> None:
    completed = run(command, cwd)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:512]
        raise InstallerError(code, f"{message}: {detail}" if detail else message)


def assert_disk_space(layout: InstallLayout, *, minimum_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
    """Fail fast before staging when the destination filesystem lacks headroom."""

    for label, target in (
        ("DISPATCH_HOME", layout.dispatch_home),
        ("temporary staging", Path(tempfile.gettempdir())),
    ):
        probe = target if target.exists() else target.parent
        free = shutil.disk_usage(probe).free
        if free < minimum_bytes:
            raise InstallerError(
                "disk_space_insufficient",
                f"insufficient disk space on {label} filesystem: "
                f"{free // (1024 * 1024)} MB free, at least "
                f"{minimum_bytes // (1024 * 1024)} MB required",
            )


def sweep_stale_swap_directories(layout: InstallLayout) -> None:
    """Best-effort removal of orphaned rollback directories from prior failed swaps."""

    for parent in (layout.dispatch_home,):
        try:
            if not parent.is_dir() or parent.is_symlink():
                return
            entries = list(parent.iterdir())
        except OSError:
            return
        now = datetime.now(UTC).timestamp()
        for entry in entries:
            name = entry.name
            if not (name.startswith(".previous-") or ".previous-" in name or ".failed-" in name):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime > 86400:
                    _safe_remove(entry)
            except (OSError, InstallerError):
                continue


def _selected_plugins(layout: InstallLayout) -> list[str]:
    path = layout.config / "plugins.json"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InstallerError("plugin_config_invalid", "plugin configuration is unreadable") from exc
    selected = payload.get("selected_plugins", [])
    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
        raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")
    return selected


def _load_browser_provisioning(layout: InstallLayout) -> Any:
    relative = Path("dispatch-core") / "browser_manager" / "provisioning.py"
    checkout = Path(os.path.abspath(layout.clone))
    source = checkout / relative
    current = checkout
    try:
        checkout_details = current.lstat()
        if current.is_symlink() or not stat.S_ISDIR(checkout_details.st_mode):
            raise OSError("unsafe checkout root")
        for index, part in enumerate(relative.parts):
            current = current / part
            details = current.lstat()
            if current.is_symlink() or details.st_uid != os.geteuid():
                raise OSError("unsafe provisioner path")
            final = index == len(relative.parts) - 1
            if final:
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise OSError("unsafe provisioner file")
            elif not stat.S_ISDIR(details.st_mode):
                raise OSError("unsafe provisioner ancestor")
        canonical_checkout = checkout.resolve(strict=True)
        canonical_source = source.resolve(strict=True)
        canonical_source.relative_to(canonical_checkout)
        if canonical_source != source:
            raise OSError("aliased provisioner path")
    except (OSError, ValueError) as exc:
        raise InstallerError("browser_provisioner_unsafe", "Browser Manager provisioner path is unsafe") from exc
    spec = importlib.util.spec_from_file_location(
        f"_dispatch_browser_provisioning_{uuid.uuid4().hex}",
        source,
    )
    if spec is None or spec.loader is None:
        raise InstallerError("browser_provisioner_invalid", "Browser Manager provisioner cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise InstallerError("browser_provisioner_invalid", "Browser Manager provisioner could not be loaded") from exc
    if not callable(getattr(module, "provision_managed_browser", None)):
        raise InstallerError("browser_provisioner_invalid", "Browser Manager provisioner contract is unavailable")
    return module


def _provision_browser(
    layout: InstallLayout,
    *,
    python: Path,
    staging_cache: Path,
    run: RunCommand,
) -> Any:
    module = _load_browser_provisioning(layout)
    try:
        return module.provision_managed_browser(
            python=python,
            active_cache=layout.browser_cache,
            staging_cache=staging_cache,
            legacy_cache=layout.legacy_browser_cache,
            run=run,
            install_system_dependencies=True,
        )
    except Exception as exc:
        error_type = getattr(module, "BrowserProvisioningError", ())
        if error_type and isinstance(exc, error_type):
            raise InstallerError(str(getattr(exc, "code", "browser_provisioning_failed")), str(exc)) from exc
        raise


def ensure_venv(
    layout: InstallLayout,
    *,
    destination: Path | None = None,
    browser_cache: Path | None = None,
    browser_results: list[Any] | None = None,
    selected_plugins: Sequence[str] | None = None,
    provision_browser: bool = True,
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

    core = layout.clone / "dispatch-core"
    try:
        core_resolved = core.resolve(strict=True)
        clone_resolved = layout.clone.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("requirements_missing", "Core source directory is missing or unsafe") from exc
    if core.is_symlink() or not core.is_dir() or not core_resolved.is_relative_to(clone_resolved):
        raise InstallerError("requirements_missing", "Core source directory is missing or unsafe")
    assert_source_project_safe(core)
    requirements = core / "requirements.txt"
    if not requirements.is_file() or requirements.is_symlink():
        raise InstallerError("requirements_missing", "Core runtime requirements are missing")
    _checked(
        private_run,
        (
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
            BUILD_BACKEND_REQUIREMENT,
        ),
        None,
        "core_dependencies_failed",
        "could not install Core dependencies",
    )
    if install_source_distribution(
        python,
        layout.installer_source,
        no_deps=True,
        run=private_run,
    ).returncode != 0:
        raise InstallerError("installer_install_failed", "could not install the source installer adapter")

    selected = _selected_plugins(layout) if selected_plugins is None else list(selected_plugins)
    plugin_sources: list[tuple[str, Path]] = []
    dependency_by_name: dict[str, str] = {}
    for plugin_id in selected:
        source = layout.clone / "plugins" / plugin_id
        try:
            source.relative_to(layout.clone)
        except ValueError as exc:
            raise InstallerError("selected_plugin_invalid", f"selected plugin path is invalid: {plugin_id}") from exc
        if not source.is_dir() or source.is_symlink():
            raise InstallerError("selected_plugin_missing", f"selected plugin source is missing: {plugin_id}")
        dependencies = plugin_dependencies(source, expected_id=plugin_id)
        for dependency in dependencies:
            package = dependency.split("==", 1)[0].split("[", 1)[0]
            canonical = re.sub(r"[-_.]+", "-", package).lower()
            previous = dependency_by_name.get(canonical)
            if previous is not None and previous != dependency:
                raise InstallerError(
                    "plugin_dependency_conflict",
                    f"selected plugins require conflicting pins for {canonical}",
                )
            dependency_by_name[canonical] = dependency
        plugin_sources.append((plugin_id, source))
    if dependency_by_name:
        _checked(
            private_run,
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *(dependency_by_name[name] for name in sorted(dependency_by_name)),
            ),
            None,
            "plugin_dependencies_failed",
            "could not install the selected plugin dependency closure",
        )
    for plugin_id, source in plugin_sources:
        if install_source_distribution(
            python,
            source,
            no_deps=True,
            run=private_run,
        ).returncode != 0:
            raise InstallerError("plugin_install_failed", f"could not install selected plugin: {plugin_id}")

    if not provision_browser:
        return target
    staging_cache = browser_cache or target.parent / "browser-manager-playwright"
    result = _provision_browser(
        layout,
        python=python,
        staging_cache=staging_cache,
        run=private_run,
    )
    if browser_results is not None:
        browser_results.append(result)
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
        return DEVELOPMENT_BRANCH
    if channel != "stable":
        raise InstallerError("channel_invalid", "channel must be stable or dev")
    if version is None:
        return resolve_latest_release() if opener is None else resolve_latest_release(opener=opener)
    return resolve_published_release(version) if opener is None else resolve_published_release(version, opener=opener)


def _safe_remove(path: Path) -> None:
    path = pinned_installation_path(path)
    if not path.exists() and not path.is_symlink():
        return
    details = path.lstat()
    if details.st_uid != os.geteuid():
        raise InstallerError("managed_path_unsafe", f"managed path is not user-owned: {path}")
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        if details.st_mode & 0o022:
            raise InstallerError("managed_path_unsafe", f"managed directory is not private: {path}")
        shutil.rmtree(path)
    elif path.is_file():
        if details.st_nlink != 1:
            raise InstallerError("managed_path_unsafe", f"managed file has unsafe hard links: {path}")
        path.unlink()
    else:
        raise InstallerError("managed_path_unsafe", f"managed path is unsafe: {path}")


def _snapshot_file(path: Path) -> tuple[bytes, int] | None:
    path = pinned_installation_path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid():
        raise InstallerError("managed_file_unsafe", f"managed file is unsafe: {path}")
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_file(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    path = pinned_installation_path(path)
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


def _swap_directory(
    replacement: Path,
    target: Path,
    *,
    state: _SwapState | None = None,
) -> Path | None:
    """Replace one managed directory and retain one rollback backup."""

    transaction = state or _SwapState()
    if (
        replacement.is_symlink()
        or not replacement.is_dir()
        or replacement.stat().st_uid != os.geteuid()
        or replacement.stat().st_mode & 0o022
    ):
        raise InstallerError("replacement_unsafe", f"replacement directory is unsafe: {replacement}")
    backup: Path | None = None
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or not target.is_dir()
            or target.stat().st_uid != os.geteuid()
            or target.stat().st_mode & 0o022
        ):
            raise InstallerError("managed_directory_unsafe", f"managed directory is unsafe: {target}")
        backup = target.parent / f".{target.name}.previous-{uuid.uuid4().hex}"
    transaction.backup = backup

    def rollback(primary: BaseException) -> None:
        try:
            if target.exists() or target.is_symlink():
                _safe_remove(target)
            if backup is not None and backup.exists():
                try:
                    os.replace(backup, target)
                except BaseException:
                    if not target.exists() or backup.exists():
                        raise
            transaction.active = False
        except BaseException:
            # Preserve active=True so the caller performs bounded rollback.
            raise primary
        raise primary

    if backup is not None:
        try:
            os.replace(target, backup)
        except BaseException as primary:
            if target.exists() or not backup.exists():
                raise
            transaction.active = True
            rollback(primary)
        transaction.active = True

    try:
        os.replace(replacement, target)
    except BaseException as primary:
        if not target.exists() or replacement.exists():
            if backup is None:
                raise
        transaction.active = True
        rollback(primary)
    transaction.active = True
    try:
        target.chmod(0o700)
    except BaseException as primary:
        rollback(primary)
    return backup


def _restore_directory(target: Path, backup: Path | None) -> None:
    """Restore a retained backup and discard the failed active generation."""

    if backup is None:
        if target.exists() or target.is_symlink():
            _safe_remove(target)
        return
    if (
        backup.is_symlink()
        or not backup.is_dir()
        or backup.stat().st_uid != os.geteuid()
        or backup.stat().st_mode & 0o022
    ):
        raise InstallerError("directory_restore_state_unsafe", "directory restore backup is unsafe")
    backup_details = backup.stat(follow_symlinks=False)
    backup_identity = (backup_details.st_dev, backup_details.st_ino)
    displaced: Path | None = None
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or not target.is_dir()
            or target.stat().st_uid != os.geteuid()
            or target.stat().st_mode & 0o022
        ):
            raise InstallerError("directory_restore_state_unsafe", "active directory is unsafe")
        displaced = target.parent / f".{target.name}.failed-{uuid.uuid4().hex}"
        try:
            os.replace(target, displaced)
        except BaseException:
            if target.exists() or target.is_symlink() or not displaced.exists():
                raise
    try:
        os.replace(backup, target)
        target.chmod(0o700)
    except BaseException:
        restored = target.stat(follow_symlinks=False) if target.is_dir() and not target.is_symlink() else None
        if (
            restored is not None
            and (restored.st_dev, restored.st_ino) == backup_identity
            and not backup.exists()
            and not backup.is_symlink()
        ):
            if displaced is not None and displaced.exists():
                _safe_remove(displaced)
            return
        if displaced is not None and displaced.exists() and not target.exists():
            os.replace(displaced, target)
        raise
    if displaced is not None and displaced.exists():
        try:
            _safe_remove(displaced)
        except BaseException:
            if displaced.exists() or displaced.is_symlink():
                raise


def _complete_rollback(action: Callable[[], RollbackResult]) -> RollbackResult:
    """Retry terminal interruption a bounded number of times."""
    interruption: BaseException | None = None
    for _attempt in range(3):
        try:
            return action()
        except (KeyboardInterrupt, SystemExit) as exc:
            interruption = interruption or exc
    raise InstallerError(
        "rollback_persistently_interrupted",
        "rollback could not complete after repeated terminal interruptions",
    ) from interruption


def _promote_clone(
    layout: InstallLayout,
    source: Path,
    *,
    state: _SwapState | None = None,
) -> Path | None:
    assert_installation_root_current(layout)
    legacy_root = pinned_installation_path(layout.dispatch_home / ".install-tmp")
    allowed_roots: list[Path] = []
    if legacy_root.exists() or legacy_root.is_symlink():
        if (
            legacy_root.is_symlink()
            or not legacy_root.is_dir()
            or legacy_root.stat(follow_symlinks=False).st_uid != os.geteuid()
            or stat.S_IMODE(legacy_root.stat(follow_symlinks=False).st_mode) != 0o700
        ):
            raise InstallerError("staging_unsafe", "installation staging directory is unsafe")
        allowed_roots.append(legacy_root.resolve(strict=True))
    try:
        candidate = pinned_installation_path(source).resolve(strict=True)
        temporary_base = Path(tempfile.gettempdir()).resolve(strict=True)
        for work in candidate.parents:
            if work.parent != temporary_base:
                continue
            if (
                work.name.startswith(f"dispatch-installer-{os.geteuid()}-")
                and not work.is_symlink()
                and work.is_dir()
                and work.stat(follow_symlinks=False).st_uid == os.geteuid()
                and stat.S_IMODE(work.stat(follow_symlinks=False).st_mode) == 0o700
            ):
                allowed_roots.append(work)
            break
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ValueError("candidate is outside approved staging")
    except (OSError, ValueError) as exc:
        raise InstallerError(
            "clone_outside_staging",
            "staged clone must be inside the private installation staging directory",
        ) from exc
    if candidate.is_symlink() or not candidate.is_dir() or not (candidate / ".git").is_dir():
        raise InstallerError("clone_invalid", "staged source is not a safe Git checkout")
    return _swap_directory(candidate, layout.clone, state=state)


def _prepare_temporary_root(layout: InstallLayout) -> Path:
    legacy_root = pinned_installation_path(layout.dispatch_home / ".install-tmp")
    if legacy_root.exists() or legacy_root.is_symlink():
        if (
            legacy_root.is_symlink()
            or not legacy_root.is_dir()
            or legacy_root.stat(follow_symlinks=False).st_uid != os.geteuid()
            or stat.S_IMODE(legacy_root.stat(follow_symlinks=False).st_mode) != 0o700
        ):
            raise InstallerError("staging_unsafe", "installation staging directory is unsafe")
    temporary_parent = Path(tempfile.gettempdir())
    assert_directory_ancestors(temporary_parent, "temporary directory")
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f"dispatch-installer-{os.geteuid()}-", dir=temporary_parent)
    )
    temporary_root.chmod(0o700)
    return temporary_root


def _build_replacement_venv(layout: InstallLayout, *, run: RunCommand) -> tuple[Path, Path | None, Path, Any]:
    work = _prepare_temporary_root(layout)
    replacement = work / "venv"
    browser_replacement = work / "browser-manager" / "playwright"
    browser_results: list[Any] = []
    try:
        ensure_venv(
            layout,
            destination=replacement,
            browser_cache=browser_replacement,
            browser_results=browser_results,
            run=run,
        )
    except BaseException as primary:
        try:
            _safe_remove(work)
        except BaseException as cleanup_error:
            raise InstallerError(
                "venv_stage_cleanup_failed",
                "replacement environment failed and private staging could not be removed",
            ) from cleanup_error
        raise primary
    if len(browser_results) != 1:
        try:
            _safe_remove(work)
        except BaseException as cleanup_error:
            raise InstallerError(
                "venv_stage_cleanup_failed",
                "invalid browser result left private environment staging behind",
            ) from cleanup_error
        raise InstallerError("browser_provisioning_failed", "Browser Manager did not return one provisioning result")
    browser_result = browser_results[0]
    _verify_staged_core(replacement, layout.clone / "dispatch-core", work, run=run)
    staged = browser_replacement if bool(getattr(browser_result, "replacement_required", False)) else None
    return replacement, staged, work, browser_result


def _approved_host_tool(path: Path, label: str) -> str:
    """Validate one root-owned system helper before invoking it (install-time)."""

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path("/usr"))
        details = resolved.stat(follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise InstallerError("host_tool_missing", f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise InstallerError("host_tool_unsafe", f"{label} is unsafe")
    return str(resolved)


def _verify_staged_core(python: Path, code_root: Path, work: Path, *, run: RunCommand) -> None:
    """Post-install verification gate: the staged environment must answer `--help`.

    Non-mutating contract check from docs/phase-5-installation-contract.md
    phase 6, executed against the replacement venv before any activation swap.
    """

    if code_root.is_symlink() or not code_root.is_dir():
        raise InstallerError("core_help_gate_failed", "staged Core directory is missing or unsafe")
    entrypoint = code_root / "__main__.py"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise InstallerError("core_help_gate_failed", "staged Core entry point is missing or unsafe")
    timeout = _approved_host_tool(Path("/usr/bin/timeout"), "timeout")
    completed = run(
        (
            timeout,
            "--signal=TERM",
            "--kill-after=10s",
            "60s",
            "env",
            "-i",
            f"HOME={work}",
            "PATH=/usr/bin:/bin",
            str(python),
            str(code_root),
            "--help",
        ),
        None,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:512]
        raise InstallerError(
            "core_help_gate_failed",
            f"staged Core failed its non-mutating verification run: {detail}" if detail else "staged Core failed its non-mutating verification run",
        )


def _reconcile_installation(
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
        assert_checkout_clean(pinned_installation_path(layout.clone), run=run)
        migrate_legacy_plugin_config(layout)
        replacement, browser_replacement, work, browser_result = _build_replacement_venv(layout, run=run)
        assert_checkout_clean(pinned_installation_path(layout.clone), run=run)
        snapshots = {
            "command": (layout.command_path, _snapshot_file(layout.command_path)),
            "installation": (layout.installation_record, _snapshot_file(layout.installation_record)),
            "browser_installation": (
                layout.browser_installation_record,
                _snapshot_file(layout.browser_installation_record),
            ),
            "service": (layout.service_path, _snapshot_file(layout.service_path)),
            "plugin_config": (plugin_config, plugin_snapshot),
        }
        if snapshots["service"][1] is not None and not service_unit_is_owned(layout):
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
    except BaseException as primary:
        rollback_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            _restore_file(plugin_config, plugin_snapshot)
        except BaseException as exc:
            rollback_error = exc
        if work is not None:
            try:
                _safe_remove(work)
            except BaseException as exc:
                cleanup_error = exc
        if rollback_error is not None:
            raise InstallerError(
                "preflight_rollback_failed",
                "installation preflight failed and plugin configuration could not be restored",
            ) from primary
        if cleanup_error is not None:
            raise InstallerError(
                "venv_stage_cleanup_failed",
                "installation preflight failed and private environment staging could not be removed",
            ) from cleanup_error
        raise primary
    assert work is not None
    old_service_present = snapshots["service"][1] is not None
    main_service_state = (
        systemd_service_state("dispatch.service", run=run)
        if old_service_present
        else {"active": False, "enabled": False}
    )
    selected_plugins = _selected_plugins(layout)
    stopped_plugin_services: list[dict[str, object]] = []
    venv_swap = _SwapState()
    browser_swap = _SwapState()
    generation_lock: int | None = None
    try:
        stopped_plugin_services = stop_plugin_services_for_activation(
            layout,
            selected_plugins,
            run=run,
        )
        if legacy_owned:
            stop_legacy_user_service(layout, run=run)
        if old_service_present:
            stopped = run(("systemctl", "--user", "stop", "dispatch.service"), None)
            if stopped.returncode != 0:
                raise InstallerError("service_stop_failed", "Dispatch service could not be stopped before activation")
        generation_lock = acquire_browser_generation_lock(layout)
        assert_no_unresolved_browser_leases(layout)
        if browser_replacement is not None:
            ensure_private_directory(layout.browser_manager_cache, "Browser Manager cache")
            _swap_directory(browser_replacement, layout.browser_cache, state=browser_swap)
        _swap_directory(replacement, layout.venv, state=venv_swap)
        command = install_user_command(layout)
        payload = _installation_record(layout, channel=channel, ref=ref, commit=commit, now=now)
        atomic_json(pinned_installation_path(layout.installation_record), payload)
        browser_payload = browser_result.installation_record(layout.browser_cache)
        atomic_json(pinned_installation_path(layout.browser_installation_record), browser_payload)
        service = install_user_service(layout, run=run, activate=True)
        plugin_services = reconcile_plugin_services(layout, selected_plugins, run=run)
        restore_plugin_service_states(
            layout,
            stopped_plugin_services,
            allowed_ids=selected_plugins,
            run=run,
        )
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
        if venv_swap.active:
            attempt(lambda: _restore_directory(layout.venv, venv_swap.backup))
            venv_swap.active = False
        if browser_swap.active:
            attempt(lambda: _restore_directory(layout.browser_cache, browser_swap.backup))
            browser_swap.active = False
        for path, snapshot in snapshots.values():
            attempt(lambda path=path, snapshot=snapshot: _restore_file(path, snapshot))
        attempt(lambda: run(("systemctl", "--user", "daemon-reload"), None))
        if legacy_owned:
            attempt(lambda: run(("systemctl", "--user", "enable", "--now", "dispatch-core.service"), None))
        elif old_service_present:
            attempt(
                lambda: restore_systemd_service_state(
                    "dispatch.service",
                    main_service_state,
                    run=run,
                )
            )
        attempt(lambda: restore_plugin_service_states(layout, stopped_plugin_services, run=run))
        try:
            _safe_remove(work)
        except BaseException as exc:
            rollback_failure = rollback_failure or exc
        release_browser_generation_lock(generation_lock)
        generation_lock = None
        if rollback_failure is not None:
            raise InstallerError(
                "activation_rollback_failed",
                "activation failed and the prior generation could not be fully restored",
            ) from activation_error
        raise

    release_browser_generation_lock(generation_lock)
    generation_lock = None

    # Activation is committed. Cleanup failures must be explicit without
    # triggering a checkout-only rollback of an already active generation.
    cleanup_failed = False
    for obsolete in (venv_swap.backup, browser_swap.backup, work):
        if obsolete is None:
            continue
        try:
            _safe_remove(obsolete)
        except BaseException:
            cleanup_failed = True
    try:
        if (
            getattr(browser_result, "status", "") == "migrated"
            and (layout.legacy_browser_cache.exists() or layout.legacy_browser_cache.is_symlink())
        ):
            _safe_remove(layout.legacy_browser_cache)
        if legacy_present:
            remove_legacy_user_service(layout, run=run)
    except BaseException:
        cleanup_failed = True
    result_status = f"{status}_cleanup_incomplete" if cleanup_failed else status
    result = {
        "schema_version": 1,
        "status": result_status,
        "channel": channel,
        "ref": ref,
        "commit": commit,
        "layout": layout.as_dict(),
        "browser": browser_result.safe_data(),
        "command": command,
        "service": service,
        "plugin_services": plugin_services,
        "hermes": "untouched",
    }
    if cleanup_failed:
        result["cleanup_error_code"] = "post_activation_cleanup_failed"
    return result


def install_from_clone(
    layout: InstallLayout,
    source: str | Path,
    *,
    channel: str,
    ref: str,
    run: RunCommand = run_command,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    with installation_lock(layout):
        clone_swap = _SwapState()
        try:
            _promote_clone(layout, Path(source), state=clone_swap)
            commit = verify_checkout_authority(
                pinned_installation_path(layout.clone),
                channel=channel,
                ref=ref,
                run=run,
            )
            result = _reconcile_installation(
                layout,
                channel=channel,
                ref=ref,
                commit=commit,
                run=run,
                now=now,
                status="installed",
            )
        except BaseException as exc:
            if clone_swap.active:
                try:
                    _complete_rollback(lambda: _restore_directory(layout.clone, clone_swap.backup))
                    clone_swap.active = False
                except BaseException:
                    raise InstallerError(
                        "clone_rollback_failed",
                        "installation failed and the prior checkout could not be restored",
                    ) from exc
            raise
        if clone_swap.backup is not None:
            try:
                _safe_remove(clone_swap.backup)
            except BaseException:
                result = dict(result)
                result["status"] = f"{result.get('status', 'installed')}_cleanup_incomplete"
                result["cleanup_error_code"] = "post_activation_cleanup_failed"
        return result


def _stage_repository(layout: InstallLayout, *, channel: str, ref: str, run: RunCommand) -> tuple[Path, Path]:
    work = _prepare_temporary_root(layout)
    source = work / "dispatch"
    try:
        clone_repository(source, channel=channel, ref=ref, run=run)
    except BaseException as primary:
        try:
            _safe_remove(work)
        except BaseException as cleanup_error:
            raise InstallerError(
                "repository_stage_cleanup_failed",
                "repository staging failed and private work could not be removed",
            ) from cleanup_error
        raise primary
    return source, work


def recover_incomplete_installation(
    layout: InstallLayout,
) -> dict[str, object]:
    """Remove unrecorded managed paths left by a crashed first install.

    Safe by construction: with no installation record, any clone/venv under
    DISPATCH_HOME was created by a prior installer run and never activated.
    """

    if read_installation(layout) is not None:
        raise InstallerError(
            "recover_not_applicable",
            "an installation record exists; use update or repair instead",
        )
    removed: list[str] = []
    for managed in (layout.clone, layout.venv):
        if managed.exists() or managed.is_symlink():
            if managed.is_symlink() or not managed.is_dir():
                raise InstallerError(
                    "managed_directory_unsafe",
                    f"unrecorded managed path is unsafe: {managed}",
                )
            _safe_remove(managed)
            removed.append(managed.name)
    return {"status": "recovered", "removed": removed}


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
    if current is None:
        for managed in (layout.clone, layout.venv):
            if managed.exists() or managed.is_symlink():
                raise InstallerError(
                    "incomplete_installation",
                    f"unrecorded managed path blocks installation: {managed}; "
                    "run 'dispatch-installer recover' to remove it",
                )
    assert_disk_space(layout)
    sweep_stale_swap_directories(layout)

    def result_status() -> str:
        if current is None:
            return "installed"
        if update_current and current.get("channel") == channel:
            return "updated"
        return "switched"

    if source is not None:
        layout.prepare()
        with installation_lock(layout):
            if current is not None and (layout.clone.exists() or layout.clone.is_symlink()):
                assert_checkout_clean(layout.clone, run=run)
        result = install_from_clone(layout, source=source, channel=channel, ref=ref, run=run, now=now)
        result["status"] = result_status()
        return result

    layout.prepare()
    with installation_lock(layout):
        assert_installation_root_current(layout)
        if current is not None and (layout.clone.exists() or layout.clone.is_symlink()):
            assert_checkout_clean(layout.clone, run=run)
        staged, work = _stage_repository(layout, channel=channel, ref=ref, run=run)
    try:
        result = install_from_clone(layout, source=staged, channel=channel, ref=ref, run=run, now=now)
        result["status"] = result_status()
        return result
    finally:
        try:
            _safe_remove(work)
        except BaseException as cleanup_error:
            raise InstallerError(
                "repository_stage_cleanup_failed",
                "repository reconciliation completed but private staging could not be removed",
            ) from cleanup_error


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
        clone = pinned_installation_path(layout.clone)
        channel = str(current["channel"])
        ref = str(current["ref"])
        commit = verify_checkout_authority(
            clone,
            channel=channel,
            ref=ref,
            run=run,
        )
        if commit != str(current["commit"]):
            raise InstallerError(
                "installation_commit_mismatch",
                "the checkout differs from the recorded installation; run dispatch update",
            )
        return _reconcile_installation(
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
    "recover_incomplete_installation",
    "repair_existing",
    "resolve_ref",
]
