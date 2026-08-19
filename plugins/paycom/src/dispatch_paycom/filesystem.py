"""Small no-follow helpers for private Paycom files and directories."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator


class FilesystemError(RuntimeError):
    pass


def _absolute(path: str | Path) -> Path:
    value = Path(os.path.abspath(path))
    if value == Path(value.anchor) or any(part in {"", ".", ".."} for part in value.parts[1:]):
        raise FilesystemError("path_invalid")
    return value


def validate_private_directory(path: str | Path) -> Path:
    value = _absolute(path)
    try:
        details = value.lstat()
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise FilesystemError("directory_invalid") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
        or resolved != value
    ):
        raise FilesystemError("directory_invalid")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_private_directory(path: str | Path) -> Path:
    """Create missing descendants below one validated private existing ancestor."""
    value = _absolute(path)
    missing: list[Path] = []
    current = value
    while not os.path.lexists(current):
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise FilesystemError("directory_invalid")
        current = parent
    validate_private_directory(current)
    for candidate in reversed(missing):
        parent = validate_private_directory(candidate.parent)
        try:
            os.mkdir(candidate, 0o700)
        except OSError as exc:
            raise FilesystemError("directory_create_failed") from exc
        validate_private_directory(candidate)
        _fsync_directory(parent)
    return validate_private_directory(value)


def validate_private_regular_file(path: str | Path) -> Path:
    value = _absolute(path)
    validate_private_directory(value.parent)
    try:
        details = value.lstat()
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise FilesystemError("file_invalid") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o077
        or resolved != value
    ):
        raise FilesystemError("file_invalid")
    return value


@contextmanager
def pinned_private_directory(path: str | Path) -> Iterator[tuple[int, Path]]:
    value = validate_private_directory(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(value, flags)
    except OSError as exc:
        raise FilesystemError("directory_invalid") from exc
    try:
        opened = os.fstat(descriptor)
        current = value.lstat()
        anchor = Path(f"/proc/self/fd/{descriptor}")
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or Path(os.path.realpath(anchor)) != value
        ):
            raise FilesystemError("directory_invalid")
        yield descriptor, anchor
    finally:
        os.close(descriptor)


def fsync_open_directory(descriptor: int) -> None:
    os.fsync(descriptor)


@contextmanager
def exclusive_private_lock(root: str | Path, name: str = ".collector.lock") -> Iterator[None]:
    if not name or Path(name).name != name:
        raise FilesystemError("lock_invalid")
    directory = ensure_private_directory(root)
    path = directory / name
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
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
    "ensure_private_directory",
    "exclusive_private_lock",
    "fsync_open_directory",
    "pinned_private_directory",
    "validate_private_directory",
    "validate_private_regular_file",
]
