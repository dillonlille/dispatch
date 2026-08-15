from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch_installer.setup as setup_runtime
import dispatch_installer.launcher as launcher_runtime
from dispatch_installer.layout import InstallLayout, InstallerError
from dispatch_installer.manifest import load_manifest
from dispatch_installer.setup import (
    active_plugin_paths,
    complete_core_only_setup_migration,
    configure_plugins,
    load_installed_manifest,
    persist_release_manifest,
    prepare_core_only_setup_migration,
)

ROOT = Path(__file__).resolve().parents[2]


def test_installed_launcher_routes_setup_before_loading_core(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def run(layout, arguments):
        observed["layout"] = layout
        observed["arguments"] = arguments
        return 7

    monkeypatch.setattr(launcher_runtime, "run_setup", run)

    assert launcher_runtime.main(["setup", "--yes"]) == 7
    assert observed["arguments"] == ["--yes"]


def test_launcher_pairs_approved_plugin_ids_with_their_paths(monkeypatch, tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    core_path = layout.releases / "core-release" / "site-packages"
    plugin_path = layout.plugins / "handbook" / "releases" / "release" / "site-packages"
    core_path.mkdir(parents=True)
    plugin_path.mkdir(parents=True)
    observed: dict[str, object] = {}

    def command_main(arguments):
        observed["arguments"] = arguments
        observed["ids"] = os.environ["DISPATCH_ACTIVE_PLUGINS"]
        observed["paths"] = os.environ["DISPATCH_PLUGIN_PATHS"]
        return 0

    for name in (
        "DISPATCH_HOME",
        "DISPATCH_CODE_ROOT",
        "DISPATCH_CONFIG_ROOT",
        "DISPATCH_DATA_ROOT",
        "DISPATCH_STATE_ROOT",
        "DISPATCH_CACHE_ROOT",
        "DISPATCH_RUNTIME_ROOT",
        "DISPATCH_ACTIVE_PLUGINS",
        "DISPATCH_PLUGIN_PATHS",
    ):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(
        launcher_runtime,
        "InstallLayout",
        SimpleNamespace(from_environment=lambda: layout),
    )
    monkeypatch.setattr(
        launcher_runtime,
        "inspect_installation",
        lambda _layout: {"checks": {"core": {"status": "ready", "release_id": "core-release"}}},
    )
    monkeypatch.setattr(launcher_runtime, "active_plugins", lambda _layout: [("handbook", plugin_path)])
    monkeypatch.setattr(launcher_runtime.importlib, "import_module", lambda _name: SimpleNamespace(main=command_main))

    try:
        assert launcher_runtime.main(["plugin", "list"]) == 0
    finally:
        for path in (str(core_path), str(plugin_path)):
            while path in sys.path:
                sys.path.remove(path)

    assert observed == {
        "arguments": ["plugin", "list"],
        "ids": "handbook",
        "paths": str(plugin_path),
    }


MANIFEST = ROOT / "packaging" / "installation-release-manifest.json"


def _wheel(
    tmp_path: Path,
    *,
    license_bytes: bytes | None = None,
    entry_point_bytes: bytes | None = None,
) -> Path:
    wheel = tmp_path / "dispatch_local_handbook-0.1.0-py3-none-any.whl"
    root = "dispatch_local_handbook-0.1.0.dist-info/"
    members = {
        "dispatch_handbook/__init__.py": b"VALUE = 'installed'\n",
        f"{root}METADATA": (
            "Metadata-Version: 2.4\n"
            "Name: dispatch-local-handbook\n"
            "Version: 0.1.0\n"
            "License-Expression: Apache-2.0\n"
            "Requires-Python: <3.14,>=3.11\n"
            "Requires-Dist: dispatch-core==1.0.0\n"
            'Requires-Dist: pytest==9.1.1; extra == "dev"\n\n'
        ).encode(),
        f"{root}WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        f"{root}top_level.txt": b"dispatch_handbook\n",
        f"{root}entry_points.txt": (
            entry_point_bytes
            if entry_point_bytes is not None
            else b"[dispatch.plugins]\nhandbook = dispatch_handbook.service:handle\n"
        ),
        f"{root}licenses/LICENSE": (
            license_bytes if license_bytes is not None else (ROOT / "LICENSE").read_bytes()
        ),
    }
    rows = []
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append((name, f"sha256={digest}", str(len(content))))
    record_name = f"{root}RECORD"
    rows.append((record_name, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    members[record_name] = stream.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel


def _ready_manifest(tmp_path: Path, wheel: Path) -> tuple[Path, str]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    payload["ready"] = True
    payload["post_install"]["setup_implemented"] = True
    payload["builtin_plugins"] = [
        {
            "artifact": {"sha256": None, "size": None, "url": None},
            "capabilities": [],
            "id": "handbook",
            "package": "dispatch-local-handbook",
            "requires_dist": ['dispatch-core==1.0.0', 'pytest==9.1.1; extra == "dev"'],
            "version": "0.1.0",
        }
    ]
    filenames = (
        "dispatch_installer-0.1.5-py3-none-any.whl",
        "dispatch_core-1.0.0-py3-none-any.whl",
        wheel.name,
    )
    artifacts = (
        payload["installer"]["artifact"],
        payload["core"]["artifact"],
        payload["builtin_plugins"][0]["artifact"],
    )
    for index, (artifact, filename) in enumerate(zip(artifacts, filenames), start=1):
        artifact.update(
            url=f"https://dispatch.dillonlille.com/releases/0.0.7/{filename}",
            size=index,
            sha256=str(index) * 64,
        )
    payload["builtin_plugins"][0]["artifact"].update(
        size=wheel.stat().st_size,
        sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    runtime = tmp_path / "run"
    home.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime)})
    layout.prepare()
    return layout


def test_release_authority_is_persisted_for_later_setup(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    manifest_path, digest = _ready_manifest(tmp_path, wheel)
    layout = _layout(tmp_path)

    persisted = persist_release_manifest(
        layout,
        manifest_path,
        expected_sha256=digest,
        product_version="0.0.7",
    )
    loaded = load_installed_manifest(layout)

    assert persisted.read_bytes() == manifest_path.read_bytes()
    assert loaded.product_version == "0.0.7"
    assert loaded.builtin_plugins[0].id == "handbook"


def test_builtin_plugin_requires_exact_apache_license(tmp_path: Path, monkeypatch) -> None:
    wheel = _wheel(tmp_path, license_bytes=b"not the approved license\n")
    manifest_path, digest = _ready_manifest(tmp_path, wheel)
    layout = _layout(tmp_path)
    persist_release_manifest(layout, manifest_path, expected_sha256=digest, product_version="0.0.7")

    def download(_url, destination, **_policy):
        destination.write_bytes(wheel.read_bytes())
        destination.chmod(0o600)
        return {"path": str(destination)}

    monkeypatch.setattr(setup_runtime, "download_release_artifact", download)
    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, ["handbook"])

    assert error.value.code == "plugin_wheel_license"


def test_builtin_plugin_requires_matching_discovery_entry_point(tmp_path: Path, monkeypatch) -> None:
    wheel = _wheel(tmp_path, entry_point_bytes=b"[dispatch.plugins]\nother = dispatch_handbook.service:handle\n")
    manifest_path, digest = _ready_manifest(tmp_path, wheel)
    layout = _layout(tmp_path)
    persist_release_manifest(layout, manifest_path, expected_sha256=digest, product_version="0.0.7")

    def download(_url, destination, **_policy):
        destination.write_bytes(wheel.read_bytes())
        destination.chmod(0o600)
        return {"path": str(destination)}

    monkeypatch.setattr(setup_runtime, "download_release_artifact", download)
    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, ["handbook"])

    assert error.value.code == "plugin_wheel_entry_point"


def test_selected_builtin_plugin_is_verified_activated_and_receipted(tmp_path: Path, monkeypatch) -> None:
    wheel = _wheel(tmp_path)
    manifest_path, digest = _ready_manifest(tmp_path, wheel)
    layout = _layout(tmp_path)
    persist_release_manifest(layout, manifest_path, expected_sha256=digest, product_version="0.0.7")

    def download(url, destination, **_policy):
        destination.write_bytes(wheel.read_bytes())
        destination.chmod(0o600)
        return {"path": str(destination)}

    monkeypatch.setattr(setup_runtime, "download_release_artifact", download)
    monkeypatch.setattr(
        setup_runtime.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = configure_plugins(layout, ["handbook"])
    paths = active_plugin_paths(layout)
    receipt = json.loads((layout.state / "install" / "setup.json").read_text(encoding="utf-8"))

    assert result["status"] == "complete"
    assert receipt["selected_plugins"] == ["handbook"]
    assert len(paths) == 1
    assert (paths[0] / "dispatch_handbook" / "__init__.py").read_text(encoding="utf-8") == "VALUE = 'installed'\n"
    assert (layout.plugins / "handbook" / "active.json").is_file()

    package = paths[0] / "dispatch_handbook" / "__init__.py"
    package.chmod(0o600)
    package.write_text("VALUE = 'tampered'\n", encoding="utf-8")
    with pytest.raises(InstallerError, match="differs from its receipt"):
        active_plugin_paths(layout)


def test_core_only_setup_is_a_valid_explicit_selection(tmp_path: Path, monkeypatch) -> None:
    wheel = _wheel(tmp_path)
    manifest_path, digest = _ready_manifest(tmp_path, wheel)
    layout = _layout(tmp_path)
    persist_release_manifest(layout, manifest_path, expected_sha256=digest, product_version="0.0.7")
    monkeypatch.setattr(
        setup_runtime.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = configure_plugins(layout, [])

    assert result == {"status": "complete", "selected_plugins": [], "plugins": []}
    assert active_plugin_paths(layout) == []


def test_empty_core_only_setup_migrates_durably_between_product_manifests(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    payload = json.loads((ROOT / "packaging" / "installation-release-manifest.json").read_text())
    payload["ready"] = True
    payload["installer"]["artifact"].update(
        url="https://dispatch.dillonlille.com/releases/0.0.7/dispatch_installer-0.1.5-py3-none-any.whl",
        size=1,
        sha256="1" * 64,
    )
    payload["core"]["artifact"].update(
        url="https://dispatch.dillonlille.com/releases/0.0.7/dispatch_core-1.0.0-py3-none-any.whl",
        size=1,
        sha256="1" * 64,
    )
    old_payload = json.loads(json.dumps(payload))
    old_payload["product"]["version"] = "0.0.1"
    old_payload["installer"]["artifact"]["url"] = (
        "https://dispatch.dillonlille.com/releases/0.0.1/dispatch_installer-0.1.5-py3-none-any.whl"
    )
    old_payload["core"]["artifact"]["url"] = (
        "https://dispatch.dillonlille.com/releases/0.0.1/dispatch_core-1.0.0-py3-none-any.whl"
    )
    old_path = tmp_path / "old.json"
    old_path.write_text(json.dumps(old_payload), encoding="utf-8")
    old_digest = hashlib.sha256(old_path.read_bytes()).hexdigest()
    new_path = tmp_path / "new.json"
    new_path.write_text(json.dumps(payload), encoding="utf-8")
    new_digest = hashlib.sha256(new_path.read_bytes()).hexdigest()
    target = load_manifest(new_path, expected_sha256=new_digest)
    persist_release_manifest(layout, old_path, expected_sha256=old_digest, product_version="0.0.1")
    setup_runtime.atomic_json(
        layout.state / "install" / "setup.json",
        {
            "schema_version": 1,
            "status": "complete",
            "product_version": "0.0.1",
            "selected_plugins": [],
            "plugins": [],
            "contains_secrets": False,
        },
        mode=0o600,
    )

    assert prepare_core_only_setup_migration(layout, target) is True
    persist_release_manifest(layout, new_path, expected_sha256=new_digest, product_version="0.0.7")
    assert prepare_core_only_setup_migration(layout, target) is True
    complete_core_only_setup_migration(layout, target)

    setup = json.loads((layout.state / "install" / "setup.json").read_text())
    assert setup["product_version"] == "0.0.7"
    assert setup["selected_plugins"] == []
    assert not (layout.state / "install" / "setup-migration.json").exists()
