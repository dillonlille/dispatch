"""Filesystem layout and small atomic state-record helpers for Dispatch."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping


class InstallerError(RuntimeError):
    """A bounded installer failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _absolute(value: str | Path, label: str) -> Path:
    raw = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise InstallerError("path_control_character", f"{label} contains a control character")
    text = raw
    if not text:
        raise InstallerError("path_empty", f"{label} is empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise InstallerError("path_not_absolute", f"{label} must be absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise InstallerError("path_traversal", f"{label} contains traversal")
    if path.exists() and path.is_symlink():
        raise InstallerError("path_symlink", f"{label} cannot be a symlink")
    return path


def assert_directory_ancestors(path: Path, label: str) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise InstallerError("path_symlink", f"{label} must not contain symlink ancestors")
        if candidate.exists():
            if not candidate.is_dir():
                raise InstallerError("path_not_directory", f"{label} must contain only directory ancestors")
            details = candidate.stat(follow_symlinks=False)
            writable = stat.S_IMODE(details.st_mode) & 0o022
            sticky_boundary = bool(details.st_mode & stat.S_ISVTX)
            if details.st_uid not in {0, os.geteuid()} or (writable and not sticky_boundary):
                raise InstallerError("directory_unsafe", f"{label} contains an unsafe directory ancestor")


def assert_user_owned_directory(path: Path, label: str) -> None:
    assert_directory_ancestors(path, label)
    if not path.exists():
        return
    details = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o022
    ):
        raise InstallerError("directory_unsafe", f"{label} is not a user-owned directory")


def _ensure_directory(path: Path, *, mode: int = 0o700) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise InstallerError("unsafe_existing_path", f"unsafe existing path: {path}")
        details = path.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != mode:
            raise InstallerError("unsafe_existing_path", f"existing directory is unsafe: {path}")
        return
    missing: list[Path] = []
    candidate = path
    while not candidate.exists() and not candidate.is_symlink():
        missing.append(candidate)
        candidate = candidate.parent
    assert_directory_ancestors(candidate, str(path))
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        directory.chmod(mode)
        details = directory.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != mode:
            raise InstallerError("unsafe_existing_path", f"created directory is unsafe: {directory}")


