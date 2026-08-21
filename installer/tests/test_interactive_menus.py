"""Tests for arrow-key interactive menus (single + multi select)."""
from __future__ import annotations

import pytest

from dispatch_installer import interactive, ui
from dispatch_installer.layout import InstallerError


class FakeRawKeys:
    """Scripted key sequence; emulates the _RawKeys context protocol."""

    def __init__(self, keys: list[str]):
        self._keys = list(keys)

    def __enter__(self):
        self._active = True
        return self

    def __exit__(self, *exc) -> None:
        self._active = False

    def read_key(self) -> str:
        if not self._keys:
            return "enter"
        return self._keys.pop(0)


@pytest.fixture()
def fake_keys(monkeypatch):
    def _install(keys: list[str]):
        holder = FakeRawKeys(keys)
        monkeypatch.setattr(interactive, "_RawKeys", lambda: holder)
        monkeypatch.setattr(interactive, "_arrow_keys_available", lambda: True)
        return holder

    return _install


def test_single_select_arrow_down_then_enter(fake_keys, capsys):
    fake_keys(["down", "enter"])
    index = ui.select_menu("Pick", [("a", ""), ("b", "")], interactive=True)
    assert index == 1
    assert "selected: b" in capsys.readouterr().out


def test_single_select_up_clamps_at_top(fake_keys, capsys):
    fake_keys(["up", "up", "enter"])
    index = ui.select_menu("Pick", [("a", ""), ("b", "")], interactive=True)
    assert index == 0
    assert "❯" in capsys.readouterr().out


def test_multi_select_space_toggles_and_enter_confirms(fake_keys, capsys):
    fake_keys(["space", "down", "space", "enter"])
    picked = interactive.multi_select_menu(
        "Plugins",
        [("handbook", ""), ("companion-bridge", "")],
        interactive=True,
    )
    assert picked == [0, 1]
    assert "selected: handbook, companion-bridge" in capsys.readouterr().out


def test_multi_select_toggle_off_preselection(fake_keys):
    fake_keys(["space", "enter"])
    picked = interactive.multi_select_menu(
        "Plugins",
        [("handbook", ""), ("paycom", "")],
        preselected=["handbook"],
        interactive=True,
    )
    assert picked == []


def test_multi_select_preselection_confirmed_without_changes(fake_keys, capsys):
    fake_keys(["enter"])
    picked = interactive.multi_select_menu(
        "Plugins",
        [("handbook", ""), ("paycom", "")],
        preselected=["paycom"],
        interactive=True,
    )
    assert picked == [1]
    out = capsys.readouterr().out
    assert "[x]" in out or "selected: paycom" in out


def test_ctrl_c_raises_keyboard_interrupt(fake_keys):
    fake_keys(["ctrl-c"])
    with pytest.raises(KeyboardInterrupt):
        ui.select_menu("Pick", [("a", "")], interactive=True)


def test_multi_select_ctrl_c_raises(fake_keys):
    fake_keys(["ctrl-c"])
    with pytest.raises(KeyboardInterrupt):
        interactive.multi_select_menu("Plugins", [("a", "")], interactive=True)


def test_numbered_fallback_when_no_raw_mode(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "_arrow_keys_available", lambda: False)
    monkeypatch.setattr(ui, "_arrow_single_select", None)
    prompts: list[str] = []
    answers = iter(["2"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    index = ui.select_menu("Pick", [("a", ""), ("b", "")], input_fn=fake_input, interactive=True)
    assert index == 1
    assert prompts and "Select [1-2]" in prompts[0]
    # Single-render contract: fallback shows the numbered list, never the live menu.
    out = capsys.readouterr().out
    assert "❯" not in out
    assert "2. b" in out


def test_multi_select_returns_none_without_raw_mode(monkeypatch):
    monkeypatch.setattr(interactive, "_arrow_keys_available", lambda: False)
    result = interactive.multi_select_menu("Plugins", [("a", "")], interactive=True)
    assert result is None


def test_arrow_path_never_shows_numbered_list(fake_keys, capsys):
    fake_keys(["down", "enter"])
    index = ui.select_menu("Pick", [("a", ""), ("b", "")], interactive=True)
    assert index == 1
    out = capsys.readouterr().out
    # Regression: the static numbered list must not precede the live menu.
    assert "Select [1-" not in out
    assert "1. a" not in out
    assert out.count("Pick") == 1


def test_multi_select_renders_single_representation(fake_keys, capsys):
    fake_keys(["space", "enter"])
    picked = interactive.multi_select_menu(
        "Plugins",
        [("handbook", ""), ("paycom", "")],
        interactive=True,
    )
    assert picked == [0]
    out = capsys.readouterr().out
    assert "Select plugin numbers" not in out
    assert "1. handbook" not in out
    assert out.count("Plugins") == 1


def test_multi_select_custom_hint_replaces_default_controls(fake_keys, capsys):
    fake_keys(["enter"])
    picked = interactive.multi_select_menu(
        "Plugins",
        [("handbook", ""), ("paycom", "")],
        hint="empty = Core only",
        interactive=True,
    )
    assert picked == []
    out = capsys.readouterr().out
    assert "empty = Core only" in out
    assert "space select" not in out


def test_single_select_custom_hint_shown_once(fake_keys, capsys):
    fake_keys(["enter"])
    index = ui.select_menu(
        "Model",
        [("luna", ""), ("sol", "")],
        hint="choose deliberately",
        interactive=True,
    )
    assert index == 0
    out = capsys.readouterr().out
    assert out.count("choose deliberately") == 1


def test_multi_select_none_without_tty_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "_arrow_keys_available", lambda: True)
    result = interactive.multi_select_menu(
        "Plugins",
        [("handbook", "")],
        interactive=False,
    )
    assert result is None
    assert capsys.readouterr().out == ""


def test_option_height_depends_on_descriptions():
    assert interactive._option_height([("a", ""), ("b", "")]) == 1
    assert interactive._option_height([("a", "desc"), ("b", "")]) == 2


def test_parse_plugin_selection_dedupes_and_orders():
    plugins = ["handbook", "companion-bridge", "paycom"]
    # duplicates dropped, user entry order preserved
    assert interactive.parse_plugin_selection("1,3,3,2", plugins) == [
        "handbook",
        "paycom",
        "companion-bridge",
    ]
    assert interactive.parse_plugin_selection("", plugins) == []


def test_parse_plugin_selection_rejects_out_of_range():
    with pytest.raises(InstallerError) as error:
        interactive.parse_plugin_selection("9", ["a"])
    assert error.value.code == "plugin_selection_invalid"
