from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Collection, Mapping
from pathlib import Path

from .core_release import activate_core_release, stage_core_wheel
from .layout import InstallLayout, InstallerError


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_launcher(layout: InstallLayout, python: Path) -> Path:
    python = python.expanduser()
    if not python.is_absolute() or not python.exists():
        raise InstallerError("launcher_python_invalid", "installer Python path is invalid")
    try:
        python.relative_to(layout.dispatch_home)
    except ValueError as exc:
        raise InstallerError("launcher_python_outside_home", "installer Python must be inside DISPATCH_HOME") from exc
    if not os.access(python, os.X_OK):
        raise InstallerError("launcher_python_invalid", "installer Python is not executable")

    script = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"exec {shlex.quote(str(python))} -m dispatch_installer.launcher \"$@\"\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".dispatch-launcher-", dir=layout.bin)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o700)
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with stream:
            stream.write(script)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, layout.bin / "dispatch")
        _fsync_directory(layout.bin)
    except OSError as exc:
        raise InstallerError("launcher_publication_failed", "Dispatch launcher could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return layout.bin / "dispatch"


def install_core_application(
    layout: InstallLayout,
    wheel: Path,
    *,
    expected_sha256: str,
    expected_version: str,
    expected_package_files: Mapping[str, str],
    expected_requires_dist: Collection[str],
    launcher_python: Path,
) -> dict[str, object]:
    layout.prepare()
    staged = stage_core_wheel(
        layout,
        wheel,
        expected_sha256=expected_sha256,
        expected_version=expected_version,
        expected_package_files=expected_package_files,
        expected_requires_dist=expected_requires_dist,
    )
    launcher = install_launcher(layout, launcher_python)
    release = layout.releases / str(staged["release_id"])
    active = activate_core_release(layout, release)
    return {
        "status": "installed",
        "release": active,
        "launcher": str(launcher),
        "setup_required": True,
    }
