#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
CORE = WORKSPACE / "dispatch-core"
CORE_SOURCE_ROOT = CORE / "src"
if str(CORE_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_SOURCE_ROOT))

from dispatch_core.paths import DispatchPaths

MANIFEST = "release-manifest.json"
TOP_LEVEL = (
    "README.md",
    "SKILL.md",
    "dispatch-plugin.yaml",
    "integration/hermes-plugins/dispatch_handbook/__init__.py",
    "integration/hermes-plugins/dispatch_handbook/plugin.yaml",
    "pyproject.toml",
)


class ReleaseError(RuntimeError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe(relative: Path) -> None:
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReleaseError("unsafe_relative_path")
    if any(part in {".git", "__pycache__", "db", "runtime", "state"} for part in relative.parts):
        raise ReleaseError("forbidden_release_member")


def inputs(root: Path = ROOT) -> list[Path]:
    values = [Path(item) for item in TOP_LEVEL]
    values.extend(path.relative_to(root) for path in sorted((root / "src" / "dispatch_handbook").glob("*.py")))
    result = sorted(set(values), key=lambda item: item.as_posix())
    for relative in result:
        _safe(relative)
        path = root / relative
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError(f"unsafe_release_input:{relative.as_posix()}")
    return result


def entries(root: Path = ROOT) -> list[dict[str, object]]:
    return [
        {
            "mode": "0444",
            "path": relative.as_posix(),
            "sha256": digest(root / relative),
            "size": (root / relative).stat().st_size,
        }
        for relative in inputs(root)
    ]


def identity(files: list[dict[str, object]]) -> str:
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def verify(release: Path, expected: list[dict[str, object]] | None = None) -> dict[str, object]:
    if not release.is_dir() or release.is_symlink():
        raise ReleaseError("release_root_invalid")
    manifest_path = release / MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("release_manifest_unavailable") from exc
    files = manifest.get("files")
    if set(manifest) != {"contract_version", "files", "release_id"} or manifest.get("contract_version") != 1:
        raise ReleaseError("release_manifest_invalid")
    if not isinstance(files, list) or identity(files) != manifest.get("release_id") or release.name != manifest.get("release_id"):
        raise ReleaseError("release_identity_mismatch")
    if expected is not None and files != expected:
        raise ReleaseError("release_differs_from_source")
    listed = [entry.get("path") for entry in files if isinstance(entry, dict)]
    actual = sorted(
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file() and path.name != MANIFEST
    )
    if actual != sorted(listed) or len(listed) != len(set(listed)):
        raise ReleaseError("release_membership_mismatch")
    for directory in [release, *(path for path in release.rglob("*") if path.is_dir())]:
        if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) != 0o555:
            raise ReleaseError("release_directory_not_sealed")
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"mode", "path", "sha256", "size"}:
            raise ReleaseError("release_member_record_invalid")
        relative = Path(str(entry["path"]))
        _safe(relative)
        path = release / relative
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != int(str(entry["mode"]), 8)
            or info.st_size != entry["size"]
            or digest(path) != entry["sha256"]
        ):
            raise ReleaseError(f"release_member_mismatch:{relative.as_posix()}")
    if stat.S_IMODE(manifest_path.stat().st_mode) != 0o444:
        raise ReleaseError("release_manifest_not_sealed")
    return manifest


def build(output_root: Path, root: Path = ROOT) -> dict[str, object]:
    files = entries(root)
    release_id = identity(files)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = output_root / release_id
    if final.exists():
        verify(final, files)
        return {"release_id": release_id, "path": str(final), "reused": True}
    stage = Path(tempfile.mkdtemp(prefix="handbook-", dir=output_root))
    try:
        for entry in files:
            source = root / str(entry["path"])
            target = stage / str(entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o444)
        manifest = stage / MANIFEST
        manifest.write_text(
            json.dumps(
                {"contract_version": 1, "files": files, "release_id": release_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        manifest.chmod(0o444)
        for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), reverse=True):
            directory.chmod(0o555)
        stage.chmod(0o555)
        os.replace(stage, final)
        verify(final, files)
    except Exception:
        if stage.exists():
            for path in [stage, *stage.rglob("*")]:
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"release_id": release_id, "path": str(final), "reused": False}


def default_output() -> Path:
    paths = DispatchPaths.from_environment(code_root=ROOT.parents[1])
    return paths.build_output("handbook")
