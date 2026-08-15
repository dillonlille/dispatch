from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

import dispatch_installer.doctor as doctor_module
from dispatch_installer.core_release import (
    activate_core_release,
    sha256_file,
    stage_core_wheel as _stage_core_wheel,
    verify_core_release,
)
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.layout import InstallLayout, InstallerError


def layout_for(tmp_path: Path) -> InstallLayout:
    layout = InstallLayout.from_environment(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    layout.home.mkdir(parents=True, mode=0o700)
    layout.runtime.parent.mkdir(parents=True, mode=0o700)
    return layout


def make_wheel(
    path: Path,
    *,
    value: str = "1.0.0",
    corrupt_record: bool = False,
    extra: dict[str, bytes] | None = None,
) -> Path:
    files = {
        "dispatch_core/__init__.py": f'__version__ = "{value}"\n'.encode(),
        "dispatch_core-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: dispatch-core\nVersion: 1.0.0\n"
            b"Requires-Python: <3.14,>=3.11\n"
            b"Requires-Dist: cryptography==48.0.1\nRequires-Dist: playwright==1.62.0\n\n"
        ),
        "dispatch_core-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: dispatch-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "dispatch_core-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\ndispatch-core = dispatch_core.command_interface:main\n"
        ),
        "dispatch_core-1.0.0.dist-info/top_level.txt": b"dispatch_core\n",
    }
    files.update(extra or {})
    record_name = "dispatch_core-1.0.0.dist-info/RECORD"
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, data in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if corrupt_record and name == "dispatch_core/__init__.py":
            encoded = "A" * len(encoded)
        writer.writerow((name, f"sha256={encoded}", str(len(data))))
    writer.writerow((record_name, "", ""))
    files[record_name] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    path.chmod(0o644)
    return path


def stage_core_wheel(layout: InstallLayout, wheel: Path, **kwargs):
    with zipfile.ZipFile(wheel) as archive:
        package = archive.read("dispatch_core/__init__.py")
    return _stage_core_wheel(
        layout,
        wheel,
        expected_package_files={"dispatch_core/__init__.py": hashlib.sha256(package).hexdigest()},
        expected_requires_dist={"cryptography==48.0.1", "playwright==1.62.0"},
        **kwargs,
    )


