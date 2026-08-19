"""Filesystem layout and small atomic state-record helpers for Dispatch."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping


class InstallerError(RuntimeError):
    """A bounded installer failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_ACTIVE_INSTALLATION_ROOT: ContextVar[tuple[Path, int, int, int] | None] = ContextVar(
    "active_dispatch_installation_root",
    default=None,
)


_PRIVATE_ROOT_ENV = {
    "config": "DISPATCH_CONFIG_ROOT",
    "secrets": "DISPATCH_SECRETS_ROOT",
    "data": "DISPATCH_DATA_ROOT",
    "state": "DISPATCH_STATE_ROOT",
    "cache": "DISPATCH_CACHE_ROOT",
    "logs": "DISPATCH_LOGS_ROOT",
    "run": "DISPATCH_RUNTIME_ROOT",
}


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

    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    pinned = open_pinned_installation_parent(path, create_parents=True)
    if pinned is not None:
        parent_descriptor, name = pinned
        temporary_name = f".{name}.{uuid.uuid4().hex}"
        descriptor = -1
        try:
            try:
                details = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                details = None
            if details is not None and (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
            ):
                raise InstallerError("unsafe_existing_path", f"existing file is unsafe: {path}")
            descriptor = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                mode,
                dir_fd=parent_descriptor,
            )
            os.fchmod(descriptor, mode)
            os.write(descriptor, data)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.chmod(name, mode, dir_fd=parent_descriptor, follow_symlinks=False)
            os.fsync(parent_descriptor)
            return
        except OSError as exc:
            raise InstallerError("atomic_publish_failed", f"could not publish {name}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            os.close(parent_descriptor)

    path = pinned_installation_path(path)
    if is_pinned_installation_path(path):
        details = path.parent.stat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
            raise InstallerError("unsafe_existing_path", f"unsafe existing path: {path.parent}")
    else:
        _ensure_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallerError("unsafe_existing_path", f"unsafe existing file: {path}")
    if path.exists():
        details = path.stat()
        if details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("unsafe_existing_path", f"existing file is not user-owned: {path}")
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
        private_values: dict[str, Path] = {}
        for name, variable in _PRIVATE_ROOT_ENV.items():
            default = root / ("run" if name == "run" else name)
            private = _absolute(env[variable], variable) if env.get(variable) else default
            assert_directory_ancestors(private, variable)
            if private == root or private in root.parents:
                raise InstallerError("private_root_unsafe", f"{variable} cannot equal or contain DISPATCH_HOME")
            for managed in (root / "dispatch", root / "venv", root / ".install-tmp"):
                if private == managed or managed in private.parents or private in managed.parents:
                    raise InstallerError("private_root_unsafe", f"{variable} cannot overlap managed installation code")
            private_values[name] = private
        private_items = list(private_values.items())
        for index, (left_name, left) in enumerate(private_items):
            for right_name, right in private_items[index + 1 :]:
                if left == right or left in right.parents or right in left.parents:
                    raise InstallerError(
                        "private_roots_overlap",
                        f"{left_name} and {right_name} roots cannot overlap",
                    )
        values = {
            "clone": root / "dispatch",
            "venv": root / "venv",
            **private_values,
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
    def browser_manager_cache(self) -> Path:
        return self.cache / "browser-manager"

    @property
    def browser_cache(self) -> Path:
        return self.browser_manager_cache / "playwright"

    @property
    def legacy_browser_cache(self) -> Path:
        return self.cache / "browser"

    @property
    def browser_installation_record(self) -> Path:
        return self.state / "browser-manager" / "installation.json"

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
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InstallerError("record_invalid", f"record is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise InstallerError("record_invalid", f"record is invalid: {path}")
    return payload


def read_installation(layout: InstallLayout) -> dict[str, object] | None:
    if not layout.installation_record.exists() and not layout.installation_record.is_symlink():
        return None
    payload = read_json(layout.installation_record)
    from .repository import DEVELOPMENT_REFS, REPOSITORY_URL, validate_ref

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
        or (channel == "dev" and ref not in DEVELOPMENT_REFS)
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("commit"))) is None
        or not valid_timestamp
    ):
        raise InstallerError("installation_record_invalid", "installation record is invalid")
    return payload


