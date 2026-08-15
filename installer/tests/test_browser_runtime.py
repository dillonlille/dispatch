from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

import dispatch_installer.browser_runtime as browser_runtime_module
from dispatch_installer.browser_runtime import (
    activate_browser_generation,
    inspect_browser_runtime,
    load_browser_runtime_manifest,
    rollback_browser_generation,
    stage_browser_runtime,
    verify_browser_generation,
)
from dispatch_installer.layout import InstallLayout, InstallerError


PLATFORM = {
    "system": "linux",
    "distribution": "ubuntu",
    "distribution_version": "24.04",
    "architecture": "x86_64",
}


def _encoded(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    runtime_parent = tmp_path / "run"
    system = tmp_path / "system"
    home.mkdir(parents=True, mode=0o700)
    runtime_parent.mkdir(parents=True, mode=0o700)
    system.mkdir(parents=True, mode=0o755)
    layout = InstallLayout.from_environment({"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime_parent)})
    return replace(
        layout,
        browser_selector=system / "config" / "browser-runtime-active.json",
        browser_generations=system / "browser-runtimes",
    )


def _runtime_fixture(
    tmp_path: Path,
    *,
    generation: str = "chromium-151.0.7922.34-r1234-a",
    resource: bytes = b"trusted resource\n",
) -> tuple[Path, Path, str, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / f"source-{generation}"
    files = {
        "python/playwright/__init__.py": b"# pinned Playwright module\n",
        "python/playwright/driver/node": b"#!/bin/sh\nexit 0\n",
        "python/playwright/driver/package/cli.js": b"// pinned driver CLI\n",
        "chrome-linux64/chrome": b"#!/bin/sh\nexit 0\n",
        "chrome-linux64/chrome_crashpad_handler": b"#!/bin/sh\nexit 0\n",
        "chrome-linux64/resources.pak": resource,
    }
    executable_paths = {
        "python/playwright/driver/node",
        "chrome-linux64/chrome",
        "chrome-linux64/chrome_crashpad_handler",
    }
    for relative, data in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_bytes(data)
        path.chmod(0o755 if relative in executable_paths else 0o644)
    for directory in (source, *[path for path in source.rglob("*") if path.is_dir()]):
        directory.chmod(0o755)

    manifest: dict[str, object] = {
        "schema_version": 1,
        "generation": generation,
        "installer_release": "dispatch-installer-0.1.0",
        "platform": PLATFORM,
        "playwright": {
            "version": "1.62.0",
            "module_relative_path": "python/playwright/__init__.py",
            "driver_executable_relative_path": "python/playwright/driver/node",
            "driver_cli_relative_path": "python/playwright/driver/package/cli.js",
        },
        "browser": {
            "family": "chromium",
            "version": "151.0.7922.34",
            "playwright_revision": "1234",
            "executable_relative_path": "chrome-linux64/chrome",
        },
        "sandbox": {"policy_id": "dispatch-chromium-apparmor-v1"},
        "files": {
            relative: {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "executable": relative in executable_paths,
            }
            for relative, data in sorted(files.items())
        },
    }
    manifest_path = tmp_path / f"{generation}.json"
    manifest_path.write_bytes(_encoded(manifest))
    manifest_path.chmod(0o600)
    return source, manifest_path, _digest(manifest_path), manifest


def _write_evidence(
    layout: InstallLayout,
    manifest_path: Path,
    manifest: dict[str, object],
    *,
    launch_passed: bool = True,
    verified_at: datetime | None = None,
) -> Path:
    generation = str(manifest["generation"])
    files = manifest["files"]
    browser = manifest["browser"]
    assert isinstance(files, dict) and isinstance(browser, dict)
    executable = files[str(browser["executable_relative_path"])]
    assert isinstance(executable, dict)
    timestamp = (verified_at or datetime.now(timezone.utc)).isoformat()
    common = {
        "schema_version": 1,
        "generation": generation,
        "verified_at": timestamp,
        "platform": PLATFORM,
    }
    receipts = {
        "os-dependencies.json": {**common, "verified": True, "dependency_set_sha256": "1" * 64},
        "sandbox.json": {
            **common,
            "verified": True,
            "policy_id": "dispatch-chromium-apparmor-v1",
            "policy_sha256": "2" * 64,
        },
        "launch-probe.json": {
            **common,
            "passed": launch_passed,
            "manifest_sha256": _digest(manifest_path),
            "executable_sha256": executable["sha256"],
        },
    }
    config = layout.browser_selector.parent
    evidence_parent = config / "browser-runtime-evidence"
    root = evidence_parent / generation
    config.mkdir(parents=True, exist_ok=True, mode=0o755)
    evidence_parent.mkdir(exist_ok=True, mode=0o755)
    root.mkdir(exist_ok=True, mode=0o755)
    for path in (config, evidence_parent, root):
        path.chmod(0o755)
    for filename, payload in receipts.items():
        path = root / filename
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(_encoded(payload))
        path.chmod(0o444)
    return root


def _stage(layout: InstallLayout, source: Path, manifest: Path, digest: str) -> dict[str, str | int | bool]:
    return stage_browser_runtime(layout, source, manifest, expected_manifest_sha256=digest)


def test_browser_generation_staging_activation_and_fresh_evidence_reuse(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    _write_evidence(layout, manifest_path, manifest)

    staged = _stage(layout, source, manifest_path, manifest_digest)
    activated = activate_browser_generation(layout, str(staged["generation"]))
    _write_evidence(layout, manifest_path, manifest, verified_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    reused = _stage(layout, source, manifest_path, manifest_digest)
    verified = verify_browser_generation(layout, str(staged["generation"]))
    generation_root = layout.browser_generations / str(staged["generation"])

    assert staged["reused"] is False
    assert reused["reused"] is True
    assert activated == {
        "generation": "chromium-151.0.7922.34-r1234-a",
        "previous_generation": None,
        "reused": False,
    }
    assert verified["files"] == 10
    assert stat.S_IMODE(generation_root.stat().st_mode) == 0o555
    assert stat.S_IMODE((generation_root / "chrome-linux64/chrome").stat().st_mode) == 0o555
    assert stat.S_IMODE((generation_root / "chrome-linux64/resources.pak").stat().st_mode) == 0o444
    assert stat.S_IMODE(layout.browser_selector.stat().st_mode) == 0o444
    assert inspect_browser_runtime(layout)["status"] == "verified"


def test_source_tampering_and_fifo_are_rejected_before_generation_mutation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    _write_evidence(layout, manifest_path, manifest)
    resource = source / "chrome-linux64/resources.pak"
    resource.write_bytes(b"changed\n")

    with pytest.raises(InstallerError) as changed:
        _stage(layout, source, manifest_path, manifest_digest)
    assert changed.value.code == "browser_source_digest"
    assert not layout.browser_generations.exists()

    resource.unlink()
    os.mkfifo(resource, mode=0o644)
    with pytest.raises(InstallerError) as special:
        _stage(layout, source, manifest_path, manifest_digest)
    assert special.value.code == "browser_source_unsafe"
    assert not layout.browser_generations.exists()


def test_manifest_is_closed_and_boolean_schema_version_is_rejected(tmp_path: Path) -> None:
    _, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    assert load_browser_runtime_manifest(manifest_path, expected_sha256=manifest_digest).generation.endswith("-a")

    manifest["plugins"] = []
    manifest_path.write_bytes(_encoded(manifest))
    with pytest.raises(InstallerError) as unknown:
        load_browser_runtime_manifest(manifest_path, expected_sha256=_digest(manifest_path))
    assert unknown.value.code == "browser_manifest_shape"

    manifest.pop("plugins")
    manifest["schema_version"] = True
    manifest_path.write_bytes(_encoded(manifest))
    with pytest.raises(InstallerError) as boolean_version:
        load_browser_runtime_manifest(manifest_path, expected_sha256=_digest(manifest_path))
    assert boolean_version.value.code == "browser_manifest_shape"


def test_fixed_trusted_evidence_receipts_are_required_and_validated(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)

    with pytest.raises(InstallerError) as missing:
        _stage(layout, source, manifest_path, manifest_digest)
    assert missing.value.code == "browser_evidence_unsafe"

    _write_evidence(layout, manifest_path, manifest, launch_passed=False)
    with pytest.raises(InstallerError) as incomplete:
        _stage(layout, source, manifest_path, manifest_digest)
    assert incomplete.value.code == "browser_evidence_incomplete"

    _write_evidence(
        layout,
        manifest_path,
        manifest,
        verified_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    with pytest.raises(InstallerError) as stale:
        _stage(layout, source, manifest_path, manifest_digest)
    assert stale.value.code == "browser_evidence_stale"


def test_full_tree_digest_mode_and_evidence_tampering_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    _write_evidence(layout, manifest_path, manifest)
    staged = _stage(layout, source, manifest_path, manifest_digest)
    generation = str(staged["generation"])
    generation_root = layout.browser_generations / generation
    resource = generation_root / "chrome-linux64/resources.pak"
    resource.chmod(0o644)
    resource.write_bytes(b"tampered resource\n")
    resource.chmod(0o444)

    with pytest.raises(InstallerError) as tampered:
        verify_browser_generation(layout, generation)
    assert tampered.value.code == "browser_tree_mismatch"

    source, manifest_path, manifest_digest, manifest = _runtime_fixture(
        tmp_path / "second", generation="chromium-151.0.7922.34-r1234-b"
    )
    _write_evidence(layout, manifest_path, manifest)
    second = _stage(layout, source, manifest_path, manifest_digest)
    receipt = layout.browser_generations / str(second["generation"]) / "installation-evidence" / "sandbox.json"
    receipt.chmod(0o644)
    receipt.write_bytes(b"{}\n")
    receipt.chmod(0o444)
    with pytest.raises(InstallerError) as evidence_tampered:
        verify_browser_generation(layout, str(second["generation"]))
    assert evidence_tampered.value.code == "browser_evidence_mismatch"


def test_activation_and_explicit_target_rollback_are_single_selector_replacements(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first_source, first_manifest, first_digest, first_payload = _runtime_fixture(
        tmp_path / "first", generation="chromium-151.0.7922.34-r1234-a"
    )
    second_source, second_manifest, second_digest, second_payload = _runtime_fixture(
        tmp_path / "second",
        generation="chromium-151.0.7922.34-r1234-b",
        resource=b"second resource\n",
    )
    _write_evidence(layout, first_manifest, first_payload)
    _write_evidence(layout, second_manifest, second_payload)
    first = _stage(layout, first_source, first_manifest, first_digest)
    second = _stage(layout, second_source, second_manifest, second_digest)

    activate_browser_generation(layout, str(first["generation"]))
    switched = activate_browser_generation(layout, str(second["generation"]))
    rolled_back = rollback_browser_generation(layout, str(first["generation"]))
    rolled_forward = rollback_browser_generation(layout, str(second["generation"]))

    assert switched["previous_generation"] == first["generation"]
    assert rolled_back["previous_generation"] == second["generation"]
    assert rolled_forward["previous_generation"] == first["generation"]
    assert not layout.browser_selector.with_name("browser-runtime-previous.json").exists()
    assert inspect_browser_runtime(layout)["generation"] == second["generation"]


def test_invalid_explicit_rollback_target_does_not_change_active_selector(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first_source, first_manifest, first_digest, first_payload = _runtime_fixture(
        tmp_path / "first", generation="chromium-151.0.7922.34-r1234-a"
    )
    second_source, second_manifest, second_digest, second_payload = _runtime_fixture(
        tmp_path / "second", generation="chromium-151.0.7922.34-r1234-b"
    )
    _write_evidence(layout, first_manifest, first_payload)
    _write_evidence(layout, second_manifest, second_payload)
    first = _stage(layout, first_source, first_manifest, first_digest)
    second = _stage(layout, second_source, second_manifest, second_digest)
    activate_browser_generation(layout, str(first["generation"]))
    selector_before = layout.browser_selector.read_bytes()
    candidate = layout.browser_generations / str(second["generation"]) / "chrome-linux64/resources.pak"
    candidate.chmod(0o644)
    candidate.write_bytes(b"damaged\n")
    candidate.chmod(0o444)

    with pytest.raises(InstallerError):
        rollback_browser_generation(layout, str(second["generation"]))

    assert layout.browser_selector.read_bytes() == selector_before
    assert inspect_browser_runtime(layout)["generation"] == first["generation"]


def test_uncertain_generation_publication_is_preserved_and_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    _write_evidence(layout, manifest_path, manifest)
    generation = str(manifest["generation"])
    real_fsync = browser_runtime_module._fsync_directory

    def fail_parent_after_publication(path: Path) -> None:
        if path == layout.browser_generations and (path / generation).exists():
            raise OSError("injected parent fsync failure")
        real_fsync(path)

    monkeypatch.setattr(browser_runtime_module, "_fsync_directory", fail_parent_after_publication)
    with pytest.raises(InstallerError) as uncertain:
        _stage(layout, source, manifest_path, manifest_digest)
    assert uncertain.value.code == "browser_generation_publish_uncertain"
    assert (layout.browser_generations / generation).exists()

    monkeypatch.setattr(browser_runtime_module, "_fsync_directory", real_fsync)
    assert _stage(layout, source, manifest_path, manifest_digest)["reused"] is True
    assert verify_browser_generation(layout, generation)["generation"] == generation


def test_uncertain_selector_publication_is_preserved_and_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, manifest = _runtime_fixture(tmp_path)
    _write_evidence(layout, manifest_path, manifest)
    generation = str(_stage(layout, source, manifest_path, manifest_digest)["generation"])
    real_fsync = browser_runtime_module._fsync_directory

    def fail_selector_parent(path: Path) -> None:
        if path == layout.browser_selector.parent and layout.browser_selector.exists():
            raise OSError("injected selector parent fsync failure")
        real_fsync(path)

    monkeypatch.setattr(browser_runtime_module, "_fsync_directory", fail_selector_parent)
    with pytest.raises(InstallerError) as uncertain:
        activate_browser_generation(layout, generation)
    assert uncertain.value.code == "browser_selector_publish_uncertain"
    assert layout.browser_selector.exists()

    monkeypatch.setattr(browser_runtime_module, "_fsync_directory", real_fsync)
    assert activate_browser_generation(layout, generation)["reused"] is True
    assert inspect_browser_runtime(layout)["generation"] == generation


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root privilege boundary")
def test_canonical_browser_authority_requires_privilege_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home)})
    source, manifest_path, manifest_digest, _ = _runtime_fixture(tmp_path)

    with pytest.raises(InstallerError) as blocked:
        _stage(layout, source, manifest_path, manifest_digest)

    assert blocked.value.code == "browser_privilege_required"
