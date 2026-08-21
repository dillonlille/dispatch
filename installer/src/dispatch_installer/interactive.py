"""Arrow-key interactive menus: single-select and multi-select.

Single-choice (``select_menu``) lives in ``ui``; this module adds raw-mode
arrow navigation and the multiple-choice variant. All helpers degrade to
numbered fallbacks when no TTY is available.
"""
from __future__ import annotations

import sys

from .layout import InstallerError
from .ui import accent, bold, dim, error, success


class _RawKeys:
    """Bounded cbreak-mode key reader (POSIX). Restores the terminal on exit."""

    def __init__(self):
        self._termios = None
        self._fd = None
        self._old = None

    def __enter__(self):
        try:
            import termios
            import tty
        except ImportError:
            return None
        try:
            self._fd = sys.stdin.fileno()
            self._termios = termios
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            return self
        except (termios.error, ValueError, OSError):
            return None

    def __exit__(self, *exc) -> None:
        if self._termios is not None and self._old is not None and self._fd is not None:
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old)

    def read_key(self) -> str:
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            try:
                seq = sys.stdin.read(2)
            except Exception:
                return "escape"
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x03":
            return "ctrl-c"
        return ch


def _arrow_keys_available() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401

        return True
    except (ImportError, ValueError, OSError):
        return False


def _option_height(options: list[tuple[str, str]]) -> int:
    """Terminal lines consumed by one rendered option row."""
    return 2 if any(description for _v, description in options) else 1


def _render_choice_list(
    options: list[tuple[str, str]],
    cursor: int,
    checked: list[bool] | None,
    recommended: str | None,
) -> int:
    """Render the list in place; return the number of lines drawn."""
    lines = 0
    for index, (value, description) in enumerate(options):
        pointer = accent("❯") if index == cursor else " "
        checkbox = ""
        if checked is not None:
            box = success("[x]") if checked[index] else dim("[ ]")
            checkbox = f"{box} "
        marker = ""
        if recommended is not None and value == recommended:
            marker = success("  Recommended")
        line = f"  {pointer} {checkbox}{bold(value) if index == cursor else value}{marker}"
        print(f"\r\033[K{line}")
        lines += 1
        if description:
            print(f"\r\033[K     {dim(description)}")
            lines += 1
    return lines


def _finalize_menu(options: list[tuple[str, str]], cursor: int, checked: list[bool] | None) -> None:
    chosen = options[cursor][0]
    if checked is not None:
        picked = [value for index, (value, _d) in enumerate(options) if checked[index]]
        label = ", ".join(picked) if picked else "none"
        print(f"  {dim('selected: ' + label)}")
    else:
        print(f"  {dim('selected: ' + chosen)}")


def select_menu(
    title: str,
    options: list[tuple[str, str]],
    *,
    recommended: str | None = None,
    hint: str = "",
    input_fn=input,
    interactive: bool | None = None,
) -> int | None:
    """Single-choice menu.

    Arrow keys + Enter on an interactive TTY; numbered-entry fallback when
    raw mode is unavailable (pipes, CI, restricted SSH). Returns the chosen
    index, or ``None`` when selection is unavailable.
    """
    print(f"\n  {bold(title)}\n")
    for index, (value, description) in enumerate(options, start=1):
        marker = ""
        if recommended is not None and value == recommended:
            marker = success("   Recommended")
        print(f"    {accent(str(index))}. {bold(value)}{marker}")
        if description:
            print(f"       {dim(description)}")
    if hint:
        print(f"\n  {dim(hint)}")
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        return None
    if _arrow_keys_available():
        keys = _RawKeys().__enter__()
        if keys is not None:
            try:
                cursor = 0
                total = len(options)
                height = _option_height(options)
                print(f"  {dim('↑↓ move · enter select')}")
                drawn = _render_choice_list(options, cursor, None, recommended)
                while True:
                    key = keys.read_key()
                    if key == "ctrl-c":
                        raise KeyboardInterrupt
                    if key == "up":
                        cursor = max(0, cursor - 1)
                    elif key == "down":
                        cursor = min(total - 1, cursor + 1)
                    elif key == "enter":
                        print("\r\033[K", end="")
                        _finalize_menu(options, cursor, None)
                        return cursor
                    print(f"\r\033[{drawn}A", end="")
                    drawn = _render_choice_list(options, cursor, None, recommended)
            finally:
                keys.__exit__(None, None, None)
    while True:
        try:
            raw = input_fn("  Select [1-" + str(len(options)) + "]: ").strip()
        except EOFError:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  {error('Invalid selection.')}")


