"""Tests for the harness setup wizard flow."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatch_installer import harness_setup
from dispatch_installer.harness import HARNESS_CATALOG, DetectionResult, write_selection
from dispatch_installer.layout import InstallerError


class FakeHermes:
    """Records hermes CLI invocations; configurable per subcommand."""

    def __init__(self, *, profiles: list[str] | None = None, auth_logged_in: bool = False):
        self.profiles = profiles or []
        self.auth_logged_in = auth_logged_in
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, launcher: str, arguments: tuple[str, ...], **kwargs):
        self.calls.append(arguments)
        import subprocess

        if arguments[:2] == ("profile", "list"):
            output = "\n".join(f" {name}" for name in self.profiles)
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:2] == ("profile", "create"):
            self.profiles.append(arguments[2])
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if arguments[:2] == ("auth", "status"):
            out = "logged in" if self.auth_logged_in else "logged out"
            code = 0 if self.auth_logged_in else 1
            return subprocess.CompletedProcess(arguments, code, out, "")
        return subprocess.CompletedProcess(arguments, 0, json.dumps({"ok": True}), "")


@pytest.fixture()
def layout(tmp_path):
    class FakeLayout:
        config = tmp_path / "config"

    fake = FakeLayout()
    fake.config.mkdir(mode=0o700)
    return fake


def _skip_install(monkeypatch):
    monkeypatch.setattr(
        harness_setup,
        "detect_harness",
        lambda spec: DetectionResult("ready", version="v1.2.3", home="/tmp/hermes"),
    )


def test_headless_without_selection_skips(layout, capsys):
    result = harness_setup.run_harness_setup(layout, human=False)
    assert result.selected is False
    assert result.pending == []
    assert "skipping harness setup" in capsys.readouterr().out


def test_headless_with_existing_selection_completes(layout, monkeypatch):
    write_selection(layout.config, HARNESS_CATALOG["hermes"], DetectionResult("ready", version="v1", home="/h"))
    _skip_install(monkeypatch)
    fake = FakeHermes(profiles=[harness_setup.PROFILE_NAME], auth_logged_in=True)
    monkeypatch.setattr(harness_setup, "_hermes_command", fake)
    result = harness_setup.run_harness_setup(layout, human=False)
    assert result.selected is True
    assert result.profile == harness_setup.PROFILE_NAME
    assert result.model == "gpt-5.6-luna"
    assert result.reasoning == "high"
    assert result.pending == []
    created = [call for call in fake.calls if call[:2] == ("profile", "create")]
    assert created == []  # profile already existed
    set_calls = [call for call in fake.calls if call[:2] == ("config", "set")]
    assert len(set_calls) == 3


def test_profile_created_when_missing(layout, monkeypatch):
    write_selection(layout.config, HARNESS_CATALOG["hermes"], DetectionResult("ready", version="v1", home="/h"))
    _skip_install(monkeypatch)
    fake = FakeHermes(auth_logged_in=True)
    monkeypatch.setattr(harness_setup, "_hermes_command", fake)
    result = harness_setup.run_harness_setup(layout, human=False)
    assert ("profile", "create", harness_setup.PROFILE_NAME, "--no-skills", "--no-alias") in fake.calls
    assert result.profile == harness_setup.PROFILE_NAME


def test_codex_logout_becomes_pending_requirement(layout, monkeypatch):
    write_selection(layout.config, HARNESS_CATALOG["hermes"], DetectionResult("ready", version="v1", home="/h"))
    _skip_install(monkeypatch)
    monkeypatch.setattr(harness_setup, "_hermes_command", FakeHermes(auth_logged_in=False))
    result = harness_setup.run_harness_setup(layout, human=False)
    assert any(item["requirement"] == "codex_authentication" for item in result.pending)


def test_interactive_skip_choice_returns_unselected(layout, monkeypatch):
    answers = iter(["2"])  # choose "none" on the harness menu
    result = harness_setup.run_harness_setup(layout, human=True, input_fn=lambda _p: next(answers))
    assert result.selected is False


def test_interactive_absent_harness_declined_stays_pending(layout, monkeypatch):
    monkeypatch.setattr(
        harness_setup,
        "detect_harness",
        lambda spec: DetectionResult("absent"),
    )
    answers = iter(["1", "2"])  # select hermes, decline install
    result = harness_setup.run_harness_setup(layout, human=True, input_fn=lambda _p: next(answers))
    assert result.selected is True
    assert any(item["requirement"] == "harness_install" for item in result.pending)


def test_unhealthy_harness_fails_closed(layout, monkeypatch):
    write_selection(layout.config, HARNESS_CATALOG["hermes"], DetectionResult("ready", version="v1", home="/h"))
    monkeypatch.setattr(
        harness_setup,
        "detect_harness",
        lambda spec: DetectionResult("unhealthy", detail="broken install"),
    )
    with pytest.raises(InstallerError) as error:
        harness_setup.run_harness_setup(layout, human=False)
    assert error.value.code == "harness_unhealthy"


def test_result_envelope_never_contains_secrets(layout, monkeypatch):
    write_selection(layout.config, HARNESS_CATALOG["hermes"], DetectionResult("ready", version="v1", home="/h"))
    _skip_install(monkeypatch)
    monkeypatch.setattr(harness_setup, "_hermes_command", FakeHermes(auth_logged_in=True))
    result = harness_setup.run_harness_setup(layout, human=False)
    payload = json.dumps(result.as_dict())
    assert "contains_secrets" in payload
    assert json.loads(payload)["contains_secrets"] is False