def _open_pinned_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", directory_flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        pinned = os.fstat(descriptor)
        current = absolute.stat(follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise OSError("installation root changed identity")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def pinned_installation_path(path: Path) -> Path:
    active = _ACTIVE_INSTALLATION_ROOT.get()
    if active is None:
        return path
    expected_path, descriptor, _device, _inode = active
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(expected_path)
    except ValueError:
        return path
    if not relative.parts:
        return expected_path
    return Path(f"/proc/{os.getpid()}/fd/{descriptor}") / relative


def open_pinned_installation_parent(
    path: Path,
    *,
    create_parents: bool = False,
) -> tuple[int, str] | None:
    active = _ACTIVE_INSTALLATION_ROOT.get()
    if active is None:
        return None
    expected_path, root_descriptor, _device, _inode = active
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(expected_path)
    except ValueError:
        return None
    if not relative.parts:
        raise InstallerError("unsafe_existing_path", "installation root is not a file path")
    descriptor = os.dup(root_descriptor)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in relative.parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create_parents:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, directory_flags, dir_fd=descriptor)
            details = os.fstat(child)
            if details.st_uid != os.geteuid() or details.st_mode & 0o022:
                os.close(child)
                raise InstallerError("unsafe_existing_path", "installation path parent is unsafe")
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.name
    except BaseException:
        os.close(descriptor)
        raise


def is_pinned_installation_path(path: Path) -> bool:
    active = _ACTIVE_INSTALLATION_ROOT.get()
    if active is None:
        return False
    _expected_path, descriptor, _device, _inode = active
    root = Path(f"/proc/{os.getpid()}/fd/{descriptor}")
    absolute = Path(os.path.abspath(path))
    return absolute == root or root in absolute.parents


def assert_installation_root_current(layout: InstallLayout | None = None) -> None:
    active = _ACTIVE_INSTALLATION_ROOT.get()
    if active is None:
        return
    expected_path, descriptor, device, inode = active
    if layout is not None and expected_path != layout.dispatch_home:
        raise InstallerError("installation_root_changed", "active installation root does not match this lifecycle")
    current_path = expected_path
    try:
        pinned = os.fstat(descriptor)
        current = current_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise InstallerError("installation_root_changed", "installation root changed while locked") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (pinned.st_dev, pinned.st_ino) != (device, inode)
        or (current.st_dev, current.st_ino) != (device, inode)
    ):
        raise InstallerError("installation_root_changed", "installation root changed while locked")


@contextmanager
def installation_lock(layout: InstallLayout, *, prepare: bool = True):
    """Serialize lifecycle changes while retaining the exact installation root."""

    if _ACTIVE_INSTALLATION_ROOT.get() is not None:
        assert_installation_root_current(layout)
        yield
        return
    if prepare:
        layout.prepare()
    else:
        assert_user_owned_directory(layout.dispatch_home, "DISPATCH_HOME")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor: int | None = None
    lock_descriptor: int | None = None
    token = None
    try:
        root_descriptor = _open_pinned_directory(layout.dispatch_home)
        root_details = os.fstat(root_descriptor)
        lock_descriptor = os.open(".install.lock", flags, 0o600, dir_fd=root_descriptor)
        details = os.fstat(lock_descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("lock_unsafe", "installation lock is not a private regular file")
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        current = layout.dispatch_home.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (root_details.st_dev, root_details.st_ino)
        ):
            raise InstallerError("installation_root_changed", "installation root changed before lifecycle mutation")
        token = _ACTIVE_INSTALLATION_ROOT.set(
            (layout.dispatch_home, root_descriptor, root_details.st_dev, root_details.st_ino)
        )
        yield
    except OSError as exc:
        raise InstallerError("lock_unsafe", "installation lock could not be opened safely") from exc
    finally:
        if token is not None:
            _ACTIVE_INSTALLATION_ROOT.reset(token)
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


__all__ = [
    "InstallLayout",
    "InstallerError",
    "assert_directory_ancestors",
    "assert_installation_root_current",
    "atomic_json",
    "installation_lock",
    "is_pinned_installation_path",
    "open_pinned_installation_parent",
    "pinned_installation_path",
    "read_installation",
    "read_json",
]
