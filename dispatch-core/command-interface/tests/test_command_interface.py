from __future__ import annotations

import json
from pathlib import Path

from dispatch_core.command_interface import main

ENVELOPE = {"ok", "action", "status", "data", "freshness", "delivery", "error"}


def test_health_command_serializes_the_standard_envelope(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["health"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == ENVELOPE
    assert payload["action"] == "health"
    assert payload["status"] == "setup_incomplete"


def test_browser_doctor_is_bounded_and_does_not_launch_a_browser(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["browser-doctor"]) in {0, 1}
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == ENVELOPE
    assert payload["action"] == "browser-doctor"
    assert payload["data"]["operational"] is False
    assert payload["data"]["browser_manager"]["launch_probe"] == "not_verified"
    assert "profiles" not in payload["data"]["browser_manager"]


def test_auth_status_is_ready_without_creating_private_state(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["auth", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "auth-status"
    assert payload["data"]["configured"] is False
    assert not home.exists()


def test_auth_enroll_uses_hidden_prompts_and_never_outputs_values(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))
    answers = iter(("synthetic-user", "synthetic-password-not-a-secret"))
    monkeypatch.setattr(
        "dispatch_core.command_interface.getpass.getpass",
        lambda prompt: next(answers),
    )

    assert main(["auth", "enroll", "amazon-operations"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["data"]["status"] == "configured"
    assert "synthetic-user" not in output
    assert "synthetic-password-not-a-secret" not in output


def test_collection_status_is_read_only_with_no_queue(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["collection", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "collection-status"
    assert payload["data"]["status"] == "empty"
    assert payload["data"]["workers"] == 0
    assert not home.exists()


def test_collection_worker_once_is_bounded_and_idle_without_collectors(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["collection", "worker-once"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "collection-worker-once"
    assert payload["data"]["status"] == "idle"
    assert payload["data"]["process_cleaned"] is True
