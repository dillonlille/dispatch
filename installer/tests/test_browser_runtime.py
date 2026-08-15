from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from dispatch_installer.browser_runtime import (
    activate_browser_generation,
    inspect_browser_runtime,
    load_browser_runtime_manifest,
    rollback_browser_generation,
    stage_browser_runtime,
    verify_browser_generation,
)
from dispatch_installer.layout import InstallLayout, InstallerError


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    runtime_parent = tmp_path / "run"
    system = tmp_path / "system"
    home.mkdir(mode=0o700)
    runtime_parent.mkdir(mode=0o700)
    system.mkdir(mode=0o755)
    layout = InstallLayout.from_environment(
        {"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime_parent)}
    )
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
) -> tuple[Path, Path, str, Path, str, dict[str, object]]:
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
        "platform": {
            "system": "linux",
            "distribution": "ubuntu",
            "distribution_version": "24.04",
            "architecture": "x86_64",
        },
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
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    browser_digest = hashlib.sha256(files["chrome-linux64/chrome"]).hexdigest()
    evidence = {
        "schema_version": 1,
        "generation": generation,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "system": "linux",
            "distribution": "ubuntu",
            "distribution_version": "24.04",
            "architecture": "x86_64",
        },
        "os_dependencies": {"verified": True, "receipt_sha256": "1" * 64},
        "sandbox": {
            "verified": True,
            "policy_id": "dispatch-chromium-apparmor-v1",
            "receipt_sha256": "2" * 64,
        },
        "launch_probe": {"passed": True, "executable_sha256": browser_digest},
    }
    evidence_path = tmp_path / f"{generation}-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)
    return source, manifest_path, _digest(manifest_path), evidence_path, _digest(evidence_path), manifest


def _stage(
    layout: InstallLayout,
    source: Path,
    manifest: Path,
    digest: str,
    evidence: Path,
    evidence_digest: str,
) -> dict[str, str | int | bool]:
    return stage_browser_runtime(
        layout,
        source,
        manifest,
        evidence,
        expected_manifest_sha256=digest,
        expected_evidence_sha256=evidence_digest,
    )


def test_browser_generation_staging_activation_and_reuse(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, evidence_path, evidence_digest, _ = _runtime_fixture(tmp_path)

    staged = _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)
    activated = activate_browser_generation(layout, str(staged["generation"]))
    reused = _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)
    verified = verify_browser_generation(layout, str(staged["generation"]))
    generation_root = layout.browser_generations / str(staged["generation"])

    assert staged["reused"] is False
    assert reused["reused"] is True
    assert activated == {
        "generation": "chromium-151.0.7922.34-r1234-a",
        "previous_generation": None,
        "reused": False,
    }
    assert verified["files"] == 7
    assert stat.S_IMODE(generation_root.stat().st_mode) == 0o555
    assert stat.S_IMODE((generation_root / "chrome-linux64/chrome").stat().st_mode) == 0o555
    assert stat.S_IMODE((generation_root / "chrome-linux64/resources.pak").stat().st_mode) == 0o444
    assert stat.S_IMODE(layout.browser_selector.stat().st_mode) == 0o444
    assert inspect_browser_runtime(layout)["status"] == "verified"


def test_tampered_or_extra_source_is_rejected_before_authority_mutation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, evidence_path, evidence_digest, _ = _runtime_fixture(tmp_path)
    resource = source / "chrome-linux64/resources.pak"
    resource.write_bytes(b"changed\n")

    with pytest.raises(InstallerError) as changed:
        _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)
    assert changed.value.code in {"browser_source_unsafe", "browser_source_digest"}
    assert not layout.browser_generations.exists()

    resource.write_bytes(b"trusted resource\n")
    extra = source / "chrome-linux64/unapproved.so"
    extra.write_bytes(b"extra")
    with pytest.raises(InstallerError) as unapproved:
        _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)
    assert unapproved.value.code == "browser_source_scope"
    assert not layout.browser_generations.exists()