def test_core_release_staging_activation_and_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    digest = sha256_file(wheel)

    staged = stage_core_wheel(layout, wheel, expected_sha256=digest, expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    activated = activate_core_release(layout, release)
    release.chmod(0o700)
    reused = stage_core_wheel(layout, wheel, expected_sha256=digest, expected_version="1.0.0")

    assert staged["reused"] is False
    assert reused["reused"] is True
    assert activated["version"] == "1.0.0"
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    assert stat.S_IMODE(layout.active_release_selector.stat().st_mode) == 0o600
    monkeypatch.setattr(
        doctor_module,
        "inspect_browser_runtime",
        lambda unused_layout: {"status": "verified", "generation": "synthetic"},
    )
    report = inspect_installation(layout)
    assert report["checks"]["core"]["status"] == "ready"
    assert report["checks"]["browser_authority"]["status"] == "verified"
    assert report["checks"]["production_release"]["status"] == "blocked"
    assert report["checks"]["browser_launch_composition"]["status"] == "blocked"
    assert report["ok"] is False
    assert report["status"] == "incomplete"


def test_unsafe_artifact_alias_and_mode_are_rejected(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    wheel = make_wheel(real / "dispatch_core-1.0.0-py3-none-any.whl")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    aliased_wheel = alias / wheel.name

    with pytest.raises(InstallerError, match="non-symlink"):
        stage_core_wheel(layout, aliased_wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    hardlink = tmp_path / "artifact-hardlink.whl"
    os.link(wheel, hardlink)
    with pytest.raises(InstallerError, match="link count"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    hardlink.unlink()

    wheel.chmod(0o664)
    with pytest.raises(InstallerError, match="ownership, link count, or mode"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert not layout.dispatch_home.exists()


def test_tampered_artifact_is_rejected_without_activation(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")

    with pytest.raises(InstallerError, match="SHA-256 mismatch"):
        stage_core_wheel(layout, wheel, expected_sha256="0" * 64, expected_version="1.0.0")

    assert not layout.active_release_selector.exists()
    assert not layout.dispatch_home.exists()


def test_tampered_installed_member_is_rejected(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    member = release / "site-packages" / "dispatch_core" / "__init__.py"
    member.chmod(0o644)
    member.write_text('__version__ = "changed"\n', encoding="utf-8")

    with pytest.raises(InstallerError, match="member differs"):
        verify_core_release(release)


def test_tampered_release_directory_mode_is_rejected(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    package_directory = layout.releases / str(staged["release_id"]) / "site-packages" / "dispatch_core"
    package_directory.chmod(0o755)

    with pytest.raises(InstallerError, match="directory mode"):
        verify_core_release(layout.releases / str(staged["release_id"]))


def test_hardlinked_release_member_is_rejected(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    member = release / "site-packages" / "dispatch_core" / "__init__.py"
    os.link(member, tmp_path / "external-hardlink")

    with pytest.raises(InstallerError, match="hard-linked"):
        verify_core_release(release)


def test_self_consistent_rewrite_cannot_be_reused_for_verified_artifact(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    digest = sha256_file(wheel)
    staged = stage_core_wheel(layout, wheel, expected_sha256=digest, expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    member = release / "site-packages" / "dispatch_core" / "__init__.py"
    member.chmod(0o644)
    member.write_bytes(b'x = "rewritten"\n')
    member.chmod(0o444)
    manifest_path = release / "tree-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"].endswith("dispatch_core/__init__.py"))
    entry["size"] = member.stat().st_size
    entry["sha256"] = sha256_file(member)
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest_path.chmod(0o444)
    assert verify_core_release(release)["version"] == "1.0.0"

    with pytest.raises(InstallerError, match="differs from artifact"):
        stage_core_wheel(layout, wheel, expected_sha256=digest, expected_version="1.0.0")


def test_bad_wheel_record_is_rejected_and_stage_is_cleaned(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl", corrupt_record=True)

    with pytest.raises(InstallerError, match="RECORD mismatch"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert not layout.dispatch_home.exists()


def test_plugin_bytes_inside_core_namespace_are_rejected_before_layout_mutation(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(
        tmp_path / "dispatch_core-1.0.0-py3-none-any.whl",
        extra={"dispatch_core/plugins/handbook.py": b"PLUGIN = True\n"},
    )

    with pytest.raises(InstallerError) as error:
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert error.value.code == "wheel_package_scope"
    assert not layout.dispatch_home.exists()

    with zipfile.ZipFile(wheel) as archive:
        core_digest = hashlib.sha256(archive.read("dispatch_core/__init__.py")).hexdigest()
        plugin_digest = hashlib.sha256(archive.read("dispatch_core/plugins/handbook.py")).hexdigest()
    with pytest.raises(InstallerError) as policy_error:
        _stage_core_wheel(
            layout,
            wheel,
            expected_sha256=sha256_file(wheel),
            expected_version="1.0.0",
            expected_package_files={
                "dispatch_core/__init__.py": core_digest,
                "dispatch_core/plugins/handbook.py": plugin_digest,
            },
            expected_requires_dist={"cryptography==48.0.1", "playwright==1.62.0"},
        )
    assert policy_error.value.code == "wheel_package_policy"
    assert not layout.dispatch_home.exists()


def test_active_dependency_cannot_hide_behind_extra_marker(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    metadata = (
        b"Metadata-Version: 2.1\nName: dispatch-core\nVersion: 1.0.0\n"
        b"Requires-Python: <3.14,>=3.11\n"
        b"Requires-Dist: cryptography==48.0.1\n"
        b"Requires-Dist: playwright==1.62.0\n"
        b'Requires-Dist: dispatch-local-handbook==0.1.0; extra == "dev" or python_version >= "3.11"\n\n'
    )
    wheel = make_wheel(
        tmp_path / "dispatch_core-1.0.0-py3-none-any.whl",
        extra={"dispatch_core-1.0.0.dist-info/METADATA": metadata},
    )

    with pytest.raises(InstallerError) as error:
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert error.value.code == "wheel_dependencies"
    assert not layout.dispatch_home.exists()


def test_approved_package_digest_is_required_before_layout_mutation(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")

    with pytest.raises(InstallerError) as error:
        _stage_core_wheel(
            layout,
            wheel,
            expected_sha256=sha256_file(wheel),
            expected_version="1.0.0",
            expected_package_files={"dispatch_core/__init__.py": "0" * 64},
            expected_requires_dist={"cryptography==48.0.1", "playwright==1.62.0"},
        )

    assert error.value.code == "wheel_package_digest"
    assert not layout.dispatch_home.exists()


def test_wheel_path_traversal_is_rejected_before_layout_mutation(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "traversal.whl", extra={"../escape": b"not allowed"})

    with pytest.raises(InstallerError, match="unsafe wheel member"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert not layout.dispatch_home.exists()


def test_unsafe_existing_transaction_lock_fails_closed(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    layout.prepare()
    lock = layout.state / "install" / "installer.lock"
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)

    with pytest.raises(InstallerError, match="installer lock"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert list(layout.releases.iterdir()) == []


def test_stale_core_staging_is_cleaned_before_retry(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.prepare()
    stale = layout.staging / ".core-interrupted"
    stale.mkdir(mode=0o700)
    (stale / "partial").write_bytes(b"partial")
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")

    stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")

    assert not stale.exists()


def test_unsafe_stale_core_staging_fails_closed(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.prepare()
    target = tmp_path / "outside"
    target.mkdir()
    (layout.staging / ".core-hostile").symlink_to(target, target_is_directory=True)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")

    with pytest.raises(InstallerError, match="unsafe stale Core staging"):
        stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")


def test_doctor_rejects_absolute_release_identity_outside_layout(tmp_path: Path) -> None:
    source_layout = layout_for(tmp_path / "source")
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(source_layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    external_release = source_layout.releases / str(staged["release_id"])
    target_layout = layout_for(tmp_path / "target")
    target_layout.prepare()
    target_layout.active_release_selector.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": str(external_release),
                "tree_manifest_sha256": sha256_file(external_release / "tree-manifest.json"),
                "release_receipt_sha256": sha256_file(external_release / "release-receipt.json"),
            }
        ),
        encoding="utf-8",
    )
    target_layout.active_release_selector.chmod(0o600)

    assert inspect_installation(target_layout)["checks"]["core"]["status"] == "unsafe"


def test_doctor_rejects_fifo_duplicate_and_oversized_core_selectors(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.prepare()
    selector = layout.active_release_selector

    os.mkfifo(selector, mode=0o600)
    assert inspect_installation(layout)["checks"]["core"]["status"] == "unsafe"
    selector.unlink()

    selector.write_text(
        '{"schema_version":1,"schema_version":1,"release_id":"invalid",'
        '"tree_manifest_sha256":"invalid","release_receipt_sha256":"invalid"}\n',
        encoding="utf-8",
    )
    selector.chmod(0o600)
    assert inspect_installation(layout)["checks"]["core"]["status"] == "unsafe"

    selector.write_bytes(b"x" * 4097)
    selector.chmod(0o600)
    assert inspect_installation(layout)["checks"]["core"]["status"] == "unsafe"


def test_failed_replacement_does_not_change_active_selector(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    activate_core_release(layout, release)
    original = layout.active_release_selector.read_bytes()

    damaged = make_wheel(tmp_path / "damaged.whl", corrupt_record=True)
    with pytest.raises(InstallerError):
        stage_core_wheel(layout, damaged, expected_sha256=sha256_file(damaged), expected_version="1.0.0")

    assert layout.active_release_selector.read_bytes() == original
