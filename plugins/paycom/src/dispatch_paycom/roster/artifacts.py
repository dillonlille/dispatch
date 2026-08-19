"""Private, durable roster artifact staging and verification."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from dispatch_paycom.filesystem import (
    FilesystemError,
    _mkdir_private_directory_at,
    _open_private_directory_at,
    _write_private_file_at,
    ensure_private_directory,
    fsync_open_directory,
    pinned_private_directory,
    validate_private_directory,
)


@dataclass(frozen=True, slots=True)
class RosterArtifact:
    directory: Path
    source_path: Path
    manifest_path: Path
    source_sha256: str
    manifest_sha256: str


def _private_dir(path: Path, *, create: bool = False) -> Path:
    try:
        return ensure_private_directory(path) if create else validate_private_directory(path)
    except FilesystemError as exc:
        raise ValueError("artifact_root_invalid") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_private_file_at(directory_descriptor: int, name: str) -> tuple[os.stat_result, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ValueError("artifact_invalid") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o177
            or before.st_size > 2 * 1024 * 1024
        ):
            raise ValueError("artifact_invalid")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("artifact_invalid")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_uid, after.st_nlink, stat.S_IMODE(after.st_mode), after.st_size)
            != (before.st_dev, before.st_ino, before.st_uid, before.st_nlink, stat.S_IMODE(before.st_mode), len(data))
        ):
            raise ValueError("artifact_invalid")
        return before, bytes(data)
    except OSError as exc:
        raise ValueError("artifact_invalid") from exc
    finally:
        os.close(descriptor)


def _verify_open_artifact(directory_descriptor: int, artifact: RosterArtifact) -> bool:
    source_name = artifact.source_path.name
    manifest_name = artifact.manifest_path.name
    if (
        not source_name
        or Path(source_name).name != source_name
        or not manifest_name
        or Path(manifest_name).name != manifest_name
        or set(os.listdir(directory_descriptor)) != {source_name, manifest_name}
    ):
        raise ValueError("artifact_invalid")
    _source_details, source_bytes = _read_private_file_at(directory_descriptor, source_name)
    _manifest_details, manifest_bytes = _read_private_file_at(directory_descriptor, manifest_name)
    if _digest(source_bytes) != artifact.source_sha256 or _digest(manifest_bytes) != artifact.manifest_sha256:
        raise ValueError("artifact_invalid")
    try:
        value = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("artifact_invalid") from exc
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("sourceSha256") != artifact.source_sha256 or value.get("sourceBytes") != len(source_bytes):
        raise ValueError("artifact_invalid")
    return True


def _remove_unsealed_stage(
    stage_descriptor: int,
    parent_descriptor: int,
    stage_name: str,
    stage_details: os.stat_result,
    *,
    expected_names: set[str] | None = None,
) -> None:
    names = set(os.listdir(stage_descriptor))
    if expected_names is not None and names != expected_names:
        raise ValueError("artifact_cleanup_failed")
    before: dict[str, tuple[int, int]] = {}
    for name in names:
        details = os.stat(name, dir_fd=stage_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o177
        ):
            raise ValueError("artifact_cleanup_failed")
        before[name] = (details.st_dev, details.st_ino)
    for name in sorted(names):
        current = os.stat(name, dir_fd=stage_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) & 0o177
            or (current.st_dev, current.st_ino) != before[name]
        ):
            raise ValueError("artifact_cleanup_failed")
        os.unlink(name, dir_fd=stage_descriptor)
    fsync_open_directory(stage_descriptor)
    current = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) & 0o077
        or (current.st_dev, current.st_ino) != (stage_details.st_dev, stage_details.st_ino)
    ):
        raise ValueError("artifact_cleanup_failed")
    os.rmdir(stage_name, dir_fd=parent_descriptor)
    fsync_open_directory(parent_descriptor)


def stage_roster_artifact(root: Path, target: str, source: bytes, parsed: dict[str, Any], collected_at: str) -> RosterArtifact:
    if not isinstance(target, str) or len(target) != 10 or target[4] != "-" or target[7] != "-" or not isinstance(source, bytes) or not source:
        raise ValueError("artifact_invalid")
    base = _private_dir(Path(root), create=True)
    artifacts = _private_dir(base / "artifacts", create=True)
    target_dir = _private_dir(artifacts / target, create=True)
    source_sha = _digest(source)
    stage_name = f".staging-{secrets.token_hex(16)}"
    destination: Path | None = None
    stage_descriptor: int | None = None
    stage_details: os.stat_result | None = None
    stage_removed = False
    with pinned_private_directory(target_dir) as (target_descriptor, _anchor):
        try:
            stage_descriptor = _mkdir_private_directory_at(target_descriptor, stage_name)
            stage_details = os.fstat(stage_descriptor)
            extension = "json" if parsed.get("sourceFormat") == "paycom-employees-json.v1" else "csv"
            source_name = f"source.{extension}"
            _write_private_file_at(stage_descriptor, source_name, source)
            manifest = {
                "version": 1,
                "sourceFormat": parsed.get("sourceFormat"),
                "target": target,
                "collectedAt": collected_at,
                "sourceSha256": source_sha,
                "sourceBytes": len(source),
                "rowCount": parsed.get("rowCount"),
                "employeeCount": parsed.get("employeeCount"),
                "activeEmployeeCount": parsed.get("activeEmployeeCount"),
                "activeDriverCount": parsed.get("activeDriverCount"),
            }
            manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
            _write_private_file_at(stage_descriptor, "manifest.json", manifest_bytes)
            manifest_sha = _digest(manifest_bytes)
            destination_name = manifest_sha
            destination = target_dir / destination_name
            try:
                destination_details = os.stat(destination_name, dir_fd=target_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(stage_name, destination_name, src_dir_fd=target_descriptor, dst_dir_fd=target_descriptor)
                fsync_open_directory(target_descriptor)
                stage_removed = True
            else:
                if not stat.S_ISDIR(destination_details.st_mode):
                    raise ValueError("artifact_invalid")
                existing_descriptor = _open_private_directory_at(target_descriptor, destination_name)
                try:
                    _verify_open_artifact(existing_descriptor, RosterArtifact(destination, destination / source_name, destination / "manifest.json", source_sha, manifest_sha))
                finally:
                    os.close(existing_descriptor)
                _remove_unsealed_stage(stage_descriptor, target_descriptor, stage_name, stage_details, expected_names={source_name, "manifest.json"})
                stage_removed = True
            return RosterArtifact(destination, destination / source_name, destination / "manifest.json", source_sha, manifest_sha)
        except BaseException:
            if stage_descriptor is not None and stage_details is not None and not stage_removed:
                _remove_unsealed_stage(stage_descriptor, target_descriptor, stage_name, stage_details)
            raise
        finally:
            if stage_descriptor is not None:
                os.close(stage_descriptor)


def verify_roster_artifact(artifact: RosterArtifact) -> bool:
    directory = _private_dir(artifact.directory)
    source = Path(artifact.source_path)
    manifest = Path(artifact.manifest_path)
    if source.parent != directory or manifest.parent != directory:
        raise ValueError("artifact_invalid")
    with pinned_private_directory(directory) as (descriptor, _anchor):
        return _verify_open_artifact(descriptor, artifact)


def discard_roster_artifact(artifact: RosterArtifact) -> None:
    directory = _private_dir(artifact.directory)
    source = Path(artifact.source_path)
    manifest = Path(artifact.manifest_path)
    if source.parent != directory or manifest.parent != directory:
        raise ValueError("artifact_cleanup_failed")
    parent = _private_dir(directory.parent)
    with pinned_private_directory(parent) as (parent_descriptor, _parent_anchor):
        try:
            directory_details = os.stat(directory.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(directory_details.st_mode):
            raise ValueError("artifact_cleanup_failed")
        descriptor = _open_private_directory_at(parent_descriptor, directory.name)
        try:
            _verify_open_artifact(descriptor, artifact)
            names = {source.name, manifest.name}
            observed = set(os.listdir(descriptor))
            if observed != names:
                raise ValueError("artifact_cleanup_failed")
            expected: dict[str, tuple[int, int]] = {}
            for name in names:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or details.st_nlink != 1
                    or stat.S_IMODE(details.st_mode) & 0o177
                ):
                    raise ValueError("artifact_cleanup_failed")
                expected[name] = (details.st_dev, details.st_ino)
            for name in sorted(names):
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != expected[name] or not stat.S_ISREG(current.st_mode):
                    raise ValueError("artifact_cleanup_failed")
                os.unlink(name, dir_fd=descriptor)
            fsync_open_directory(descriptor)
            current = os.stat(directory.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (directory_details.st_dev, directory_details.st_ino):
                raise ValueError("artifact_cleanup_failed")
            os.rmdir(directory.name, dir_fd=parent_descriptor)
            fsync_open_directory(parent_descriptor)
        finally:
            os.close(descriptor)


__all__ = ["RosterArtifact", "discard_roster_artifact", "stage_roster_artifact", "verify_roster_artifact"]
