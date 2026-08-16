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
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dispatch_core.paths import DispatchPaths, PathConfigError

RELEASE_FILES = (
    "README.md",
    "core-manifest.json",
    "pyproject.toml",
    "src/dispatch_core/__init__.py",
    "src/dispatch_core/plugin_runtime.py",
    "paths/README.md",
    "src/dispatch_core/paths/__init__.py",
    "health/README.md",
    "src/dispatch_core/health/__init__.py",
    "command-interface/README.md",
    "src/dispatch_core/command_interface/__init__.py",
    "src/dispatch_core/command_interface/__main__.py",
    "plugin-policy/README.md",
    "plugin-policy/plugin_conformance.py",
    "lifecycle/README.md",
    "lifecycle/build_component.py",
    "lifecycle/verify_component.py",
    "collection-manager/README.md",
    "src/dispatch_core/collection_manager/__init__.py",
    "src/dispatch_core/collection_manager/queue.py",
    "src/dispatch_core/collection_manager/supervisor.py",
    "authentication/README.md",
    "src/dispatch_core/authentication/__init__.py",
    "src/dispatch_core/authentication/workflow.py",
    "browser-manager/README.md",
    "src/dispatch_core/browser_manager/__init__.py",
    "src/dispatch_core/browser_manager/manager.py",
    "src/dispatch_core/browser_manager/models.py",
    "src/dispatch_core/browser_manager/policy.py",
    "src/dispatch_core/browser_manager/runtime.py",
    "src/dispatch_core/browser_manager/runtime_authority.py",
    "src/dispatch_core/browser_manager/store.py",
    "scripts/build",
    "scripts/health",
    "scripts/verify",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_entries() -> list[dict]:
    entries = []
    for relative in RELEASE_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"missing_or_unsafe_source:{relative}")
        data = path.read_bytes()
        mode = "0555" if path.stat().st_mode & stat.S_IXUSR else "0444"
        entries.append(
            {
                "path": relative,
                "sha256": digest(data),
                "size": len(data),
                "mode": mode,
            }
        )
    return entries


def release_identity(entries: list[dict]) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return digest(canonical)[:24]


def verify_existing(destination: Path, expected_manifest: bytes) -> None:
    manifest = destination / "release-manifest.json"
    if not destination.is_dir() or destination.is_symlink() or not manifest.is_file():
        raise RuntimeError("existing_release_is_unsafe")
    if manifest.read_bytes() != expected_manifest:
        raise RuntimeError("existing_release_identity_collision")


def build(output_root: Path) -> dict:
    entries = source_entries()
    release_id = release_identity(entries)
    payload = {
        "schema_version": 1,
        "owner": "dispatch-core",
        "release_id": release_id,
        "files": entries,
    }
    manifest_bytes = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = output_root / release_id
    if destination.exists():
        verify_existing(destination, manifest_bytes)
        return {"release_id": release_id, "path": str(destination), "reused": True}

    stage = Path(tempfile.mkdtemp(prefix="dispatch-core-", dir=output_root))
    try:
        for entry in entries:
            source = ROOT / entry["path"]
            target = stage / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(int(entry["mode"], 8))
        manifest = stage / "release-manifest.json"
        manifest.write_bytes(manifest_bytes)
        manifest.chmod(0o444)
        for directory in sorted(
            (path for path in stage.rglob("*") if path.is_dir()), reverse=True
        ):
            directory.chmod(0o555)
        stage.chmod(0o555)
        os.replace(stage, destination)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"release_id": release_id, "path": str(destination), "reused": False}


def main() -> int:
    try:
        paths = DispatchPaths.from_environment(code_root=ROOT.parent)
        result = build(paths.build_output("dispatch-core"))
    except (OSError, PathConfigError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "build",
                    "status": "error",
                    "data": {},
                    "freshness": None,
                    "delivery": None,
                    "error": {"code": "build_failed", "message": str(exc)},
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "action": "build",
                "status": "ready",
                "data": result,
                "freshness": None,
                "delivery": None,
                "error": None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
