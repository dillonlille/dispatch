"""Installer-side exclusive lock for Browser Manager generation mutation."""
from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
from .layout import InstallLayout, InstallerError, ensure_private_directory


def acquire_browser_generation_lock(layout: InstallLayout) -> int:
    locks = layout.state / "browser-manager"
    ensure_private_directory(locks, "Browser Manager state")
    path = locks / "generation.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InstallerError("browser_generation_lock_unsafe", "browser generation lock cannot be opened") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise InstallerError("browser_generation_lock_unsafe", "browser generation lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    database = layout.data / "db" / "browser-manager" / "browser-manager.sqlite3"
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
