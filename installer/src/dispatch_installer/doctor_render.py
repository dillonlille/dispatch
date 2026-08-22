"""Human-facing rendering of `dispatch doctor` / `dispatch verify` reports.

Turns the machine report from :mod:`.doctor` into grouped, prioritized
terminal rows using the shared UI kit. Pure function over the report dict:
every fact rendered comes from the payload, healthy groups collapse to one
line, and every non-ready check carries a concrete remedy. Colors and
degradation are handled by :mod:`.ui`; ``--json`` consumers never see this
module.
"""
from __future__ import annotations

from typing import Any

from .ui import (
    accent,
    bold,
    dim,
    error,
    success,
    summary_divider,
    terminal_width,
    warn,
)

_LABEL_WIDTH = 13  # longest group label ("Installation") + spacing

# Statuses that read as informational rather than attention-worthy.
_INFO_STATUSES = {"prepared", "not_configured"}
_WARN_STATUSES = {"missing", "incomplete"}

_VERDICTS = {
    "ready": "Installation ready",
    "incomplete": "Installation incomplete",
    "unsafe": "Installation needs attention",
}

_DIRECTORY_ORDER = (
    "dispatch_home",
    "clone",
    "venv",
    "config",
    "secrets",
    "data",
    "state",
    "cache",
    "logs",
    "run",
)

_GROUP_ORDER = (
    "installation",
    "code",
    "runtime",
    "launcher",
    "service",
    "plugins",
    "directories",
)


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - 1, 1)] + "…"


def _row(glyph: str, label: str, detail: str) -> str:
    return f"  {glyph} {bold(label.ljust(_LABEL_WIDTH))}{_truncate(detail, max(terminal_width() - _LABEL_WIDTH - 6, 24))}"


def _note(lines: list[str], text: str, *, remedy: str | None = None) -> None:
    lines.append(f"      {dim(_truncate(text, max(terminal_width() - 10, 24)))}")
    if remedy:
        # Remedies are copy-pasteable commands: never truncate them.
        lines.append(f"      {accent('try:')} {remedy}")


_STATUS_WORDS = {
    "ready": "verified",
    "missing": "not found",
    "unsafe": "failed safety checks",
    "incomplete": "not fully operational",
    "prepared": "staged",
    "not_configured": "not configured",
}


def _reason(check: dict[str, Any]) -> str:
    reason = check.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    if check.get("error"):
        return str(check["error"])
    return _STATUS_WORDS.get(str(check.get("status")), str(check.get("status", "unknown")))


def _hint(check: dict[str, Any]) -> str | None:
    hint = check.get("hint")
    return hint if isinstance(hint, str) and hint else None


def _glyph_for(statuses: list[str]) -> str:
    if any(status == "unsafe" for status in statuses):
        return error("✗")
    if any(status in _WARN_STATUSES for status in statuses):
        return warn("⚠")
    if any(status in _INFO_STATUSES for status in statuses):
        return accent("●")
    return success("✓")


def _installation_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    check = checks.get("installation", {})
    status = str(check.get("status", "missing"))
    lines: list[str] = []
    if status == "ready":
        channel = check.get("channel") or "?"
        ref = check.get("ref") or "?"
        commit = str(check.get("commit") or "")
        stamp = f" ({commit[:8]})" if commit else ""
        detail = f"{channel} @ {ref}{stamp}"
    else:
        detail = "no installation record"
        _note(
            lines,
            str(check.get("reason") or _reason(check)),
            remedy=_hint(check) or "run the Dispatch installer to complete setup",
        )
    return _row(_glyph_for([status]), "Installation", detail), lines


def _installer_or(alternative: str, checks: dict[str, Any]) -> str:
    """Remedy chooser: before an installation exists, repair is meaningless."""
    installation = checks.get("installation", {})
    if str(installation.get("status", "missing")) != "ready":
        return "run the Dispatch installer to complete setup"
    return alternative


def _code_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    check = checks.get("clone", {})
    statuses = [str(check.get("status", "missing")), str(check.get("git", "missing"))]
    lines: list[str] = []
    if "unsafe" in statuses:
        detail = "managed checkout is compromised or drifted"
        _note(
            lines,
            str(check.get("reason") or "the checkout does not match the installation record"),
            remedy=_installer_or("dispatch update", checks),
        )
    elif "missing" in statuses:
        detail = "clone is absent or lacks git metadata"
        _note(
            lines,
            f"clone repository is absent at {check.get('path', 'the expected location')}",
            remedy=_installer_or("dispatch update", checks),
        )
    else:
        detail = "checkout matches installation record"
    return _row(_glyph_for(statuses), "Code", detail), lines


def _runtime_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    venv = checks.get("venv", {})
    python_status = str(venv.get("python", "missing"))
    statuses = [str(venv.get("status", "missing")), python_status]
    lines: list[str] = []
    reason = str(venv.get("python_reason") or "") if python_status in {"unsafe", "missing"} else ""
    hint = str(venv.get("python_hint") or "") if reason else ""
    if "unsafe" in statuses:
        detail = "virtual environment is unusable"
        _note(lines, reason or "the virtual environment failed safety checks", remedy=_installer_or(hint or "dispatch repair", checks))
    elif "missing" in statuses:
        detail = "virtual environment is missing"
        _note(lines, reason or "no virtual environment was found", remedy=_installer_or(hint or "dispatch repair", checks))
    else:
        detail = "virtual environment verified"
    return _row(_glyph_for(statuses), "Runtime", detail), lines


