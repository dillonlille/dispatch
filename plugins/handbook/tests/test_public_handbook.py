from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tomllib

import pytest

from dispatch_handbook.chunking import ChunkingError, PageText, build_chunks
from dispatch_handbook.demo import build_demo
from dispatch_handbook.index import IndexError, verify_index
from dispatch_handbook.service import ACTIONS, handle

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


def test_lookup_rejects_unsearchable_question_as_invalid_input(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    result = handle({"action": "lookup", "question": ". . ."}, index_path=index)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_input"
    assert "no searchable terms" in result["error"]["message"]


def test_lookup_rejects_non_string_question() -> None:
    result = handle({"action": "lookup", "question": 123})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_input"
    assert result["status"] == "error"


def test_lookup_validates_stripped_question_length(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    # Padding whitespace no longer counts toward the length limit: the
    # stripped question is both validated and searched.
    padded = "star" + " " * 600
    padded_result = handle({"action": "lookup", "question": padded}, index_path=index)
    assert padded_result["ok"] is True
    assert padded_result["status"] == "found"

    long = "a" * 501
    too_long = handle({"action": "lookup", "question": long}, index_path=index)
    assert too_long["ok"] is False
    assert too_long["error"]["code"] == "invalid_input"

    short = handle({"action": "lookup", "question": "  star  "}, index_path=index)
    assert short["ok"] is True
    assert short["status"] == "found"


def test_search_rejects_world_readable_index(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    os.chmod(index, 0o644)
    try:
        result = handle({"action": "lookup", "question": "paper star"}, index_path=index)
        assert result["ok"] is False
        assert result["error"]["code"] == "index_unavailable"
        assert "owner-only" in result["error"]["message"]
    finally:
        os.chmod(index, 0o600)


def test_read_rejects_live_wal_sidecar(tmp_path: Path) -> None:
    index = make_index(tmp_path)
    (tmp_path / "synthetic.sqlite3-wal").write_bytes(b"")
    try:
        for request in (
            {"action": "health"},
            {"action": "contents"},
            {"action": "overview"},
            {"action": "lookup", "question": "paper star"},
        ):
            result = handle(request, index_path=index)
            assert result["ok"] is False, request
            assert "write-ahead log" in result["error"]["message"], request
    finally:
        (tmp_path / "synthetic.sqlite3-wal").unlink()


def test_read_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    index = make_index(tmp_path)
    shutil.copy(index, real / "synthetic.sqlite3")
    link = tmp_path / "link"
    link.symlink_to(real)
    # The service layer resolves configured paths before verification, so the
    # guard is enforced at the storage layer where raw paths arrive.
    with pytest.raises(IndexError) as excinfo:
        verify_index(link / "synthetic.sqlite3")
    assert "physical directory" in str(excinfo.value)


def test_chunk_identity_binds_to_content_hash() -> None:
    pages = [PageText(1, "s1", "T", "k", "en", "one two three four five")]
    first = build_chunks(pages, document_version="2026 v1", source_sha256="a" * 64)
    second = build_chunks(pages, document_version="2026 v1", source_sha256="b" * 64)
    assert first[0].chunk_id != second[0].chunk_id
    assert first[0].citation_id != second[0].citation_id
    # Same content stays deterministic across runs.
    again = build_chunks(pages, document_version="2026 v1", source_sha256="a" * 64)
    assert again[0].chunk_id == first[0].chunk_id
    assert again[0].citation_id == first[0].citation_id


def test_non_contiguous_section_fails_with_clear_error() -> None:
    pages = [
        PageText(1, "s1", "T", "k", "en", "alpha beta gamma"),
        PageText(2, "s2", "T2", "k", "en", "delta epsilon"),
        PageText(3, "s1", "T", "k", "en", "zeta eta theta"),
    ]
    with pytest.raises(ChunkingError) as excinfo:
        build_chunks(pages, document_version="2026", source_sha256="a" * 64)
    assert "not contiguous" in str(excinfo.value)


def test_hermes_adapter_actions_match_service_actions() -> None:
    adapter_path = ROOT / "integration" / "hermes-plugins" / "dispatch_handbook" / "__init__.py"
    adapter_text = adapter_path.read_text(encoding="utf-8")
    match = re.search(r"ACTIONS = \{([^}]*)\}", adapter_text)
    assert match is not None, "adapter ACTIONS set not found"
    adapter_actions = {value.strip().strip('"') for value in match.group(1).split(",")}
    assert adapter_actions == ACTIONS
