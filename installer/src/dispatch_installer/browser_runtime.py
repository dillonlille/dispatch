from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from .layout import InstallLayout, InstallerError

_SCHEMA_VERSION = 1
_PLAYWRIGHT_VERSION = "1.62.0"
_BROWSER_FAMILY = "chromium"
_BROWSER_VERSION = "151.0.7922.34"
_PLAYWRIGHT_REVISION = "1234"
_SANDBOX_POLICY_ID = "dispatch-chromium-apparmor-v1"
_PLATFORM = {
    "system": "linux",
    "distribution": "ubuntu",
    "distribution_version": "24.04",
    "architecture": "x86_64",
}
_CANONICAL_SELECTOR = Path("/etc/dispatch/browser-runtime-active.json")
_CANONICAL_GENERATIONS = Path("/opt/dispatch/browser-runtimes")
_GENERATION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RELEASE = re.compile(r"^dispatch-installer-[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_TREE_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
_MAX_TREE_BYTES_EXPANDED = 2 * 1024 * 1024 * 1024
_MAX_MEMBERS = 100_000

_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "generation",
        "installer_release",
        "platform",
        "playwright",
        "browser",
        "sandbox",
        "files",
    }
)
_PLATFORM_KEYS = frozenset({"system", "distribution", "distribution_version", "architecture"})
_PLAYWRIGHT_KEYS = frozenset(
    {
        "version",
        "module_relative_path",
        "driver_executable_relative_path",
        "driver_cli_relative_path",
    }
)
_BROWSER_KEYS = frozenset({"family", "version", "playwright_revision", "executable_relative_path"})
_SANDBOX_KEYS = frozenset({"policy_id"})
_EVIDENCE_KEYS = frozenset(
    {"schema_version", "generation", "verified_at", "platform", "os_dependencies", "sandbox", "launch_probe"}
)
_OS_EVIDENCE_KEYS = frozenset({"verified", "receipt_sha256"})
_SANDBOX_EVIDENCE_KEYS = frozenset({"verified", "policy_id", "receipt_sha256"})
_LAUNCH_EVIDENCE_KEYS = frozenset({"passed", "executable_sha256"})
_MEMBER_KEYS = frozenset({"size", "sha256", "executable"})
_SELECTOR_KEYS = frozenset({"schema_version", "generation", "receipt_sha256"})
_TREE_KEYS = frozenset({"schema_version", "files"})
_TREE_MEMBER_KEYS = frozenset({"size", "sha256", "mode"})
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "generation",
        "installed_at",
        "installer_release",
        "platform_system",
        "distribution",
        "distribution_version",
        "architecture",
        "playwright_version",
        "playwright_module_relative_path",
        "playwright_module_size",
        "playwright_module_sha256",
        "playwright_driver_executable_relative_path",
        "playwright_driver_executable_size",
        "playwright_driver_executable_sha256",
        "playwright_driver_cli_relative_path",
        "playwright_driver_cli_size",
        "playwright_driver_cli_sha256",
        "browser_family",
        "browser_version",
        "playwright_revision",
        "executable_relative_path",
        "executable_size",
        "executable_sha256",
        "tree_manifest_relative_path",
        "tree_manifest_sha256",
        "os_dependencies_verified",
        "sandbox_verified",
        "sandbox_policy_id",
        "launch_probe_passed",
    }
)


@dataclass(frozen=True, slots=True)
class BrowserRuntimeMember:
    size: int
    sha256: str
    executable: bool


