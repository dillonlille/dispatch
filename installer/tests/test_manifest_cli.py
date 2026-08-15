from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import dispatch_installer.cli as cli_module
from dispatch_installer.cli import main
from dispatch_installer.layout import InstallerError
from dispatch_installer.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "packaging" / "installation-release-manifest.json"


def _handbook_plugin() -> dict:
    return {
        "artifact": {"sha256": None, "size": None, "url": None},
        "capabilities": [],
        "id": "handbook",
        "package": "dispatch-local-handbook",
        "requires_dist": ['dispatch-core==1.0.0', 'pytest==9.1.1; extra == "dev"'],
        "version": "0.1.0",
    }


def test_incomplete_repository_manifest_is_valid_and_fail_closed() -> None:
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    manifest = load_manifest(MANIFEST, expected_sha256=digest)

    assert manifest.ready is False
    assert manifest.product_version == "0.0.3"
    assert manifest.installer_version == "0.1.2"
    assert manifest.installer_artifact.complete is False
    assert manifest.core_version == "1.0.0"
    assert manifest.core_artifact_url is None
    assert manifest.builtin_plugins == ()
    assert manifest.browser_ready is False
    assert manifest.browser_install_phase == "setup"
    assert manifest.setup_implemented is True
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


def test_schema_v1_rejects_partial_ready_manifest(tmp_path: Path) -> None:
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

    assert error.value.code == "manifest_partial_artifact"


def test_complete_ready_manifest_authorizes_core_policy(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["ready"] = True
    payload["post_install"]["setup_implemented"] = True
    artifacts = [payload["installer"]["artifact"], payload["core"]["artifact"]]
    filenames = [
        "dispatch_installer-0.1.2-py3-none-any.whl",
        "dispatch_core-1.0.0-py3-none-any.whl",
    ]
    for index, (artifact, filename) in enumerate(zip(artifacts, filenames)):
        artifact.update(
            url=f"https://dispatch.dillonlille.com/releases/0.0.3/{filename}",
            size=index + 1,
            sha256=str(index) * 64,
        )
    path = tmp_path / "ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    assert manifest.ready is True
    assert dict(manifest.core_package_files)["dispatch_core/__init__.py"]
    assert any('extra == "browser"' in item for item in manifest.core_requires_dist)


def test_install_uses_ready_manifest_core_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "run"
    home.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    wheel = tmp_path / "core.whl"
    wheel.write_bytes(b"reviewed-core")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["ready"] = True
    payload["post_install"]["setup_implemented"] = True
    payload["installer"]["artifact"] = {
        "url": "https://dispatch.dillonlille.com/releases/0.0.3/dispatch_installer-0.1.2-py3-none-any.whl",
        "size": 1,
        "sha256": "1" * 64,
    }
    payload["core"]["artifact"] = {
        "url": "https://dispatch.dillonlille.com/releases/0.0.3/dispatch_core-1.0.0-py3-none-any.whl",
        "size": wheel.stat().st_size,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }

    path = tmp_path / "ready.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict[str, object] = {}

    def install(layout, selected_wheel, **policy):
        captured.update({"layout": layout, "wheel": selected_wheel, **policy})
        return {"status": "installed", "launcher": "/tmp/dispatch", "setup_required": True}

    monkeypatch.setattr(cli_module, "install_core_application", install)
    monkeypatch.setattr(cli_module, "persist_release_manifest", lambda *args, **kwargs: path)
    monkeypatch.setattr(
        cli_module,
        "install_user_service",
        lambda layout, launcher: {"status": "active", "unit": "/tmp/dispatch-core.service"},
    )
    monkeypatch.setattr(
        cli_module,
        "inspect_installation",
        lambda layout: {"ok": True, "status": "ready", "checks": {}},
    )
    result = main(
        [
            "--dispatch-home",
            str(tmp_path / "home" / ".dispatch"),
            "install",
            "--manifest",
            str(path),
            "--manifest-sha256",
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "--core-wheel",
            str(wheel),
            "--yes",
        ]
    )

    assert result == 0
    assert captured["wheel"] == wheel
    assert captured["expected_sha256"] == payload["core"]["artifact"]["sha256"]
    assert captured["expected_package_files"] == dict(
        (item["path"], item["sha256"]) for item in payload["core"]["package_files"]
    )
    transaction = json.loads((home / ".dispatch" / "state" / "install" / "install-transaction.json").read_text())
    assert transaction["phase"] == "complete"
    assert transaction["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert json.loads(capsys.readouterr().out)["status"] == "installed"


def test_install_refuses_untracked_dispatch_command_before_core_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "run"
    home.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    local = home / ".local"
    local.mkdir(mode=0o700)
    command_directory = local / "bin"
    command_directory.mkdir(mode=0o700)
    command = command_directory / "dispatch"
    command.write_text("#!/bin/sh\nexec false\n", encoding="utf-8")
    command.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    wheel = tmp_path / "core.whl"
    wheel.write_bytes(b"reviewed-core")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["ready"] = True
    payload["installer"]["artifact"] = {
        "url": "https://dispatch.dillonlille.com/releases/0.0.3/dispatch_installer-0.1.2-py3-none-any.whl",
        "size": 1,
        "sha256": "1" * 64,
    }
    payload["core"]["artifact"] = {
        "url": "https://dispatch.dillonlille.com/releases/0.0.3/dispatch_core-1.0.0-py3-none-any.whl",
        "size": wheel.stat().st_size,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    manifest = tmp_path / "ready.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    called = False

    def install(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(cli_module, "install_core_application", install)
    result = main(
        [
            "install",
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "--core-wheel",
            str(wheel),
            "--yes",
        ]
    )
    response = json.loads(capsys.readouterr().out)

    assert result == 1
    assert response["error"]["code"] == "command_conflict"
    assert called is False
    assert command.read_text(encoding="utf-8") == "#!/bin/sh\nexec false\n"


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


def test_manifest_rejects_undeclared_plugin_shapes(tmp_path: Path) -> None:
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


def test_manifest_rejects_duplicate_or_invalid_builtin_plugins(tmp_path: Path) -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["builtin_plugins"] = [_handbook_plugin()]
    payload["builtin_plugins"].append(dict(payload["builtin_plugins"][0]))
    path = tmp_path / "duplicate-plugin.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallerError) as duplicate:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert duplicate.value.code == "manifest_builtin_plugin_duplicate"

    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["builtin_plugins"] = [_handbook_plugin()]
    payload["builtin_plugins"][0]["capabilities"] = ["browser", "browser"]
    path = tmp_path / "duplicate-capability.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InstallerError) as capability:
        load_manifest(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert capability.value.code == "manifest_builtin_plugin"


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
    assert payload["data"]["manifest"]["product_version"] == "0.0.3"
    assert payload["data"]["manifest"]["builtin_plugins"] == []
    assert payload["data"]["manifest"]["browser_ready"] is False
    assert payload["data"]["manifest"]["browser_install_phase"] == "setup"
    assert payload["data"]["manifest"]["setup_implemented"] is True
    assert payload["data"]["manifest"]["setup_command"] == "dispatch setup"
    assert payload["data"]["manifest"]["uninstall_user_scope_implemented"] is True
    assert payload["data"]["manifest"]["uninstall_default_mode"] == "keep-data"
    assert payload["data"]["manifest"]["uninstall_privileged_browser_removal_implemented"] is False
    assert not home.exists()
