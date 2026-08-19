"""Descriptor-relative helpers for private Paycom files and directories."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator


class FilesystemError(RuntimeError):
    pass


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _absolute(path: str | Path) -> Path:
    raw = Path(path)
    if any(part in {"", ".", ".."} for part in raw.parts):
        raise FilesystemError("path_invalid")
    value = Path(os.path.abspath(raw))
    if value == Path(value.anchor):
        raise FilesystemError("path_invalid")
    return value


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise FilesystemError("path_invalid")
    return name


def _validate_directory_details(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise FilesystemError("directory_invalid")


def _validate_file_details(details: os.stat_result) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise FilesystemError("file_invalid")


def _open_path_no_follow(value: Path) -> int:
    """Open every component without following a symlink."""
    descriptor = os.open(value.anchor, _DIR_FLAGS)
    try:
        for name in value.parts[1:]:
            child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
            try:
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise FilesystemError("directory_invalid")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_directory_at(parent_descriptor: int, name: str) -> int:
    _safe_name(name)
    try:
        descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_descriptor)
    except OSError as exc:
        raise FilesystemError("directory_invalid") from exc
    try:
        _validate_directory_details(os.fstat(descriptor))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _mkdir_private_directory_at(parent_descriptor: int, name: str) -> int:
    """Create/open one private child while its parent remains descriptor-pinned."""
    _safe_name(name)
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise FilesystemError("directory_create_failed") from exc
    descriptor = _open_private_directory_at(parent_descriptor, name)
    if created:
        try:
            os.fsync(parent_descriptor)
        except BaseException:
            os.close(descriptor)
            raise
    return descriptor


def validate_private_directory(path: str | Path) -> Path:
    value = _absolute(path)
    try:
        descriptor = _open_path_no_follow(value)
        try:
            opened = os.fstat(descriptor)
            _validate_directory_details(opened)
            current = os.lstat(value)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise FilesystemError("directory_invalid")
        finally:
            os.close(descriptor)
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError("directory_invalid") from exc
    return value


def ensure_private_directory(path: str | Path) -> Path:
    """Create missing descendants below one validated private existing ancestor."""
    value = _absolute(path)
    missing: list[str] = []
    current = value
    while not os.path.lexists(current):
        missing.append(current.name)
        parent = current.parent
        if parent == current:
            raise FilesystemError("directory_invalid")
        current = parent
    descriptor: int | None = None
    try:
        descriptor = _open_path_no_follow(current)
        _validate_directory_details(os.fstat(descriptor))
        for name in reversed(missing):
            child = _mkdir_private_directory_at(descriptor, name)
            os.close(descriptor)
            descriptor = child
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError("directory_create_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value


def validate_private_regular_file(path: str | Path) -> Path:
    value = _absolute(path)
    try:
        parent_descriptor = _open_path_no_follow(value.parent)
        try:
            _validate_directory_details(os.fstat(parent_descriptor))
            details = os.stat(value.name, dir_fd=parent_descriptor, follow_symlinks=False)
            _validate_file_details(details)
        finally:
            os.close(parent_descriptor)
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError("file_invalid") from exc
    return value


@contextmanager
def pinned_private_directory(path: str | Path) -> Iterator[tuple[int, Path]]:
    value = _absolute(path)
    descriptor: int | None = None
    try:
        descriptor = _open_path_no_follow(value)
        opened = os.fstat(descriptor)
        _validate_directory_details(opened)
        current = os.lstat(value)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise FilesystemError("directory_invalid")
    except FilesystemError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise FilesystemError("directory_invalid") from exc
    try:
        yield descriptor, Path(f"/proc/self/fd/{descriptor}")
    finally:
        os.close(descriptor)


def fsync_open_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _write_private_file_at(parent_descriptor: int, name: str, data: bytes) -> os.stat_result:
    """Create and durably write a private regular child using only ``dir_fd``."""
    _safe_name(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    except OSError as exc:
        raise FilesystemError("file_create_failed") from exc
    try:
        before = os.fstat(descriptor)
        _validate_file_details(before)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short file write")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_uid, after.st_mode) != (before.st_dev, before.st_ino, before.st_uid, before.st_mode):
            raise FilesystemError("file_create_failed")
        os.fsync(parent_descriptor)
        return after
    except FilesystemError:
        raise
    except OSError as exc:
        raise FilesystemError("file_write_failed") from exc
    finally:
        os.close(descriptor)


def create_private_file(path: str | Path) -> Path:
    """Create one empty private regular file below a descriptor-pinned parent."""
    value = _absolute(path)
    parent = ensure_private_directory(value.parent)
    with pinned_private_directory(parent) as (parent_descriptor, _anchor):
        _write_private_file_at(parent_descriptor, value.name, b"")
    return value


@contextmanager
def exclusive_private_lock(root: str | Path, name: str = ".collector.lock") -> Iterator[None]:
    try:
        _safe_name(name)
        directory = ensure_private_directory(root)
    except FilesystemError as exc:
        raise FilesystemError("lock_invalid") from exc
    with pinned_private_directory(directory) as (directory_descriptor, _anchor):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
        except OSError as exc:
            raise FilesystemError("lock_invalid") from exc
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) & 0o077
            ):
                raise FilesystemError("lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


__all__ = [
    "FilesystemError",
    "create_private_file",
    "ensure_private_directory",
    "exclusive_private_lock",
    "fsync_open_directory",
    "pinned_private_directory",
    "validate_private_directory",
    "validate_private_regular_file",
]
