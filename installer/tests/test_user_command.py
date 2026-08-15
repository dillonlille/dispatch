from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

import dispatch_installer.cli as cli_module
from dispatch_installer import launcher as launcher_module
from dispatch_installer.layout import InstallLayout, InstallerError
from dispatch_installer.user_command import (
    command_path,
    command_receipt_path,
    inspect_user_command,
    install_user_command,
    remove_user_command,
    validate_user_command_install,
)


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


def test_user_command_is_atomic_receipt_owned_and_repairable(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)

    installed = install_user_command(layout)

    command = command_path(layout)
    receipt = json.loads(command_receipt_path(layout).read_text(encoding="utf-8"))
    assert installed["status"] == "ready"
    assert stat.S_IMODE(command.stat().st_mode) == 0o700
    assert str(layout.bin / "dispatch") in command.read_text(encoding="utf-8")
    assert receipt["status"] == "active"
    assert receipt["command"] == str(command)
    assert receipt["command_sha256"] == hashlib.sha256(command.read_bytes()).hexdigest()
    assert inspect_user_command(layout)["status"] == "ready"

    command.unlink()
    assert inspect_user_command(layout)["status"] == "incomplete"
    install_user_command(layout)
    assert inspect_user_command(layout)["status"] == "ready"


def test_user_command_refuses_untracked_collision(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    command = command_path(layout)
    command.parent.parent.mkdir(mode=0o700)
    command.parent.mkdir(mode=0o700)
    command.write_text("#!/bin/sh\nexec false\n", encoding="utf-8")
    command.chmod(0o700)

    with pytest.raises(InstallerError) as error:
        validate_user_command_install(layout)

    assert error.value.code == "command_conflict"
    assert command.read_text(encoding="utf-8") == "#!/bin/sh\nexec false\n"
    assert not command_receipt_path(layout).exists()


def test_user_command_tampering_blocks_reinstall_and_removal(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    install_user_command(layout)
    command = command_path(layout)
    command.write_text("#!/bin/sh\nexec false\n", encoding="utf-8")
    command.chmod(0o700)

    assert inspect_user_command(layout)["status"] == "unsafe"
    with pytest.raises(InstallerError) as reinstall_error:
        install_user_command(layout)
    with pytest.raises(InstallerError) as removal_error:
        remove_user_command(layout)

    assert reinstall_error.value.code == "command_receipt_mismatch"
    assert removal_error.value.code == "command_receipt_mismatch"
    assert command.exists()


def test_user_command_prepared_receipt_recovers_old_authorized_bytes(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    install_user_command(layout)
    command = command_path(layout)
    old_digest = hashlib.sha256(command.read_bytes()).hexdigest()
    receipt_path = command_receipt_path(layout)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["status"] = "prepared"
    receipt["previous_sha256"] = old_digest
    receipt["command_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)

    assert inspect_user_command(layout)["status"] == "incomplete"
    install_user_command(layout)
    assert inspect_user_command(layout)["status"] == "ready"


def test_user_command_removal_preserves_other_user_commands(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    install_user_command(layout)
    other = command_path(layout).parent / "other-tool"
    other.write_text("unrelated", encoding="utf-8")
    other.chmod(0o700)

    assert remove_user_command(layout) is True

    assert not command_path(layout).exists()
    assert not command_receipt_path(layout).exists()
    assert other.read_text(encoding="utf-8") == "unrelated"


def test_user_command_rejects_writable_publication_directory(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    directory = command_path(layout).parent
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o777)

    with pytest.raises(InstallerError) as error:
        install_user_command(layout)

    assert error.value.code == "command_directory_unsafe"


def test_dispatch_uninstall_routes_to_installer_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = layout_for(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))

    def installer_main(arguments: list[str], *, prog: str, public_uninstall: bool) -> int:
        captured["arguments"] = arguments
        captured["prog"] = prog
        captured["public_uninstall"] = public_uninstall
        return 7

    monkeypatch.setattr(cli_module, "main", installer_main)

    result = launcher_module.main(["uninstall", "--plan"])

    assert result == 7
    assert captured == {
        "arguments": [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--plan",
        ],
        "prog": "dispatch uninstall",
        "public_uninstall": True,
    }


def test_dispatch_renders_human_output_by_default_and_keeps_json_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = layout_for(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))
    payload = {
        "ok": False,
        "action": "uninstall",
        "status": "error",
        "data": {},
        "error": {
            "code": "confirmation_required",
            "message": "uninstall requires --yes or --plan",
        },
    }

    def installer_main(_arguments: list[str], *, prog: str, public_uninstall: bool) -> int:
        assert prog == "dispatch uninstall"
        assert public_uninstall is True
        print(json.dumps(payload, sort_keys=True))
        return 1

    monkeypatch.setattr(cli_module, "main", installer_main)

    assert launcher_module.main(["uninstall"]) == 1
    human = capsys.readouterr().out
    assert human.startswith("✗ Confirmation required")
    assert "dispatch uninstall --plan" in human
    assert "{" not in human

    assert launcher_module.main(["--json", "uninstall"]) == 1
    assert json.loads(capsys.readouterr().out) == payload


def test_dispatch_without_arguments_shows_simple_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert launcher_module.main([]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Dispatch\n\nUsage:\n  dispatch <command> [options]")
    assert "health           Show Dispatch health" in output
    assert "uninstall        Safely remove Dispatch" in output
    assert "Add --json for machine-readable output." in output
    assert "dispatch-core" not in output


def test_json_help_is_one_machine_readable_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert launcher_module.main(["--json", "--help"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "help"
    assert payload["status"] == "help"
    assert payload["data"]["help"].startswith("Dispatch\n\nUsage:")


def test_json_mode_stays_structured_when_core_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = layout_for(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))
    monkeypatch.setattr(
        launcher_module,
        "inspect_installation",
        lambda _layout: {"checks": {"core": {"status": "missing"}}},
    )

    assert launcher_module.main(["--json", "health"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "launch"
    assert payload["error"]["code"] == "core_release_unavailable"


def test_json_mode_wraps_public_parser_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = layout_for(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))

    assert launcher_module.main(["--json", "uninstall", "--bad-option"]) == 2

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["action"] == "uninstall"
    assert payload["error"]["code"] == "invalid_arguments"
    assert "--bad-option" in payload["error"]["message"]
    assert "dispatch-installer" not in captured.out


def test_human_uninstall_parser_errors_use_public_program_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = layout_for(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(layout.runtime.parent))

    assert launcher_module.main(["uninstall", "--bad-option"]) == 2

    captured = capsys.readouterr()
    assert "usage: dispatch uninstall" in captured.err
    assert "dispatch-installer" not in captured.err
    assert "layout" not in captured.err


def test_json_mode_wraps_parser_help_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    def command(arguments: list[str]) -> int:
        argparse.ArgumentParser(prog="dispatch health").parse_args(arguments)
        return 0

    assert launcher_module._run_structured(
        lambda: command(["--help"]), json_output=True, action="health"
    ) == 0
    help_payload = json.loads(capsys.readouterr().out)
    assert help_payload["status"] == "help"
    assert "usage: dispatch health" in help_payload["data"]["help"]

    assert launcher_module._run_structured(
        lambda: command(["--bad-option"]), json_output=True, action="health"
    ) == 2
    error_payload = json.loads(capsys.readouterr().out)
    assert error_payload["error"]["code"] == "invalid_arguments"
