from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_installer_package_plan_matches_source_and_metadata() -> None:
    plan = json.loads((ROOT / "packaging" / "installer-package-plan.json").read_text(encoding="utf-8"))
    pyproject = tomllib.loads((ROOT / plan["distribution"]["pyproject"]).read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert plan["schema_version"] == 1
    assert plan["online_only"] is True
    assert plan["production_install_ready"] is False
    assert plan["distribution"]["name"] == project["name"]
    assert plan["distribution"]["version"] == project["version"]
    assert plan["distribution"]["python_requires"] == project["requires-python"]
    assert plan["distribution"]["dependencies"] == project["dependencies"]
    assert pyproject["tool"]["setuptools"]["packages"] == ["dispatch_installer"]
    assert pyproject["tool"]["setuptools"]["package-dir"] == {
        "dispatch_installer": "src/dispatch_installer"
    }

    declared = set()
    for entry in plan["files"]:
        path = ROOT / entry["path"]
        declared.add(entry["path"])
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_size == entry["size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]

    actual = {"installer/pyproject.toml"} | {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "installer" / "src").rglob("*.py")
    }
    assert declared == actual
