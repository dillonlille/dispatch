"""Systemd user-service publication."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from .layout import (
    InstallLayout,
    InstallerError,
    assert_user_owned_directory,
    atomic_json,
    ensure_private_directory,
    read_json,
)

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _quote_systemd(value: str) -> str:
    return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(layout: InstallLayout) -> bytes:
    return (
        "[Unit]\n"
        "Description=Dispatch\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "UMask=0077\n"
        f"ExecStart={_quote_systemd(str(layout.command_path))} service\n"
        f"Environment=DISPATCH_HOME={_quote_systemd(str(layout.dispatch_home))}\n"
        f"Environment=PLAYWRIGHT_BROWSERS_PATH={_quote_systemd(str(layout.browser_cache))}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _previous_service_unit(layout: InstallLayout) -> bytes:
    return service_unit(layout).replace(b"UMask=0077\n", b"", 1)


def _clone_service_unit(layout: InstallLayout) -> bytes:
    """Clone-lifecycle service used before Browser Manager cache ownership."""
    return service_unit(layout).replace(
        str(layout.browser_cache).encode("utf-8"),
        str(layout.legacy_browser_cache).encode("utf-8"),
        1,
    )


def _previous_clone_service_unit(layout: InstallLayout) -> bytes:
    return _clone_service_unit(layout).replace(b"UMask=0077\n", b"", 1)


def legacy_service_unit(layout: InstallLayout) -> bytes:
    launcher = layout.dispatch_home / "bin" / "dispatch"
    quoted_launcher = '"' + str(launcher).replace("\\", "\\\\").replace('"', '\\"') + '"'
    quoted_home = '"' + str(layout.dispatch_home).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        "[Unit]\n"
        "Description=Dispatch Core\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={quoted_launcher} service\n"
        f"Environment=DISPATCH_HOME={quoted_home}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent, "service directory")
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.write_bytes(content)
    temporary.chmod(mode)
    temporary.replace(path)
    path.chmod(mode)


def _record_matches(layout: InstallLayout, content: bytes) -> bool:
    try:
        payload = read_json(layout.state / "service.json")
    except InstallerError:
        return False
    return (
        set(payload) == {"schema_version", "unit", "unit_sha256", "service", "contains_secrets"}
        and payload.get("schema_version") == 1
        and payload.get("unit") == str(layout.service_path)
        and payload.get("service") == "dispatch.service"
        and payload.get("contains_secrets") is False
        and payload.get("unit_sha256") == hashlib.sha256(content).hexdigest()
    )


def service_unit_is_owned(layout: InstallLayout) -> bool:
    path = layout.service_path
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
    except InstallerError:
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        details = path.stat()
        if details.st_size > 64 * 1024:
            return False
        content = path.read_bytes()
    except OSError:
        return False
    return (
        details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and stat.S_IMODE(details.st_mode) == 0o600
        and details.st_size <= 64 * 1024
        and content in {
            service_unit(layout),
            _previous_service_unit(layout),
            _clone_service_unit(layout),
            _previous_clone_service_unit(layout),
        }
        and _record_matches(layout, content)
    )


def legacy_service_unit_is_owned(layout: InstallLayout) -> bool:
    unit = layout.service_directory / "dispatch-core.service"
    receipt_path = layout.state / "install" / "service.json"
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
        assert_user_owned_directory(receipt_path.parent, "legacy installation state")
    except InstallerError:
        return False
    if unit.is_symlink() or not unit.is_file():
        return False
    try:
        details = unit.stat()
        receipt = read_json(receipt_path, maximum=16 * 1024)
        content = unit.read_bytes()
    except (OSError, InstallerError):
        return False
    return (
        details.st_uid == os.geteuid()
        and details.st_nlink == 1
        and stat.S_IMODE(details.st_mode) == 0o600
        and set(receipt)
        == {"schema_version", "unit", "unit_sha256", "launcher", "service", "status", "contains_secrets"}
        and receipt.get("schema_version") == 1
        and receipt.get("unit") == str(unit)
        and receipt.get("launcher") == str(layout.dispatch_home / "bin" / "dispatch")
        and receipt.get("service") == "dispatch-core.service"
        and receipt.get("status") == "active"
        and receipt.get("contains_secrets") is False
        and content == legacy_service_unit(layout)
        and receipt.get("unit_sha256") == hashlib.sha256(content).hexdigest()
    )


def install_user_service(
    layout: InstallLayout,
    *,
    run: RunCommand = _run,
    activate: bool = True,
) -> dict[str, object]:
    ensure_private_directory(layout.service_directory, "service directory")
    content = service_unit(layout)
    if layout.service_path.exists() or layout.service_path.is_symlink():
        if layout.service_path.is_symlink() or not layout.service_path.is_file():
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
        details = layout.service_path.stat()
        if details.st_size > 64 * 1024:
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
        if not service_unit_is_owned(layout):
            raise InstallerError("service_conflict", "existing service unit is not Dispatch-owned")
    _atomic_bytes(layout.service_path, content)
    record = {
        "schema_version": 1,
        "unit": str(layout.service_path),
        "unit_sha256": hashlib.sha256(content).hexdigest(),
        "service": "dispatch.service",
        "contains_secrets": False,
    }
    atomic_json(layout.state / "service.json", record)
    if activate:
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch.service"),
            ("systemctl", "--user", "restart", "dispatch.service"),
        ):
            completed = run(command, None)
            if completed.returncode != 0:
                raise InstallerError("service_activation_failed", "Dispatch user service could not be activated and restarted")
    return {"status": "active" if activate else "prepared", "unit": str(layout.service_path), "service": "dispatch.service"}


def inspect_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> dict[str, object]:
    try:
        assert_user_owned_directory(layout.service_directory, "service directory")
    except InstallerError as exc:
        return {"status": "unsafe", "service": "dispatch.service", "unit": str(layout.service_path), "error": str(exc)[:256]}
    if not layout.service_path.exists() and not layout.service_path.is_symlink():
        return {"status": "missing", "service": "dispatch.service", "unit": str(layout.service_path)}
    try:
        if not service_unit_is_owned(layout):
            raise InstallerError(
                "service_unit_unsafe",
                "Dispatch service unit is unsafe or differs from its record",
            )
        active = run(("systemctl", "--user", "is-active", "--quiet", "dispatch.service"), None).returncode == 0
        enabled = run(("systemctl", "--user", "is-enabled", "--quiet", "dispatch.service"), None).returncode == 0
        return {
            "status": "ready" if active and enabled else "incomplete",
            "service": "dispatch.service",
            "unit": str(layout.service_path),
            "active": active,
            "enabled": enabled,
        }
    except (OSError, InstallerError, ValueError) as exc:
        return {"status": "unsafe", "service": "dispatch.service", "unit": str(layout.service_path), "error": str(exc)[:256]}


def remove_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> None:
    if not layout.service_path.exists() and not layout.service_path.is_symlink():
        return
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not service_unit_is_owned(layout):
        raise InstallerError("service_unit_unsafe", "Dispatch service unit is not Dispatch-owned")
    completed = run(("systemctl", "--user", "disable", "--now", "dispatch.service"), None)
    if completed.returncode != 0:
        raise InstallerError("service_stop_failed", "Dispatch user service could not be stopped")
    content = layout.service_path.read_bytes()
    layout.service_path.unlink()
    reload_result = run(("systemctl", "--user", "daemon-reload"), None)
    if reload_result.returncode != 0:
        rollback_failure: BaseException | None = None
        try:
            _atomic_bytes(layout.service_path, content)
        except BaseException as exc:
            rollback_failure = exc
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch.service"),
        ):
            try:
                result = run(command, None)
                if result.returncode != 0:
                    raise InstallerError("service_rollback_command_failed", "service rollback command failed")
            except BaseException as exc:
                if rollback_failure is None:
                    rollback_failure = exc
        if rollback_failure is not None:
            raise InstallerError(
                "service_rollback_failed",
                "service removal failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise InstallerError("service_reload_failed", "systemd user manager could not reload")
    (layout.state / "service.json").unlink(missing_ok=True)


def stop_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> bool:
    path = layout.service_directory / "dispatch-core.service"
    if not path.exists() and not path.is_symlink():
        return False
    assert_user_owned_directory(layout.service_directory, "service directory")
    if not legacy_service_unit_is_owned(layout):
        raise InstallerError("legacy_service_unsafe", "legacy service unit is not Dispatch-owned")
    completed = run(("systemctl", "--user", "disable", "--now", "dispatch-core.service"), None)
    if completed.returncode != 0:
        raise InstallerError("legacy_service_stop_failed", "legacy Dispatch service could not be stopped")
    return True


def remove_legacy_user_service(layout: InstallLayout, *, run: RunCommand = _run) -> None:
    path = layout.service_directory / "dispatch-core.service"
    if not stop_legacy_user_service(layout, run=run):
        return
    receipt = layout.state / "install" / "service.json"
    assert_user_owned_directory(receipt.parent, "legacy installation state")
    content = path.read_bytes()
    path.unlink()
    reload_result = run(("systemctl", "--user", "daemon-reload"), None)
    if reload_result.returncode != 0:
        rollback_failure: BaseException | None = None
        try:
            _atomic_bytes(path, content)
        except BaseException as exc:
            rollback_failure = exc
        for command in (
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", "dispatch-core.service"),
        ):
            try:
                result = run(command, None)
                if result.returncode != 0:
                    raise InstallerError("legacy_service_rollback_command_failed", "legacy service rollback command failed")
            except BaseException as exc:
                if rollback_failure is None:
                    rollback_failure = exc
        if rollback_failure is not None:
            raise InstallerError(
                "legacy_service_rollback_failed",
                "legacy service cleanup failed and the previous service could not be fully restored",
            ) from rollback_failure
        raise InstallerError("legacy_service_reload_failed", "systemd user manager could not reload")
    receipt.unlink(missing_ok=True)
    try:
        receipt.parent.rmdir()
    except OSError:
        pass


__all__ = [
    "install_user_service",
    "inspect_user_service",
    "legacy_service_unit_is_owned",
    "remove_legacy_user_service",
    "remove_user_service",
    "service_unit_is_owned",
    "service_unit",
    "stop_legacy_user_service",
]
