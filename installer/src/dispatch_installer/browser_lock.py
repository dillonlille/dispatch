"""Installer-side exclusive lock for Browser Manager generation mutation."""
from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
from pathlib import Path

from .layout import (
    InstallLayout,
    InstallerError,
    open_pinned_installation_parent,
    pinned_installation_path,
)


def _open_pinned_lock(path: Path, flags: int) -> int:
    absolute = Path(os.path.abspath(path))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open("/", directory_flags)
    try:
        for part in absolute.parent.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=parent_descriptor)
            os.close(parent_descriptor)
            parent_descriptor = child
        pinned = os.fstat(parent_descriptor)
        descriptor = os.open(absolute.name, flags, 0o600, dir_fd=parent_descriptor)
        try:
            current = absolute.parent.stat(follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("lock parent changed identity")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent_descriptor)


def acquire_browser_generation_lock(layout: InstallLayout) -> int:
    canonical_path = layout.state / "browser-manager" / "generation.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor: int | None = None
    try:
        pinned_parent = open_pinned_installation_parent(canonical_path, create_parents=True)
        if pinned_parent is not None:
            parent_descriptor, name = pinned_parent
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        else:
            locks = canonical_path.parent
            if not locks.exists() and not locks.is_symlink():
                locks.mkdir(parents=True, mode=0o700)
                locks.chmod(0o700)
            if locks.is_symlink() or not locks.is_dir():
                raise InstallerError("browser_generation_lock_unsafe", "Browser Manager state is unsafe")
            lock_details = locks.stat(follow_symlinks=False)
            if lock_details.st_uid != os.geteuid() or stat.S_IMODE(lock_details.st_mode) != 0o700:
                raise InstallerError("browser_generation_lock_unsafe", "Browser Manager state is not private")
            descriptor = _open_pinned_lock(canonical_path, flags)
    except OSError as exc:
        raise InstallerError("browser_generation_lock_unsafe", "browser generation lock cannot be opened") from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("browser_generation_lock_unsafe", "browser generation lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(descriptor).st_nlink != 1:
            raise InstallerError("browser_generation_lock_unsafe", "browser generation lock changed identity")
    except BlockingIOError as exc:
        os.close(descriptor)
        raise InstallerError("browser_generation_busy", "an active browser lease blocks generation mutation") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def release_browser_generation_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(descriptor)
    except OSError:
        pass


def assert_no_unresolved_browser_leases(layout: InstallLayout) -> None:
    database = pinned_installation_path(
        layout.data / "db" / "browser-manager" / "browser-manager.sqlite3"
    )
    if not database.exists() and not database.is_symlink():
        return
    try:
        details = database.stat(follow_symlinks=False)
    except OSError as exc:
        raise InstallerError("browser_lease_state_unsafe", "Browser Manager lease state cannot be inspected") from exc
    if (
        database.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_size > 128 * 1024 * 1024
    ):
        raise InstallerError("browser_lease_state_unsafe", "Browser Manager lease state is unsafe")
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT lease_id, state FROM leases WHERE state NOT IN (?, ?, ?) LIMIT 1",
                ("closed", "failed", "cancelled"),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise InstallerError("browser_lease_state_invalid", "Browser Manager lease state is invalid") from exc
    if row is not None:
        state = str(row[1])[:64]
        raise InstallerError(
            "browser_reconciliation_required",
            f"Browser Manager has an unresolved {state} lease; run `dispatch browser reconcile` before mutation",
        )


__all__ = [
    "acquire_browser_generation_lock",
    "assert_no_unresolved_browser_leases",
    "release_browser_generation_lock",
]
