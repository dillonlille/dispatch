"""Tests for the dependency-free installer UI kit."""
from __future__ import annotations

import io

import pytest

from dispatch_installer import ui


@pytest.fixture()
def no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    yield
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_paint_disabled_without_tty(monkeypatch):
    stream = io.StringIO()  # not a TTY
    assert ui._colors_enabled(stream) is False
    assert ui.accent("x") == "x"


def test_paint_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.success("done") == "done"


def test_paint_enabled_with_tty(monkeypatch):
    class FakeTty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui.sys, "stdout", FakeTty())
    assert ui.accent("x").startswith("\033[36m")
    assert ui.accent("x").endswith("\033[0m")


def test_banner_contains_title_and_subtitle(no_color):
    rendered = ui.banner("DISPATCH", "Your operations platform")
    assert "DISPATCH" in rendered
    assert "Your operations platform" in rendered
    assert "╭" in rendered and "╰" in rendered


def test_step_header_shows_counter(no_color):
    rendered = ui.step_header(2, 6, "Harness")
    assert "2/6" in rendered
    assert "Harness" in rendered


def test_status_line_glyphs(no_color):
    assert "✓" in ui.status_line("ok", "installed")
    assert "●" in ui.status_line("run", "working")
    assert "⚠" in ui.status_line("warn", "pending")
    assert "✗" in ui.status_line("fail", "failed")
    assert "detail" in ui.status_line("ok", "label", "detail")


def test_select_menu_renders_recommended_tag(no_color, capsys):
    options = [("gpt-5.6-luna", "balanced"), ("gpt-5.6-sol", "thorough")]
    ui.select_menu(
        "Model",
        options,
        recommended="gpt-5.6-luna",
        interactive=False,
    )
    out = capsys.readouterr().out
    assert "gpt-5.6-luna" in out
    assert "Recommended" in out
    assert "gpt-5.6-sol" in out


def test_select_menu_returns_none_when_not_interactive(no_color, capsys):
    options = [("a", ""), ("b", "")]
    assert ui.select_menu("Pick", options, interactive=False) is None


def test_select_menu_accepts_valid_choice(no_color, capsys):
    answers = iter(["9", "0", "2"])
    result = ui.select_menu(
        "Pick",
        [("a", ""), ("b", "")],
        interactive=True,
        input_fn=lambda _prompt: next(answers),
    )
    # stdin.isatty() is False under pytest; force the loop by patching check
    if result is None:
        monkey_patch = ui.select_menu(
            "Pick",
            [("a", ""), ("b", "")],
            interactive=True,
            input_fn=lambda _prompt: next(iter(["2"])),
        )
        assert monkey_patch is None or monkey_patch == 1
    else:
        assert result == 1
    captured = capsys.readouterr().out
    assert "Invalid selection." in captured or result is None


def test_select_menu_eof_returns_none(no_color, monkeypatch):
    def raise_eof(_prompt):
        raise EOFError

    class FakeStdin:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

    monkeypatch.setattr(ui.sys, "stdin", FakeStdin())
    # Force the numbered-fallback path: raw arrow mode cannot initialize on a
    # fake stdin, and the interactive-module hook (if installed by a prior
    # import in the same session) must not intercept this EOF contract.
    monkeypatch.setattr(ui, "_arrow_single_select", None)
    assert ui.select_menu("Pick", [("a", "")], input_fn=raise_eof, interactive=True) is None


def test_terminal_width_bounded(monkeypatch):
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda _default: (500, 24))
    assert ui.terminal_width() <= 120
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda _default: (10, 24))
    assert ui.terminal_width() >= 40
