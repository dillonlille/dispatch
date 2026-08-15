from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

import dispatch_installer.layout as layout_module
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.layout import InstallLayout, InstallerError, atomic_json


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }


def test_default_layout_is_dispatch_owned_and_non_mutating(tmp_path: Path) -> None:
    env = environment(tmp_path)
    layout = InstallLayout.from_environment(env)

    assert layout.dispatch_home == tmp_path / "home" / ".dispatch"
    assert layout.releases == layout.dispatch_home / "releases"
    assert layout.data == layout.dispatch_home / "data"
    assert layout.state == layout.dispatch_home / "state"
    assert layout.runtime == tmp_path / "run" / "dispatch"
    assert layout.browser_selector == Path("/etc/dispatch/browser-runtime-active.json")
    assert "hermes_profile" not in layout.as_dict()
    assert not (tmp_path / "home").exists()


def test_prepare_is_private_idempotent_and_secret_free(tmp_path: Path) -> None:
    layout = InstallLayout.from_environment(environment(tmp_path))
    layout.home.mkdir(parents=True, mode=0o700)
    layout.runtime.parent.mkdir(parents=True, mode=0o700)
    first = layout.prepare()
    first_receipt = layout.layout_receipt.read_bytes()
    second = layout.prepare()

    assert first == second
    assert layout.layout_receipt.read_bytes() == first_receipt
    assert stat.S_IMODE(layout.dispatch_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(layout.layout_receipt.stat().st_mode) == 0o600
    for path in (layout.releases, layout.bin, layout.config, layout.data, layout.state, layout.cache, layout.staging, layout.runtime):
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    receipt = json.loads(first_receipt)
    assert receipt["schema_version"] == 2
    assert re.fullmatch(r"[0-9a-f]{32}", receipt["installation_id"])
    assert receipt["contains_secrets"] is False
    assert "credential" not in first_receipt.decode().lower()


def test_prepare_rejects_symlinked_child(tmp_path: Path) -> None:
    layout = InstallLayout.from_environment(environment(tmp_path))
    layout.dispatch_home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    layout.data.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallerError, match="symlink"):
        InstallLayout.from_environment(environment(tmp_path))


def test_symlink_parent_alias_is_rejected(tmp_path: Path) -> None:
    env = environment(tmp_path)
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    env["DISPATCH_HOME"] = str(alias / "dispatch")
    with pytest.raises(InstallerError, match="symlink alias"):
        InstallLayout.from_environment(env)


def test_hermes_environment_is_not_consumed_or_mutated(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["HERMES_HOME"] = "relative/value/that-would-be-unsafe-if-consumed"
    env["HERMES_REAL_HOME"] = str(tmp_path / "other-home")

    layout = InstallLayout.from_environment(env)

    assert layout.home == tmp_path / "home"
    assert layout.dispatch_home == tmp_path / "home" / ".dispatch"


def test_doctor_is_non_mutating(tmp_path: Path) -> None:
    layout = InstallLayout.from_environment(environment(tmp_path))
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = inspect_installation(layout)
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    assert result["ok"] is False
    assert result["status"] == "incomplete"
    assert before == after


def test_runtime_and_persistent_roots_cannot_overlap(tmp_path: Path) -> None:
    env = environment(tmp_path)
    env["DISPATCH_RUNTIME_ROOT"] = str(tmp_path / "home" / ".dispatch" / "state" / "run")
    with pytest.raises(InstallerError, match="runtime and state roots"):
        InstallLayout.from_environment(env)


def test_prepare_rejects_writable_home_parent(tmp_path: Path) -> None:
    layout = InstallLayout.from_environment(environment(tmp_path))
    layout.home.mkdir(parents=True, mode=0o700)
    layout.home.chmod(0o777)
    with pytest.raises(InstallerError, match="parent ownership or mode"):
        layout.prepare()


def test_prepare_rejects_writable_runtime_parent(tmp_path: Path) -> None:
    layout = InstallLayout.from_environment(environment(tmp_path))
    layout.home.mkdir(parents=True, mode=0o700)
    layout.runtime.parent.mkdir(parents=True, mode=0o700)
    layout.runtime.parent.chmod(0o777)
    with pytest.raises(InstallerError, match="runtime parent ownership or mode"):
        layout.prepare()


def test_atomic_json_reports_visible_but_uncertain_publication(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    destination = directory / "selector.json"
    atomic_json(destination, {"generation": "old"})
    real_fsync = layout_module.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(layout_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(InstallerError) as error:
        atomic_json(destination, {"generation": "new"})

    assert error.value.code == "atomic_publish_uncertain"
    assert json.loads(destination.read_text(encoding="utf-8")) == {"generation": "new"}
