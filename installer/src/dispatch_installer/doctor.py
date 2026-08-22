"""Read-only installation health checks."""
from __future__ import annotations

import os

import shlex
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from .layout import InstallLayout, InstallerError, read_installation
from .repository import local_channel_drift, local_checkout_matches_record
from .service import inspect_plugin_services, inspect_user_service
from .setup import load_plugin_config, selected_long_running_plugins
from .user_command import inspect_user_command

_BROWSER_DIGEST_MARKER = ".dispatch-content-sha256"


def _directory_check(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {
            "status": "missing",
            "path": str(path),
            "reason": "directory has not been created",
            "hint": "dispatch repair",
        }
    if path.is_symlink() or not path.is_dir():
        return {
            "status": "unsafe",
            "path": str(path),
            "reason": "expected a real directory, found a symlink or other file",
            "hint": "dispatch repair",
        }
    details = path.stat()
    if details.st_uid != os.geteuid():
        return {
            "status": "unsafe",
            "path": str(path),
            "reason": "directory is owned by another user",
            "hint": "dispatch repair",
        }
    if details.st_mode & 0o077:
        return {
            "status": "unsafe",
            "path": str(path),
            "reason": f"permissions are too open ({stat.filemode(details.st_mode)})",
            "hint": f"chmod 700 {shlex.quote(str(path))}",
        }
    return {
        "status": "ready",
        "path": str(path),
    }


def _git_status(path: Path, record: dict[str, object] | None) -> str:
    metadata = path / ".git"
    if not metadata.exists() and not metadata.is_symlink():
        return "missing"
    return "ready" if local_checkout_matches_record(path, record) else "unsafe"


def _venv_python_status(path: Path) -> str:
    if not path.exists() and not path.is_symlink():
        return "missing"
    if not path.is_file():
        return "unsafe"
    link_details = path.lstat()
    try:
        resolved = path.resolve(strict=True)
        details = resolved.stat(follow_symlinks=False)
    except OSError:
        return "unsafe"
    if path.is_symlink() and link_details.st_uid != os.geteuid():
        return "unsafe"
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid not in {0, os.geteuid()}
        or details.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        return "unsafe"
    return "ready"


def _browser_digest_check(cache: Path) -> dict[str, object]:
    """Scan managed Chromium generations for missing content-digest markers.

    Advisory only: a pre-marker generation (installed before digest
    verification shipped) is not unsafe, but the next update will require
    staged adoption, so doctor surfaces it as an early heads-up.
    """

    generations: dict[str, int] = {}
    cache_present = False
    try:
        if cache.is_symlink() or not cache.is_dir():
            return {"status": "ready", "generations": {}, "cache_present": False}
        cache_present = True
        for entry in sorted(cache.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith("chromium-"):
                continue
            marker = entry / _BROWSER_DIGEST_MARKER
            if marker.is_file():
                continue
            try:
                file_count = sum(1 for item in entry.rglob("*") if item.is_file())
            except OSError:
                file_count = 0
            generations[entry.name] = min(file_count, 99999)
    except OSError:
        return {"status": "ready", "generations": {}, "cache_present": cache_present}
    return {
        "status": "ready" if not generations else "unverified",
        "generations": generations,
        "cache_present": cache_present,
    }


def inspect_installation(layout: InstallLayout) -> dict[str, Any]:
    _started = monotonic()
    checks: dict[str, Any] = {}
    for name in ("dispatch_home", "clone", "venv", "config", "secrets", "data", "state", "cache", "logs", "run"):
        checks[name] = _directory_check(getattr(layout, name))
    checks["venv"]["python"] = _venv_python_status(layout.venv_python)
    try:
        checks["venv"]["python_version"] = (
            subprocess.run(
                (str(layout.venv_python), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"),
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if checks["venv"]["python"] == "unsafe":
        checks["venv"]["python_reason"] = "interpreter failed ownership or executability checks"
        checks["venv"]["python_hint"] = "dispatch repair"
    elif checks["venv"]["python"] == "missing":
        checks["venv"]["python_reason"] = "no virtual environment interpreter was found"
        checks["venv"]["python_hint"] = "dispatch repair"
    checks["command"] = inspect_user_command(layout)
    checks["service"] = inspect_user_service(layout)
    if checks["service"].get("status") == "incomplete":
        if checks["service"].get("active"):
            checks["service"]["reason"] = "service is running but will not start at login"
            checks["service"]["hint"] = "systemctl --user enable dispatch.service  ·  or: dispatch repair"
        else:
            checks["service"]["reason"] = "service is enabled but not currently running"
            checks["service"]["hint"] = "systemctl --user start dispatch.service  ·  or: dispatch repair"
    try:
        record = read_installation(layout)
    except InstallerError as exc:
        checks["installation"] = {
            "status": "unsafe",
            "error": str(exc)[:256],
            "reason": str(exc)[:256],
            "hint": "dispatch recover",
        }
        record = None
    else:
        checks["installation"] = {
            "status": "ready" if record is not None else "missing",
            "channel": record.get("channel") if record else None,
            "ref": record.get("ref") if record else None,
            "commit": record.get("commit") if record else None,
        }
        if record is None:
            checks["installation"]["reason"] = "no installation record was found"
            checks["installation"]["hint"] = "run the Dispatch installer to complete setup"
    checks["clone"]["git"] = _git_status(layout.clone, record)
    checks["clone"]["drift"] = local_channel_drift(layout.clone, record)
    plugin_config = layout.config / "plugins.json"
    service_plugins: list[str] = []
    if plugin_config.exists() or plugin_config.is_symlink():
        try:
            plugin_payload = load_plugin_config(layout)
            selected = plugin_payload.get("selected_plugins", [])
            if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")
            service_plugins = selected_long_running_plugins(layout)
        except InstallerError as exc:
            checks["plugins"] = {
                "status": "unsafe",
                "path": str(plugin_config),
                "error": str(exc)[:256],
                "reason": str(exc)[:256],
                "hint": "dispatch setup --list  ·  or: dispatch repair",
            }
        else:
            checks["plugins"] = {"status": "ready", "path": str(plugin_config)}
    else:
        checks["plugins"] = {
            "status": "not_configured",
            "path": str(plugin_config),
            "reason": "no plugin configuration exists yet",
            "hint": "dispatch setup",
        }
    checks["plugin_services"] = inspect_plugin_services(layout, service_plugins)
    checks["browser"] = _browser_digest_check(layout.browser_cache)
    advisories: list[dict[str, str]] = []
    browser = checks["browser"]
    if isinstance(browser, dict) and browser.get("status") == "unverified":
        raw_generations = browser.get("generations")
        names = ", ".join(sorted(raw_generations)) if isinstance(raw_generations, dict) else "unknown"
        advisories.append(
            {
                "kind": "browser_digest_unverified",
                "detail": f"managed Chromium generation(s) lack content-digest markers: {names}",
                "hint": "dispatch update adopts them through staging",
            }
        )
    required = ("dispatch_home", "clone", "venv", "config", "secrets", "data", "state", "cache", "logs", "run")
    unsafe = (
        any(checks[name]["status"] == "unsafe" for name in required)
        or checks["clone"]["git"] == "unsafe"
        or checks["venv"]["python"] == "unsafe"
        or checks["plugins"]["status"] == "unsafe"
        or checks["plugin_services"]["status"] == "unsafe"
    )
    ready = (
        not unsafe
        and checks["installation"]["status"] == "ready"
        and checks["clone"]["git"] == "ready"
        and checks["venv"]["python"] == "ready"
        and checks["command"]["status"] == "ready"
        and checks["service"]["status"] == "ready"
    )
    return {
        "ok": ready,
        "status": "ready" if ready else ("unsafe" if unsafe else "incomplete"),
        "checks": checks,
        "advisories": advisories,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_ms": int((monotonic() - _started) * 1000),
    }


__all__ = ["inspect_installation"]