@dataclass(frozen=True, slots=True)
class BrowserRuntimeManifest:
    generation: str
    installer_release: str
    playwright_module: str
    playwright_driver_executable: str
    playwright_driver_cli: str
    browser_executable: str
    members: dict[str, BrowserRuntimeMember]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class BrowserInstallationEvidence:
    data: bytes
    sha256: str
    verified_at: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded_json(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _decode_json(data: bytes, *, code: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallerError(code, f"{label} is invalid") from exc
    if not isinstance(payload, dict):
        raise InstallerError(code, f"{label} must be a JSON object")
    return payload


def _relative(value: object, *, code: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise InstallerError(code, f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallerError(code, f"{label} must be a safe canonical relative path")
    if value in {"tree-manifest.json", "installation-receipt.json", "installation-evidence.json"}:
        raise InstallerError(code, f"{label} uses an installer-reserved path")
    return value


def _authority_owner(layout: InstallLayout) -> int:
    canonical_selector = layout.browser_selector == _CANONICAL_SELECTOR
    canonical_generations = layout.browser_generations == _CANONICAL_GENERATIONS
    if canonical_selector != canonical_generations:
        raise InstallerError("browser_authority_layout", "browser authority paths must both be canonical or both be isolated")
    return 0 if canonical_selector else os.geteuid()


def _require_mutation_authority(layout: InstallLayout) -> int:
    owner_uid = _authority_owner(layout)
    if owner_uid == 0 and os.geteuid() != 0:
        raise InstallerError("browser_privilege_required", "canonical browser authority requires a privileged installer")
    return owner_uid


def _validate_absolute_authority_paths(layout: InstallLayout) -> None:
    selector = layout.browser_selector
    generations = layout.browser_generations
    if not selector.is_absolute() or not generations.is_absolute():
        raise InstallerError("browser_authority_layout", "browser authority paths must be absolute")
    if selector.resolve(strict=False) != selector or generations.resolve(strict=False) != generations:
        raise InstallerError("browser_authority_layout", "browser authority paths must be canonical and unaliased")
    if selector == generations or selector in generations.parents or generations in selector.parents:
        raise InstallerError("browser_authority_layout", "browser selector and generation roots must remain separate")


def _validate_directory(path: Path, *, owner_uid: int, mode: int | None = None, code: str) -> os.stat_result:
    try:
        details = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise InstallerError(code, f"required browser authority directory is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISDIR(details.st_mode) or details.st_uid != owner_uid or details.st_mode & 0o022:
        raise InstallerError(code, f"browser authority directory is unsafe: {path}")
    if mode is not None and stat.S_IMODE(details.st_mode) != mode:
        raise InstallerError(code, f"browser authority directory mode differs: {path}")
    return details


def _ensure_directory(path: Path, *, owner_uid: int, mode: int) -> None:
    if path.exists() or path.is_symlink():
        _validate_directory(path, owner_uid=owner_uid, mode=mode, code="browser_authority_unsafe")
        return
    parent = path.parent
    _validate_directory(parent, owner_uid=owner_uid, code="browser_authority_parent_unsafe")
    try:
        path.mkdir(mode=mode)
    except OSError as exc:
        raise InstallerError("browser_authority_create_failed", f"cannot create browser authority directory: {path}") from exc
    _validate_directory(path, owner_uid=owner_uid, mode=mode, code="browser_authority_unsafe")


def _prepare_authority_roots(layout: InstallLayout, owner_uid: int) -> None:
    _validate_absolute_authority_paths(layout)
    _ensure_directory(layout.browser_selector.parent, owner_uid=owner_uid, mode=0o755)
    generations_parent = layout.browser_generations.parent
    if not generations_parent.exists():
        _ensure_directory(generations_parent, owner_uid=owner_uid, mode=0o755)
    else:
        _validate_directory(
            generations_parent,
            owner_uid=owner_uid,
            mode=0o755,
            code="browser_authority_parent_unsafe",
        )
    _ensure_directory(layout.browser_generations, owner_uid=owner_uid, mode=0o755)


@contextmanager
def browser_authority_lock(layout: InstallLayout) -> Iterator[None]:
    owner_uid = _require_mutation_authority(layout)
    _prepare_authority_roots(layout, owner_uid)
    lock_path = layout.browser_selector.with_name("browser-runtime.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != owner_uid
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise InstallerError("browser_lock_unsafe", "browser authority lock file is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise InstallerError("browser_lock_failed", "cannot acquire browser authority lock") from exc
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        os.close(descriptor)


def _read_regular(
    path: Path,
    *,
    owner_uid: int,
    max_bytes: int,
    code: str,
    allowed_modes: set[int] | None = None,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerError(code, f"cannot safely open browser authority file: {path}") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != owner_uid
            or details.st_nlink != 1
            or details.st_mode & 0o022
            or details.st_size > max_bytes
            or (allowed_modes is not None and stat.S_IMODE(details.st_mode) not in allowed_modes)
        ):
            raise InstallerError(code, f"browser authority file is unsafe: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(max_bytes + 1)
        if len(data) != details.st_size or len(data) > max_bytes:
            raise InstallerError(code, f"browser authority file changed or exceeds policy: {path}")
        return data, details
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_regular(
    path: Path,
    *,
    owner_uid: int,
    max_bytes: int,
    code: str,
    allowed_modes: set[int],
) -> tuple[str, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerError(code, f"cannot safely open browser authority file: {path}") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != owner_uid
            or details.st_nlink != 1
            or details.st_mode & 0o022
            or details.st_size > max_bytes
            or stat.S_IMODE(details.st_mode) not in allowed_modes
        ):
            raise InstallerError(code, f"browser authority file is unsafe: {path}")
        digest = hashlib.sha256()
        observed = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            observed += len(block)
            if observed > max_bytes:
                raise InstallerError(code, f"browser authority file exceeds policy: {path}")
            digest.update(block)
        if observed != details.st_size:
            raise InstallerError(code, f"browser authority file changed during verification: {path}")
        return digest.hexdigest(), details
    finally:
        os.close(descriptor)


def _read_input_manifest(path: Path, expected_sha256: str) -> tuple[dict[str, object], str]:
    if not path.is_absolute() or path.resolve(strict=False) != path or not _SHA256.fullmatch(expected_sha256):
        raise InstallerError("browser_manifest_unsafe", "browser runtime manifest path or digest is invalid")
    data, _ = _read_regular(
        path,
        owner_uid=os.geteuid(),
        max_bytes=_MAX_MANIFEST_BYTES,
        code="browser_manifest_unsafe",
    )
    observed = _sha256_bytes(data)
    if observed != expected_sha256:
        raise InstallerError("browser_manifest_digest", "browser runtime manifest digest does not match")
    return _decode_json(data, code="browser_manifest_invalid", label="browser runtime manifest"), observed


def load_browser_runtime_manifest(path: Path, *, expected_sha256: str) -> BrowserRuntimeManifest:
    payload, manifest_sha256 = _read_input_manifest(path, expected_sha256)
    if frozenset(payload) != _MANIFEST_KEYS or payload.get("schema_version") != _SCHEMA_VERSION:
        raise InstallerError("browser_manifest_shape", "browser runtime manifest schema is invalid")
    generation = payload.get("generation")
    installer_release = payload.get("installer_release")
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise InstallerError("browser_manifest_generation", "browser runtime generation is invalid")
    if not isinstance(installer_release, str) or not _RELEASE.fullmatch(installer_release):
        raise InstallerError("browser_manifest_installer", "browser runtime installer release is invalid")

    platform_payload = payload.get("platform")
    playwright = payload.get("playwright")
    browser = payload.get("browser")
    sandbox = payload.get("sandbox")
    files = payload.get("files")
    if not isinstance(platform_payload, dict) or frozenset(platform_payload) != _PLATFORM_KEYS or platform_payload != _PLATFORM:
        raise InstallerError("browser_manifest_platform", "browser runtime platform is not approved")
    if not isinstance(playwright, dict) or frozenset(playwright) != _PLAYWRIGHT_KEYS:
        raise InstallerError("browser_manifest_playwright", "browser runtime Playwright declaration is invalid")
    if playwright.get("version") != _PLAYWRIGHT_VERSION:
        raise InstallerError("browser_manifest_playwright", "browser runtime Playwright version is not approved")
    if not isinstance(browser, dict) or frozenset(browser) != _BROWSER_KEYS:
        raise InstallerError("browser_manifest_browser", "browser runtime browser declaration is invalid")
    if browser != {
        "family": _BROWSER_FAMILY,
        "version": _BROWSER_VERSION,
        "playwright_revision": _PLAYWRIGHT_REVISION,
        "executable_relative_path": browser.get("executable_relative_path"),
    }:
        raise InstallerError("browser_manifest_browser", "browser runtime browser version is not approved")
    if (
        not isinstance(sandbox, dict)
        or frozenset(sandbox) != _SANDBOX_KEYS
        or sandbox.get("policy_id") != _SANDBOX_POLICY_ID
    ):
        raise InstallerError("browser_manifest_sandbox", "browser runtime sandbox policy is not approved")

    if not isinstance(files, dict) or not files or len(files) > _MAX_MEMBERS:
        raise InstallerError("browser_manifest_files", "browser runtime file policy is invalid")

    members: dict[str, BrowserRuntimeMember] = {}
    expanded = 0
    for raw_path, raw_member in files.items():
        relative = _relative(raw_path, code="browser_manifest_member", label="browser runtime member")
        if not isinstance(raw_member, dict) or frozenset(raw_member) != _MEMBER_KEYS:
            raise InstallerError("browser_manifest_member", "browser runtime member declaration is invalid")
        size = raw_member.get("size")
        digest = raw_member.get("sha256")
        executable = raw_member.get("executable")
        if (
            type(size) is not int
            or size < 0
            or size > _MAX_MEMBER_BYTES
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or type(executable) is not bool
        ):
            raise InstallerError("browser_manifest_member", "browser runtime member declaration is invalid")
        expanded += size
        if expanded > _MAX_TREE_BYTES_EXPANDED:
            raise InstallerError("browser_manifest_expanded_size", "browser runtime expanded size exceeds policy")
        members[relative] = BrowserRuntimeMember(size=size, sha256=digest, executable=executable)

    playwright_module = _relative(
        playwright.get("module_relative_path"), code="browser_manifest_playwright", label="Playwright module"
    )
    playwright_driver_executable = _relative(
        playwright.get("driver_executable_relative_path"),
        code="browser_manifest_playwright",
        label="Playwright driver executable",
    )
    playwright_driver_cli = _relative(
        playwright.get("driver_cli_relative_path"), code="browser_manifest_playwright", label="Playwright driver CLI"
    )
    browser_executable = _relative(
        browser.get("executable_relative_path"), code="browser_manifest_browser", label="Chromium executable"
    )
    required = {
        playwright_module: False,
        playwright_driver_executable: True,
        playwright_driver_cli: False,
        browser_executable: True,
    }
    if any(path not in members or members[path].executable is not executable or members[path].size < 1 for path, executable in required.items()):
        raise InstallerError("browser_manifest_required_files", "browser runtime required files are missing or invalid")

    return BrowserRuntimeManifest(
        generation=generation,
        installer_release=installer_release,
        playwright_module=playwright_module,
        playwright_driver_executable=playwright_driver_executable,
        playwright_driver_cli=playwright_driver_cli,
        browser_executable=browser_executable,
        members=members,
        manifest_sha256=manifest_sha256,
    )


def _validate_evidence_payload(
    payload: dict[str, object],
    *,
    generation: str,
    executable_sha256: str,
) -> str:
    if frozenset(payload) != _EVIDENCE_KEYS or payload.get("schema_version") != _SCHEMA_VERSION:
        raise InstallerError("browser_evidence_shape", "browser installation evidence schema is invalid")
    if payload.get("generation") != generation or payload.get("platform") != _PLATFORM:
        raise InstallerError("browser_evidence_mismatch", "browser installation evidence identity differs")
    verified_at = payload.get("verified_at")
    if not isinstance(verified_at, str):
        raise InstallerError("browser_evidence_invalid", "browser installation evidence timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstallerError("browser_evidence_invalid", "browser installation evidence timestamp is invalid") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InstallerError("browser_evidence_invalid", "browser installation evidence timestamp requires a timezone")
    os_evidence = payload.get("os_dependencies")
    sandbox_evidence = payload.get("sandbox")
    launch_evidence = payload.get("launch_probe")
    if (
        not isinstance(os_evidence, dict)
        or frozenset(os_evidence) != _OS_EVIDENCE_KEYS
        or os_evidence.get("verified") is not True
        or not isinstance(os_evidence.get("receipt_sha256"), str)
        or not _SHA256.fullmatch(str(os_evidence["receipt_sha256"]))
    ):
        raise InstallerError("browser_evidence_incomplete", "operating-system dependency evidence is incomplete")
    if (
        not isinstance(sandbox_evidence, dict)
        or frozenset(sandbox_evidence) != _SANDBOX_EVIDENCE_KEYS
        or sandbox_evidence.get("verified") is not True
        or sandbox_evidence.get("policy_id") != _SANDBOX_POLICY_ID
        or not isinstance(sandbox_evidence.get("receipt_sha256"), str)
        or not _SHA256.fullmatch(str(sandbox_evidence["receipt_sha256"]))
    ):
        raise InstallerError("browser_evidence_incomplete", "sandbox evidence is incomplete")
    if (
        not isinstance(launch_evidence, dict)
        or frozenset(launch_evidence) != _LAUNCH_EVIDENCE_KEYS
        or launch_evidence.get("passed") is not True
        or launch_evidence.get("executable_sha256") != executable_sha256
    ):
        raise InstallerError("browser_evidence_incomplete", "browser launch-probe evidence is incomplete")
    return verified_at


def _load_installation_evidence(
    path: Path,
    *,
    expected_sha256: str,
    manifest: BrowserRuntimeManifest,
) -> BrowserInstallationEvidence:
    if not path.is_absolute() or path.resolve(strict=False) != path or not _SHA256.fullmatch(expected_sha256):
        raise InstallerError("browser_evidence_unsafe", "browser installation evidence path or digest is invalid")
    data, _ = _read_regular(
        path,
        owner_uid=os.geteuid(),
        max_bytes=_MAX_RECEIPT_BYTES,
        code="browser_evidence_unsafe",
    )
    observed = _sha256_bytes(data)
    if observed != expected_sha256:
        raise InstallerError("browser_evidence_digest", "browser installation evidence digest does not match")
    payload = _decode_json(data, code="browser_evidence_invalid", label="browser installation evidence")
    verified_at = _validate_evidence_payload(
        payload,
        generation=manifest.generation,
        executable_sha256=manifest.members[manifest.browser_executable].sha256,
    )
    timestamp = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if timestamp > now + timedelta(minutes=5) or now - timestamp > timedelta(hours=1):
        raise InstallerError("browser_evidence_stale", "browser installation evidence is not fresh")
    return BrowserInstallationEvidence(data=data, sha256=observed, verified_at=verified_at)


def _source_tree_paths(source: Path, manifest: BrowserRuntimeManifest) -> None:
    if not source.is_absolute() or source.resolve(strict=False) != source:
        raise InstallerError("browser_source_unsafe", "browser runtime source must be an absolute unaliased directory")
    _validate_directory(source, owner_uid=os.geteuid(), code="browser_source_unsafe")
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for directory, directories, filenames in os.walk(source, followlinks=False):
        directory_path = Path(directory)
        if directory_path != source:
            observed_directories.add(directory_path.relative_to(source).as_posix())
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise InstallerError("browser_source_unsafe", "browser runtime source contains a symlink")
            _validate_directory(candidate, owner_uid=os.geteuid(), code="browser_source_unsafe")
        for name in filenames:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise InstallerError("browser_source_unsafe", "browser runtime source contains a symlink")
            observed_files.add(candidate.relative_to(source).as_posix())
    expected_directories: set[str] = set()
    for relative in manifest.members:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if observed_files != set(manifest.members) or observed_directories != expected_directories:
        raise InstallerError("browser_source_scope", "browser runtime source tree differs from manifest")


def _copy_source_member(source: Path, target: Path, member: BrowserRuntimeMember) -> None:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise InstallerError("browser_source_unsafe", f"cannot safely open browser runtime source member: {source}") from exc
    target_fd = -1
    try:
        details = os.fstat(source_fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_mode & 0o022
            or details.st_size != member.size
        ):
            raise InstallerError("browser_source_unsafe", f"browser runtime source member is unsafe: {source}")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            copied += len(block)
            if copied > member.size:
                raise InstallerError("browser_source_changed", f"browser runtime source member changed: {source}")
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        if copied != member.size or digest.hexdigest() != member.sha256:
            raise InstallerError("browser_source_digest", f"browser runtime source member differs from manifest: {source}")
        os.fchmod(target_fd, 0o555 if member.executable else 0o444)
        os.fsync(target_fd)
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_staged_tree(path: Path) -> None:
    if not path.exists():
        return
    for directory in sorted((candidate for candidate in path.rglob("*") if candidate.is_dir()), reverse=True):
        directory.chmod(0o700)
    path.chmod(0o700)
    shutil.rmtree(path)


def _tree_payload(
    manifest: BrowserRuntimeManifest,
    evidence: BrowserInstallationEvidence,
) -> dict[str, object]:
    files = {
        path: {
            "size": member.size,
            "sha256": member.sha256,
            "mode": "0555" if member.executable else "0444",
        }
        for path, member in sorted(manifest.members.items())
    }
    files["installation-evidence.json"] = {
        "size": len(evidence.data),
        "sha256": evidence.sha256,
        "mode": "0444",
    }
    return {
        "schema_version": _SCHEMA_VERSION,
        "files": dict(sorted(files.items())),
    }


def _receipt_payload(manifest: BrowserRuntimeManifest, *, tree_sha256: str, installed_at: str) -> dict[str, object]:
    module = manifest.members[manifest.playwright_module]
    driver = manifest.members[manifest.playwright_driver_executable]
    cli = manifest.members[manifest.playwright_driver_cli]
    executable = manifest.members[manifest.browser_executable]
    return {
        "schema_version": _SCHEMA_VERSION,
        "generation": manifest.generation,
        "installed_at": installed_at,
        "installer_release": manifest.installer_release,
        "platform_system": _PLATFORM["system"],
        "distribution": _PLATFORM["distribution"],
        "distribution_version": _PLATFORM["distribution_version"],
        "architecture": _PLATFORM["architecture"],
        "playwright_version": _PLAYWRIGHT_VERSION,
        "playwright_module_relative_path": manifest.playwright_module,
        "playwright_module_size": module.size,
        "playwright_module_sha256": module.sha256,
        "playwright_driver_executable_relative_path": manifest.playwright_driver_executable,
        "playwright_driver_executable_size": driver.size,
        "playwright_driver_executable_sha256": driver.sha256,
        "playwright_driver_cli_relative_path": manifest.playwright_driver_cli,
        "playwright_driver_cli_size": cli.size,
        "playwright_driver_cli_sha256": cli.sha256,
        "browser_family": _BROWSER_FAMILY,
        "browser_version": _BROWSER_VERSION,
        "playwright_revision": _PLAYWRIGHT_REVISION,
        "executable_relative_path": manifest.browser_executable,
        "executable_size": executable.size,
        "executable_sha256": executable.sha256,
        "tree_manifest_relative_path": "tree-manifest.json",
        "tree_manifest_sha256": tree_sha256,
        "os_dependencies_verified": True,
        "sandbox_verified": True,
        "sandbox_policy_id": _SANDBOX_POLICY_ID,
        "launch_probe_passed": True,
    }


def _secure_generation_directory(path: Path, root: Path, owner_uid: int) -> None:
    _validate_directory(root, owner_uid=owner_uid, code="browser_generation_root_unsafe")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InstallerError("browser_generation_path", "browser generation escapes the approved root") from exc
    current = root
    for part in relative.parts:
        current /= part
        _validate_directory(current, owner_uid=owner_uid, mode=0o555, code="browser_generation_unsafe")


def _verify_generation(
    layout: InstallLayout,
    generation: str,
) -> tuple[dict[str, object], dict[str, object], str]:
    _validate_absolute_authority_paths(layout)
    owner_uid = _authority_owner(layout)
    if not _GENERATION.fullmatch(generation):
        raise InstallerError("browser_generation_invalid", "browser runtime generation is invalid")
    root = layout.browser_generations
    generation_root = root / generation
    _secure_generation_directory(generation_root, root, owner_uid)
    tree_data, _ = _read_regular(
        generation_root / "tree-manifest.json",
        owner_uid=owner_uid,
        max_bytes=_MAX_TREE_BYTES,
        code="browser_tree_unsafe",
        allowed_modes={0o444},
    )
    receipt_data, _ = _read_regular(
        generation_root / "installation-receipt.json",
        owner_uid=owner_uid,
        max_bytes=_MAX_RECEIPT_BYTES,
        code="browser_receipt_unsafe",
        allowed_modes={0o444},
    )
    tree = _decode_json(tree_data, code="browser_tree_invalid", label="browser tree manifest")
    receipt = _decode_json(receipt_data, code="browser_receipt_invalid", label="browser installation receipt")
    if frozenset(tree) != _TREE_KEYS or tree.get("schema_version") != _SCHEMA_VERSION:
        raise InstallerError("browser_tree_invalid", "browser tree manifest schema is invalid")
    if frozenset(receipt) != _RECEIPT_KEYS or receipt.get("schema_version") != _SCHEMA_VERSION:
        raise InstallerError("browser_receipt_invalid", "browser installation receipt schema is invalid")
    if receipt.get("generation") != generation:
        raise InstallerError("browser_receipt_mismatch", "browser installation receipt generation differs")
    try:
        installed = datetime.fromisoformat(str(receipt.get("installed_at")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstallerError("browser_receipt_invalid", "browser installation timestamp is invalid") from exc
    if installed.tzinfo is None or installed.utcoffset() is None:
        raise InstallerError("browser_receipt_invalid", "browser installation timestamp requires a timezone")
    if not isinstance(receipt.get("installer_release"), str) or not _RELEASE.fullmatch(str(receipt["installer_release"])):
        raise InstallerError("browser_receipt_invalid", "browser installer release is invalid")
    expected_receipt = {
        "platform_system": _PLATFORM["system"],
        "distribution": _PLATFORM["distribution"],
        "distribution_version": _PLATFORM["distribution_version"],
        "architecture": _PLATFORM["architecture"],
        "playwright_version": _PLAYWRIGHT_VERSION,
        "browser_family": _BROWSER_FAMILY,
        "browser_version": _BROWSER_VERSION,
        "playwright_revision": _PLAYWRIGHT_REVISION,
        "tree_manifest_relative_path": "tree-manifest.json",
        "tree_manifest_sha256": _sha256_bytes(tree_data),
        "os_dependencies_verified": True,
        "sandbox_verified": True,
        "sandbox_policy_id": _SANDBOX_POLICY_ID,
        "launch_probe_passed": True,
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise InstallerError("browser_receipt_mismatch", "browser installation receipt differs from policy")
    files = tree.get("files")
    if not isinstance(files, dict) or not files or len(files) > _MAX_MEMBERS:
        raise InstallerError("browser_tree_invalid", "browser tree file declaration is invalid")
    declared: dict[str, dict[str, object]] = {}
    expanded = 0
    for raw_path, entry in files.items():
        relative = (
            "installation-evidence.json"
            if raw_path == "installation-evidence.json"
            else _relative(raw_path, code="browser_tree_invalid", label="browser tree member")
        )
        if not isinstance(entry, dict) or frozenset(entry) != _TREE_MEMBER_KEYS:
            raise InstallerError("browser_tree_invalid", "browser tree member declaration is invalid")
        size = entry.get("size")
        digest = entry.get("sha256")
        mode = entry.get("mode")
        if (
            type(size) is not int
            or size < 0
            or size > _MAX_MEMBER_BYTES
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or mode not in {"0444", "0555"}
        ):
            raise InstallerError("browser_tree_invalid", "browser tree member declaration is invalid")
        expanded += size
        if expanded > _MAX_TREE_BYTES_EXPANDED:
            raise InstallerError("browser_tree_invalid", "browser tree expanded size exceeds policy")
        declared[relative] = entry

    evidence_entry = declared.get("installation-evidence.json")
    if evidence_entry is None or evidence_entry["mode"] != "0444":
        raise InstallerError("browser_evidence_missing", "browser installation evidence is not bound to the generation")
    evidence_data, _ = _read_regular(
        generation_root / "installation-evidence.json",
        owner_uid=owner_uid,
        max_bytes=_MAX_RECEIPT_BYTES,
        code="browser_evidence_unsafe",
        allowed_modes={0o444},
    )
    if len(evidence_data) != evidence_entry["size"] or _sha256_bytes(evidence_data) != evidence_entry["sha256"]:
        raise InstallerError("browser_evidence_mismatch", "browser installation evidence differs from the tree manifest")
    evidence_payload = _decode_json(
        evidence_data,
        code="browser_evidence_invalid",
        label="browser installation evidence",
    )
    _validate_evidence_payload(
        evidence_payload,
        generation=generation,
        executable_sha256=str(receipt.get("executable_sha256")),
    )

    special = {
        "playwright_module_relative_path": ("playwright_module_size", "playwright_module_sha256", False),
        "playwright_driver_executable_relative_path": (
            "playwright_driver_executable_size",
            "playwright_driver_executable_sha256",
            True,
        ),
        "playwright_driver_cli_relative_path": ("playwright_driver_cli_size", "playwright_driver_cli_sha256", False),
        "executable_relative_path": ("executable_size", "executable_sha256", True),
    }
    for path_key, (size_key, digest_key, executable) in special.items():
        relative = _relative(receipt.get(path_key), code="browser_receipt_invalid", label=path_key)
        entry = declared.get(relative)
        if (
            entry is None
            or receipt.get(size_key) != entry["size"]
            or receipt.get(digest_key) != entry["sha256"]
            or entry["mode"] != ("0555" if executable else "0444")
        ):
            raise InstallerError("browser_receipt_mismatch", "browser receipt file binding differs from tree manifest")

    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for directory, directories, filenames in os.walk(generation_root, followlinks=False):
        directory_path = Path(directory)
        if directory_path != generation_root:
            observed_directories.add(directory_path.relative_to(generation_root).as_posix())
        for name in directories:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise InstallerError("browser_generation_unsafe", "browser generation contains a symlink")
            _validate_directory(candidate, owner_uid=owner_uid, mode=0o555, code="browser_generation_unsafe")
        for name in filenames:
            candidate = directory_path / name
            relative = candidate.relative_to(generation_root).as_posix()
            if relative in {"tree-manifest.json", "installation-receipt.json"}:
                continue
            entry = declared.get(relative)
            if entry is None:
                raise InstallerError("browser_tree_scope", "browser generation contains an undeclared member")
            digest, details = _hash_regular(
                candidate,
                owner_uid=owner_uid,
                max_bytes=_MAX_MEMBER_BYTES,
                code="browser_generation_unsafe",
                allowed_modes={int(str(entry["mode"]), 8)},
            )
            expected_mode = int(str(entry["mode"]), 8)
            if stat.S_IMODE(details.st_mode) != expected_mode:
                raise InstallerError("browser_generation_unsafe", "browser executable member mode is invalid")
            if details.st_size != entry["size"] or digest != entry["sha256"]:
                raise InstallerError("browser_tree_mismatch", "browser generation member differs from tree manifest")
            observed_files.add(relative)
    expected_directories: set[str] = set()
    for relative in declared:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if observed_files != set(declared) or observed_directories != expected_directories:
        raise InstallerError("browser_tree_scope", "browser generation member set differs from tree manifest")
    return receipt, tree, _sha256_bytes(receipt_data)


def verify_browser_generation(layout: InstallLayout, generation: str) -> dict[str, str | int]:
    receipt, tree, receipt_sha256 = _verify_generation(layout, generation)
    return {
        "generation": generation,
        "files": len(tree["files"]),
        "receipt_sha256": receipt_sha256,
        "installer_release": str(receipt["installer_release"]),
    }


def _generation_matches_manifest(
    layout: InstallLayout,
    manifest: BrowserRuntimeManifest,
    evidence: BrowserInstallationEvidence,
) -> bool:
    receipt, tree, _ = _verify_generation(layout, manifest.generation)
    expected_tree = _tree_payload(manifest, evidence)
    if tree != expected_tree:
        return False
    expected_receipt = _receipt_payload(
        manifest,
        tree_sha256=_sha256_bytes(_encoded_json(expected_tree)),
        installed_at=str(receipt["installed_at"]),
    )
    return receipt == expected_receipt


def stage_browser_runtime(
    layout: InstallLayout,
    source_root: Path,
    manifest_path: Path,
    evidence_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_evidence_sha256: str,
) -> dict[str, str | int | bool]:
    manifest = load_browser_runtime_manifest(manifest_path, expected_sha256=expected_manifest_sha256)
    evidence = _load_installation_evidence(
        evidence_path,
        expected_sha256=expected_evidence_sha256,
        manifest=manifest,
    )
    _source_tree_paths(source_root, manifest)
    with browser_authority_lock(layout):
        destination = layout.browser_generations / manifest.generation
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not _generation_matches_manifest(layout, manifest, evidence):
                raise InstallerError("browser_generation_collision", "existing browser generation differs from manifest")
            verified = verify_browser_generation(layout, manifest.generation)
            return {**verified, "reused": True}
        stage = Path(tempfile.mkdtemp(prefix=".browser-", dir=layout.browser_generations))
        published = False
        try:
            for relative, member in sorted(manifest.members.items()):
                _copy_source_member(source_root.joinpath(*PurePosixPath(relative).parts), stage.joinpath(*PurePosixPath(relative).parts), member)
            evidence_target = stage / "installation-evidence.json"
            evidence_target.write_bytes(evidence.data)
            evidence_target.chmod(0o444)
            tree_data = _encoded_json(_tree_payload(manifest, evidence))
            tree_path = stage / "tree-manifest.json"
            tree_path.write_bytes(tree_data)
            tree_path.chmod(0o444)
            receipt = _receipt_payload(
                manifest,
                tree_sha256=_sha256_bytes(tree_data),
                installed_at=datetime.now(timezone.utc).isoformat(),
            )
            receipt_path = stage / "installation-receipt.json"
            receipt_path.write_bytes(_encoded_json(receipt))
            receipt_path.chmod(0o444)
            for path in sorted((candidate for candidate in stage.rglob("*") if candidate.is_dir()), reverse=True):
                path.chmod(0o555)
            for path in sorted((candidate for candidate in stage.rglob("*") if candidate.is_file())):
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            stage.chmod(0o555)
            _fsync_directory(stage)
            os.replace(stage, destination)
            published = True
            _fsync_directory(layout.browser_generations)
            if not _generation_matches_manifest(layout, manifest, evidence):
                raise InstallerError("browser_generation_verification", "published browser generation failed verification")
            verified = verify_browser_generation(layout, manifest.generation)
        except Exception:
            if stage.exists():
                _remove_staged_tree(stage)
            if published and destination.exists():
                _remove_staged_tree(destination)
                _fsync_directory(layout.browser_generations)
            raise
    return {**verified, "reused": False}


def _selector_path(layout: InstallLayout, *, previous: bool = False) -> Path:
    return layout.browser_selector.with_name("browser-runtime-previous.json") if previous else layout.browser_selector


def _read_selector(layout: InstallLayout, *, previous: bool = False, missing_ok: bool = False) -> dict[str, object] | None:
    path = _selector_path(layout, previous=previous)
    owner_uid = _authority_owner(layout)
    if missing_ok:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise InstallerError("browser_selector_unsafe", "browser runtime selector cannot be inspected") from exc
    _validate_directory(path.parent, owner_uid=owner_uid, code="browser_authority_unsafe")
    data, _ = _read_regular(
        path,
        owner_uid=owner_uid,
        max_bytes=4096,
        code="browser_selector_unsafe",
        allowed_modes={0o444},
    )
    payload = _decode_json(data, code="browser_selector_invalid", label="browser runtime selector")
    if frozenset(payload) != _SELECTOR_KEYS or payload.get("schema_version") != _SCHEMA_VERSION:
        raise InstallerError("browser_selector_invalid", "browser runtime selector schema is invalid")
    generation = payload.get("generation")
    receipt_sha256 = payload.get("receipt_sha256")
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation) or not isinstance(receipt_sha256, str) or not _SHA256.fullmatch(receipt_sha256):
        raise InstallerError("browser_selector_invalid", "browser runtime selector identity is invalid")
    verified = verify_browser_generation(layout, generation)
    if verified["receipt_sha256"] != receipt_sha256:
        raise InstallerError("browser_selector_mismatch", "browser runtime selector receipt digest differs")
    return payload


def _atomic_authority_json(path: Path, payload: dict[str, object], owner_uid: int) -> None:
    _validate_directory(path.parent, owner_uid=owner_uid, code="browser_authority_unsafe")
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise InstallerError("browser_selector_unsafe", "browser runtime selector is a symlink")
        _read_regular(path, owner_uid=owner_uid, max_bytes=4096, code="browser_selector_unsafe", allowed_modes={0o444})
    data = _encoded_json(payload)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        published = True
        _fsync_directory(path.parent)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if published:
            raise InstallerError(
                "browser_selector_publish_uncertain",
                "browser selector was replaced but durability confirmation failed; inspect before retrying",
            ) from exc
        raise


def _selector_for_generation(layout: InstallLayout, generation: str) -> dict[str, object]:
    verified = verify_browser_generation(layout, generation)
    return {
        "schema_version": _SCHEMA_VERSION,
        "generation": generation,
        "receipt_sha256": verified["receipt_sha256"],
    }


def activate_browser_generation(layout: InstallLayout, generation: str) -> dict[str, str | bool | None]:
    with browser_authority_lock(layout):
        owner_uid = _authority_owner(layout)
        candidate = _selector_for_generation(layout, generation)
        current = _read_selector(layout, missing_ok=True)
        if current == candidate:
            return {"generation": generation, "previous_generation": None, "reused": True}
        previous_generation = str(current["generation"]) if current is not None else None
        if current is not None:
            _atomic_authority_json(_selector_path(layout, previous=True), current, owner_uid)
        elif _selector_path(layout, previous=True).exists() or _selector_path(layout, previous=True).is_symlink():
            raise InstallerError("browser_previous_selector_unexpected", "previous browser selector exists before first activation")
        _atomic_authority_json(layout.browser_selector, candidate, owner_uid)
        verified = _read_selector(layout)
        if verified != candidate:
            raise InstallerError("browser_activation_mismatch", "active browser selector does not match candidate")
        return {"generation": generation, "previous_generation": previous_generation, "reused": False}


def rollback_browser_generation(layout: InstallLayout) -> dict[str, str]:
    with browser_authority_lock(layout):
        owner_uid = _authority_owner(layout)
        current = _read_selector(layout)
        previous = _read_selector(layout, previous=True)
        assert current is not None and previous is not None
        if current == previous:
            raise InstallerError("browser_rollback_invalid", "active and previous browser selectors are identical")
        _atomic_authority_json(layout.browser_selector, previous, owner_uid)
        if _read_selector(layout) != previous:
            raise InstallerError("browser_rollback_mismatch", "rolled-back browser selector does not match previous generation")
        try:
            _atomic_authority_json(_selector_path(layout, previous=True), current, owner_uid)
        except Exception as exc:
            raise InstallerError(
                "browser_rollback_bookkeeping_uncertain",
                "browser rollback activated but previous-selector bookkeeping failed",
            ) from exc
        return {
            "generation": str(previous["generation"]),
            "previous_generation": str(current["generation"]),
        }


def inspect_browser_runtime(layout: InstallLayout) -> dict[str, str | int | None]:
    try:
        selector = _read_selector(layout, missing_ok=True)
        if selector is None:
            return {
                "status": "missing",
                "selector": str(layout.browser_selector),
                "generation_root": str(layout.browser_generations),
                "generation": None,
            }
        verified = verify_browser_generation(layout, str(selector["generation"]))
        return {
            "status": "verified",
            "selector": str(layout.browser_selector),
            "generation_root": str(layout.browser_generations),
            **verified,
        }
    except InstallerError as exc:
        return {
            "status": "unsafe",
            "selector": str(layout.browser_selector),
            "generation_root": str(layout.browser_generations),
            "generation": None,
            "error_code": exc.code,
            "error": str(exc)[:512],
        }


__all__ = [
    "BrowserRuntimeManifest",
    "activate_browser_generation",
    "inspect_browser_runtime",
    "load_browser_runtime_manifest",
    "rollback_browser_generation",
    "stage_browser_runtime",
    "verify_browser_generation",
]
