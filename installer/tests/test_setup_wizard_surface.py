"""Integration coverage for the wizard surface wired into ``_run_setup``.

Complements ``test_stage_rail.py`` (unit + pty): here a recording stub
proves the exact begin/enter/advance/end-or-fail choreography on the setup
path, and a piped-run assertion keeps human output free of escape bytes.
"""
from __future__ import annotations

import importlib

import pytest

from dispatch_installer.layout import InstallerError

setup_runtime = importlib.import_module("dispatch_installer.setup")


class RecordingRail:
    """Stand-in for StageRail that records every lifecycle call."""

    def __init__(self, *, begin_result: bool = True) -> None:
        self.calls: list[tuple] = []
        self._begin_result = begin_result

    def begin(self, stages, current: int = 0) -> bool:
        self.calls.append(("begin", tuple(stages), current))
        return self._begin_result

    def enter(self, title: str, subtitle: str = "") -> None:
        self.calls.append(("enter", title))

    def advance(self, index: int) -> None:
        self.calls.append(("advance", index))

    def fail(self) -> None:
        self.calls.append(("fail",))

    def end(self, summary_lines=None) -> None:
        self.calls.append(("end", list(summary_lines or [])))


def _patch_success_path(monkeypatch, *, configured=None) -> dict[str, object]:
    monkeypatch.setattr(setup_runtime, "available_plugins", lambda _layout: ["handbook"])
    monkeypatch.setattr(
        setup_runtime,
        "configure_plugins",
        lambda _layout, _selected, run: {"ok": True, "action": "setup"},
    )
    recorded: dict[str, object] = {}

    def fake_auth(_layout, _selected, *, human):
        recorded["auth_human"] = human
        return (configured if configured is not None else []), []

    monkeypatch.setattr(setup_runtime, "_setup_auth_profiles", fake_auth)
    return recorded


def test_interactive_selection_run_drives_full_rail_choreography(monkeypatch, capsys) -> None:
    recorded = _patch_success_path(
        monkeypatch,
        configured=[{"plugin": "handbook", "profile": "test", "type": "synthetic", "status": "enrolled"}],
    )
    rail = RecordingRail()
    monkeypatch.setattr(setup_runtime, "StageRail", lambda: rail)
    monkeypatch.setattr(setup_runtime.interactive, "multi_select_menu", lambda *a, **k: [0])

    code = setup_runtime._run_setup(object(), ["--plugin=handbook"], human=True, run=setup_runtime._run)

    assert code == 0
    assert recorded["auth_human"] is True
    kinds = [call[0] for call in rail.calls]
    assert kinds[0] == "begin"
    assert rail.calls[0][1] == ("Plugins", "Credentials", "Done")
    assert rail.calls[0][2] == 0
    assert ("enter", "Built-in plugins") in rail.calls
    assert ("advance", 1) in rail.calls
    assert ("enter", "Authentication profiles") in rail.calls
    assert ("advance", 2) in rail.calls
    assert kinds[-1] == "end"
    summary = rail.calls[-1][1]
    assert any("Dispatch setup complete" in line for line in summary)
    assert any("handbook" in line for line in summary)


def test_failure_between_sections_freezes_via_fail(monkeypatch) -> None:
    _patch_success_path(monkeypatch)
    rail = RecordingRail()
    monkeypatch.setattr(setup_runtime, "StageRail", lambda: rail)
    monkeypatch.setattr(setup_runtime.interactive, "multi_select_menu", lambda *a, **k: [0])

    def exploding_auth(_layout, _selected, *, human):
        raise InstallerError("profile_selection_invalid", "boom")

    monkeypatch.setattr(setup_runtime, "_setup_auth_profiles", exploding_auth)

    with pytest.raises(InstallerError):
        setup_runtime._run_setup(object(), ["--plugin=handbook"], human=True, run=setup_runtime._run)

    assert ("advance", 1) in rail.calls
    assert rail.calls[-1] == ("fail",)
    assert ("end", ) not in [(c[0],) for c in rail.calls]


def test_confirmed_run_never_begins_the_rail(monkeypatch, capsys) -> None:
    _patch_success_path(monkeypatch)
    rail = RecordingRail()
    monkeypatch.setattr(setup_runtime, "StageRail", lambda: rail)

    code = setup_runtime._run_setup(object(), ["--plugin=handbook", "--yes"], human=True, run=setup_runtime._run)

    assert code == 0
    assert rail.calls == []
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "Dispatch setup complete" in out


def test_degraded_rail_interactive_run_contains_zero_escape_bytes(monkeypatch, capsys) -> None:
    """Rail refuses a non-TTY stream: full interactive run stays escape-free."""
    _patch_success_path(monkeypatch)
    rail = RecordingRail(begin_result=False)  # e.g. piped stdout / NO_COLOR
    monkeypatch.setattr(setup_runtime, "StageRail", lambda: rail)
    monkeypatch.setattr(setup_runtime.interactive, "multi_select_menu", lambda *a, **k: [0])

    code = setup_runtime._run_setup(object(), ["--plugin=handbook"], human=True, run=setup_runtime._run)

    assert code == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "Dispatch setup complete" in out
    # Only the refused begin was attempted; no surface calls followed.
    assert [call[0] for call in rail.calls] == ["begin"]
