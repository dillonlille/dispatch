from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

class InstallerError(RuntimeError):
    """A bounded installer failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


def _absolute(value: str | Path, label: str) -> Path:
    text = str(value).strip()
    if not text:
        raise InstallerError("path_empty", f"{label} is empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise InstallerError("path_not_absolute", f"{label} must be absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise InstallerError("path_traversal", f"{label} contains traversal")
    if _contains_symlink(path):
        raise InstallerError("path_symlink", f"{label} cannot use a symlink alias")
    return path.resolve(strict=False)


def _within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise InstallerError("path_escape", f"{label} escapes DISPATCH_HOME") from exc


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _private_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise InstallerError("unsafe_existing_path", f"unsafe existing path: {path}")
        if path.stat().st_uid != os.geteuid():
            raise InstallerError("path_owner", f"path is not owned by the current user: {path}")
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            path.chmod(0o700)
        return
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError as exc:
        raise InstallerError("path_parent_missing", f"parent directory is missing: {path.parent}") from exc


def _safe_existing_parent(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InstallerError("path_parent_missing", f"{label} parent is missing or unsafe: {path}")
    details = path.stat()
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o022:
        raise InstallerError("path_parent_unsafe", f"{label} parent ownership or mode is unsafe: {path}")


def atomic_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_directory(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise InstallerError("unsafe_existing_path", f"unsafe existing file: {path}")
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        published = True
        path.chmod(mode)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        if published:
            raise InstallerError(
                "atomic_publish_uncertain",
                f"{path.name} was replaced but durability confirmation failed; inspect before retrying",
            ) from exc
        raise


def _installation_id(path: Path, expected_layout: dict[str, str]) -> str:
    if not path.exists() and not path.is_symlink():
        return secrets.token_hex(16)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerError("layout_receipt_unsafe", f"cannot safely open layout receipt: {path}") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 16 * 1024
        ):
            raise InstallerError("layout_receipt_unsafe", f"layout receipt is unsafe: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("layout_receipt_invalid", f"layout receipt is invalid: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "installation_id",
            "layout",
            "ownership",
            "browser_authority",
            "contains_secrets",
        }
        or payload.get("schema_version") != 2
        or not isinstance(payload.get("installation_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["installation_id"]) is None
        or payload.get("layout") != expected_layout
        or payload.get("ownership") != "per-user"
        or payload.get("browser_authority") != "installer-owned-system"
        or payload.get("contains_secrets") is not False
    ):
        raise InstallerError("layout_receipt_invalid", f"layout receipt is invalid: {path}")
    return payload["installation_id"]


@contextmanager
def lifecycle_lock(layout: "InstallLayout"):
    _safe_existing_parent(layout.home, "HOME")
    home_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        home_flags |= os.O_NOFOLLOW
    home_descriptor = os.open(layout.home, home_flags)
    home_details = os.fstat(home_descriptor)
    if home_details.st_uid != os.geteuid() or stat.S_IMODE(home_details.st_mode) & 0o022:
        os.close(home_descriptor)
        raise InstallerError("lifecycle_lock_unsafe", "HOME ownership or mode is unsafe for lifecycle locking")
    fcntl.flock(home_descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(home_descriptor)


@contextmanager
def installation_transaction_lock(
    layout: "InstallLayout",
    *,
    prepare_layout: bool = False,
    allow_state_creation: bool = False,
    strict_existing: bool = False,
    root_descriptor: int | None = None,
):
    if root_descriptor is not None:
        if prepare_layout:
            raise InstallerError("install_root_unsafe", "a pinned transaction lock cannot prepare the layout")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        root_fd = os.dup(root_descriptor)
        state_fd = -1
        lock_directory_fd = -1
        lock_fd = -1
        try:
            try:
                root_details = os.fstat(root_fd)
                if (
                    not stat.S_ISDIR(root_details.st_mode)
                    or root_details.st_uid != os.geteuid()
                    or stat.S_IMODE(root_details.st_mode) != 0o700
                ):
                    raise InstallerError("install_root_unsafe", "pinned DISPATCH_HOME is unsafe for transaction locking")
                try:
                    state_fd = os.open("state", directory_flags, dir_fd=root_fd)
                except FileNotFoundError:
                    if not allow_state_creation:
                        raise InstallerError("install_state_missing", "installation state root is missing")
                    os.mkdir("state", 0o700, dir_fd=root_fd)
                    state_fd = os.open("state", directory_flags, dir_fd=root_fd)
                except OSError as exc:
                    raise InstallerError("install_state_unsafe", "installation state root is unsafe") from exc
                state_details = os.fstat(state_fd)
                if (
                    not stat.S_ISDIR(state_details.st_mode)
                    or state_details.st_uid != os.geteuid()
                    or stat.S_IMODE(state_details.st_mode) != 0o700
                ):
                    raise InstallerError("install_state_unsafe", "installation state root is unsafe")
                try:
                    lock_directory_fd = os.open("install", directory_flags, dir_fd=state_fd)
                except FileNotFoundError:
                    os.mkdir("install", 0o700, dir_fd=state_fd)
                    lock_directory_fd = os.open("install", directory_flags, dir_fd=state_fd)
                except OSError as exc:
                    raise InstallerError("install_lock_unsafe", "installer lock directory is unsafe") from exc
                lock_directory_details = os.fstat(lock_directory_fd)
                if (
                    not stat.S_ISDIR(lock_directory_details.st_mode)
                    or lock_directory_details.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_directory_details.st_mode) != 0o700
                ):
                    raise InstallerError("install_lock_unsafe", "installer lock directory is unsafe")
                lock_flags = os.O_CREAT | os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    lock_flags |= os.O_NOFOLLOW
                lock_fd = os.open("installer.lock", lock_flags, 0o600, dir_fd=lock_directory_fd)
                lock_details = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_details.st_mode)
                    or lock_details.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_details.st_mode) != 0o600
                ):
                    raise InstallerError("install_lock_unsafe", "installer lock ownership or mode is unsafe")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise InstallerError("install_lock_unsafe", "cannot acquire pinned installation transaction lock") from exc
            yield
        finally:
            for descriptor in (lock_fd, lock_directory_fd, state_fd, root_fd):
                if descriptor >= 0:
                    os.close(descriptor)
        return
    if prepare_layout:
        layout.prepare()
    elif (
        layout.dispatch_home.is_symlink()
        or not layout.dispatch_home.is_dir()
        or layout.dispatch_home.stat().st_uid != os.geteuid()
        or stat.S_IMODE(layout.dispatch_home.stat().st_mode) != 0o700
    ):
        raise InstallerError("install_root_unsafe", "DISPATCH_HOME is unsafe for transaction locking")
    if not layout.state.exists():
        if not allow_state_creation:
            raise InstallerError("install_state_missing", "installation state root is missing")
        layout.state.mkdir(mode=0o700)
    if strict_existing:
        if (
            layout.state.is_symlink()
            or not layout.state.is_dir()
            or layout.state.stat().st_uid != os.geteuid()
            or stat.S_IMODE(layout.state.stat().st_mode) != 0o700
        ):
            raise InstallerError("install_state_unsafe", "installation state root is unsafe")
    else:
        _private_directory(layout.state)
    lock_directory = layout.state / "install"
    if not lock_directory.exists():
        if lock_directory.is_symlink():
            raise InstallerError("install_lock_unsafe", "installer lock directory is unsafe")
        lock_directory.mkdir(mode=0o700)
    if strict_existing:
        if (
            lock_directory.is_symlink()
            or not lock_directory.is_dir()
            or lock_directory.stat().st_uid != os.geteuid()
            or stat.S_IMODE(lock_directory.stat().st_mode) != 0o700
        ):
            raise InstallerError("install_lock_unsafe", "installer lock directory is unsafe")
    else:
        _private_directory(lock_directory)
    lock_path = lock_directory / "installer.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise InstallerError("install_lock_unsafe", "installer lock file is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise InstallerError("install_lock_unsafe", "installer lock ownership or mode is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def installation_lock(layout: "InstallLayout", *, prepare_layout: bool = False):
    with lifecycle_lock(layout):
        with installation_transaction_lock(layout, prepare_layout=prepare_layout):
            yield


@dataclass(frozen=True, slots=True)
class InstallLayout:
    home: Path
    dispatch_home: Path
    releases: Path
    plugins: Path
    bin: Path
    config: Path
    data: Path
    state: Path
    cache: Path
    staging: Path
    runtime: Path
    browser_selector: Path
    browser_generations: Path

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
        children = {
            name: root / name
            for name in ("releases", "plugins", "bin", "config", "data", "state", "cache", "staging")
        }
        for name, path in children.items():
            _within(path, root, name)
            if path.is_symlink():
                raise InstallerError("path_symlink", f"{name} cannot be a symlink")
        runtime_value = env.get("DISPATCH_RUNTIME_ROOT")
        if runtime_value:
            runtime = _absolute(runtime_value, "DISPATCH_RUNTIME_ROOT")
        elif env.get("XDG_RUNTIME_DIR"):
            runtime = _absolute(env["XDG_RUNTIME_DIR"], "XDG_RUNTIME_DIR") / "dispatch"
        else:
            runtime = root / "runtime"
        for name, path in children.items():
            if _overlap(runtime, path):
                raise InstallerError("path_overlap", f"runtime and {name} roots must remain separate")
        return cls(
            home=home,
            dispatch_home=root,
            releases=children["releases"],
            plugins=children["plugins"],
            bin=children["bin"],
            config=children["config"],
            data=children["data"],
            state=children["state"],
            cache=children["cache"],
            staging=children["staging"],
            runtime=runtime,
            browser_selector=Path("/etc/dispatch/browser-runtime-active.json"),
            browser_generations=Path("/opt/dispatch/browser-runtimes"),
        )

    @property
    def active_release_selector(self) -> Path:
        return self.state / "install" / "active-release.json"

    @property
    def layout_receipt(self) -> Path:
        return self.state / "install" / "layout.json"

    def core_environment(self, release: Path) -> dict[str, str]:
        verified_release = _absolute(release, "release")
        _within(verified_release, self.releases, "release")
        return {
            "DISPATCH_HOME": str(self.dispatch_home),
            "DISPATCH_CODE_ROOT": str(verified_release),
            "DISPATCH_CONFIG_ROOT": str(self.config),
            "DISPATCH_DATA_ROOT": str(self.data),
            "DISPATCH_STATE_ROOT": str(self.state),
            "DISPATCH_CACHE_ROOT": str(self.cache),
            "DISPATCH_RUNTIME_ROOT": str(self.runtime),
        }

    def as_dict(self) -> dict[str, str]:
        return {
            "home": str(self.home),
            "dispatch_home": str(self.dispatch_home),
            "releases": str(self.releases),
            "plugins": str(self.plugins),
            "bin": str(self.bin),
            "config": str(self.config),
            "data": str(self.data),
            "state": str(self.state),
            "cache": str(self.cache),
            "staging": str(self.staging),
            "runtime": str(self.runtime),
            "browser_selector": str(self.browser_selector),
            "browser_generations": str(self.browser_generations),
        }

    def prepare(self) -> dict[str, object]:
        if self.dispatch_home.exists() and self.dispatch_home.is_symlink():
            raise InstallerError("path_symlink", "DISPATCH_HOME cannot be a symlink")
        _safe_existing_parent(self.home, "HOME")
        _safe_existing_parent(self.dispatch_home.parent, "DISPATCH_HOME")
        if not self.dispatch_home.exists():
            self.dispatch_home.mkdir(mode=0o700)
        _private_directory(self.dispatch_home)
        for path in (self.releases, self.plugins, self.bin, self.config, self.data, self.state, self.cache, self.staging):
            _within(path, self.dispatch_home, path.name)
            _private_directory(path)
        _safe_existing_parent(self.runtime.parent, "runtime")
        _private_directory(self.runtime)
        layout_payload = self.as_dict()
        payload = {
            "schema_version": 2,
            "installation_id": _installation_id(self.layout_receipt, layout_payload),
            "layout": layout_payload,
            "ownership": "per-user",
            "browser_authority": "installer-owned-system",
            "contains_secrets": False,
        }
        atomic_json(self.layout_receipt, payload)
        return self.as_dict()
