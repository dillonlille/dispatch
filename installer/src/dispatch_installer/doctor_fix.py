"""Opt-in remediation for `dispatch doctor --fix`.

Strictly scoped: only *safe* remedies ever execute — starting/enabling the
user service and tightening permissions on user-owned Dispatch roots that
doctor already verified. Everything else (update, repair, recover, setup,
foreign-owned paths) is printed as a manual command and never run here.

Contract:
- A plan is a pure function of the inspection report (deterministic order).
- Interactive runs require an explicit y/N per action; EOF declines.
- ``--yes`` runs every fixable action unprompted; ``--json --fix`` without
  ``--yes`` is plan-only and never mutates.
- Executors re-verify preconditions immediately before acting; a stale
  report never causes a blind write.
"""
from __future__ import annotations

import os
import stat
import subprocess
from typing import Any, Callable

_SERVICE_TIMEOUT_SECONDS = 30

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def build_fix_plan(report: dict[str, Any]) -> list[dict[str, str]]:
    """Derive the ordered list of safe actions from an inspection report.

    Deterministic: service actions first, then permission repairs sorted by
    path. Only statuses that are unsafe-but-owned are actionable; missing or
    foreign-owned things belong to lifecycle commands, not to --fix.
    """
    raw_checks = report.get("checks")
    checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
    plan: list[dict[str, str]] = []

    service = checks.get("service")
    if isinstance(service, dict) and service.get("status") == "incomplete":
        unit = str(service.get("service", "dispatch.service"))
        if service.get("active") is False:
            plan.append(
                {
                    "kind": "start_service",
                    "target": unit,
                    "command": f"systemctl --user start {unit}",
                }
            )
        if service.get("enabled") is False:
            plan.append(
                {
                    "kind": "enable_service",
                    "target": unit,
                    "command": f"systemctl --user enable {unit}",
                }
            )

    for name in (
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
    ):
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") != "unsafe":
            continue
        reason = str(check.get("reason") or "")
        path = str(check.get("path") or "")
        if "too open" in reason and path and "chmod 700" in str(check.get("hint") or ""):
            plan.append(
                {
                    "kind": "repair_permissions",
                    "target": path,
                    "command": f"chmod 700 {shlex_quote(path)}",
                }
            )
    return plan


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def Path_(value: str):
    from pathlib import Path

    return Path(value)


def _safe_chmod_target(path: str) -> bool:
    """Re-verify immediately before chmod: real dir, user-owned, no symlink."""
    try:
        candidate = Path_(path)
        details = candidate.lstat()
    except OSError:
        return False
    return (
        not candidate.is_symlink()
        and stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.geteuid()
    )


def apply_action(
    action: dict[str, str],
    *,
    run: RunCommand | None = None,
) -> dict[str, str]:
    """Execute one planned action; returns a result record, never raises."""
    kind = action.get("kind", "")
    try:
        if kind == "repair_permissions":
            target = action.get("target", "")
            if not _safe_chmod_target(target):
                return {**action, "status": "skipped", "error": "target changed and is no longer a user-owned directory"}
            os.chmod(target, 0o700)
            return {**action, "status": "fixed"}
        if kind == "start_service":
            runner = run or _default_run
            completed = runner(("systemctl", "--user", "start", action["target"]), None)
            if completed.returncode != 0:
                return {**action, "status": "failed", "error": (completed.stderr.strip() or "systemctl failed")[:256]}
            return {**action, "status": "fixed"}
        if kind == "enable_service":
            runner = run or _default_run
            completed = runner(("systemctl", "--user", "enable", action["target"]), None)
            if completed.returncode != 0:
                return {**action, "status": "failed", "error": (completed.stderr.strip() or "systemctl failed")[:256]}
            return {**action, "status": "fixed"}
        return {**action, "status": "skipped", "error": "unknown action kind"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {**action, "status": "failed", "error": str(exc)[:256]}


def _default_run(command: tuple[str, ...], cwd: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=_SERVICE_TIMEOUT_SECONDS,
    )


def summarize(results: list[dict[str, str]]) -> dict[str, int]:
    counts = {"fixed": 0, "failed": 0, "skipped": 0}
    for item in results:
        status = item.get("status", "skipped")
        if status in counts:
            counts[status] += 1
    return counts


__all__ = ["apply_action", "build_fix_plan", "summarize"]
