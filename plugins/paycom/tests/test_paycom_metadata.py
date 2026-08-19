from __future__ import annotations

import json
from pathlib import Path
import re
import stat
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAMES = ("test", "build", "verify", "health")
EXPECTED_CAPABILITIES = [
    "read_local_data",
    "mutate_data",
    "collect",
    "network",
    "authentication",
]


def load_project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_metadata_declares_source_owned_paycom_contract() -> None:
    project = load_project()
    assert project["project"]["name"] == "dispatch-paycom"
    assert project["project"]["requires-python"] == ">=3.11,<3.14"
    assert project["project"]["dependencies"] == []
    assert project["project"]["entry-points"]["dispatch.plugins"] == {
        "paycom": "dispatch_paycom.service:handle"
    }
    assert project["project"]["entry-points"]["dispatch.collectors"] == {
        "paycom": "dispatch_paycom.collector_provider:registrations"
    }
    assert project["tool"]["dispatch"] == {
        "id": "paycom",
        "capabilities": EXPECTED_CAPABILITIES,
    }
    assert "dispatch.services" not in project["project"].get("entry-points", {})
    assert "dispatch.configurators" not in project["project"].get("entry-points", {})


def test_collector_provider_entry_point_matches_collect_capability() -> None:
    project = load_project()
    groups = project["project"].get("entry-points", {})
    assert groups["dispatch.collectors"] == {
        "paycom": "dispatch_paycom.collector_provider:registrations"
    }
    assert "dispatch.providers" not in groups
    assert "long_running" not in project["tool"]["dispatch"]["capabilities"]


def test_optional_manifest_is_source_only_and_matches_identity() -> None:
    manifest = (ROOT / "dispatch-plugin.yaml").read_text(encoding="utf-8")
    assert "schema_version: 1" in manifest
    assert "id: paycom" in manifest
    for forbidden in (
        "runtime:",
        "releases:",
        "activation_record:",
        "launcher_manifest:",
        "service_units:",
        "receipts:",
    ):
        assert forbidden not in manifest


def test_lifecycle_scripts_are_owner_executable_and_not_writable() -> None:
    for name in SCRIPT_NAMES:
        path = ROOT / "scripts" / name
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & stat.S_IXUSR
        assert not mode & 0o022
        assert not path.is_symlink()


def test_lifecycle_scripts_are_source_only() -> None:
    forbidden = (
        "hermes",
        "authenticate",
        "pip install",
        "build_release",
        "runtime/",
        "release-manifest",
    )
    for name in SCRIPT_NAMES:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8").casefold()
        assert not any(marker in text for marker in forbidden), name
        assert not re.search(r"\\bcollect\\b", text), name


def test_health_is_bounded_and_reports_source_readiness() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts" / "health")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "action", "status", "data", "freshness", "delivery", "error"}
    assert payload["ok"] is True
    assert payload["action"] == "health"
    assert payload["status"] == "degraded"
    assert payload["data"]["registration"] == "ready"
    assert payload["data"]["query"] == "ready"
    assert payload["data"]["data"] == "not_loaded"
    assert payload["data"]["collector"] == "not_checked"
    assert payload["error"] is None


def test_license_and_operator_boundaries_are_documented() -> None:
    assert (ROOT / "LICENSE").stat().st_size > 1000
    readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
    for marker in (
        "private roots",
        "currently deployed legacy paycom plugin",
        "remains untouched",
        "does not install or modify hermes",
        "core owns",
        "live paycom extraction",
    ):
        assert marker in readme
