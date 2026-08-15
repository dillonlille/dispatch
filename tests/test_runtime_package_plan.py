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
        f'{dependency}; extra == "{extra}"'
        for extra, dependencies in project["optional-dependencies"].items()
        for dependency in dependencies
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
    generation_schema = json.loads(
        (ROOT / "docs" / "schemas" / "dispatch-browser-runtime-generation-v1.schema.json").read_text(encoding="utf-8")
    )
    evidence_schema = json.loads(
        (ROOT / "docs" / "schemas" / "dispatch-browser-runtime-evidence-v1.schema.json").read_text(encoding="utf-8")
    )

    assert (
        f"{plan['automation']['package']}=={plan['automation']['version']}; extra == \"browser\""
        in package_plan["distributions"][0]["optional_requires_dist"]
    )
    assert plan["browser"]["family"] == "chromium"
    assert plan["download_policy"]["collection_downloads_allowed"] is False
    assert plan["sandbox"]["production_required"] is True
    assert plan["sandbox"]["silent_no_sandbox_fallback_allowed"] is False
    assert plan["installer_foundation"] == {
        "local_evidence_schema": "docs/schemas/dispatch-browser-runtime-evidence-v1.schema.json",
        "generation_manifest_schema": "docs/schemas/dispatch-browser-runtime-generation-v1.schema.json",
        "implemented_operations": [
            "validate_digest_bound_generation_manifest",
            "validate_fixed_authority_evidence_receipts",
            "stage_exact_immutable_generation",
            "verify_complete_generation_tree",
            "activate_atomic_selector",
            "explicit_target_rollback",
        ],
        "launch_composition_ready": False,
        "privileged_helper_ready": False,
        "production_artifacts_available": False,
        "trusted_evidence_producers_ready": False,
        "synthetic_path_tests_only": True,
    }
    assert (ROOT / plan["installer_foundation"]["generation_manifest_schema"]).is_file()
    assert (ROOT / plan["installer_foundation"]["local_evidence_schema"]).is_file()
    assert generation_schema["additionalProperties"] is False
    assert set(generation_schema["required"]) == {
        "schema_version",
        "generation",
        "installer_release",
        "platform",
        "playwright",
        "browser",
        "sandbox",
        "files",
    }
    assert evidence_schema["additionalProperties"] is False
    assert evidence_schema["properties"]["os_dependencies"]["properties"]["verified"]["const"] is True
    assert evidence_schema["properties"]["sandbox"]["properties"]["verified"]["const"] is True
    assert evidence_schema["properties"]["launch_probe"]["properties"]["passed"]["const"] is True
    assert "receipt_sha256" in evidence_schema["properties"]["launch_probe"]["required"]
    assert plan["runtime_consumer_contract"]["tree_manifest_binds_member_modes"] is True
    assert plan["runtime_consumer_contract"]["rollback_requires_explicit_verified_generation"] is True
    assert plan["runtime_consumer_contract"]["evidence_receipts"] == [
        "os-dependencies.json",
        "sandbox.json",
        "launch-probe.json",
    ]
    assert plan["release_artifact_manifest"]["ready"] is False
