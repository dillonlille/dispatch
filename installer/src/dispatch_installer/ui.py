"""Bounded terminal presentation helpers for the Dispatch installer.

A dependency-free ANSI/Unicode UI kit. Every helper degrades gracefully:
when stdout is not a TTY, ``NO_COLOR`` is set, or the terminal lacks
capabilities, output falls back to plain text with identical information
content. Nothing here reads secrets or makes network calls.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

_ACCENT = "36"      # cyan
_SUCCESS = "32"     # green
_WARN = "33"        # yellow
_ERROR = "31"       # red
_DIM = "2"

_RESET = "\033[0m"


def _colors_enabled(stream: object = None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    stream = stream or sys.stdout
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    return True


def _paint(text: str, code: str) -> str:
    if not _colors_enabled():
        return text
    return f"\033[{code}m{text}{_RESET}"


def accent(text: str) -> str:
    return _paint(text, _ACCENT)


def success(text: str) -> str:
    return _paint(text, _SUCCESS)


def warn(text: str) -> str:
    return _paint(text, _WARN)


def error(text: str) -> str:
    return _paint(text, _ERROR)


def dim(text: str) -> str:
    return _paint(text, _DIM)


def bold(text: str) -> str:
    return _paint(text, "1")


def terminal_width(default: int = 72) -> int:
    try:
        width = shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError, AttributeError):
        return default
    return max(40, min(width, 120))


def banner(title: str, subtitle: str = "") -> str:
    """Render the product banner shown at install/setup start."""
    inner = max(len(title), len(subtitle)) + 6
    top = "  ╭" + "─" * (inner + 2) + "╮"
    bottom = "  ╰" + "─" * (inner + 2) + "╯"
    def line(text: str) -> str:
        padding = (inner - len(text)) // 2
        return "  │" + " " * padding + text + " " * (inner - len(text) - padding) + "│"
    rows = [top, line(""), line(accent(bold(f"◆  {title}")))]
    if subtitle:
        rows.append(line(dim(subtitle)))
    rows.extend([line(""), bottom])
    return "\n".join(rows)


def step_header(current: int, total: int, title: str) -> str:
    counter = dim(f"{current}/{total}")
    return f"\n  {accent('◆')} Dispatch Setup {counter}\n\n  {bold(title)}\n"


def status_line(glyph: str, label: str, detail: str = "") -> str:
    rendered = {
        "ok": success("✓"),
        "run": accent("●"),
        "warn": warn("⚠"),
        "fail": error("✗"),
    }.get(glyph, glyph)
    suffix = dim(f"  {detail}") if detail else ""
    return f"  {rendered} {label}{suffix}"


def summary_divider() -> str:
    return dim("  " + "─" * min(46, terminal_width() - 4))


def select_menu(
    title: str,
    options: list[tuple[str, str]],
    *,
    recommended: str | None = None,
    hint: str = "",
    input_fn=input,
    interactive: bool | None = None,
) -> int | None:
    """Present a numbered selection menu; return the chosen index.

    ``options`` is a list of ``(value, description)`` pairs. The option whose
    value equals ``recommended`` is tagged. Returns ``None`` when stdin is
    unavailable; raises nothing. Arrow-key navigation is intentionally not
    used here: a numbered menu works identically piped, scripted, and over
    every SSH client.
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
    while True:
        try:
            raw = input_fn("  Select [1-" + str(len(options)) + "]: ").strip()
        except EOFError:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  {error('Invalid selection.')}")


@dataclass(slots=True)
class MenuOption:
    value: str
    description: str = ""


__all__ = [
    "MenuOption",
    "accent",
    "banner",
    "bold",
    "dim",
    "error",
    "select_menu",
    "status_line",
    "step_header",
    "success",
    "summary_divider",
    "terminal_width",
    "warn",
]
