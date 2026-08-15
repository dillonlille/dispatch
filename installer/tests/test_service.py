from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import dispatch_installer.uninstall as uninstall_module
from dispatch_installer.layout import InstallLayout, InstallerError
from dispatch_installer.service import install_user_service
from dispatch_installer.uninstall import _remove_user_service


def layout_for(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    runtime = tmp_path / "run"
    home.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home), "XDG_RUNTIME_DIR": str(runtime)})
    layout.prepare()
    launcher = layout.bin / "dispatch"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o700)
    return layout


def test_user_service_is_receipt_bound_and_activated(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    commands: list[tuple[str, ...]] = []

    def run(command):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = install_user_service(layout, layout.bin / "dispatch", run=run)

    unit = layout.home / ".config" / "systemd" / "user" / "dispatch-core.service"
    receipt = json.loads((layout.state / "install" / "service.json").read_text(encoding="utf-8"))
    assert result["status"] == "active"
    assert "ExecStart=" in unit.read_text(encoding="utf-8")
    assert str(layout.bin / "dispatch") in unit.read_text(encoding="utf-8")
    assert receipt["status"] == "active"
    assert receipt["unit"] == str(unit)
    assert commands == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "dispatch-core.service"),
        ("systemctl", "--user", "is-active", "--quiet", "dispatch-core.service"),
    ]


def test_failed_service_activation_preserves_prepared_receipt(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)

    def fail(command):
        return subprocess.CompletedProcess(command, 1, "", "unavailable")

    with pytest.raises(InstallerError) as error:
        install_user_service(layout, layout.bin / "dispatch", run=fail)

    receipt = json.loads((layout.state / "install" / "service.json").read_text(encoding="utf-8"))
    assert error.value.code == "service_activation_failed"
    assert receipt["status"] == "prepared"


def test_receipt_bound_service_is_disabled_and_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = layout_for(tmp_path)
    successful = lambda command: subprocess.CompletedProcess(command, 0, "", "")
    install_user_service(layout, layout.bin / "dispatch", run=successful)
    commands: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(uninstall_module.subprocess, "run", run)
    _remove_user_service(layout)

    assert not (layout.home / ".config" / "systemd" / "user" / "dispatch-core.service").exists()
    assert not (layout.state / "install" / "service.json").exists()
    assert commands[0] == ("systemctl", "--user", "disable", "--now", "dispatch-core.service")
