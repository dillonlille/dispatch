from __future__ import annotations

import json
from pathlib import Path
import tomllib

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def test_installation_release_manifest_matches_schema_and_is_fail_closed() -> None:
    manifest = json.loads((ROOT / "packaging" / "installation-release-manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "docs" / "schemas" / "dispatch-installation-release-v1.schema.json").read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(manifest)
    assert manifest["ready"] is False
    assert manifest["product"] == {"name": "dispatch", "version": "0.0.2"}
    assert manifest["installer"]["artifact"] == {"url": None, "size": None, "sha256": None}
    assert manifest["core"]["artifact"] == {"url": None, "size": None, "sha256": None}
    assert manifest["builtin_plugins"] == []
    assert manifest["browser_runtime"]["ready"] is False
    assert manifest["browser_runtime"]["install_phase"] == "setup"
    assert manifest["post_install"] == {
        "setup_implemented": True,
        "setup_command": "dispatch setup",
        "choices": ["start_setup", "skip_for_now"],
    }
    assert manifest["uninstall"] == {
        "user_scope_implemented": True,
        "administrative_command": "dispatch-installer uninstall",
        "future_user_command": "dispatch uninstall",
        "default_mode": "keep-data",
        "purge_requires_confirmation": True,
        "privileged_browser_removal_implemented": False,
    }


def test_product_manifest_versions_match_component_sources() -> None:
    manifest = json.loads((ROOT / "packaging" / "installation-release-manifest.json").read_text(encoding="utf-8"))
    installer = tomllib.loads((ROOT / "installer" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core = tomllib.loads((ROOT / "dispatch-core" / "pyproject.toml").read_text(encoding="utf-8"))["project"]


    assert (manifest["installer"]["name"], manifest["installer"]["version"]) == (
        installer["name"],
        installer["version"],
    )
    assert (manifest["core"]["name"], manifest["core"]["version"]) == (core["name"], core["version"])
    runtime_plan = json.loads((ROOT / "packaging" / "runtime-package-plan.json").read_text(encoding="utf-8"))
    core_plan = next(item for item in runtime_plan["distributions"] if item["name"] == "dispatch-core")
    assert manifest["core"]["package_files"] == [
        {"path": item["path"], "sha256": item["sha256"]} for item in core_plan["files"]
    ]
    assert manifest["core"]["requires_dist"] == [
        *core_plan["requires_dist"],
        *core_plan["optional_requires_dist"],
    ]
    assert manifest["builtin_plugins"] == []
