"""The user-owned dispatch launcher in ~/.local/bin."""
from __future__ import annotations

import os
import shlex
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from .layout import InstallLayout, InstallerError, assert_user_owned_directory, ensure_private_directory


def launcher_script(layout: InstallLayout) -> bytes:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"export DISPATCH_HOME={shlex.quote(str(layout.dispatch_home))}\n"
        f"exec {shlex.quote(str(layout.venv_python))} -I -B -m dispatch_installer.launcher \"$@\"\n"
    ).encode("utf-8")


def _legacy_launcher_script(layout: InstallLayout) -> bytes:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f'exec {shlex.quote(str(layout.dispatch_home / "bin" / "dispatch"))} "$@"\n'
    ).encode("utf-8")


def install_user_command(layout: InstallLayout) -> dict[str, object]:
    directory = layout.command_path.parent
    ensure_private_directory(directory, "launcher directory")
    path = layout.command_path
    desired = launcher_script(layout)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise InstallerError("command_conflict", f"existing command is not Dispatch-owned: {path}")
        details = path.stat()
        if details.st_uid != os.geteuid() or details.st_nlink != 1 or details.st_size > 64 * 1024:
            raise InstallerError("command_conflict", f"existing command is not Dispatch-owned: {path}")
        current = path.read_bytes()
        if current == _legacy_launcher_script(layout):
            path.unlink()
        elif current != desired:
            raise InstallerError("command_conflict", f"existing command is not Dispatch-owned: {path}")
        else:
            path.chmod(0o700)
            return {"status": "ready", "command": str(path)}
    descriptor, temporary_name = tempfile.mkstemp(prefix=".dispatch-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o700)
    except OSError as exc:
        raise InstallerError("command_publish_failed", "Dispatch launcher could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return {"status": "ready", "command": str(path)}


def inspect_user_command(layout: InstallLayout) -> dict[str, object]:
    path = layout.command_path
    if not path.exists() and not path.is_symlink():
        return {"status": "missing", "command": str(path)}
    try:
        assert_user_owned_directory(path.parent, "launcher directory")
        if path.is_symlink() or not path.is_file():
            raise InstallerError("command_unsafe", "Dispatch launcher is not a regular file")
        details = path.stat()
        if (
            details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_size > 64 * 1024
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise InstallerError("command_unsafe", "Dispatch launcher ownership or mode is unsafe")
        status = "ready" if path.read_bytes() == launcher_script(layout) else "unsafe"
        return {"status": status, "command": str(path)}
    except (OSError, InstallerError) as exc:
        return {"status": "unsafe", "command": str(path), "error": str(exc)[:256]}


def remove_user_command(layout: InstallLayout) -> None:
    path = layout.command_path
    if not path.exists() and not path.is_symlink():
        return
    assert_user_owned_directory(path.parent, "launcher directory")
    if path.is_symlink() or not path.is_file():
        raise InstallerError("command_conflict", "Dispatch launcher changed and will not be removed")
    details = path.stat()
    if (
        details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or details.st_size > 64 * 1024
        or stat.S_IMODE(details.st_mode) != 0o700
        or path.read_bytes() != launcher_script(layout)
    ):
        raise InstallerError("command_conflict", "Dispatch launcher changed and will not be removed")
    path.unlink()


__all__ = ["install_user_command", "inspect_user_command", "launcher_script", "remove_user_command"]
