"""Tests for harness detection, catalog, and selection recording."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from dispatch_installer import harness
from dispatch_installer.layout import InstallerError


def _make_spec(tmp_path: Path) -> harness.HarnessSpec:
    payload = b"#!/bin/sh\nexit 0\n"
    return harness.HarnessSpec(
        id="test-harness",
        display_name="Test Harness",
        description="",
        installer_url="https://example.invalid/install.sh",
        installer_digest=hashlib.sha256(payload).hexdigest(),
        installer_flags=("--quiet",),
        home_env="TEST_HARNESS_HOME",
        default_home=".test-harness",
        launcher="test-harness-launcher",
        minimum_version=(0, 1, 0),
    )


def test_catalog_contains_only_hermes():
    assert set(harness.HARNESS_CATALOG) == {"hermes"}
    spec = harness.HERMES_SPEC
    assert spec.installer_url.startswith("https://")
    assert "--skip-setup" in spec.installer_flags
    assert "--no-skills" in spec.installer_flags


def test_parse_version_variants():
    assert harness.parse_version("Hermes Agent v0.20.5 (2026.8.19)") == (0, 20, 5)
    assert harness.parse_version("1.2.3") == (1, 2, 3)
    assert harness.parse_version("garbage") is None
    assert harness.parse_version("") is None


def test_detect_absent_when_home_missing(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(tmp_path / "missing"))
    result = harness.detect_harness(spec)
    assert result.status == "absent"


def test_detect_ready_with_fake_launcher(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    home = tmp_path / "harness-home"
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    launcher = home / "hermes-agent" / "venv" / "bin" / spec.launcher
    launcher.write_text("#!/bin/sh\necho 'v1.2.3 ready'\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(home))
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    result = harness.detect_harness(spec)
    assert result.status == "ready"
    assert result.version.startswith("v1.2.3")


def test_detect_unhealthy_on_bad_version(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    home = tmp_path / "harness-home"
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    launcher = home / "hermes-agent" / "venv" / "bin" / spec.launcher
    launcher.write_text("#!/bin/sh\necho 'not-a-version'\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(home))
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    result = harness.detect_harness(spec)
    assert result.status == "unhealthy"


def test_detect_unhealthy_below_floor(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    home = tmp_path / "harness-home"
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    launcher = home / "hermes-agent" / "venv" / "bin" / spec.launcher
    launcher.write_text("#!/bin/sh\necho 'v0.0.1'\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(home))
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    result = harness.detect_harness(spec)
    assert result.status == "unhealthy"
    assert "floor" in result.detail


def test_install_refuses_without_authorization(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(tmp_path / "missing"))
    with pytest.raises(InstallerError) as error:
        harness.install_harness(spec)
    assert error.value.code == "harness_install_unauthorized"


def test_install_refuses_when_unhealthy(tmp_path, monkeypatch):
    spec = _make_spec(tmp_path)
    home = tmp_path / "harness-home"
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    launcher = home / "hermes-agent" / "venv" / "bin" / spec.launcher
    launcher.write_text("#!/bin/sh\nexit 3\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("TEST_HARNESS_HOME", str(home))
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    with pytest.raises(InstallerError) as error:
        harness.install_harness(spec, allow_install=True)
    assert error.value.code == "harness_unhealthy"


def test_selection_roundtrip(tmp_path):
    config_root = tmp_path / "config"
    config_root.mkdir(mode=0o700)
    assert harness.load_selection(config_root) is None
    detection = harness.DetectionResult("ready", version="v1.0.0", home="/tmp/x")
    harness.write_selection(config_root, harness.HERMES_SPEC, detection)
    assert harness.load_selection(config_root) == "hermes"
    record = json.loads((config_root / "harness.json").read_text())
    assert record["contains_secrets"] is False
    mode = os.stat(config_root / "harness.json").st_mode & 0o777
    assert mode == 0o600


def test_load_selection_rejects_unknown_id(tmp_path):
    config_root = tmp_path / "config"
    config_root.mkdir(mode=0o700)
    (config_root / "harness.json").write_text(json.dumps({"selected": "unknown-harness"}))
    with pytest.raises(InstallerError) as error:
        harness.load_selection(config_root)
    assert error.value.code == "harness_record_invalid"
