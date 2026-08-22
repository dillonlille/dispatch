"""Uninstall category model: taxonomy, mode presets, and selection rules.

Uninstall work is expressed as a set of named categories. Every invocation
resolves to exactly one selection:

- ``standard`` removes validated Dispatch-owned code and disposable runtime
  material while preserving durable private data (the historical default);
- ``complete`` removes the entire Dispatch footprint including durable data
  (the historical ``--purge``);
- ``custom`` resolves an explicit include/exclude subset of the same closed
  vocabulary, and can therefore never be broader than ``complete``.

Selection resolution never touches the filesystem; it raises
:class:`InstallerError` on unknown names, contradictory arguments, or an
unconfirmed secrets removal so callers can surface precise machine-readable
errors before any plan or mutation exists.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .layout import InstallerError

STANDARD_MODE = "standard"
COMPLETE_MODE = "complete"
CUSTOM_MODE = "custom"
UNINSTALL_MODES = (STANDARD_MODE, COMPLETE_MODE, CUSTOM_MODE)


@dataclass(frozen=True, slots=True)
class CategorySpec:
    name: str
    title: str
    summary: str
    durable: bool


CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec(
        "code",
        "Application code",
        "the cloned repository, installation record, and install scratch",
        durable=False,
    ),
    CategorySpec(
        "runtime",
        "Runtime",
        "the virtual environment and launcher-side process scratch",
        durable=False,
    ),
    CategorySpec(
        "services",
        "Services",
        "Dispatch and plugin systemd user units with their receipts",
        durable=False,
    ),
    CategorySpec(
        "launcher",
        "Launcher",
        "the ~/.local/bin/dispatch command when Dispatch-owned",
        durable=False,
    ),
    CategorySpec(
        "cache",
        "Cache",
        "disposable downloads including the managed browser",
        durable=False,
    ),
    CategorySpec(
        "logs",
        "Logs",
        "operational logs from services and commands",
        durable=True,
    ),
    CategorySpec(
        "state",
        "State",
        "operational receipts, browser installation state, migration records",
        durable=True,
    ),
    CategorySpec(
        "data",
        "Data",
        "databases and durable application data",
        durable=True,
    ),
    CategorySpec(
        "config",
        "Configuration",
        "user configuration files",
        durable=True,
    ),
    CategorySpec(
        "secrets",
        "Secrets",
        "stored credentials; requires explicit confirmation",
        durable=True,
    ),
)

CATEGORY_NAMES = frozenset(spec.name for spec in CATEGORIES)
DURABLE_CATEGORIES = frozenset(spec.name for spec in CATEGORIES if spec.durable)

_STANDARD_REMOVED = frozenset({"code", "runtime", "services", "launcher", "cache"})
_COMPLETE_REMOVED = frozenset(CATEGORY_NAMES)


def category_spec(name: str) -> CategorySpec:
    for spec in CATEGORIES:
        if spec.name == name:
            return spec
    raise InstallerError("uninstall_category_unknown", f"unknown uninstall category: {name}")


def preset_removed(mode: str) -> frozenset[str]:
    """Categories removed by a preset mode before any exclusion is applied."""
    if mode == STANDARD_MODE:
        return _STANDARD_REMOVED
    if mode == COMPLETE_MODE:
        return _COMPLETE_REMOVED
    raise InstallerError("uninstall_mode_unknown", f"unknown uninstall mode: {mode}")


def resolve_selection(
    mode: str,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
    *,
    secrets_confirmed: bool = False,
) -> frozenset[str]:
    """Resolve a mode plus include/exclude refinements into a selection.

    Presets accept exclusions only; ``custom`` accepts either an include list
    (exactly these categories are removed) or an exclude list (the complete
    preset minus these), never both. Removing secrets through ``custom``
    requires explicit confirmation; the ``standard`` and ``complete`` presets
    carry their own documented contracts and remain governed by the caller's
    confirmation flag alone.
    """
    if mode not in UNINSTALL_MODES:
        raise InstallerError("uninstall_mode_unknown", f"unknown uninstall mode: {mode}")
    requested = set(include) | set(exclude)
    unknown = sorted(requested - CATEGORY_NAMES)
    if unknown:
        raise InstallerError(
            "uninstall_category_unknown",
            f"unknown uninstall category: {', '.join(unknown)}",
        )
    if mode == CUSTOM_MODE:
        if not requested:
            raise InstallerError(
                "uninstall_selection_required",
                "custom mode requires at least one --with/--without category or an interactive selection",
            )
        if include and exclude:
            raise InstallerError(
                "uninstall_arguments",
                "--with and --without cannot be combined in custom mode",
            )
        if include:
            removed = frozenset(include)
        else:
            removed = frozenset(_COMPLETE_REMOVED - set(exclude))
        if "secrets" in removed and not secrets_confirmed:
            raise InstallerError(
                "uninstall_secrets_unconfirmed",
                'removing secrets in custom mode requires explicit confirmation ("delete secrets")',
            )
    else:
        if include:
            raise InstallerError("uninstall_arguments", "--with categories apply only to --mode custom")
        removed = frozenset(preset_removed(mode) - set(exclude))
    if not removed:
        raise InstallerError("uninstall_selection_empty", "the resolved selection removes nothing")
    return removed


def dependency_notes(removed: Iterable[str]) -> list[str]:
    """Warnings about partially-kept installations a selection implies.

    Selections are honored exactly as requested; these notes explain the
    consequences instead of silently widening the deletion.
    """
    selected = frozenset(removed)
    notes: list[str] = []
    if "code" in selected and "runtime" not in selected:
        notes.append(
            "runtime kept: the virtual environment remains without the application that uses it"
        )
    if "runtime" in selected and "code" not in selected:
        notes.append(
            "code kept: Dispatch cannot run until the runtime is restored (dispatch repair)"
        )
    if "code" in selected and "services" not in selected:
        notes.append(
            "services kept: installed units will keep launching removed code; disable them first"
        )
    if "services" in selected and {"code", "runtime"}.isdisjoint(selected):
        notes.append(
            "code kept: only background units are removed; direct CLI use is unaffected"
        )
    if "state" in selected and selected != _COMPLETE_REMOVED:
        notes.append(
            "state removed: operational receipts and browser installation state are erased"
        )
    if "cache" in selected and "code" not in selected:
        notes.append(
            "cache removed: the managed browser is deleted and is re-downloaded on next need"
        )
    return notes


__all__ = [
    "CATEGORIES",
    "CATEGORY_NAMES",
    "COMPLETE_MODE",
    "CUSTOM_MODE",
    "CategorySpec",
    "DURABLE_CATEGORIES",
    "STANDARD_MODE",
    "UNINSTALL_MODES",
    "category_spec",
    "dependency_notes",
    "preset_removed",
    "resolve_selection",
]
