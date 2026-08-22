"""Tests for the wizard-surface stage rail (docs/setup-wizard-surface.md).

Two drivers, mirroring the merged PR #37 recipe:

- In-process fakes (StringIO subclass with ``isatty``) for logic, escape
  sequences, and degradation — fully capture-safe under pytest.
- A real pty pair for the end-to-end "rail survives on a genuine terminal"
  scenario that fake streams cannot prove.
"""
from __future__ import annotations

import io
import os
import select
import signal

import pytest

from dispatch_installer import stage_rail
from dispatch_installer.stage_rail import RAIL_ROWS, StageRail


class FakeTty(io.StringIO):
    """StringIO that claims to be a terminal so capability gates pass."""

    def isatty(self) -> bool:  # noqa: FBT001
        return True


@pytest.fixture(autouse=True)
def _color_env(monkeypatch):
    """Give every test a color-capable environment baseline."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def make_rail(**overrides) -> tuple[StageRail, FakeTty]:
    stream = FakeTty()
    defaults = {
        "stream": stream,
        "width_fn": lambda: 100,
        "rows_fn": lambda: 24,
    }
    defaults.update(overrides)
    return StageRail(**defaults), stream


# --------------------------------------------------------------------- #
# Capability gate                                                        #
# --------------------------------------------------------------------- #


def test_begin_refuses_non_tty_stream():
    stream = io.StringIO()
    rail = StageRail(stream=stream, width_fn=lambda: 100, rows_fn=lambda: 24)
    assert rail.begin(("Plugins", "Credentials", "Done")) is False
    assert rail.active is False
    rail.enter("X")  # no-op, no escapes anywhere
    rail.advance(1)
    rail.end(["summary"])
    assert stream.getvalue() == ""


def test_begin_refuses_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    rail, stream = make_rail()
    assert rail.begin(("Plugins", "Credentials", "Done")) is False
    assert stream.getvalue() == ""


def test_begin_refuses_dumb_term(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    rail, stream = make_rail()
    assert rail.begin(("Plugins", "Credentials", "Done")) is False
    assert stream.getvalue() == ""


def test_begin_refuses_short_terminal():
    rail, stream = make_rail(rows_fn=lambda: stage_rail.MIN_ROWS - 1)
    assert rail.begin(("Plugins", "Credentials", "Done")) is False
    assert stream.getvalue() == ""


# --------------------------------------------------------------------- #
# Lifecycle + escape sequences                                           #
# --------------------------------------------------------------------- #


def test_begin_draws_rail_and_fences_scroll_region():
    rail, stream = make_rail()
    assert rail.begin(("Plugins", "Credentials", "Done"), current=1) is True
    out = stream.getvalue()
    assert out.startswith("\033[2J\033[H")
    assert "\033[4;24r" in out  # DECSTBM fence below RAIL_ROWS
    assert f"\033[{RAIL_ROWS + 1};1H" in out
    assert "Dispatch Setup" in out
    assert "stage 2/3" in out
    assert "✓ Plugins" in out  # before current -> done
    assert "● Credentials" in out  # current
    assert "○ Done" in out  # ahead
    # Rail rows are drawn before the fence; nothing scrolls them away.


def test_advance_repaints_tracker_in_place():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    rail.advance(2)
    out = stream.getvalue()
    assert out.startswith("\033[2;1H\033[2K")
    assert "✓ Credentials" in out
    assert "● Done" in out
    assert "\033[r" not in out  # region stays fenced during repaints


def test_enter_erases_live_area_and_prints_header():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    rail.enter("Authentication Profiles", "Profiles store secrets once.")
    out = stream.getvalue()
    assert out.startswith(f"\033[{RAIL_ROWS + 1};1H\033[J")
    assert "Authentication Profiles" in out
    assert "Profiles store secrets once." in out


def test_section_context_success_enters_once_and_never_fails():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    with rail.section("Credentials") as ctx:
        assert ctx is rail
        print("body line", file=stream)
    out = stream.getvalue()
    assert "\033[J" in out
    assert "body line" in out
    assert "\033[r" not in out  # still active: no freeze, no teardown


def test_section_context_failure_freezes_transcript():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    with pytest.raises(RuntimeError, match="boom"):
        with rail.section("Credentials"):
            print("progress before crash", file=stream)
            raise RuntimeError("boom")
    out = stream.getvalue()
    assert "progress before crash" in out  # frozen, never wiped
    assert out.endswith("\033[r") or "\033[r" in out  # region released
    assert rail.active is False


def test_fail_then_methods_noop():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    rail.fail()
    stream.seek(0)
    stream.truncate()
    rail.advance(1)
    rail.enter("Next")
    rail.fail()
    rail.end(["summary"])
    assert stream.getvalue() == ""
    assert rail.active is False


def test_end_releases_wipes_and_prints_summary():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    rail.end(["  ✓ Profiles ready"])
    out = stream.getvalue()
    assert out.startswith("\033[r\033[2J\033[H")
    assert "✓ Profiles ready" in out
    assert rail.active is False


def test_end_without_summary_still_teardowns():
    rail, stream = make_rail()
    rail.begin(("Plugins", "Credentials", "Done"))
    stream.seek(0)
    stream.truncate()
    rail.end()
    out = stream.getvalue()
    assert out == "\033[r\033[2J\033[H"
    assert rail.active is False


def test_begin_twice_is_idempotent():
    rail, stream = make_rail()
    assert rail.begin(("A",)) is True
    first = stream.getvalue()
    assert rail.begin(("B", "C")) is True
    assert stream.getvalue() == first  # second begin changed nothing


# --------------------------------------------------------------------- #
# Rendering details                                                      #
# --------------------------------------------------------------------- #


def test_compact_tracker_on_narrow_terminal():
    rail, stream = make_rail(width_fn=lambda: 60)
    rail.begin(("Plugins", "Credentials", "Verify", "Done"))
    out = stream.getvalue()
    assert " · " in out  # compact separator
    assert " ─── " not in out


def test_truncation_drops_oldest_cells_first():
    stages = [f"Stage{index}" for index in range(8)]
    rail, stream = make_rail(width_fn=lambda: 40)
    rail.begin(stages)
    out = stream.getvalue()
    line = next(l for l in out.splitlines() if "●" in l)
    assert "…" in line
    assert _visible(line) <= 40


def _visible(text: str) -> int:
    import re

    return len(re.sub(r"\033\[[0-9;]*m", "", text))


# --------------------------------------------------------------------- #
# Crash-safety guards                                                    #
# --------------------------------------------------------------------- #


def test_signal_guards_installed_and_restored():
    prior = signal.getsignal(signal.SIGINT)
    rail, _s = make_rail()
    try:
        rail.begin(("Plugins", "Credentials", "Done"))
        assert signal.getsignal(signal.SIGINT) == rail._on_signal
        assert signal.getsignal(signal.SIGTERM) == rail._on_signal
    finally:
        rail.end()
    assert signal.getsignal(signal.SIGINT) is prior


def test_sigint_handler_freezes_prints_resume_and_chains():
    seen = []
    rail, stream = make_rail()

    def previous(signum, frame):
        seen.append(signum)

    old_int = signal.signal(signal.SIGINT, previous)
    try:
        rail.begin(("Plugins", "Credentials", "Done"))
        assert signal.getsignal(signal.SIGINT) == rail._on_signal
        rail._on_signal(signal.SIGINT, None)
    finally:
        rail.end()
        signal.signal(signal.SIGINT, old_int)
    out = stream.getvalue()
    assert "rerun 'dispatch setup' to resume" in out.replace("\u2019", "'")
    assert seen == [signal.SIGINT]  # chained to the pre-rail handler
    assert "\033[r" in out.split("rerun")[0]


# --------------------------------------------------------------------- #
# Resize deferral                                                        #
# --------------------------------------------------------------------- #


def test_resize_deferred_until_next_boundary():
    rail, stream = make_rail(width_fn=lambda: 100)
    rail.begin(("Plugins", "Credentials", "Done"))
    rail.request_redraw()
    stream.seek(0)
    stream.truncate()
    rail.request_redraw()
    assert stream.getvalue() == ""  # nothing written until a boundary call
    rail.enter("Next")
    out = stream.getvalue()
    assert out.startswith("\033[2J")  # full surface redraw happened first
    assert "\033[" in out


# --------------------------------------------------------------------- #
# Real pty end-to-end                                                    #
# --------------------------------------------------------------------- #


def _drain(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def test_real_pty_rail_survives_section_transitions():
    import pty

    master, slave = pty.openpty()
    stream = os.fdopen(slave, "w", encoding="utf-8", closefd=False)
    try:
        rail = StageRail(stream=stream, width_fn=lambda: 100, rows_fn=lambda: 30)
        assert rail.begin(("Plugins", "Credentials", "Done"), current=0) is True

        first = _drain(master).decode("utf-8", "replace")
        assert "\033[4;30r" in first  # DECSTBM fence on the real terminal
        assert "● Plugins" in first

        with rail.section("Authentication Profiles") as ctx:
            print("menu placeholder", file=ctx._stream)
            stream.flush()
            body = _drain(master).decode("utf-8", "replace")
            assert "Authentication Profiles" in body
            assert body.startswith(f"\033[{RAIL_ROWS + 1};1H\033[J")

        rail.advance(1)
        rail.enter("Services")
        second = _drain(master).decode("utf-8", "replace")
        assert "✓ Plugins" in second  # completed stage ticked on the pinned rail

        rail.end(["  ✓ Profiles ready"])
        tail = _drain(master).decode("utf-8", "replace")
        assert tail.startswith("\033[r")  # region released on the real terminal
        assert "✓ Profiles ready" in tail
        assert rail.active is False
    finally:
        try:
            stream.close()
        except OSError:
            pass
        os.close(master)