def test_manifest_is_closed_and_local_installation_evidence_is_digest_bound(tmp_path: Path) -> None:
    source, manifest_path, manifest_digest, evidence_path, evidence_digest, manifest = _runtime_fixture(tmp_path)
    layout = _layout(tmp_path)
    assert load_browser_runtime_manifest(manifest_path, expected_sha256=manifest_digest).generation.endswith("-a")

    with pytest.raises(InstallerError) as wrong_digest:
        load_browser_runtime_manifest(manifest_path, expected_sha256="0" * 64)
    assert wrong_digest.value.code == "browser_manifest_digest"

    manifest["plugins"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InstallerError) as unknown:
        load_browser_runtime_manifest(manifest_path, expected_sha256=_digest(manifest_path))
    assert unknown.value.code == "browser_manifest_shape"

    manifest.pop("plugins")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_digest = _digest(manifest_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    launch_probe = evidence["launch_probe"]
    assert isinstance(launch_probe, dict)
    launch_probe["passed"] = False
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(InstallerError) as incomplete:
        _stage(
            layout,
            source,
            manifest_path,
            manifest_digest,
            evidence_path,
            _digest(evidence_path),
        )
    assert incomplete.value.code == "browser_evidence_incomplete"

    with pytest.raises(InstallerError) as stale_digest:
        _stage(
            layout,
            source,
            manifest_path,
            manifest_digest,
            evidence_path,
            evidence_digest,
        )
    assert stale_digest.value.code == "browser_evidence_digest"


def test_full_tree_digest_and_mode_tampering_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    source, manifest_path, manifest_digest, evidence_path, evidence_digest, _ = _runtime_fixture(tmp_path)
    staged = _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)
    generation = str(staged["generation"])
    resource = layout.browser_generations / generation / "chrome-linux64/resources.pak"
    resource.chmod(0o644)
    resource.write_bytes(b"tampered resource\n")
    resource.chmod(0o444)

    with pytest.raises(InstallerError) as tampered:
        verify_browser_generation(layout, generation)
    assert tampered.value.code == "browser_tree_mismatch"

    resource.chmod(0o555)
    with pytest.raises(InstallerError) as wrong_mode:
        verify_browser_generation(layout, generation)
    assert wrong_mode.value.code == "browser_generation_unsafe"


def test_activation_tracks_previous_and_rollback_is_reversible(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first_source, first_manifest, first_digest, first_evidence, first_evidence_digest, _ = _runtime_fixture(
        tmp_path / "first", generation="chromium-151.0.7922.34-r1234-a"
    )
    second_source, second_manifest, second_digest, second_evidence, second_evidence_digest, _ = _runtime_fixture(
        tmp_path / "second",
        generation="chromium-151.0.7922.34-r1234-b",
        resource=b"second resource\n",
    )
    first = _stage(layout, first_source, first_manifest, first_digest, first_evidence, first_evidence_digest)
    second = _stage(layout, second_source, second_manifest, second_digest, second_evidence, second_evidence_digest)

    activate_browser_generation(layout, str(first["generation"]))
    switched = activate_browser_generation(layout, str(second["generation"]))
    rolled_back = rollback_browser_generation(layout)
    rolled_forward = rollback_browser_generation(layout)

    assert switched["previous_generation"] == first["generation"]
    assert rolled_back == {
        "generation": first["generation"],
        "previous_generation": second["generation"],
    }
    assert rolled_forward == {
        "generation": second["generation"],
        "previous_generation": first["generation"],
    }
    assert inspect_browser_runtime(layout)["generation"] == second["generation"]


def test_invalid_candidate_does_not_change_active_selector(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    first_source, first_manifest, first_digest, first_evidence, first_evidence_digest, _ = _runtime_fixture(
        tmp_path / "first", generation="chromium-151.0.7922.34-r1234-a"
    )
    second_source, second_manifest, second_digest, second_evidence, second_evidence_digest, _ = _runtime_fixture(
        tmp_path / "second", generation="chromium-151.0.7922.34-r1234-b"
    )
    first = _stage(layout, first_source, first_manifest, first_digest, first_evidence, first_evidence_digest)
    second = _stage(layout, second_source, second_manifest, second_digest, second_evidence, second_evidence_digest)
    activate_browser_generation(layout, str(first["generation"]))
    selector_before = layout.browser_selector.read_bytes()
    candidate_resource = layout.browser_generations / str(second["generation"]) / "chrome-linux64/resources.pak"
    candidate_resource.chmod(0o644)
    candidate_resource.write_bytes(b"damaged\n")
    candidate_resource.chmod(0o444)

    with pytest.raises(InstallerError):
        activate_browser_generation(layout, str(second["generation"]))

    assert layout.browser_selector.read_bytes() == selector_before
    assert inspect_browser_runtime(layout)["generation"] == first["generation"]


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root privilege boundary")
def test_canonical_browser_authority_requires_privilege_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home)})
    source, manifest_path, manifest_digest, evidence_path, evidence_digest, _ = _runtime_fixture(tmp_path)

    with pytest.raises(InstallerError) as blocked:
        _stage(layout, source, manifest_path, manifest_digest, evidence_path, evidence_digest)

    assert blocked.value.code == "browser_privilege_required"