def multi_select_menu(
    title: str,
    options: list[tuple[str, str]],
    *,
    preselected: list[str] | None = None,
    hint: str = "",
    interactive: bool | None = None,
) -> list[int] | None:
    """Multiple-choice menu: ↑↓ navigate, Space toggles, Enter confirms.

    Returns selected indices, or ``None`` when arrow-key selection is
    unavailable — callers fall back to their numbered/flag paths.
    """
    print(f"\n  {bold(title)}\n")
    for index, (value, description) in enumerate(options, start=1):
        print(f"    {accent(str(index))}. {bold(value)}")
        if description:
            print(f"       {dim(description)}")
    if hint:
        print(f"\n  {dim(hint)}")
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive or not _arrow_keys_available():
        return None
    keys = _RawKeys().__enter__()
    if keys is None:
        return None
    try:
        checked = [value in set(preselected or []) for value, _d in options]
        cursor = 0
        total = len(options)
        print(f"  {dim('↑↓ move · space select · enter confirm')}")
        drawn = _render_choice_list(options, cursor, checked, None)
        while True:
            key = keys.read_key()
            if key == "ctrl-c":
                raise KeyboardInterrupt
            if key == "up":
                cursor = max(0, cursor - 1)
            elif key == "down":
                cursor = min(total - 1, cursor + 1)
            elif key == "space":
                checked[cursor] = not checked[cursor]
            elif key == "enter":
                print("\r\033[K", end="")
                picked = [index for index, flag in enumerate(checked) if flag]
                names = [options[index][0] for index in picked]
                print(f"  {dim('selected: ' + (', '.join(names) if names else 'none'))}")
                return picked
            print(f"\r\033[{drawn}A", end="")
            drawn = _render_choice_list(options, cursor, checked, None)
    finally:
        keys.__exit__(None, None, None)


def _single_select_arrow_path(title, options, *, recommended, input_fn, interactive):
    """Arrow-key single-select used by ui.select_menu when raw mode works."""
    if not _arrow_keys_available():
        return None
    keys = _RawKeys().__enter__()
    if keys is None:
        return None
    try:
        cursor = 0
        total = len(options)
        height = _option_height(options)
        print(f"  {dim('↑↓ move · enter select')}")
        drawn = _render_choice_list(options, cursor, checked=None, recommended=recommended)
        while True:
            key = keys.read_key()
            if key == "ctrl-c":
                raise KeyboardInterrupt
            if key == "up":
                cursor = max(0, cursor - 1)
            elif key == "down":
                cursor = min(total - 1, cursor + 1)
            elif key == "enter":
                print("\r\033[K", end="")
                _finalize_menu(options, cursor, checked=None)
                return cursor
            print(f"\r\033[{drawn}A", end="")
            drawn = _render_choice_list(options, cursor, checked=None, recommended=recommended)
    finally:
        keys.__exit__(None, None, None)


# Install the arrow path into ui.select_menu (no circular import: ui does not import us).
from . import ui as _ui

_ui._arrow_single_select = _single_select_arrow_path


__all__ = [
    "multi_select_menu",
    "parse_plugin_selection",
]


def parse_plugin_selection(answer: str, plugins: list[str]) -> list[str]:
    """Parse a comma-separated numbered selection; empty answer = Core only."""
    if not answer.strip():
        return []
    try:
        indexes = [int(value.strip()) for value in answer.split(",")]
        if any(index < 1 or index > len(plugins) for index in indexes):
            raise ValueError
        seen: set[int] = set()
        ordered: list[str] = []
        for index in indexes:
            if index in seen:
                continue
            seen.add(index)
            ordered.append(plugins[index - 1])
        return ordered
    except ValueError as exc:
        raise InstallerError("plugin_selection_invalid", "plugin selection is invalid") from exc
