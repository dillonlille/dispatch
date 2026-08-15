from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dispatch_installer.cli import main
from dispatch_installer.layout import InstallerError
from dispatch_installer.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "installation-release-manifest.json"


def test_incomplete_repository_manifest_is_valid_and_fail_closed() -> None:
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    manifest = load_manifest(MANIFEST, expected_sha256=digest)

    assert manifest.ready is False
    assert manifest.core_version == "1.0.0"
    assert manifest.core_artifact_url is None
    assert manifest.browser_ready is False
    assert manifest.setup_implemented is False
    assert manifest.setup_command == "dispatch setup"
    assert manifest.uninstall_user_scope_implemented is True
    assert manifest.uninstall_administrative_command == "dispatch-installer uninstall"
    assert manifest.uninstall_future_user_command == "dispatch uninstall"
    assert manifest.uninstall_default_mode == "keep-data"
    assert manifest.uninstall_purge_requires_confirmation is True
    assert manifest.uninstall_privileged_browser_removal_implemented is False


def test_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    copied = tmp_path / "manifest.json"
    copied.write_bytes(MANIFEST.read_bytes())
    digest = hashlib.sha256(copied.read_bytes()).hexdigest()
    payload = json.loads(copied.read_text(encoding="utf-8"))
    payload["ready"] = True
    copied.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InstallerError, match="SHA-256 mismatch"):
        load_manifest(copied, expected_sha256=digest)


def test_schema_v1_rejects_all_ready_manifests(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["ready"] = True
    payload["core"]["artifact"] = {
        "url": "https://example.invalid/dispatch-core.whl",
        "size": 1,
        "sha256": "0" * 64,
    }
    payload["browser_runtime"]["ready"] = True
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    assert error.value.code == "manifest_ready_unsupported"


def test_scalar_manifest_uses_bounded_shape_error(tmp_path: Path) -> None:
    path = tmp_path / "scalar.json"
    path.write_text("1\n", encoding="utf-8")
    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert error.value.code == "manifest_shape"


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    content = MANIFEST.read_text(encoding="utf-8").replace(
        '"ready": false,',
        '"ready": false,\n  "ready": false,',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    assert error.value.code == "manifest_json_duplicate"


def test_manifest_rejects_plugin_declarations(tmp_path: Path) -> None:
    for key, value in (("plugins", []), ("plugin_artifacts", [])):
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        payload[key] = value
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(InstallerError) as error:
            load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        assert error.value.code == "manifest_shape"

    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["core"]["plugins"] = []
    path = tmp_path / "nested-plugin.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert error.value.code == "manifest_core"


def test_manifest_rejects_boolean_schema_version(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    path = tmp_path / "boolean-version.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert error.value.code == "manifest_version"


def test_schema_v1_rejects_browser_ready_when_release_is_not_ready(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["browser_runtime"]["ready"] = True
    path = tmp_path / "browser-ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallerError) as error:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert error.value.code == "manifest_browser_ready"


def test_prepare_cli_requires_explicit_confirmation(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    result = main(["prepare"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 1
    assert payload["error"]["code"] == "confirmation_required"
    assert not home.exists()


def test_plan_reports_current_publication_blocker(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()

    result = main(["plan", "--manifest", str(MANIFEST), "--manifest-sha256", digest])
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "blocked"
    assert payload["data"]["manifest"]["browser_ready"] is False
    assert payload["data"]["manifest"]["setup_implemented"] is False
    assert payload["data"]["manifest"]["setup_command"] == "dispatch setup"
    assert payload["data"]["manifest"]["uninstall_user_scope_implemented"] is True
    assert payload["data"]["manifest"]["uninstall_default_mode"] == "keep-data"
    assert payload["data"]["manifest"]["uninstall_privileged_browser_removal_implemented"] is False
    assert not home.exists()
