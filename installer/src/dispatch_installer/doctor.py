from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .browser_runtime import inspect_browser_runtime
from .core_release import verify_core_release
from .layout import InstallLayout, InstallerError


_PRODUCTION_INSTALL_READY = False
_BROWSER_LAUNCH_COMPOSITION_READY = False
_MAX_SELECTOR_BYTES = 4096


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _read_core_selector(path: Path) -> bytes | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallerError("selector_unsafe", "active Core selector cannot be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > _MAX_SELECTOR_BYTES
        ):
            raise InstallerError("selector_unsafe", "active Core selector is unsafe")
        data = bytearray()
        while len(data) <= _MAX_SELECTOR_BYTES:
            block = os.read(descriptor, min(4096, _MAX_SELECTOR_BYTES + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) != details.st_size or len(data) > _MAX_SELECTOR_BYTES:
            raise InstallerError("selector_unsafe", "active Core selector changed or exceeds policy")
        return bytes(data)
    except OSError as exc:
        raise InstallerError("selector_unsafe", "active Core selector cannot be read safely") from exc
    finally:
        os.close(descriptor)


def _check_private_directory(path: Path) -> dict[str, str | bool | None]:
    if not path.exists():
        return {"status": "missing", "path": str(path), "mode": None}
    if path.is_symlink() or not path.is_dir():
        return {"status": "unsafe", "path": str(path), "mode": None}
    mode = stat.S_IMODE(path.stat().st_mode)
    return {"status": "ready" if mode == 0o700 else "unsafe", "path": str(path), "mode": f"{mode:04o}"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_installation(layout: InstallLayout) -> dict:
    checks: dict[str, dict] = {}
    checks["dispatch_home"] = _check_private_directory(layout.dispatch_home)
    for name in ("releases", "bin", "config", "data", "state", "cache", "staging"):
        checks[name] = _check_private_directory(getattr(layout, name))

    core: dict[str, str | int | bool | None] = {
        "status": "missing",
        "selector": str(layout.active_release_selector),
        "release_id": None,
    }
    selector = layout.active_release_selector
    try:
        selector_data = _read_core_selector(selector)
        if selector_data is not None:
            payload = json.loads(selector_data.decode("utf-8"), object_pairs_hook=_object_pairs)
            if not isinstance(payload, dict):
                raise InstallerError("selector_shape", "active Core selector shape is invalid")
            if set(payload) != {"schema_version", "release_id", "tree_manifest_sha256", "release_receipt_sha256"}:
                raise InstallerError("selector_shape", "active Core selector shape is invalid")
            if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
                raise InstallerError("selector_version", "active Core selector version is unsupported")
            release_id = payload["release_id"]
            if not isinstance(release_id, str) or not re.fullmatch(
                r"dispatch-core-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{16}",
                release_id,
            ):
                raise InstallerError("selector_release_id", "active Core release identity is invalid")
            release = layout.releases / release_id
            try:
                release.resolve(strict=False).relative_to(layout.releases.resolve(strict=False))
            except ValueError as exc:
                raise InstallerError("selector_release_path", "active Core release is outside the release root") from exc
            verified = verify_core_release(release)
            if _sha256(release / "tree-manifest.json") != payload["tree_manifest_sha256"]:
                raise InstallerError("selector_tree_digest", "active Core tree manifest differs")
            if _sha256(release / "release-receipt.json") != payload["release_receipt_sha256"]:
                raise InstallerError("selector_receipt_digest", "active Core release receipt differs")
            core = {"status": "ready", "selector": str(selector), **verified}
    except (InstallerError, OSError, ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        core = {"status": "unsafe", "selector": str(selector), "release_id": None, "error": str(exc)[:512]}
    checks["core"] = core

    checks["browser_authority"] = inspect_browser_runtime(layout)
    checks["production_release"] = {
        "status": "ready" if _PRODUCTION_INSTALL_READY else "blocked",
        "ready": _PRODUCTION_INSTALL_READY,
        "reason": None if _PRODUCTION_INSTALL_READY else "production release manifest is intentionally not ready",
    }
    checks["browser_launch_composition"] = {
        "status": "ready" if _BROWSER_LAUNCH_COMPOSITION_READY else "blocked",
        "ready": _BROWSER_LAUNCH_COMPOSITION_READY,
        "reason": None
        if _BROWSER_LAUNCH_COMPOSITION_READY
        else "selected-generation Playwright bootstrap is not implemented",
    }
    unsafe = any(check.get("status") == "unsafe" for check in checks.values())
    ready = (
        not unsafe
        and _PRODUCTION_INSTALL_READY
        and _BROWSER_LAUNCH_COMPOSITION_READY
        and checks["core"]["status"] == "ready"
        and checks["browser_authority"]["status"] == "verified"
    )
    return {"ok": ready, "status": "ready" if ready else ("unsafe" if unsafe else "incomplete"), "checks": checks}
