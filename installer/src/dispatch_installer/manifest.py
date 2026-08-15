from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .download import validate_core_release_asset_url
from .layout import InstallerError


@dataclass(frozen=True, slots=True)
class InstallationManifest:
    ready: bool
    core_version: str
    core_artifact_url: str | None
    core_artifact_size: int | None
    core_artifact_sha256: str | None
    browser_ready: bool
    setup_implemented: bool
    setup_command: str
    uninstall_user_scope_implemented: bool
    uninstall_administrative_command: str
    uninstall_future_user_command: str
    uninstall_default_mode: str
    uninstall_purge_requires_confirmation: bool
    uninstall_privileged_browser_removal_implemented: bool


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstallerError("manifest_json_duplicate", "installation manifest contains duplicate JSON keys")
        value[key] = item
    return value


def load_manifest(path: Path, *, expected_sha256: str) -> InstallationManifest:
    if path.is_symlink() or not path.is_file():
        raise InstallerError("manifest_unsafe", "installation manifest must be a regular non-symlink file")
    if path.stat().st_size > 1024 * 1024:
        raise InstallerError("manifest_size", "installation manifest exceeds policy")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise InstallerError("manifest_digest_invalid", "expected installation manifest SHA-256 is invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise InstallerError("manifest_digest_mismatch", "installation manifest SHA-256 mismatch")
    try:
        payload = json.loads(data, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("manifest_json_invalid", "installation manifest JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise InstallerError("manifest_shape", "installation manifest shape is invalid")
    if set(payload) != {"schema_version", "ready", "core", "browser_runtime", "post_install", "uninstall"}:
        raise InstallerError("manifest_shape", "installation manifest shape is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or type(payload["ready"]) is not bool:
        raise InstallerError("manifest_version", "installation manifest version is unsupported")
    if payload["ready"]:
        raise InstallerError(
            "manifest_ready_unsupported",
            "schema version 1 cannot authorize production installation",
        )
    core = payload["core"]
    browser = payload["browser_runtime"]
    post_install = payload["post_install"]
    uninstall = payload["uninstall"]
    if not isinstance(core, dict) or set(core) != {"name", "version", "artifact"} or core["name"] != "dispatch-core":
        raise InstallerError("manifest_core", "installation manifest Core declaration is invalid")
    if not isinstance(core["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", core["version"]):
        raise InstallerError("manifest_core_version", "installation manifest Core version is invalid")
    artifact = core["artifact"]
    if not isinstance(artifact, dict) or set(artifact) != {"url", "size", "sha256"}:
        raise InstallerError("manifest_core_artifact", "installation manifest Core artifact is invalid")
    if not isinstance(browser, dict) or set(browser) != {"ready", "selector", "generation_root"}:
        raise InstallerError("manifest_browser", "installation manifest browser declaration is invalid")
    if browser["selector"] != "/etc/dispatch/browser-runtime-active.json" or browser["generation_root"] != "/opt/dispatch/browser-runtimes":
        raise InstallerError("manifest_browser_paths", "installation manifest browser authority paths differ")
    if type(browser["ready"]) is not bool or browser["ready"] is not False:
        raise InstallerError("manifest_browser_ready", "installation manifest browser readiness is invalid")
    if not isinstance(post_install, dict) or post_install != {
        "setup_implemented": False,
        "setup_command": "dispatch setup",
        "choices": ["start_setup", "skip_for_now"],
    }:
        raise InstallerError("manifest_post_install", "installation manifest post-install declaration is invalid")
    expected_uninstall = {
        "user_scope_implemented": True,
        "administrative_command": "dispatch-installer uninstall",
        "future_user_command": "dispatch uninstall",
        "default_mode": "keep-data",
        "purge_requires_confirmation": True,
        "privileged_browser_removal_implemented": False,
    }
    if not isinstance(uninstall, dict) or uninstall != expected_uninstall:
        raise InstallerError("manifest_uninstall", "installation manifest uninstall declaration is invalid")

    url = artifact["url"]
    size = artifact["size"]
    digest = artifact["sha256"]
    artifact_complete = (
        isinstance(url, str)
        and url.startswith("https://")
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )
    if artifact_complete:
        validate_core_release_asset_url(url, version=core["version"])
    if payload["ready"] and (not artifact_complete or not browser["ready"]):
        raise InstallerError("manifest_false_ready", "installation manifest is marked ready without complete artifacts")
    if not payload["ready"] and any(value is not None for value in (url, size, digest)):
        raise InstallerError("manifest_partial_artifact", "incomplete manifest must not publish partial artifact authority")
    return InstallationManifest(
        ready=payload["ready"],
        core_version=core["version"],
        core_artifact_url=url,
        core_artifact_size=size,
        core_artifact_sha256=digest,
        browser_ready=browser["ready"],
        setup_implemented=post_install["setup_implemented"],
        setup_command=post_install["setup_command"],
        uninstall_user_scope_implemented=uninstall["user_scope_implemented"],
        uninstall_administrative_command=uninstall["administrative_command"],
        uninstall_future_user_command=uninstall["future_user_command"],
        uninstall_default_mode=uninstall["default_mode"],
        uninstall_purge_requires_confirmation=uninstall["purge_requires_confirmation"],
        uninstall_privileged_browser_removal_implemented=uninstall["privileged_browser_removal_implemented"],
    )
