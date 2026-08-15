from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from .layout import InstallLayout, InstallerError, atomic_json

RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_owned_directory(path: Path, boundary: Path) -> None:
    try:
        parts = path.relative_to(boundary).parts
    except ValueError as exc:
        raise InstallerError("service_directory_unsafe", "user service directory is outside HOME") from exc
    current = boundary
    for part in parts:
        details = os.lstat(current)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
            raise InstallerError("service_directory_unsafe", "user service directory is unsafe")
        current = current / part
        if not current.exists():
            current.mkdir(mode=0o700)
    details = os.lstat(current)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
        raise InstallerError("service_directory_unsafe", "user service directory is unsafe")


def install_user_service(
    layout: InstallLayout,
    launcher: Path,
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    if launcher != layout.bin / "dispatch" or launcher.is_symlink() or not launcher.is_file():
        raise InstallerError("service_launcher_invalid", "Dispatch service launcher is invalid")
    user_unit_directory = layout.home / ".config" / "systemd" / "user"
    _ensure_owned_directory(user_unit_directory, layout.home)
    unit = user_unit_directory / "dispatch-core.service"
    content = (
        "[Unit]\n"
        "Description=Dispatch Core\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_quote(str(launcher))} service\n"
        f"Environment=DISPATCH_HOME={_quote(str(layout.dispatch_home))}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".dispatch-core-", dir=user_unit_directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, unit)
        _fsync_directory(user_unit_directory)
    except OSError as exc:
        raise InstallerError("service_publication_failed", "Dispatch user service could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    receipt = {
        "schema_version": 1,
        "unit": str(unit),
        "unit_sha256": hashlib.sha256(content).hexdigest(),
        "launcher": str(launcher),
        "service": "dispatch-core.service",
        "status": "prepared",
        "contains_secrets": False,
    }
    receipt_path = layout.state / "install" / "service.json"
    atomic_json(receipt_path, receipt, mode=0o600)
    for command in (
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "dispatch-core.service"),
        ("systemctl", "--user", "is-active", "--quiet", "dispatch-core.service"),
    ):
        completed = run(command)
        if completed.returncode != 0:
            raise InstallerError("service_activation_failed", "Dispatch user service did not become active")
    receipt["status"] = "active"
    atomic_json(receipt_path, receipt, mode=0o600)
    return {"status": "active", "unit": str(unit), "service": "dispatch-core.service"}
