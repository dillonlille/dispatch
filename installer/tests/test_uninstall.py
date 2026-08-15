from __future__ import annotations

import base64
import csv
import hashlib
import io
import importlib
import json
import os
import stat
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

uninstall_module = importlib.import_module("dispatch_installer.uninstall")
from dispatch_installer.cli import main
from dispatch_installer.core_release import (
    activate_core_release,
    sha256_file,
    stage_core_wheel as _stage_core_wheel,
)
from dispatch_installer.layout import InstallLayout, InstallerError
from dispatch_installer.uninstall import plan_uninstall, uninstall


def layout_for(tmp_path: Path) -> InstallLayout:
    layout = InstallLayout.from_environment(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        }
    )
    layout.home.mkdir(parents=True, mode=0o700)
    layout.runtime.parent.mkdir(parents=True, mode=0o700)
    return replace(
        layout,
        browser_selector=tmp_path / "system" / "browser-runtime-active.json",
        browser_generations=tmp_path / "system" / "browser-runtimes",
    )


def make_wheel(path: Path) -> Path:
    files = {
        "dispatch_core/__init__.py": b'__version__ = "1.0.0"\n',
        "dispatch_core-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: dispatch-core\nVersion: 1.0.0\n"
            b"Requires-Python: <3.14,>=3.11\n"
            b"Requires-Dist: cryptography==48.0.1\nRequires-Dist: playwright==1.62.0\n\n"
        ),
        "dispatch_core-1.0.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: dispatch-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "dispatch_core-1.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\ndispatch-core = dispatch_core.command_interface:main\n"
        ),
        "dispatch_core-1.0.0.dist-info/top_level.txt": b"dispatch_core\n",
    }
    record_name = "dispatch_core-1.0.0.dist-info/RECORD"
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", str(len(data))))
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


def installed_layout(tmp_path: Path, *, isolated_system_roots: bool = True) -> tuple[InstallLayout, Path]:
    layout = layout_for(tmp_path)
    if not isolated_system_roots:
        layout = replace(
            layout,
            browser_selector=Path("/etc/dispatch/browser-runtime-active.json"),
            browser_generations=Path("/opt/dispatch/browser-runtimes"),
        )
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    release = layout.releases / str(staged["release_id"])
    activate_core_release(layout, release)
    return layout, release


def snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    values: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted([root, *root.rglob("*")]):
        relative = path.relative_to(root).as_posix() if path != root else "."
        if path.is_symlink():
            values[relative] = ("symlink", os.readlink(path).encode())
        elif path.is_file():
            values[relative] = ("file", path.read_bytes())
        else:
            values[relative] = ("directory", None)
    return values