def _launcher_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    check = checks.get("command", {})
    status = str(check.get("status", "missing"))
    lines: list[str] = []
    command_path = str(check.get("command", ""))
    if status == "ready":
        detail = command_path
    else:
        detail = {"missing": "launcher is not installed", "unsafe": "launcher failed ownership checks"}.get(
            status,
            f"launcher status: {status}",
        )
        _note(lines, _reason(check), remedy=_installer_or(_hint(check) or "dispatch repair", checks))
    return _row(_glyph_for([status]), "Launcher", detail), lines


def _service_detail(check: dict[str, Any]) -> str:
    if check.get("status") == "ready":
        return "active, enabled"
    parts = []
    if "active" in check:
        parts.append("active" if check["active"] else "inactive")
    if "enabled" in check:
        parts.append("enabled" if check["enabled"] else "not enabled")
    return ", ".join(parts) if parts else _reason(check)


def _service_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    check = checks.get("service", {})
    status = str(check.get("status", "missing"))
    lines: list[str] = []
    if status == "ready":
        detail = _service_detail(check)
    elif status == "incomplete":
        detail = _service_detail(check)
        active = bool(check.get("active"))
        enabled = bool(check.get("enabled"))
        if not active and enabled:
            remedy = "systemctl --user start dispatch.service  ·  or: dispatch repair"
        elif active and not enabled:
            remedy = "systemctl --user enable dispatch.service  ·  or: dispatch repair"
        else:
            remedy = "dispatch repair"
        _note(lines, _reason(check), remedy=remedy)
    else:
        detail = "service unit is not installed" if status == "missing" else "service unit failed safety checks"
        _note(lines, _reason(check), remedy=_installer_or(_hint(check) or "dispatch repair", checks))
    return _row(_glyph_for([status]), "Service", detail), lines


def _plugin_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    plugins = checks.get("plugins", {})
    services = checks.get("plugin_services", {})
    statuses = [str(plugins.get("status", "not_configured")), str(services.get("status", "ready"))]
    lines: list[str] = []
    if "unsafe" in statuses:
        detail = "plugin configuration or services are unsafe"
        _note(lines, _reason(services if services.get("error") else plugins), remedy="dispatch plugin-service status <plugin>")
        return _row(_glyph_for(statuses), "Plugins", detail), lines
    service_items = services.get("services")
    prepared: list[str] = []
    if isinstance(service_items, dict):
        for plugin_id, item in sorted(service_items.items()):
            if isinstance(item, dict) and item.get("status") == "prepared":
                prepared.append(str(plugin_id))
    if plugins.get("status") == "not_configured":
        detail = "no plugins configured"
        _note(lines, _reason(plugins), remedy=_hint(plugins) or "dispatch setup")
        return _row(_glyph_for(statuses), "Plugins", detail), lines
    if prepared:
        detail = f"services staged, not running: {', '.join(prepared)}"
        return _row(accent("●"), "Plugins", detail), lines
    detail = "configuration valid, services healthy"
    return _row(_glyph_for(statuses), "Plugins", detail), lines


def _directories_row(checks: dict[str, Any]) -> tuple[str, list[str]]:
    names = [name for name in _DIRECTORY_ORDER if name in checks]
    entries = [(name, checks[name]) for name in names if isinstance(checks[name], dict)]
    failing = [(name, check) for name, check in entries if check.get("status") != "ready"]
    lines: list[str] = []
    if not failing:
        detail = f"{len(entries)} roots owned & private"
        return _row(success("✓"), "Directories", detail), lines
    glyph = warn("⚠") if all(check.get("status") == "missing" for _, check in failing) else error("✗")
    detail = f"{len(failing)} of {len(entries)} roots unhealthy"
    missing_count = sum(1 for _, check in failing if check.get("status") == "missing")
    other_count = len(failing) - missing_count
    parts = []
    if missing_count:
        parts.append(f"{missing_count} not created")
    if other_count:
        parts.append(f"{other_count} failed safety checks")
    hints = {str(check.get("hint")) for _, check in failing if check.get("hint")}
    remedy = hints.pop() if len(hints) == 1 else "dispatch repair"
    _note(lines, f"directory roots: {', '.join(parts)}", remedy=_installer_or(remedy, checks))
    return _row(glyph, "Directories", detail), lines


_ROW_BUILDERS = {
    "installation": _installation_row,
    "code": _code_row,
    "runtime": _runtime_row,
    "launcher": _launcher_row,
    "service": _service_row,
    "plugins": _plugin_row,
    "directories": _directories_row,
}


def render_doctor(report: dict[str, Any]) -> str:
    """Render an ``inspect_installation`` report as human-readable text."""
    raw_checks = report.get("checks")
    checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
    lines: list[str] = [f"\n  {accent('◆')} {bold('Dispatch Doctor')}", ""]
    rendered_rows: list[str] = []
    for group in _GROUP_ORDER:
        builder = _ROW_BUILDERS[group]
        row, notes = builder(checks)
        rendered_rows.append(row)
        lines.append(row)
        lines.extend(notes)
    status = str(report.get("status", "unsafe"))
    verdict = _VERDICTS.get(status, "Installation needs attention")
    problems = sum(1 for row in rendered_rows if "✗" in row)
    cautions = sum(1 for row in rendered_rows if "⚠" in row)
    counts = []
    if problems:
        counts.append(f"{problems} problem{'s' if problems != 1 else ''}")
    if cautions:
        counts.append(f"{cautions} warning{'s' if cautions != 1 else ''}")
    summary = verdict if not counts else f"{verdict} · {', '.join(counts)}"
    lines.extend(
        [
            "",
            summary_divider(),
            f"  {bold(summary)}",
            dim("  machine detail: dispatch --json doctor"),
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["render_doctor"]
