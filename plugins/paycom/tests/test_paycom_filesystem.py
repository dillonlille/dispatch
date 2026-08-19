from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from dispatch_paycom.filesystem import (
    FilesystemError,
    ensure_private_directory,
    exclusive_private_lock,
    pinned_private_directory,
    validate_private_directory,
)
from dispatch_paycom.roster.storage import RosterStorageError, RosterStore
from dispatch_paycom.timecards.storage import TimecardStorageError, TimecardStore


def test_private_directory_creation_is_componentwise_and_private(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    nested = ensure_private_directory(root / "one" / "two")
    assert nested == root / "one" / "two"
    assert stat.S_IMODE((root / "one").stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700


def test_unsafe_existing_directory_is_rejected_without_chmod(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    before = stat.S_IMODE(unsafe.stat().st_mode)
    with pytest.raises(FilesystemError):
        ensure_private_directory(unsafe / "child")
    assert stat.S_IMODE(unsafe.stat().st_mode) == before
    assert not (unsafe / "child").exists()


def test_symlink_parent_is_rejected_without_touching_target(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    link = private / "link"
    link.symlink_to(outside, target_is_directory=True)
    before = stat.S_IMODE(outside.stat().st_mode)
    with pytest.raises(FilesystemError):
        ensure_private_directory(link / "child")
    assert stat.S_IMODE(outside.stat().st_mode) == before
    assert not (outside / "child").exists()


def test_private_directory_creation_rejects_child_swap_without_touching_target(tmp_path: Path, monkeypatch) -> None:
    import dispatch_paycom.filesystem as filesystem

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_mkdir = filesystem.os.mkdir

    def race_mkdir(name, mode=0o777, *, dir_fd=None):
        result = original_mkdir(name, mode, dir_fd=dir_fd)
        if name == "nested":
            os.rmdir(name, dir_fd=dir_fd)
            (private / "nested").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(filesystem.os, "mkdir", race_mkdir)
    with pytest.raises(FilesystemError):
        ensure_private_directory(private / "nested" / "child")
    assert not (outside / "child").exists()


def test_pinned_directory_matches_validated_inode(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with pinned_private_directory(root) as (descriptor, anchor):
        opened = os.fstat(descriptor)
        current = root.stat()
        assert (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
        assert Path(os.path.realpath(anchor)) == validate_private_directory(root)


def test_lock_rejects_unsafe_existing_file_without_repair(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    lock = root / ".collector.lock"
    lock.write_bytes(b"")
    lock.chmod(0o644)
    with pytest.raises(FilesystemError):
        with exclusive_private_lock(root):
            pass
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


def test_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    outside.chmod(0o644)
    (root / ".collector.lock").symlink_to(outside)
    with pytest.raises(FilesystemError):
        with exclusive_private_lock(root):
            pass
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


@pytest.mark.parametrize("store_type,error_type", [(RosterStore, RosterStorageError), (TimecardStore, TimecardStorageError)])
def test_store_rejects_symlink_parent_without_chmod(tmp_path: Path, store_type, error_type) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (private / "link").symlink_to(outside, target_is_directory=True)
    before = stat.S_IMODE(outside.stat().st_mode)
    with pytest.raises(error_type):
        store_type(private / "link" / "database.sqlite3")
    assert stat.S_IMODE(outside.stat().st_mode) == before
    assert not (outside / "database.sqlite3").exists()