def test_uninstall_plan_is_non_mutating_and_preserves_user_data(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    (layout.config / "settings.json").write_text("configuration", encoding="utf-8")
    (layout.data / "records.db").write_bytes(b"durable")
    (layout.cache / "discardable").write_bytes(b"cache")
    unknown = layout.dispatch_home / "user-note.txt"
    unknown.write_text("preserve", encoding="utf-8")
    before = snapshot(layout.dispatch_home)

    result = plan_uninstall(layout)

    assert result["status"] == "planned"
    assert result["blockers"] == []
    assert str(release) in result["remove"]
    assert str(layout.config) in result["preserve"]
    assert str(layout.data) in result["preserve"]
    assert str(unknown) in result["preserve"]
    assert result["hermes"] == "untouched"
    assert snapshot(layout.dispatch_home) == before


def test_fallback_runtime_is_removed_and_not_reported_as_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home)})
    layout = replace(
        layout,
        browser_selector=tmp_path / "system" / "browser-runtime-active.json",
        browser_generations=tmp_path / "system" / "browser-runtimes",
    )
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    staged = stage_core_wheel(layout, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
    activate_core_release(layout, layout.releases / str(staged["release_id"]))

    plan = plan_uninstall(layout)
    result = uninstall(layout)

    assert str(layout.runtime) in plan["remove"]
    assert str(layout.runtime) not in plan["preserve"]
    assert result["status"] == "uninstalled"
    assert not layout.runtime.exists()


def test_keep_data_uninstall_is_idempotent(tmp_path: Path) -> None:
    layout, _ = installed_layout(tmp_path)
    (layout.config / "settings.json").write_text("configuration", encoding="utf-8")
    (layout.data / "records.db").write_bytes(b"durable")
    operator_note = layout.state / "install" / "operator-note"
    operator_note.write_text("preserve", encoding="utf-8")
    (layout.bin / "dispatch").write_text("launcher", encoding="utf-8")
    (layout.cache / "discardable").write_bytes(b"cache")
    (layout.staging / "partial").write_bytes(b"staging")
    operational = layout.state / "jobs"
    operational.mkdir(mode=0o700)
    (operational / "job.json").write_text("{}", encoding="utf-8")
    unknown = layout.dispatch_home / "unknown.txt"
    unknown.write_text("keep", encoding="utf-8")

    plan = plan_uninstall(layout)
    result = uninstall(layout)
    repeated = uninstall(layout)

    assert result["status"] == "uninstalled"
    assert repeated["status"] == "already-uninstalled"
    assert (layout.config / "settings.json").read_text(encoding="utf-8") == "configuration"
    assert (layout.data / "records.db").read_bytes() == b"durable"
    assert str(operator_note) in plan["preserve"]
    assert operator_note.read_text(encoding="utf-8") == "preserve"
    assert unknown.read_text(encoding="utf-8") == "keep"
    assert not layout.releases.exists()
    assert not layout.bin.exists()
    assert not layout.cache.exists()
    assert not layout.staging.exists()
    assert not layout.runtime.exists()
    assert not layout.active_release_selector.exists()
    assert (layout.state / "install" / "uninstall-receipt.json").is_file()


def test_purge_removes_entire_owned_root(tmp_path: Path) -> None:
    layout, _ = installed_layout(tmp_path)
    (layout.config / "settings.json").write_text("configuration", encoding="utf-8")
    (layout.data / "records.db").write_bytes(b"durable")

    result = uninstall(layout, purge=True)
    repeated = uninstall(layout, purge=True)

    assert result["status"] == "purged"
    assert repeated["status"] == "already-absent"
    assert not layout.dispatch_home.exists()
    assert layout.home.exists()


def test_uninstall_fails_closed_for_tamper_and_runtime_activity(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    member = release / "site-packages" / "dispatch_core" / "__init__.py"
    member.chmod(0o644)
    member.write_text("tampered\n", encoding="utf-8")
    runtime_marker = layout.runtime / "active.sock"
    runtime_marker.write_text("active", encoding="utf-8")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert plan["status"] == "blocked"
    assert any(str(item).startswith("release_invalid:") for item in plan["blockers"])
    assert "runtime_requires_shutdown_verification" in plan["blockers"]
    assert error.value.code == "uninstall_blocked"
    assert release.exists()
    assert layout.active_release_selector.exists()


def test_uninstall_rejects_symlink_and_unsafe_lock_without_deleting_release(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.cache.rmdir()
    layout.cache.symlink_to(outside, target_is_directory=True)

    plan = plan_uninstall(layout)
    assert "unsafe_layout_root:cache" in plan["blockers"]
    with pytest.raises(InstallerError) as blocked:
        uninstall(layout)
    assert blocked.value.code == "uninstall_blocked"
    assert release.exists()
    layout.cache.unlink()
    layout.cache.mkdir(mode=0o700)

    lock = layout.state / "install" / "installer.lock"
    lock.chmod(0o644)
    with pytest.raises(InstallerError, match="installer lock"):
        uninstall(layout)
    assert release.exists()


def test_transaction_lock_rejects_symlinked_state_without_touching_target(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    outside_state = tmp_path / "outside-state"
    layout.state.rename(outside_state)
    layout.state.symlink_to(outside_state, target_is_directory=True)
    selector = outside_state / "install" / "active-release.json"
    before = selector.read_bytes()

    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert error.value.code == "install_state_unsafe"
    assert selector.read_bytes() == before
    assert release.exists()


def test_uninstall_does_not_repair_unsafe_state_mode_before_refusing(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    layout.state.chmod(0o755)

    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert error.value.code == "install_state_unsafe"
    assert stat.S_IMODE(layout.state.stat().st_mode) == 0o755
    assert release.exists()


def test_uninstall_rejects_hardlink_without_unlinking_external_name(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    outside = tmp_path / "outside-data"
    outside.write_bytes(b"preserve")
    os.link(outside, layout.cache / "linked-data")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert "unsafe_tree:cache:uninstall_tree_hardlink" in plan["blockers"]
    assert error.value.code == "uninstall_blocked"
    assert outside.read_bytes() == b"preserve"
    assert release.exists()


def test_unknown_release_is_preserved_and_blocks_purge(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    unknown = layout.releases / "operator-file"
    unknown.write_text("unknown", encoding="utf-8")

    keep_plan = plan_uninstall(layout)
    purge_plan = plan_uninstall(layout, purge=True)
    result = uninstall(layout)

    assert str(unknown) in keep_plan["preserve"]
    assert "unknown_release_entries_require_review" in purge_plan["blockers"]
    assert result["status"] == "uninstalled"
    assert unknown.read_text(encoding="utf-8") == "unknown"
    assert not release.exists()


def test_privileged_browser_authority_blocks_before_user_scope_mutation(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    layout.browser_selector.parent.mkdir(parents=True)
    layout.browser_selector.write_text("{}", encoding="utf-8")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError, match="privileged"):
        uninstall(layout)

    assert "browser_selector_requires_privileged_uninstaller" in plan["blockers"]
    assert release.exists()
    assert layout.active_release_selector.exists()


def test_unverifiable_browser_authority_blocks_before_mutation(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    authority_parent = layout.browser_selector.parent
    authority_parent.mkdir(parents=True)
    authority_parent.chmod(0o000)
    try:
        plan = plan_uninstall(layout)
        with pytest.raises(InstallerError) as error:
            uninstall(layout)
    finally:
        authority_parent.chmod(0o700)

    assert "browser_selector_authority_unverifiable" in plan["blockers"]
    assert error.value.code == "uninstall_blocked"
    assert release.exists()


def test_interrupted_uninstall_resumes_from_transaction_journal(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    (layout.bin / "dispatch").write_text("launcher", encoding="utf-8")
    original = uninstall_module._remove_owned_tree
    failed = False

    def fail_once(
        path: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        parent_descriptor: int | None = None,
    ) -> None:
        nonlocal failed
        if path.name == layout.bin.name and not failed:
            failed = True
            raise OSError("simulated interruption")
        original(
            path,
            expected_identity=expected_identity,
            parent_descriptor=parent_descriptor,
        )

    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", fail_once)
    with pytest.raises(OSError, match="simulated interruption"):
        uninstall(layout)
    assert (layout.state / "install" / "uninstall-transaction.json").is_file()

    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", original)
    result = uninstall(layout)

    assert result["status"] == "uninstalled"
    assert not (layout.state / "install" / "uninstall-transaction.json").exists()


def test_purge_resumes_after_internal_receipt_was_removed(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    original = uninstall_module._unlink_known_install_files
    failed = False

    def fail_after_unlink(target: InstallLayout, *, keep_receipts: bool) -> None:
        nonlocal failed
        original(target, keep_receipts=keep_receipts)
        if not keep_receipts and not failed:
            failed = True
            raise OSError("simulated finalization interruption")

    monkeypatch.setattr(uninstall_module, "_unlink_known_install_files", fail_after_unlink)
    with pytest.raises(OSError, match="finalization interruption"):
        uninstall(layout, purge=True)
    assert not layout.layout_receipt.exists()
    assert list(layout.home.glob(".dispatch-uninstall-*.json"))
    with pytest.raises(InstallerError) as wrong_mode:
        plan_uninstall(layout)
    assert wrong_mode.value.code == "uninstall_resume_mode"

    monkeypatch.setattr(uninstall_module, "_unlink_known_install_files", original)
    assert plan_uninstall(layout, purge=True)["status"] == "planned"
    result = uninstall(layout, purge=True)

    assert result["status"] == "purged"
    assert not layout.dispatch_home.exists()
    assert not list(layout.home.glob(".dispatch-uninstall-*.json"))


def test_uninstall_cli_requires_confirmation_and_supports_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    layout, release = installed_layout(tmp_path, isolated_system_roots=False)
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))

    assert main(["uninstall"]) == 1
    missing_confirmation = json.loads(capsys.readouterr().out)
    assert missing_confirmation["error"]["code"] == "confirmation_required"
    assert release.exists()

    assert main(["uninstall", "--plan"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["status"] == "planned"
    assert release.exists()

    assert main(["uninstall", "--yes"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "uninstalled"


def test_purge_rejects_current_journal_with_noncanonical_receipt_digest(tmp_path: Path) -> None:
    layout, _ = installed_layout(tmp_path)
    durable = layout.data / "records.db"
    durable.write_bytes(b"preserve")
    receipt_payload = json.loads(layout.layout_receipt.read_text(encoding="utf-8"))
    uninstall_module._write_external_journal(layout)
    layout.layout_receipt.unlink()
    journal = uninstall_module._external_journal_path(layout)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert not layout.layout_receipt.exists()
    assert payload["layout_receipt"] == receipt_payload
    payload["layout_receipt_sha256"] = "0" * 64
    journal.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    journal.chmod(0o600)

    with pytest.raises(InstallerError) as error:
        uninstall(layout, purge=True)

    assert error.value.code == "uninstall_journal_invalid"
    assert durable.read_bytes() == b"preserve"


def test_stale_purge_journal_cannot_authorize_recreated_dispatch_home(tmp_path: Path) -> None:
    layout, _ = installed_layout(tmp_path)
    uninstall_module._write_external_journal(layout)
    original_inode = layout.dispatch_home.stat().st_ino
    displaced = layout.home / ".dispatch-displaced"
    layout.dispatch_home.rename(displaced)
    layout.dispatch_home.mkdir(mode=0o700)
    layout.config.mkdir(mode=0o700)
    layout.data.mkdir(mode=0o700)
    marker = layout.data / "replacement.db"
    marker.write_text("replacement", encoding="utf-8")

    assert layout.dispatch_home.stat().st_ino != original_inode
    with pytest.raises(InstallerError) as failure:
        uninstall(layout, purge=True)

    assert failure.value.code == "uninstall_journal_invalid"
    assert marker.read_text(encoding="utf-8") == "replacement"


def test_purge_rechecks_root_identity_after_journal_load_before_mutation(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    uninstall_module._write_external_journal(layout)
    original_load = uninstall_module._load_external_journal
    displaced = layout.home / ".dispatch-displaced-after-journal-load"
    replacement_marker: Path | None = None
    swapped = False

    def load_then_replace(target: InstallLayout) -> dict | None:
        nonlocal replacement_marker, swapped
        payload = original_load(target)
        if payload is not None and not swapped:
            swapped = True
            target.dispatch_home.rename(displaced)
            target.dispatch_home.mkdir(mode=0o700)
            target.config.mkdir(mode=0o700)
            target.data.mkdir(mode=0o700)
            replacement_marker = target.data / "replacement.db"
            replacement_marker.write_text("preserve", encoding="utf-8")
        return payload

    monkeypatch.setattr(uninstall_module, "_load_external_journal", load_then_replace)
    with pytest.raises(InstallerError) as failure:
        uninstall(layout, purge=True)

    assert swapped
    assert failure.value.code == "uninstall_journal_invalid"
    assert replacement_marker is not None and replacement_marker.read_text(encoding="utf-8") == "preserve"
    assert not layout.state.exists()
    assert displaced.is_dir()
    assert uninstall_module._external_journal_path(layout).is_file()


def test_purge_rechecks_root_identity_after_final_plan_before_apply(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    uninstall_module._write_external_journal(layout)
    original_plan = uninstall_module._base_plan
    displaced = layout.home / ".dispatch-displaced-after-final-plan"
    replacement_marker: Path | None = None
    resume_plans = 0

    def plan_then_replace(
        target: InstallLayout,
        *,
        purge: bool,
        resume_journal: dict | None = None,
        authority_layout: InstallLayout | None = None,
        root_descriptor: int | None = None,
    ) -> dict[str, object]:
        nonlocal replacement_marker, resume_plans
        result = original_plan(
            target,
            purge=purge,
            resume_journal=resume_journal,
            authority_layout=authority_layout,
            root_descriptor=root_descriptor,
        )
        if resume_journal is not None:
            resume_plans += 1
            if resume_plans == 2:
                authority = authority_layout or target
                authority.dispatch_home.rename(displaced)
                authority.dispatch_home.mkdir(mode=0o700)
                authority.config.mkdir(mode=0o700)
                authority.data.mkdir(mode=0o700)
                replacement_marker = authority.data / "replacement.db"
                replacement_marker.write_text("preserve", encoding="utf-8")
        return result

    monkeypatch.setattr(uninstall_module, "_base_plan", plan_then_replace)
    with pytest.raises(InstallerError) as failure:
        uninstall(layout, purge=True)

    assert resume_plans == 2
    assert failure.value.code == "uninstall_journal_invalid"
    assert replacement_marker is not None and replacement_marker.read_text(encoding="utf-8") == "preserve"
    assert not layout.state.exists()
    assert displaced.is_dir()
    assert uninstall_module._external_journal_path(layout).is_file()


def test_interrupted_release_quarantine_resumes_without_reverification(tmp_path: Path, monkeypatch) -> None:
    layout, release = installed_layout(tmp_path)
    original = uninstall_module._remove_owned_tree
    interrupted = False

    def interrupt_quarantined_release(
        path: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        parent_descriptor: int | None = None,
    ) -> None:
        nonlocal interrupted
        if path.name.startswith(uninstall_module._RELEASE_QUARANTINE_PREFIX) and not interrupted:
            interrupted = True
            path.chmod(0o700)
            member = next(item for item in path.rglob("*") if item.is_file())
            member.parent.chmod(0o700)
            member.unlink()
            raise OSError("simulated release deletion interruption")
        original(
            path,
            expected_identity=expected_identity,
            parent_descriptor=parent_descriptor,
        )

    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", interrupt_quarantined_release)
    with pytest.raises(OSError, match="release deletion interruption"):
        uninstall(layout)

    quarantine = uninstall_module._quarantined_release_path(layout, release.name)
    assert not release.exists()
    assert quarantine.is_dir()
    assert (layout.state / "install" / "uninstall-transaction.json").is_file()

    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", original)
    result = uninstall(layout)

    assert result["status"] == "uninstalled"
    assert not quarantine.exists()


def test_transaction_rejects_unrelated_pattern_shaped_release_quarantine(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    transaction = uninstall_module._ensure_transaction(
        layout,
        purge=False,
        journal=None,
        release_names={release.name},
    )
    assert transaction["releases"][0]["release_id"] == release.name
    unrelated = layout.releases / ".uninstall-dispatch-core-1.0.0-0000000000000000"
    unrelated.mkdir(mode=0o700)
    marker = unrelated / "unrelated-data"
    marker.write_bytes(b"preserve")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert plan["status"] == "blocked"
    assert "unauthorized_uninstall_release_quarantine:dispatch-core-1.0.0-0000000000000000" in plan["blockers"]
    assert error.value.code == "uninstall_blocked"
    assert marker.read_bytes() == b"preserve"


def test_transaction_rejects_same_name_quarantine_with_different_identity(tmp_path: Path) -> None:
    layout, release = installed_layout(tmp_path)
    uninstall_module._ensure_transaction(
        layout,
        purge=False,
        journal=None,
        release_names={release.name},
    )
    displaced = layout.releases / "displaced-release"
    releases_mode = stat.S_IMODE(layout.releases.stat().st_mode)
    layout.releases.chmod(0o700)
    release.rename(displaced)
    quarantine = uninstall_module._quarantined_release_path(layout, release.name)
    quarantine.mkdir(mode=0o700)
    layout.releases.chmod(releases_mode)
    marker = quarantine / "replacement-data"
    marker.write_bytes(b"preserve")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert plan["status"] == "blocked"
    assert any(
        blocker.startswith(f"unsafe_uninstall_release_quarantine:{release.name}:uninstall_release_identity_changed")
        for blocker in plan["blockers"]
    )
    assert error.value.code == "uninstall_blocked"
    assert marker.read_bytes() == b"preserve"


def test_authorized_quarantine_replacement_after_preflight_is_preserved(tmp_path: Path, monkeypatch) -> None:
    layout, release = installed_layout(tmp_path)
    transaction = uninstall_module._ensure_transaction(
        layout,
        purge=False,
        journal=None,
        release_names={release.name},
    )
    record = transaction["releases"][0]
    quarantine = uninstall_module._quarantined_release_path(layout, release.name)
    releases_mode = stat.S_IMODE(layout.releases.stat().st_mode)
    layout.releases.chmod(0o700)
    release.rename(quarantine)
    layout.releases.chmod(releases_mode)

    real_validate = uninstall_module._validate_owned_tree
    validations = 0
    replacement: Path | None = None
    marker: Path | None = None

    def replace_after_removal_preflight(
        path: Path,
        *,
        parent_descriptor: int | None = None,
    ) -> tuple[int, int, int]:
        nonlocal validations, replacement, marker
        result = real_validate(path, parent_descriptor=parent_descriptor)
        if path == quarantine:
            validations += 1
            if validations == 2:
                replacement = layout.releases / "authorized-quarantine-displaced"
                layout.releases.chmod(0o700)
                quarantine.rename(replacement)
                quarantine.mkdir(mode=0o700)
                layout.releases.chmod(releases_mode)
                marker = quarantine / "replacement-data"
                marker.write_bytes(b"preserve")
        return result

    monkeypatch.setattr(uninstall_module, "_validate_owned_tree", replace_after_removal_preflight)
    with pytest.raises(InstallerError) as error:
        uninstall_module._remove_verified_releases(layout, transaction)

    assert validations == 2
    assert error.value.code == "uninstall_release_identity_changed"
    assert marker is not None and marker.read_bytes() == b"preserve"
    assert replacement is not None and replacement.stat().st_ino == record["inode"]


def test_absent_user_root_still_reports_privileged_browser_blocker(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.browser_selector.parent.mkdir(parents=True)
    layout.browser_selector.write_text("{}", encoding="utf-8")

    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert plan["status"] == "blocked"
    assert "browser_selector_requires_privileged_uninstaller" in plan["blockers"]
    assert error.value.code == "uninstall_blocked"


def test_authority_appearing_during_final_transaction_cleanup_cannot_report_success(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    original = uninstall_module._fsync_directory
    transaction = layout.state / "install" / "uninstall-transaction.json"
    injected = False

    def add_authority_during_final_fsync(path: Path) -> None:
        nonlocal injected
        original(path)
        if path.name == transaction.parent.name and not transaction.exists() and not injected:
            injected = True
            layout.browser_selector.parent.mkdir(parents=True, exist_ok=True)
            layout.browser_selector.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(uninstall_module, "_fsync_directory", add_authority_during_final_fsync)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert error.value.code == "uninstall_incomplete"
    assert injected
    assert not transaction.exists()

    layout.browser_selector.unlink()
    monkeypatch.setattr(uninstall_module, "_fsync_directory", original)
    assert uninstall(layout)["status"] == "already-uninstalled"


def test_authority_appearing_during_final_purge_journal_cleanup_cannot_report_success(tmp_path: Path, monkeypatch) -> None:
    layout, _ = installed_layout(tmp_path)
    preserved = layout.dispatch_home / "user-note.txt"
    preserved.write_text("preserve", encoding="utf-8")
    original = uninstall_module._remove_external_journal

    def add_authority_after_journal_cleanup(target: InstallLayout) -> None:
        original(target)
        target.browser_selector.parent.mkdir(parents=True, exist_ok=True)
        target.browser_selector.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(uninstall_module, "_remove_external_journal", add_authority_after_journal_cleanup)
    with pytest.raises(InstallerError) as error:
        uninstall(layout, purge=True)

    assert error.value.code == "uninstall_incomplete"
    assert layout.browser_selector.is_file()
    assert not uninstall_module._external_journal_path(layout).exists()
    assert preserved.read_text(encoding="utf-8") == "preserve"

    layout.browser_selector.unlink()
    monkeypatch.setattr(uninstall_module, "_remove_external_journal", original)
    assert uninstall(layout, purge=True)["status"] == "already-uninstalled"


def test_install_created_after_lifecycle_lock_is_not_misreported_absent(tmp_path: Path, monkeypatch) -> None:
    layout = layout_for(tmp_path)
    wheel = make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    created_release: Path | None = None

    @contextmanager
    def install_before_locked_check(target: InstallLayout):
        nonlocal created_release
        staged = stage_core_wheel(target, wheel, expected_sha256=sha256_file(wheel), expected_version="1.0.0")
        created_release = target.releases / str(staged["release_id"])
        activate_core_release(target, created_release)
        yield

    monkeypatch.setattr(uninstall_module, "lifecycle_lock", install_before_locked_check)
    result = uninstall(layout)

    assert result["status"] == "uninstalled"
    assert created_release is not None
    assert not created_release.exists()


def test_mount_id_change_during_removal_blocks_descriptor_traversal(tmp_path: Path, monkeypatch) -> None:
    layout, release = installed_layout(tmp_path)
    mounted = layout.cache / "mounted"
    mounted.mkdir(mode=0o700)
    retained = mounted / "outside-like-data"
    retained.write_bytes(b"preserve")
    real_mount_id = uninstall_module._descriptor_mount_id
    real_validate = uninstall_module._validate_owned_tree
    real_remove = uninstall_module._remove_owned_tree
    phase = {"removing_cache": False, "validated_cache": False}

    def changed_mount_id(descriptor: int) -> int:
        value = real_mount_id(descriptor)
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if phase["validated_cache"] and target == str(mounted):
            return value + 1
        return value

    def validate_then_change(
        path: Path,
        *,
        parent_descriptor: int | None = None,
    ) -> tuple[int, int, int]:
        result = real_validate(path, parent_descriptor=parent_descriptor)
        if phase["removing_cache"] and path.name == layout.cache.name:
            phase["validated_cache"] = True
        return result

    def remove_with_late_mount_change(
        path: Path,
        *,
        expected_identity: tuple[int, int] | None = None,
        parent_descriptor: int | None = None,
    ) -> None:
        if path.name != layout.cache.name:
            real_remove(
                path,
                expected_identity=expected_identity,
                parent_descriptor=parent_descriptor,
            )
            return
        phase["removing_cache"] = True
        try:
            real_remove(
                path,
                expected_identity=expected_identity,
                parent_descriptor=parent_descriptor,
            )
        finally:
            phase["removing_cache"] = False

    monkeypatch.setattr(uninstall_module, "_descriptor_mount_id", changed_mount_id)
    monkeypatch.setattr(uninstall_module, "_validate_owned_tree", validate_then_change)
    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", remove_with_late_mount_change)
    plan = plan_uninstall(layout)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)

    assert plan["status"] == "planned"
    assert plan["blockers"] == []
    assert phase["validated_cache"]
    assert error.value.code == "uninstall_tree_boundary"
    assert retained.read_bytes() == b"preserve"
    assert not release.exists()

    monkeypatch.setattr(uninstall_module, "_descriptor_mount_id", real_mount_id)
    monkeypatch.setattr(uninstall_module, "_validate_owned_tree", real_validate)
    monkeypatch.setattr(uninstall_module, "_remove_owned_tree", real_remove)
    assert uninstall(layout)["status"] == "uninstalled"
