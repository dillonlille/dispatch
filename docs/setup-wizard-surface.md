# Dispatch Setup — Wizard Surface & Stage Rail

**Status:** Design — approved direction, not yet implemented
**Scope:** Installer setup wizard (`installer/src/dispatch_installer/`)
**Related:** `docs/harness-integration.md`, `installer/src/dispatch_installer/ui.py`

## 1. Goal

The setup wizard should feel like a product, not a log. Three properties:

1. **Pinned stage rail** — a colored stage tracker stays fixed at the top of
   the terminal for the entire run while output scrolls beneath it.
2. **Replacing sections** — when a stage completes, its screen disappears and
   the next stage's screen takes over the live area. The user never scrolls
   through completed work.
3. **Color with intent** — a strict semantic color system (see §5). Color is
   structure, not decoration: cyan = where you are, green = what's done,
   amber = attention, red = failure.

Everything degrades to plain scrolling text on non-TTY / NO_COLOR / dumb
TERM / small terminals, and machine (`--json`) output stays byte-stable.

## 2. Terminal mechanics

### 2.1 Scroll regions (DECSTBM)

The rail is pinned with the terminal's native scroll-region primitive —
the same mechanism `vim`/`htop`-class programs use for fixed chrome:

```text
\033[2J\033[3H        clear screen, home cursor
<draw rail rows>      rows 1..R (R ≈ 3)
\033[{R+1};{rows}r    DECSTBM: declare scroll region (row R+1 → bottom)
\033[{R+1};1H         park cursor inside the region
```

After this, every normal `print()` lands in the region; lines scroll only
inside it. Rows 1..R are outside the region and can never be scrolled away.
Repainting a rail cell is a row-addressed write (`\033[{row};{col}H`) that
does not disturb the region or scrollback.

Cursor position must never be guessed across repaints: query DSR
(`\033[6n`) and read the `row;colR` reply on stdin using the raw-mode reader
already present in `interactive.py`, then restore with CUP. Wrapped lines
make counted-lines math unreliable; the round-trip does not lie.

### 2.2 Replacing sections

A **section** is one wizard stage owning the live area:

- `enter(section)`: erase the live area (`\033[{R+1};1H\033[J`) *at the start
  of the new section*, then print the new header. Clearing at enter (not at
  exit) means the finished screen remains visible until the next stage
  replaces it — deliberate pacing, no strobing.
- Within a section, output scrolls normally if long; it never outlives its
  section.
- Section transitions are wrapped in synchronized-update mode when
  supported: `\033[?2026h` … `\033[?2026l`. Terminals that do not know the
  sequence ignore it harmlessly.

### 2.3 Teardown

`end()` emits, in order: release region (`\033[r`), wipe the rail rows
(`\033[4H\033[J`), print the consolidated receipt as normal scrolling output.
Because intermediate sections evaporate, the final receipt is the reviewable
record of the run.

**Failure freezes, never wipes.** Any error path calls `fail()`: release the
scroll region, leave all output on screen untouched. An error that vanishes
with its section is undebuggable. Full transcripts also go to the setup log
file regardless of screen state.

Crash safety: `try/finally` + `atexit` + SIGINT/SIGTERM handlers emit
`\033[r`. A SIGKILL can still leave the user's shell in a broken scroll
region until `reset` — accepted residual risk, documented here.

Resize: a `SIGWINCH` handler re-measures the terminal, redraws the rail,
re-emits DECSTBM, then restores the cursor via the DSR round-trip.

## 3. Module layout

```
stage_rail.py   new module (~150–200 lines), owns all escape sequences
  begin(stages, current=0)   clear · draw colored rail · DECSTBM fence · park cursor
  enter(title, subtitle)     erase live area · print colored section header
  complete(index)            row-addressed rail repaint (●→✓, cyan→green)
  advance(index)             complete previous + highlight next in one repaint
  fail()                     freeze transcript · release region · keep everything visible
  end(summary_lines)         release · wipe rail · print receipt cards

setup.py        orchestration only — zero escape sequences in this file
  with rail.section("Authentication Profiles"):
      ...existing ui.select_menu / form code unchanged...
```

`ui.py` keeps its role as the styling kit; `stage_rail.py` composes those
helpers rather than reimplementing them, so NO_COLOR handling has one source
of truth.

## 4. Screen inventory (color applied)

Legend: **C**yan accent · **G**reen success · **Y**ellow warn · dim metadata.
All examples shown with color enabled; every colored span degrades to plain
text under §6 rules.

### 4.1 Rail

```text
  ◆ Dispatch Setup                                          stage 3/5   ← ◆ and "3/5" cyan
  ✓ Plugins ─── ● Credentials ─── ○ Services ─── ○ Verify ─── ○ Done
  ────────────────────────────────────────────────────────────────────
```

State colors: `✓` green · `●` cyan (current) · `○` dim (ahead).
On transition, the current stage flips `●`→`✓` cyan→green in place — the
only animation-adjacent moment in the whole flow, kept deliberately subtle.

Narrow terminals (< ~80 cols) switch to the compact form:
`✓ Plugins · ● Credentials · ○ Verify` — separators swap from `───` to `·`,
labels truncate via `terminal_width()`.

### 4.2 Section header

```text
  AUTHENTICATION PROFILES                                            ← bold white; thin cyan rule under
  2 plugins need credentials. Profiles store secrets once,
  encrypted, and can be shared across plugins.                       ← dim explainer
```

