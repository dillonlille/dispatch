"""Read-only installation health checks."""
from __future__ import annotations

import os

import stat
from pathlib import Path
from typing import Any

from .layout import InstallLayout, InstallerError, read_installation
from .repository import local_checkout_matches_record
from .service import inspect_plugin_services, inspect_user_service
from .setup import load_plugin_config
from .user_command import inspect_user_command


def _directory_check(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"status": "missing", "path": str(path)}
    if path.is_symlink() or not path.is_dir():
        return {"status": "unsafe", "path": str(path)}
    details = path.stat()
    return {
        "status": "ready" if details.st_uid == os.geteuid() and (details.st_mode & 0o077) == 0 else "unsafe",
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


def inspect_installation(layout: InstallLayout) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for name in ("dispatch_home", "clone", "venv", "config", "secrets", "data", "state", "cache", "logs", "run"):
        checks[name] = _directory_check(getattr(layout, name))
    checks["venv"]["python"] = _venv_python_status(layout.venv_python)
    checks["command"] = inspect_user_command(layout)
    checks["service"] = inspect_user_service(layout)
    try:
        record = read_installation(layout)
    except InstallerError as exc:
        checks["installation"] = {"status": "unsafe", "error": str(exc)[:256]}
        record = None
    else:
        checks["installation"] = {
            "status": "ready" if record is not None else "missing",
            "channel": record.get("channel") if record else None,
            "ref": record.get("ref") if record else None,
            "commit": record.get("commit") if record else None,
        }
    checks["clone"]["git"] = _git_status(layout.clone, record)
    plugin_config = layout.config / "plugins.json"
    selected_plugins: list[str] = []
    if plugin_config.exists() or plugin_config.is_symlink():
        try:
            plugin_payload = load_plugin_config(layout)
            selected = plugin_payload.get("selected_plugins", [])
            if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")
            selected_plugins = list(selected)
        except InstallerError as exc:
            checks["plugins"] = {"status": "unsafe", "path": str(plugin_config), "error": str(exc)[:256]}
        else:
            checks["plugins"] = {"status": "ready", "path": str(plugin_config)}
    else:
        checks["plugins"] = {"status": "not_configured", "path": str(plugin_config)}
    checks["plugin_services"] = inspect_plugin_services(layout, selected_plugins)
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
    return {"ok": ready, "status": "ready" if ready else ("unsafe" if unsafe else "incomplete"), "checks": checks}


__all__ = ["inspect_installation"]
