"""Pinned stage rail for the Dispatch setup wizard surface.

Implements the wizard-surface model from ``docs/setup-wizard-surface.md``:
a colored stage tracker occupies the top ``RAIL_ROWS`` rows of the terminal
and is pinned there with a DECSTBM scroll region, while every later line of
output scrolls inside the remaining live area. Entering a new section erases
the live area, so completed sections disappear instead of scrolling away —
the terminal behaves as a replacing surface rather than an appending log.

Design rules honored here:

- All escape-sequence knowledge lives in this module. Callers compose
  sections; they never emit control codes themselves.
- Repaints happen only at section boundaries (``advance`` immediately before
  ``enter``/``end``), so no cursor-position tracking is needed and stdin is
  never touched.
- Failure freezes: ``fail()`` releases the scroll region and leaves every
  completed line on screen. Only successful teardown erases anything.
- Degradation: without a color-capable TTY of sufficient height the rail
  disables itself; every method then no-ops and caller output stays exactly
  what it was before this module existed. Machine (``--json``) paths never
  construct a rail.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import sys
from contextlib import contextmanager

from . import ui

RAIL_ROWS = 3
MIN_ROWS = 12
COMPACT_WIDTH = 80
_MAX_RULE = 68

_SGR = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Length of ``text`` as the terminal will draw it (SGR codes stripped)."""
    return len(_SGR.sub("", text))


def _terminal_rows() -> int:
    try:
        return max(24, shutil.get_terminal_size((72, 24)).lines)
    except (OSError, ValueError, AttributeError):
        return 24