def ensure_private_directory(path: Path, label: str, *, mode: int = 0o700) -> None:
    """Create a private directory chain without accepting or repairing unsafe leaves."""

    assert_directory_ancestors(path, label)
    _ensure_directory(path, mode=mode)
    assert_user_owned_directory(path, label)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> None:
    """Write one JSON record by replacing a file in the same directory."""

    _ensure_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallerError("unsafe_existing_path", f"unsafe existing file: {path}")
    if path.exists():
        details = path.stat()
        if details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("unsafe_existing_path", f"existing file is not user-owned: {path}")
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise InstallerError("atomic_publish_failed", f"could not publish {path.name}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class InstallLayout:
    """The complete per-user Dispatch installation layout."""

    home: Path
    dispatch_home: Path
    clone: Path
    venv: Path
    config: Path
    secrets: Path
    data: Path
    state: Path
    cache: Path
    logs: Path
    run: Path

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        dispatch_home: str | Path | None = None,
    ) -> "InstallLayout":
        env = dict(os.environ if environment is None else environment)
        home = _absolute(env.get("HOME", ""), "HOME")
        root = _absolute(dispatch_home or env.get("DISPATCH_HOME") or home / ".dispatch", "DISPATCH_HOME")
        assert_directory_ancestors(home, "HOME")
        assert_directory_ancestors(root, "DISPATCH_HOME")
        if root == home or root in home.parents:
            raise InstallerError(
                "dispatch_home_unsafe",
                "DISPATCH_HOME cannot equal HOME or contain HOME",
            )
        values = {
            "clone": root / "dispatch",
            "venv": root / "venv",
            "config": root / "config",
            "secrets": root / "secrets",
            "data": root / "data",
            "state": root / "state",
            "cache": root / "cache",
            "logs": root / "logs",
            "run": root / "run",
        }
        for name, path in values.items():
            if path.is_symlink():
                raise InstallerError("path_symlink", f"{name} cannot be a symlink")
        return cls(home=home, dispatch_home=root, **values)

    @property
    def installation_record(self) -> Path:
        return self.dispatch_home / "installation.json"

    @property
    def lock_path(self) -> Path:
        return self.dispatch_home / ".install.lock"

    @property
    def command_path(self) -> Path:
        return self.home / ".local" / "bin" / "dispatch"

    @property
    def service_directory(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    @property
    def service_path(self) -> Path:
        return self.service_directory / "dispatch.service"

    @property
    def venv_python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def installer_source(self) -> Path:
        return self.clone / "installer"

    def as_dict(self) -> dict[str, str]:
        return {
            "dispatch_home": str(self.dispatch_home),
            "dispatch": str(self.clone),
            "venv": str(self.venv),
            "config": str(self.config),
            "secrets": str(self.secrets),
            "data": str(self.data),
            "state": str(self.state),
            "cache": str(self.cache),
            "logs": str(self.logs),
            "run": str(self.run),
        }

    def prepare(self) -> dict[str, str]:
        if self.home.is_symlink() or not self.home.is_dir():
            raise InstallerError("home_unsafe", "HOME must be an existing regular directory")
        home_details = self.home.stat(follow_symlinks=False)
        if home_details.st_uid != os.geteuid() or home_details.st_mode & 0o022:
            raise InstallerError("home_unsafe", "HOME must be user-owned and not writable by group or other")
        _ensure_directory(self.dispatch_home)
        for path in (self.config, self.secrets, self.data, self.state, self.cache, self.logs, self.run):
            _ensure_directory(path)
        return self.as_dict()


def read_json(path: Path, *, maximum: int = 128 * 1024) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise InstallerError("record_missing", f"record is missing or unsafe: {path}")
    details = path.stat()
    if (
        details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_size > maximum
    ):
        raise InstallerError("record_unsafe", f"record is unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("record_invalid", f"record is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise InstallerError("record_invalid", f"record is invalid: {path}")
    return payload


def read_installation(layout: InstallLayout) -> dict[str, object] | None:
    if not layout.installation_record.exists() and not layout.installation_record.is_symlink():
        return None
    payload = read_json(layout.installation_record)
    from .repository import REPOSITORY_URL, validate_ref

    expected_fields = {
        "schema_version",
        "repository",
        "channel",
        "ref",
        "commit",
        "checkout",
        "venv",
        "paths",
        "updated_at",
        "contains_secrets",
    }
    channel = payload.get("channel")
    ref = payload.get("ref")
    timestamp = payload.get("updated_at")
    try:
        valid_ref = isinstance(ref, str) and validate_ref(ref) == ref
        valid_timestamp = isinstance(timestamp, str) and datetime.strptime(
            timestamp, "%Y-%m-%dT%H:%M:%SZ"
        ).strftime("%Y-%m-%dT%H:%M:%SZ") == timestamp
    except (InstallerError, ValueError):
        valid_ref = False
        valid_timestamp = False
    if (
        set(payload) != expected_fields
        or payload.get("schema_version") != 1
        or payload.get("repository") != REPOSITORY_URL
        or payload.get("contains_secrets") is not False
        or payload.get("paths") != layout.as_dict()
        or payload.get("checkout") != str(layout.clone)
        or payload.get("venv") != str(layout.venv)
        or channel not in {"stable", "dev"}
        or not valid_ref
        or (channel == "dev" and ref != "dev")
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit"))) is None
        or not valid_timestamp
    ):
        raise InstallerError("installation_record_invalid", "installation record is invalid")
    return payload


@contextmanager
def installation_lock(layout: InstallLayout):
    """Serialize lifecycle changes with a user-owned lock file."""

    layout.prepare()
    if layout.lock_path.is_symlink():
        raise InstallerError("lock_unsafe", "installation lock cannot be a symlink")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(layout.lock_path, flags, 0o600)
    except OSError as exc:
        raise InstallerError("lock_unsafe", "installation lock could not be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("lock_unsafe", "installation lock is not a private regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


__all__ = [
    "InstallLayout",
    "InstallerError",
    "assert_directory_ancestors",
    "atomic_json",
    "installation_lock",
    "read_installation",
    "read_json",
]