### 4.3 Select-or-create menu (the core screen)

```text
  Choose a profile for paycom

  ❯ test                Paycom API · shared with Handbook   ← name bold white; metadata dim; ❯ cyan
    Create a new profile…                                    ← ellipsis glyph signals creation path
                                                                 "Create…" row dims when cursor elsewhere
  ↑↓ move · ↵ confirm                                        ← dim controls hint
```

Single compatible existing profile gets the green `Recommended` tag from
`ui.print_numbered_options` conventions.

### 4.4 Creation form

```text
  New profile · Paycom API                                           ← "· Paycom API" cyan badge

  Profile name
  › test_2                                                           › prompt cyan; typed text bright
    ✓ available                                                      ← green inline validation
    ⚠ already exists — try another or esc back                       ← amber inline validation

  PAYCOM_API_TOKEN
  › ••••••••••••••••

  ✓ encrypted at rest            ✓ bound to paycom                   ← green reassurance row
```

Masked getpass fields render as bullets; values are never echoed.

### 4.5 Per-plugin checklist inside the auth section

```text
  ✓ Handbook   → "test"                                              ← ✓ green
  ● Paycom     → choose a profile…                                   ← ● cyan while active
  ○ CRM        waiting                                               ← ○ dim
```

### 4.6 Receipt card (per enrolled profile)

```text
  ╭─  test ──────────────────────────────────────╮                   ← border cyan
  │  type      Paycom API                        │                   ← labels dim, values bright
  │  bound to  Handbook · Paycom                 │
  │  storage   encrypted · ~/.dispatch/secrets   │
  │  status    ● enrolled · verification pending │                   ← ● cyan until verified
  ╰──────────────────────────────────────────────╯
```

### 4.7 Final summary (printed after teardown, normal scrollback)

```text
  ────────────────────────────────────────────────────               ← dim rule
  ✓ Profiles ready — 1 enrolled · 1 reused · 2 plugins bound          ← ✓ green headline
  ────────────────────────────────────────────────────
    ✓ handbook   test    enrolled                                     ← status column colored by state
    ✓ paycom     test    enrolled
  ────────────────────────────────────────────────────
  ✗ Plugin setup failed — see ~/…/setup.log                           ← red on failure paths only
```

## 5. Color system (semantic contract)

| Token | ANSI | Used exclusively for |
|---|---|---|
| accent | `36` cyan | rail current-stage, section identity, `❯`/`›` glyphs, borders, provider badges |
| success | `32` green | completed stages/ticks, validation passes, receipts, Recommended tag |
| warn | `33` yellow | inline retry warnings, pending-verification states |
| error | `31` red | failures only — never decoration |
| dim | `2` | metadata, explainers, control hints, ahead-stages |
| bold | `1` | decision text: option names, section titles |

Rules:

1. **One accent.** Cyan is the only structural hue. No second accent ever —
   this single rule is most of what separates "product" from "script".
2. **Green means done, red means broken, amber means look.** Status words
   never appear without their matching color, and colors never appear without
   their meaning.
3. **Brightness hierarchy:** bold > regular > dim maps to decision >
   content > context. If everything is emphasized nothing is.
4. **Color is additive, never load-bearing.** Any line fully readable with
   colors stripped (tests enforce this — §7).

Existing kit tokens (`_ACCENT/_SUCCESS/_WARN/_ERROR/_DIM` in `ui.py`) are
reused as-is; no new hues introduced.

## 6. Degradation matrix

| Environment | Rail | Sections | Color |
|---|---|---|---|
| Real TTY, color, ≥12 rows, DECSTBM honored | pinned | replacing | full |
| TTY but dumb TERM / NO_COLOR / <12 rows | static header printed once, scrolls away normally | appending (no erase) | off |
| Piped / CI / non-TTY / `human=False` | suppressed | appending | off |
| `--json` runs | byte-identical output to today's JSON — zero escapes anywhere | same | n/a |

Capability probing before enabling pinning: stdout.isatty, TERM != dumb,
absence of NO_COLOR, rows ≥ 12, and terminfo `scroll_region` capability.
Any probe fails → static-header ladder automatically.

## 7. Test plan (pty driver, extends installer-testing-patterns)

1. Real pty: begin emits clear + rail rows + DECSTBM; rail rows still present
   after N section transitions (never scrolled away).
2. Live-area erase emitted exactly once per section boundary.
3. Crash mid-section: `\033[r` emitted, prior output preserved on screen.
4. SIGINT path: region released + friendly resume message, no traceback.
5. Piped/non-TTY run: zero escape bytes in captured output.
6. NO_COLOR TTY run: zero SGR bytes; layout identical minus color.
7. `--json`: byte-for-byte identical to pre-change baseline.
8. Resize (SIGWINCH injected): rail redrawn, region re-fenced, cursor restored.
9. Narrow width: compact rail renders, nothing exceeds `terminal_width()`.

Regression suites that must stay green: bootstrap + profile-setup suites;
single-render menu contract tests (`test_interactive_menus.py`).

## 8. Implementation order

1. `stage_rail.py` + degradation probes (no callers yet) + pty tests 1–8.
2. Wire into `_run_setup` around the auth stage only (lowest-risk slice);
   manual acceptance against the mockups in §4.
3. Extend to remaining stages; retire the old append-only headers.
4. Docs screenshot refresh + changelog note.
