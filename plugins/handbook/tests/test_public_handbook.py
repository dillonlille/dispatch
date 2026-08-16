from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tomllib

import pytest

from dispatch_handbook.demo import build_demo
from dispatch_handbook.index import IndexError, verify_index
from dispatch_handbook.service import handle

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "synthetic-handbook.json"
ENVELOPE = {"ok", "action", "status", "data", "freshness", "delivery", "error"}


def make_index(tmp_path: Path) -> Path:
    target = tmp_path / "synthetic.sqlite3"
    receipt = build_demo(FIXTURE, target)
    assert receipt["metadata"]["synthetic"] is True
    return target


def test_explicit_demo_import_and_read_only_queries(tmp_path: Path) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["synthetic"] is True
    assert fixture["provenance"].startswith("Wholly invented")
    assert fixture["document_id"].startswith("synthetic-")
    serialized = json.dumps(fixture).casefold()
    private_markers = (
        "full" + " scale",
        "/" + "home" + "/" + "operator",
        "january" + " 2026",
        "employee" + "handbook" + "2026",
    )
    for private_marker in private_markers:
        assert private_marker not in serialized

    index = make_index(tmp_path)
    before = (hashlib.sha256(index.read_bytes()).hexdigest(), index.stat().st_mtime_ns)

    health = handle({"action": "health"}, index_path=index)
    contents = handle({"action": "contents"}, index_path=index)
    overview = handle({"action": "overview"}, index_path=index)
    lookup = handle(
        {"action": "lookup", "question": "Where does a paper star with a curled corner go?"},
        index_path=index,
    )

    assert all(set(value) == ENVELOPE for value in (health, contents, overview, lookup))
    assert health["data"]["overall"] == "ready"
    assert len(contents["data"]["sections"]) == 3
    assert overview["data"]["synthetic"] is True
    assert lookup["status"] == "found"
    assert lookup["data"]["evidence"][0]["section"] == "Paper Star Returns"
    after = (hashlib.sha256(index.read_bytes()).hexdigest(), index.stat().st_mtime_ns)
    assert after == before


def test_unconfigured_and_invalid_requests_fail_honestly(monkeypatch) -> None:
    monkeypatch.delenv("DISPATCH_HANDBOOK_INDEX", raising=False)
    monkeypatch.delenv("DISPATCH_HANDBOOK_DATA_ROOT", raising=False)
    health = handle({"action": "health"}, index_path=None)
    invalid = handle({"action": "lookup", "question": "x"}, index_path=None)
    assert health["ok"] is True
    assert health["status"] == "degraded"
    assert health["data"]["configuration"] == "not_configured"
    assert invalid["ok"] is False
    assert invalid["error"]["code"] in {"invalid_input", "not_configured"}


def test_demo_import_rejects_symlink_target(tmp_path: Path) -> None:
    physical = tmp_path / "physical.sqlite3"
    physical.write_bytes(b"preserve")
    target = tmp_path / "synthetic.sqlite3"
    target.symlink_to(physical)
    try:
        build_demo(FIXTURE, target)
    except IndexError:
        pass
    else:
        raise AssertionError("symlink target was accepted")
    assert physical.read_bytes() == b"preserve"


def test_demo_import_rejects_relative_target() -> None:
    with pytest.raises(IndexError):
        build_demo(FIXTURE, Path("relative.sqlite3"))


def test_environment_index_stays_in_declared_owner_root(tmp_path: Path, monkeypatch) -> None:
    index = make_index(tmp_path)
    monkeypatch.setenv("DISPATCH_HANDBOOK_INDEX", str(index))
    monkeypatch.delenv("DISPATCH_HANDBOOK_DATA_ROOT", raising=False)
    missing_root = handle({"action": "health"})
    assert missing_root["ok"] is False
    assert missing_root["data"]["configuration"] == "invalid"

    declared = tmp_path / "declared-owner-root"
    monkeypatch.setenv("DISPATCH_HANDBOOK_DATA_ROOT", str(declared))
    outside_root = handle({"action": "health"})
    assert outside_root["ok"] is False
    assert "outside DISPATCH_HANDBOOK_DATA_ROOT" in outside_root["error"]["message"]

    monkeypatch.setenv("DISPATCH_HANDBOOK_DATA_ROOT", str(tmp_path))
    ready = handle({"action": "health"})
    assert ready["ok"] is True
    assert ready["status"] == "ready"


def test_hermes_adapter_registers_and_uses_standard_envelope(tmp_path: Path, monkeypatch) -> None:
    index = make_index(tmp_path)
    monkeypatch.setenv("DISPATCH_HANDBOOK_INDEX", str(index))
    monkeypatch.setenv("DISPATCH_HANDBOOK_DATA_ROOT", str(tmp_path))
    adapter_path = ROOT / "integration" / "hermes-plugins" / "dispatch_handbook" / "__init__.py"
    spec = importlib.util.spec_from_file_location("public_handbook_adapter", adapter_path)
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    class Context:
        def register_tool(self, **kwargs):
            self.kwargs = kwargs

    context = Context()
    adapter.register(context)
    assert context.kwargs["check_fn"]() is True
    result = json.loads(context.kwargs["handler"]({"action": "health"}))
    assert set(result) == ENVELOPE
    assert result["data"]["overall"] == "ready"


def test_index_integrity_is_verified(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    verified = verify_index(index)
    assert verified["integrity"] == "ok"
    assert verified["chunk_count"] == 3


def test_source_scripts_do_not_build_or_verify_runtime_artifacts() -> None:
    assert not (ROOT / "scripts" / "build_release.py").exists()
    build = (ROOT / "scripts" / "build_component.py").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_component.py").read_text(encoding="utf-8")
    assert "runtime/releases" not in build
    assert "build_release" not in build
    assert "build_release" not in verify


def test_package_metadata_declares_source_plugin_identity_and_capabilities() -> None:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload["project"]
    assert project["dependencies"] == []
    assert project["entry-points"]["dispatch.plugins"] == {
        "handbook": "dispatch_handbook.service:handle"
    }
    assert payload["tool"]["dispatch"] == {
        "id": "handbook",
        "capabilities": ["read_local_data"],
    }
