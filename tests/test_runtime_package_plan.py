from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_package_plan_matches_pyproject_and_sources() -> None:
    plan = json.loads((ROOT / "packaging" / "runtime-package-plan.json").read_text(encoding="utf-8"))
    distribution = plan["distributions"][0]
    pyproject = tomllib.loads((ROOT / distribution["pyproject"]).read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert plan["installation_mode"] == "online-only"
    assert plan["runtime_downloads_allowed"] is False
    assert distribution["name"] == project["name"]
    assert distribution["version"] == project["version"]
    assert distribution["requires_dist"] == project["dependencies"]
    assert distribution["optional_requires_dist"] == [
        f'{dependency}; extra == "dev"' for dependency in project["optional-dependencies"]["dev"]
    ]
    assert plan["python_requires"] == project["requires-python"]
    assert pyproject["tool"]["setuptools"]["packages"] == [
        "dispatch_core",
        "dispatch_core.paths",
        "dispatch_core.health",
        "dispatch_core.command_interface",
        "dispatch_core.collection_manager",
        "dispatch_core.authentication",
        "dispatch_core.browser_manager",
    ]
    package_dirs = pyproject["tool"]["setuptools"]["package-dir"]
    assert package_dirs == {
        "dispatch_core": "src/dispatch_core",
        "dispatch_core.authentication": "authentication/src/dispatch_core/authentication",
        "dispatch_core.browser_manager": "browser-manager/src/dispatch_core/browser_manager",
        "dispatch_core.collection_manager": "collection-manager/src/dispatch_core/collection_manager",
        "dispatch_core.command_interface": "command-interface/src/dispatch_core/command_interface",
        "dispatch_core.health": "health/src/dispatch_core/health",
        "dispatch_core.paths": "paths/src/dispatch_core/paths",
    }
    assert all("plugins" not in Path(value).parts for value in package_dirs.values())

    declared_sources = set()
    for entry in distribution["files"]:
        source = ROOT / entry["source"]
        declared_sources.add(entry["source"])
        assert source.is_file()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == entry["sha256"]

    actual_sources = {
        path.relative_to(ROOT).as_posix()
        for source_root in distribution["source_roots"]
        for path in (ROOT / source_root).rglob("*.py")
    }
    assert actual_sources == declared_sources
    assert all("plugins" not in Path(path).parts for path in distribution["source_roots"])


def test_browser_runtime_plan_is_pinned_but_not_an_installer_release_manifest() -> None:
    plan = json.loads((ROOT / "packaging" / "browser-runtime-plan.json").read_text(encoding="utf-8"))
    package_plan = json.loads((ROOT / "packaging" / "runtime-package-plan.json").read_text(encoding="utf-8"))

    assert f"{plan['automation']['package']}=={plan['automation']['version']}" in (
        package_plan["distributions"][0]["requires_dist"]
    )
    assert plan["browser"]["family"] == "chromium"
    assert plan["download_policy"]["collection_downloads_allowed"] is False
    assert plan["sandbox"]["production_required"] is True
    assert plan["sandbox"]["silent_no_sandbox_fallback_allowed"] is False
    assert plan["release_artifact_manifest"]["ready"] is False
