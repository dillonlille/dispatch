"""Harness detection, installation, and selection recording.

Implements the authority model in ``docs/harness-integration.md``: Dispatch
may detect, digest-verified-install, and record an explicitly selected agent
harness. Dispatch never owns the harness and never handles its credentials.

All subprocess calls are bounded; all network fetches verify a SHA-256
digest recorded in the closed catalog before execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .layout import InstallerError

_HERMES_INSTALLER_URL = "https://hermes-agent.nousresearch.com/install.sh"
_HERMES_MINIMUM_VERSION = (0, 20, 0)
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_READ_CHUNK = 65536
_MAX_INSTALLER_BYTES = 4 * 1024 * 1024
_PROBE_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """One closed-catalog harness entry."""

    id: str
    display_name: str
    description: str
    installer_url: str
    installer_digest: str
    installer_flags: tuple[str, ...]
    home_env: str
    default_home: str
    launcher: str
    minimum_version: tuple[int, int, int]


HERMES_SPEC = HarnessSpec(
    id="hermes",
    display_name="Hermes Agent",
    description="Profiles, skills, and messaging gateways out of the box.",
    installer_url=_HERMES_INSTALLER_URL,
    # Digest of the pinned official installer; updated only through a
    # reviewed Dispatch release when the upstream bootstrap changes.
    installer_digest="0582d9b1562efcb6e0ac62f4451021667830b830a72ce7d91eaea9fee8b6c09b",
    installer_flags=("--skip-setup", "--no-skills"),
    home_env="HERMES_HOME",
    default_home=".hermes",
    launcher="hermes",
    minimum_version=_HERMES_MINIMUM_VERSION,
)

HARNESS_CATALOG: dict[str, HarnessSpec] = {"hermes": HERMES_SPEC}


@dataclass(frozen=True, slots=True)
class DetectionResult:
    status: str  # "ready" | "absent" | "unhealthy"
    version: str = ""
    home: str = ""
    detail: str = ""


def _run_bounded(
    command: tuple[str, ...],
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def harness_home(spec: HarnessSpec) -> Path:
    configured = os.environ.get(spec.home_env)
    base = Path(configured) if configured else Path.home() / spec.default_home
    return base.expanduser()


def parse_version(raw: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(raw or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def detect_harness(spec: HarnessSpec) -> DetectionResult:
    """Bounded detection: home layout, launcher resolution, version probe."""
    home = harness_home(spec)
    if not home.is_dir() or home.is_symlink():
        return DetectionResult("absent", detail=f"{spec.home_env} layout missing at {home}")
    launcher = shutil.which(spec.launcher)
    if launcher is None:
        candidate = home / "hermes-agent" / "venv" / "bin" / spec.launcher
        if candidate.is_file() and os.access(candidate, os.X_OK):
            launcher = str(candidate)
        else:
            return DetectionResult("absent", detail="launcher not on PATH")
    try:
        completed = _run_bounded((launcher, "--version"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DetectionResult("unhealthy", detail=f"version probe failed: {exc}")
    if completed.returncode != 0:
        return DetectionResult("unhealthy", detail="launcher exited non-zero")
    version = (completed.stdout or completed.stderr).strip().splitlines()
    version_text = version[0] if version else ""
    parsed = parse_version(version_text)
    if parsed is None:
        return DetectionResult("unhealthy", detail=f"unparseable version output: {version_text[:64]}")
    if parsed < spec.minimum_version:
        return DetectionResult(
            "unhealthy",
            version=version_text,
            home=str(home),
            detail=(
                f"version {version_text} is below the supported floor "
                f"{'.'.join(str(part) for part in spec.minimum_version)}"
            ),
        )
    return DetectionResult("ready", version=version_text, home=str(home))


def _fetch_installer(spec: HarnessSpec, destination: Path) -> None:
    request = urllib.request.Request(
        spec.installer_url,
        headers={"User-Agent": "dispatch-installer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(_MAX_INSTALLER_BYTES + 1)
    except OSError as exc:
        raise InstallerError("harness_download_failed", f"could not download the {spec.display_name} installer") from exc
    if len(payload) > _MAX_INSTALLER_BYTES:
        raise InstallerError("harness_download_invalid", "harness installer exceeds the bounded size")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != spec.installer_digest:
        raise InstallerError(
            "harness_digest_mismatch",
            "harness installer digest does not match the reviewed catalog entry",
        )
    destination.write_bytes(payload)
    destination.chmod(0o700)


def install_harness(spec: HarnessSpec, *, allow_install: bool = False) -> DetectionResult:
    """Install an absent harness from the pinned, digest-verified installer.

    Refuses unless ``allow_install`` is true: headless runs must opt in
    explicitly because this executes a third-party bootstrap.
    """
    current = detect_harness(spec)
    if current.status == "ready":
        return current
    if current.status == "unhealthy":
        raise InstallerError(
            "harness_unhealthy",
            f"{spec.display_name} is present but unhealthy: {current.detail}; resolve it before installing",
        )
    if not allow_install:
        raise InstallerError(
            "harness_install_unauthorized",
            f"{spec.display_name} is not installed; pass --install-harness to authorize installation",
        )
    staging = Path(os.environ.get("TMPDIR", "/tmp")) / f"dispatch-harness-{os.getpid()}.sh"
    try:
        _fetch_installer(spec, staging)
        details = staging.stat(follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            raise InstallerError("harness_staging_unsafe", "staged installer is not a private regular file")
        command = ("/bin/bash", str(staging), *spec.installer_flags)
        try:
            completed = _run_bounded(command, timeout=1800.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallerError("harness_install_failed", f"{spec.display_name} installation failed") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:512]
            raise InstallerError("harness_install_failed", f"{spec.display_name} installation failed: {detail}")
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
    final = detect_harness(spec)
    if final.status != "ready":
        raise InstallerError(
            "harness_install_failed",
            f"{spec.display_name} did not reach a ready state after installation: {final.detail}",
        )
    return final


def load_selection(config_root: Path) -> str | None:
    path = config_root / "harness.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InstallerError("harness_record_invalid", "harness selection record is unreadable") from exc
    selected = payload.get("selected")
    if selected is None:
        return None
    if not isinstance(selected, str) or selected not in HARNESS_CATALOG:
        raise InstallerError("harness_record_invalid", "harness selection record names an unknown harness")
    return selected


def write_selection(config_root: Path, spec: HarnessSpec, detection: DetectionResult) -> Path:
    path = config_root / "harness.json"
    if config_root.is_dir():
        details = config_root.stat(follow_symlinks=False)
        if details.st_uid != os.geteuid() or details.st_mode & 0o077:
            raise InstallerError("harness_record_target_unsafe", "configuration root is not a private user-owned directory")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "selected": spec.id,
        "status": detection.status,
        "version": detection.version,
        "home": detection.home,
        "contains_secrets": False,
    }
    temporary = path.with_suffix(".json.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


__all__ = [
    "DetectionResult",
    "HARNESS_CATALOG",
    "HarnessSpec",
    "HERMES_SPEC",
    "detect_harness",
    "harness_home",
    "install_harness",
    "load_selection",
    "parse_version",
    "write_selection",
]
