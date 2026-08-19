"""Private, durable roster artifact staging and verification."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any


@dataclass(frozen=True, slots=True)
class RosterArtifact:
    directory: Path
    source_path: Path
    manifest_path: Path
    source_sha256: str
    manifest_sha256: str


def _private_dir(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError("artifact_root_invalid") from exc
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("artifact_root_invalid")
    return path


def _private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    os.chmod(path, 0o600)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage_roster_artifact(root: Path, target: str, source: bytes, parsed: dict[str, Any], collected_at: str) -> RosterArtifact:
    if not isinstance(target, str) or len(target) != 10 or target[4] != "-" or target[7] != "-" or not isinstance(source, bytes) or not source:
        raise ValueError("artifact_invalid")
    base = _private_dir(Path(root), create=True)
    artifacts = _private_dir(base / "artifacts", create=True)
    target_dir = _private_dir(artifacts / target, create=True)
    source_sha = _digest(source)
    stage = target_dir / f".staging-{secrets.token_hex(16)}"
    _private_dir(stage, create=True)
    try:
        extension = "json" if parsed.get("sourceFormat") == "paycom-employees-json.v1" else "csv"
        source_path = stage / f"source.{extension}"
        _private_file(source_path, source)
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
        manifest_path = stage / "manifest.json"
        _private_file(manifest_path, manifest_bytes)
        manifest_sha = _digest(manifest_bytes)
        destination = target_dir / manifest_sha
        if destination.exists():
            verify_roster_artifact(RosterArtifact(destination, destination / source_path.name, destination / "manifest.json", source_sha, manifest_sha))
            for child in stage.iterdir():
                child.unlink()
            stage.rmdir()
        else:
            os.replace(stage, destination)
        return RosterArtifact(destination, destination / source_path.name, destination / "manifest.json", source_sha, manifest_sha)
    except Exception:
        if stage.exists():
            for child in stage.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
            stage.rmdir()
        raise


def verify_roster_artifact(artifact: RosterArtifact) -> bool:
    directory = _private_dir(artifact.directory)
    source = artifact.source_path
    manifest = artifact.manifest_path
    for path in (source, manifest):
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o177:
            raise ValueError("artifact_invalid")
    source_bytes = source.read_bytes()
    manifest_bytes = manifest.read_bytes()
    if _digest(source_bytes) != artifact.source_sha256 or _digest(manifest_bytes) != artifact.manifest_sha256:
        raise ValueError("artifact_invalid")
    try:
        value = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("artifact_invalid") from exc
    if value.get("version") != 1 or value.get("sourceSha256") != artifact.source_sha256 or value.get("sourceBytes") != len(source_bytes):
        raise ValueError("artifact_invalid")
    return True


def discard_roster_artifact(artifact: RosterArtifact) -> None:
    if artifact.directory.exists():
        verify_roster_artifact(artifact)
        for path in (artifact.source_path, artifact.manifest_path):
            path.unlink()
        artifact.directory.rmdir()


__all__ = ["RosterArtifact", "discard_roster_artifact", "stage_roster_artifact", "verify_roster_artifact"]
