"""Regression tests for installer CLI argument wiring.

Guards against subcommand handlers reading argparse attributes their
subparser never defines (the harness-setup ``args.yes`` crash).
"""
from __future__ import annotations

import contextlib

from dispatch_installer import cli, harness_setup


class FakeHarnessResult:
    """Minimal stand-in for HarnessSetupResult."""

    def __init__(self) -> None:
        self.pending: list[dict[str, str]] = []
        self.selected = False

    def as_dict(self) -> dict[str, object]:
        return {"selected": self.selected}


def _every_subparser() -> dict[str, object]:
    parser = cli._parser()
    choices: dict[str, object] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse introspection in tests
        subchoices = getattr(action, "choices", None)
        if isinstance(subchoices, dict):
            choices.update(subchoices)
    return choices


CONFIRM_ACTIONS = {"install", "repair", "setup", "harness-setup", "uninstall"}


def test_every_mutating_subcommand_defines_yes_flag():
    """Handlers of these actions read ``args.yes``; the flag must exist."""
    subparsers = _every_subparser()
    assert CONFIRM_ACTIONS <= set(subparsers)
    for name in sorted(CONFIRM_ACTIONS):
        subparser = subparsers[name]
        options = {
            option
            for subaction in subparser._actions  # noqa: SLF001 - argparse introspection
            for option in subaction.option_strings
        }
        assert "--yes" in options, f"{name} subcommand lacks --yes"


def _isolated_roots(tmp_path, monkeypatch) -> list[str]:
    home = tmp_path / "home"
    dispatch = tmp_path / "dispatch"
    home.mkdir(mode=0o700)
    dispatch.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return ["--dispatch-home", str(dispatch)]


def _stub_harness_run(monkeypatch, captured: dict[str, object]) -> None:
    @contextlib.contextmanager
    def fake_lock(_layout):
        yield

    def fake_run(layout, *, human, allow_install=False):
        captured["human"] = human
        captured["allow_install"] = allow_install
        return FakeHarnessResult()

    monkeypatch.setattr(cli, "installation_lock", fake_lock)
    monkeypatch.setattr(harness_setup, "run_harness_setup", fake_run)


def test_harness_setup_runs_without_yes_flag(tmp_path, monkeypatch, capsys):
    """Regression: handler read args.yes although the subparser never defined it."""
    captured: dict[str, object] = {}
    prefix = _isolated_roots(tmp_path, monkeypatch)
    _stub_harness_run(monkeypatch, captured)
    rc = cli.main([*prefix, "harness-setup"])
    assert rc == 0
    assert captured["human"] is True
    assert '"ok": true' in capsys.readouterr().out.replace("True", "true")


def test_harness_setup_yes_and_json_are_non_interactive(tmp_path, monkeypatch, capsys):
    captured: dict[str, object] = {}
    prefix = _isolated_roots(tmp_path, monkeypatch)
    _stub_harness_run(monkeypatch, captured)
    rc = cli.main([*prefix, "--json", "harness-setup", "--yes"])
    assert rc == 0
    assert captured["human"] is False
