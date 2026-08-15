#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FEATURES = {
    "paths": "implemented",
    "health": "implemented",
    "command-interface": "implemented",
    "plugin-policy": "implemented",
    "lifecycle": "implemented",
    "collection-manager": "implemented",
    "authentication": "in-progress",
    "browser-manager": "implemented",
}
REQUIRED_SOURCES = (
    "src/dispatch_core/__init__.py",
    "src/dispatch_core/plugin_runtime.py",
    "paths/src/dispatch_core/paths/__init__.py",
    "health/src/dispatch_core/health/__init__.py",
    "command-interface/src/dispatch_core/command_interface/__init__.py",
    "command-interface/src/dispatch_core/command_interface/__main__.py",
    "collection-manager/src/dispatch_core/collection_manager/__init__.py",
    "collection-manager/src/dispatch_core/collection_manager/queue.py",
    "collection-manager/src/dispatch_core/collection_manager/supervisor.py",
    "authentication/src/dispatch_core/authentication/__init__.py",
    "authentication/src/dispatch_core/authentication/workflow.py",
    "browser-manager/src/dispatch_core/browser_manager/__init__.py",
    "browser-manager/src/dispatch_core/browser_manager/manager.py",
    "browser-manager/src/dispatch_core/browser_manager/models.py",
    "browser-manager/src/dispatch_core/browser_manager/policy.py",
    "browser-manager/src/dispatch_core/browser_manager/runtime.py",
    "browser-manager/src/dispatch_core/browser_manager/runtime_authority.py",
    "browser-manager/src/dispatch_core/browser_manager/store.py",
    "plugin-policy/plugin_conformance.py",
    "lifecycle/build_component.py",
    "lifecycle/verify_component.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_layout() -> dict:
    manifest_path = ROOT / "core-manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(payload) != {"schema_version", "id", "version", "features"}:
        raise RuntimeError("core_manifest_shape")
    if payload["schema_version"] != 1 or payload["id"] != "dispatch-core" or payload["version"] != "1.0.0":
        raise RuntimeError("core_manifest_identity")
    if not isinstance(payload["features"], list):
        raise RuntimeError("core_manifest_features")

    observed: dict[str, str] = {}
    for item in payload["features"]:
        if not isinstance(item, dict) or set(item) != {"id", "location", "status"}:
            raise RuntimeError("core_feature_shape")
        feature = item["id"]
        if feature in observed or item["location"] != feature:
            raise RuntimeError("core_feature_identity")
        observed[feature] = item["status"]
        location = ROOT / feature
        if not location.is_dir() or location.is_symlink() or not (location / "README.md").is_file():
            raise RuntimeError(f"core_feature_missing:{feature}")
    if observed != EXPECTED_FEATURES:
        raise RuntimeError("core_feature_set")
    if (ROOT / "dispatch-plugin.yaml").exists():
        raise RuntimeError("core_must_not_use_plugin_manifest")

    for relative in REQUIRED_SOURCES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"core_source_missing:{relative}")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project.get("project", {})
    if metadata.get("name") != "dispatch-core" or metadata.get("version") != "1.0.0":
        raise RuntimeError("core_project_identity")
    scripts = metadata.get("scripts", {})
    if scripts.get("dispatch-core") != "dispatch_core.command_interface:main":
        raise RuntimeError("core_command_entrypoint")

    return {
        "feature_count": len(observed),
        "implemented": sorted(name for name, status in observed.items() if status == "implemented"),
        "in_progress": sorted(name for name, status in observed.items() if status == "in-progress"),
        "planned": sorted(name for name, status in observed.items() if status == "planned"),
    }


def verify_release(release: Path) -> dict:
    manifest = release / "release-manifest.json"
    if not release.is_dir() or release.is_symlink() or not manifest.is_file():
        raise RuntimeError("release_missing_or_unsafe")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("owner") != "dispatch-core" or payload.get("release_id") != release.name:
        raise RuntimeError("release_identity_mismatch")
    expected = {entry["path"] for entry in payload.get("files", [])}
    actual = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file()
    } - {"release-manifest.json"}
    if actual != expected:
        raise RuntimeError("release_member_set_mismatch")
    for entry in payload["files"]:
        path = release / entry["path"]
        if path.is_symlink() or sha256(path) != entry["sha256"]:
            raise RuntimeError(f"release_member_digest:{entry['path']}")
        if path.stat().st_size != entry["size"]:
            raise RuntimeError(f"release_member_size:{entry['path']}")
        if stat.S_IMODE(path.stat().st_mode) != int(entry["mode"], 8):
            raise RuntimeError(f"release_member_mode:{entry['path']}")
    return {"release_id": release.name, "files": len(expected)}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        data = {"owner": "dispatch-core", **verify_layout()}
        release_value = argv[0] if argv else os.environ.get("DISPATCH_VERIFY_RELEASE")
        if release_value:
            data["release"] = verify_release(Path(release_value).resolve())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "verify",
                    "status": "error",
                    "data": {},
                    "freshness": None,
                    "delivery": None,
                    "error": {"code": "verification_failed", "message": str(exc)[:512]},
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "action": "verify",
                "status": "ready",
                "data": data,
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