class StageRail:
    """Owns the pinned stage tracker and the replacing-section live area."""

    def __init__(
        self,
        *,
        stream=None,
        width_fn=ui.terminal_width,
        rows_fn=_terminal_rows,
    ) -> None:
        self._stream = stream or sys.stdout
        self._width_fn = width_fn
        self._rows_fn = rows_fn
        self._stages: tuple[str, ...] = ()
        self._current = 0
        self._active = False
        self._resize_pending = False
        self._prev_handlers: dict[int, object] = {}
        self._atexit_installed = False

    # ------------------------------------------------------------------ #
    # Capability gate                                                     #
    # ------------------------------------------------------------------ #

    def _supported(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("TERM") == "dumb":
            return False
        stream = self._stream
        isatty = getattr(stream, "isatty", None)
        if not callable(isatty) or not isatty():
            return False
        if getattr(stream, "closed", False):
            return False
        try:
            if self._rows_fn() < MIN_ROWS:
                return False
        except (OSError, ValueError, AttributeError):
            return False
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def begin(self, stages=("Plugins", "Credentials", "Done"), current: int = 0) -> bool:
        """Clear the screen, draw the rail, fence it with a scroll region."""
        if self._active:
            return True
        if not stages:
            return False
        if not self._supported():
            return False
        self._stages = tuple(str(name) for name in stages)
        self._current = max(0, min(current, len(self._stages) - 1))
        rows = self._rows_fn()
        out = self._stream
        try:
            out.write("\033[2J\033[H")
            out.write(self._title_row() + "\n")
            out.write(self._tracker_row() + "\n")
            out.write(ui.dim("─" * min(self._width_fn() - 4, _MAX_RULE)) + "\n")
            # Fence: rows below the rail become the only scrolling area.
            out.write(f"\033[{RAIL_ROWS + 1};{rows}r")
            out.write(f"\033[{RAIL_ROWS + 1};1H")
            out.flush()
        except (OSError, ValueError):
            return False
        self._active = True
        self._install_guards()
        return True

    @property
    def active(self) -> bool:
        return self._active

    def advance(self, index: int) -> None:
        """Mark stages before ``index`` done and ``index`` current (boundary call)."""
        if not self._active:
            return
        self._current = max(0, min(index, len(self._stages) - 1))
        self._repaint_tracker()

    @contextmanager
    def section(self, title: str, subtitle: str = ""):
        """Enter a section; freeze the transcript (fail) if the body raises."""
        self.enter(title, subtitle)
        try:
            yield self
        except BaseException:
            self.fail()
            raise

    def enter(self, title: str, subtitle: str = "") -> None:
        """Erase the live area and start a fresh section inside it."""
        if not self._active:
            return
        self._flush_resize()
        out = self._stream
        width = self._width_fn()
        try:
            out.write(f"\033[{RAIL_ROWS + 1};1H\033[J")
            print(f"\n  {ui.bold(title)}", file=out)
            if subtitle:
                print(f"  {ui.dim(subtitle)}", file=out)
            print(ui.dim("─" * min(_visible_len(title) + 2, width - 4, _MAX_RULE)), file=out)
            print(file=out)
            out.flush()
        except (OSError, ValueError):
            return

    def fail(self) -> None:
        """Release the region and keep every completed line visible."""
        if not self._active:
            return
        self._remove_guards()
        self._active = False
        try:
            self._stream.write("\033[r")
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def end(self, summary_lines: list[str] | None = None) -> None:
        """Release the region, wipe the surface, print the closing receipt."""
        if not self._active:
            return
        self._remove_guards()
        self._active = False
        out = self._stream
        try:
            out.write("\033[r\033[2J\033[H")
            out.flush()
            for line in summary_lines or []:
                print(line, file=out)
            out.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #
    # Rendering                                                           #
    # ------------------------------------------------------------------ #

    def _title_row(self) -> str:
        left = f"  {ui.accent('◆')} Dispatch Setup"
        counter = ui.accent(f"stage {self._current + 1}/{len(self._stages)}")
        width = self._width_fn()
        pad = width - _visible_len(left) - _visible_len(counter) - 2
        if pad < 1:
            return left
        return f"{left}{' ' * pad}{counter}"

    def _cells(self) -> list[str]:
        cells: list[str] = []
        for index, name in enumerate(self._stages):
            if index < self._current:
                glyph, label = ui.success("✓"), name
            elif index == self._current:
                glyph, label = ui.accent("●"), ui.bold(name)
            else:
                glyph, label = ui.dim("○"), ui.dim(name)
            cells.append(f"{glyph} {label}")
        return cells

    def _tracker_row(self) -> str:
        width = self._width_fn()
        compact = width < COMPACT_WIDTH
        cells = self._cells()
        separator = ui.dim(" · ") if compact else ui.dim(" ─── ")
        row = "  " + separator.join(cells)

        def truncated(cells_now: list[str]) -> str:
            body = "  " + separator.join(cells_now)
            if _visible_len(body) <= width - 1:
                return body
            return "  " + separator.join(cells_now[:-1]) + separator + ui.dim("…")

        while _visible_len(row) > width - 1 and len(cells) > 1:
            cells = cells[:-1]
            row = truncated(cells)
        return row

    def _repaint_tracker(self) -> None:
        out = self._stream
        try:
            out.write(f"\033[2;1H\033[2K{self._tracker_row()}")
            out.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #
    # Crash safety                                                        #
    # ------------------------------------------------------------------ #

    def _install_guards(self) -> None:
        if not self._atexit_installed:
            atexit.register(self._release_quietly)
            self._atexit_installed = True
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._prev_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):
                self._prev_handlers.pop(signum, None)

    def _remove_guards(self) -> None:
        for signum, handler in list(self._prev_handlers.items()):
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        self._prev_handlers.clear()

    def _on_signal(self, signum, frame):  # noqa: ANN001
        # Capture the prior handler BEFORE fail(): fail() restores the
        # previous disposition and clears the map, so looking it up after
        # would find nothing and fall through to the terminate branch.
        handler = self._prev_handlers.get(signum)
        self.fail()
        if signum == signal.SIGINT:
            try:
                print("\n  Setup interrupted — rerun 'dispatch setup' to resume.", file=self._stream)
                self._stream.flush()
            except (OSError, ValueError):
                pass
        if callable(handler) and handler not in (signal.SIG_IGN,):
            handler(signum, frame)
            return
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except (ValueError, OSError):
            raise KeyboardInterrupt from None

    def _release_quietly(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            self._stream.write("\033[r")
            self._stream.flush()
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #
    # Resize (deferred to the next boundary call)                         #
    # ------------------------------------------------------------------ #

    def request_redraw(self) -> None:
        """Note that the terminal changed size; applied at next boundary.

        A SIGWINCH handler can call this any time; the repaint is deferred
        to the next ``enter``/``advance`` so it can never interrupt a live
        menu mid-redraw.
        """
        if self._active:
            self._resize_pending = True

    def _flush_resize(self) -> None:
        if not self._resize_pending:
            return
        self._resize_pending = False
        try:
            self._stream.write("\033[2J\033[H")
            self._stream.write(self._title_row() + "\n")
            self._stream.write(self._tracker_row() + "\n")
            self._stream.write(ui.dim("─" * min(self._width_fn() - 4, _MAX_RULE)) + "\n")
            self._stream.write(f"\033[{RAIL_ROWS + 1};{self._rows_fn()}r")
            self._stream.write(f"\033[{RAIL_ROWS + 1};1H")
            self._stream.flush()
        except (OSError, ValueError):
            pass


__all__ = ["StageRail", "RAIL_ROWS", "MIN_ROWS"]
