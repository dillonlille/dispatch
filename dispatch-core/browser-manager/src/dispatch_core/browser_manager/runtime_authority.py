"""Read-only authority for installer-owned Playwright/Chromium runtimes.

Browser Manager never installs, repairs, activates, or removes these files. The
future Dispatch installer owns those mutations and publishes the root-owned
selector and receipts consumed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import stat
from typing import Callable

from .models import BrowserManagerError


PLAYWRIGHT_VERSION = "1.62.0"
CHROMIUM_VERSION = "151.0.7922.34"
PLAYWRIGHT_REVISION = "1234"
BROWSER_FAMILY = "chromium"
SANDBOX_POLICY_ID = "dispatch-chromium-apparmor-v1"

_SCHEMA_VERSION = 1
_GENERATION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_POLICY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SELECTOR_KEYS = frozenset({"schema_version", "generation", "receipt_sha256"})
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
_TREE_KEYS = frozenset({"schema_version", "files"})
_TREE_FILE_KEYS = frozenset({"size", "sha256", "mode"})


@dataclass(frozen=True)
class RuntimePlatform:
    system: str
    distribution: str
    distribution_version: str
    architecture: str


@dataclass(frozen=True)
class BrowserRuntimePolicy:
    """Fixed production policy. Paths are installer-owned, never environment-selected."""

    selector: Path = Path("/etc/dispatch/browser-runtime-active.json")
    runtime_root: Path = Path("/opt/dispatch/browser-runtimes")
    owner_uid: int = 0
    supported_platform: RuntimePlatform = RuntimePlatform(
        system="linux",
        distribution="ubuntu",
        distribution_version="24.04",
        architecture="x86_64",
    )


@dataclass(frozen=True)
class BrowserRuntimeIdentity:
    generation: str
    executable: Path
    executable_sha256: str
    control_executable: Path
    control_executable_sha256: str

    def __post_init__(self) -> None:
        if not _GENERATION.fullmatch(self.generation):
            raise BrowserManagerError("browser_runtime_identity_invalid", "browser runtime generation is invalid")
        if not self.executable.is_absolute():
            raise BrowserManagerError("browser_runtime_identity_invalid", "browser runtime executable must be absolute")
        if not _SHA256.fullmatch(self.executable_sha256):
            raise BrowserManagerError("browser_runtime_identity_invalid", "browser runtime executable digest is invalid")
        if not self.control_executable.is_absolute():
            raise BrowserManagerError("browser_runtime_identity_invalid", "browser control executable must be absolute")
        if not _SHA256.fullmatch(self.control_executable_sha256):
            raise BrowserManagerError("browser_runtime_identity_invalid", "browser control executable digest is invalid")


@dataclass(frozen=True)
class VerifiedBrowserInstallation:
    identity: BrowserRuntimeIdentity
    receipt: Path
    receipt_sha256: str
    tree_manifest: Path
    tree_manifest_sha256: str
    installed_at: str
    installer_release: str
    sandbox_policy_id: str
    playwright_module: Path
    playwright_driver_cli: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_os_release() -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unknown", "unknown"
    for line in lines:
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values.get("ID", "unknown").lower(), values.get("VERSION_ID", "unknown")


def current_platform() -> RuntimePlatform:
    distribution, distribution_version = _read_os_release()
    architecture = platform.machine().lower()
    if architecture in {"amd64", "x64"}:
        architecture = "x86_64"
    return RuntimePlatform(
        system=platform.system().lower(),
        distribution=distribution,
        distribution_version=distribution_version,
        architecture=architecture,
    )


def installed_playwright_module() -> Path:
    """Locate Playwright without importing its executable package code."""

    distribution = importlib.metadata.distribution("playwright")
    return Path(os.path.abspath(distribution.locate_file("playwright/__init__.py")))


def _json_object(path: Path, error_code: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BrowserManagerError(error_code, f"{label} is missing") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserManagerError(error_code, f"{label} is invalid") from exc
    if not isinstance(payload, dict):
        raise BrowserManagerError(error_code, f"{label} must be a JSON object")
    return payload


def _relative_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BrowserManagerError("browser_receipt_invalid", f"{label} is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BrowserManagerError("browser_receipt_invalid", f"{label} must be a safe relative path")
    return path


def _secure_directory(path: Path, boundary: Path, owner_uid: int, label: str) -> None:
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} is outside the approved root") from exc
    current = boundary
    for part in (Path("."), *relative.parts):
        if part != Path("."):
            current = current / part
        try:
            details = current.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BrowserManagerError("browser_runtime_missing", f"{label} is missing") from exc
        if not stat.S_ISDIR(details.st_mode) or current.is_symlink():
            raise BrowserManagerError("browser_runtime_unsafe", f"{label} contains an unsafe directory")
        if details.st_uid != owner_uid or details.st_mode & 0o022:
            raise BrowserManagerError("browser_runtime_unsafe", f"{label} has unsafe ownership or permissions")


def _secure_file(path: Path, boundary: Path, owner_uid: int, label: str) -> os.stat_result:
    _secure_directory(path.parent, boundary, owner_uid, label)
    try:
        details = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise BrowserManagerError("browser_runtime_missing", f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} is not a private regular file")
    if details.st_uid != owner_uid or details.st_mode & 0o022:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} has unsafe ownership or permissions")
    return details


def _string(payload: dict[str, object], key: str, pattern: re.Pattern[str], error_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BrowserManagerError(error_code, f"browser receipt field {key} is invalid")
    return value


def _verified_receipt_file(
    receipt: dict[str, object],
    generation_root: Path,
    owner_uid: int,
    *,
    relative_key: str,
    size_key: str,
    digest_key: str,
    label: str,
    executable: bool = False,
) -> tuple[Path, str]:
    relative = _relative_path(receipt.get(relative_key), relative_key)
    path = generation_root / relative
    details = _secure_file(path, generation_root, owner_uid, label)
    expected_size = receipt.get(size_key)
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size <= 0:
        raise BrowserManagerError("browser_receipt_invalid", f"browser receipt {size_key} is invalid")
    if details.st_size != expected_size:
        raise BrowserManagerError("browser_runtime_mismatch", f"{label} size does not match receipt")
    expected_digest = _string(receipt, digest_key, _SHA256, "browser_receipt_invalid")
    if _sha256(path) != expected_digest:
        raise BrowserManagerError("browser_runtime_mismatch", f"{label} digest does not match receipt")
    if executable and not details.st_mode & stat.S_IXUSR:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} is not executable by its owner")
    return path, expected_digest


class BrowserRuntimeAuthority:
    """Verify the active installer-owned browser generation without mutating it."""

    __slots__ = (
        "__policy",
        "__platform_resolver",
        "__package_version_resolver",
        "__package_module_resolver",
    )

    def __init__(self) -> None:
        self.__policy = BrowserRuntimePolicy()
        self.__platform_resolver: Callable[[], RuntimePlatform] = current_platform
        self.__package_version_resolver: Callable[[str], str] = importlib.metadata.version
        self.__package_module_resolver: Callable[[], Path] = installed_playwright_module

    @classmethod
    def production(cls) -> "BrowserRuntimeAuthority":
        return cls()

    def inspect(self, *, full_tree: bool = True) -> dict[str, object]:
        try:
            installation = self.load(full_tree=full_tree)
        except BrowserManagerError as exc:
            observed_playwright = self._observed_playwright_version()
            selector_errors = {"browser_runtime_selector_missing", "browser_runtime_selector_invalid"}
            receipt_errors = {
                "browser_receipt_missing",
                "browser_receipt_invalid",
                "browser_receipt_mismatch",
                "browser_receipt_incomplete",
            }
            runtime_errors = {
                "browser_runtime_missing",
                "browser_runtime_unsafe",
                "browser_runtime_mismatch",
                "browser_executable_mismatch",
                "browser_tree_invalid",
                "browser_tree_mismatch",
                "playwright_package_mismatch",
            }
            if exc.code in selector_errors:
                selector_state, receipt_state, integrity_state = "unavailable", "not_checked", "not_checked"
            elif exc.code in receipt_errors:
                selector_state, receipt_state, integrity_state = "verified", "failed", "not_checked"
            elif exc.code in runtime_errors:
                selector_state, receipt_state, integrity_state = "verified", "verified", "failed"
            else:
                selector_state, receipt_state, integrity_state = "not_checked", "not_checked", "not_checked"
            return {
                "installed": observed_playwright == PLAYWRIGHT_VERSION,
                "configured": selector_state == "verified",
                "ready": False,
                "operational": False,
                "error_code": exc.code,
                "error_message": str(exc)[:256],
                "playwright_version": observed_playwright,
                "required_playwright_version": PLAYWRIGHT_VERSION,
                "required_chromium_version": CHROMIUM_VERSION,
                "required_chromium_revision": PLAYWRIGHT_REVISION,
                "selector": selector_state,
                "receipt": receipt_state,
                "runtime_integrity": integrity_state,
                "os_dependencies": "not_checked",
                "sandbox": "not_checked",
                "launch_probe": "not_verified",
            }
        return {
            "installed": True,
            "configured": True,
            "ready": True,
            "operational": False,
            "error_code": None,
            "error_message": None,
            "playwright_version": PLAYWRIGHT_VERSION,
            "required_playwright_version": PLAYWRIGHT_VERSION,
            "chromium_version": CHROMIUM_VERSION,
            "chromium_revision": PLAYWRIGHT_REVISION,
            "required_chromium_version": CHROMIUM_VERSION,
            "required_chromium_revision": PLAYWRIGHT_REVISION,
            "generation": installation.identity.generation,
            "selector": "verified",
            "receipt": "verified",
            "runtime_integrity": "verified",
            "os_dependencies": "verified",
            "sandbox": "verified",
            "launch_probe": "passed_at_install",
        }

    def _observed_playwright_version(self) -> str | None:
        try:
            return self.__package_version_resolver("playwright")
        except importlib.metadata.PackageNotFoundError:
            return None
        except Exception:
            return None

    def load(self, *, full_tree: bool = False) -> VerifiedBrowserInstallation:
        observed_platform = self.__platform_resolver()
        if observed_platform != self.__policy.supported_platform:
            raise BrowserManagerError(
                "browser_platform_unsupported",
                "installed browser runtime does not support this platform",
            )
        try:
            observed_playwright = self.__package_version_resolver("playwright")
        except importlib.metadata.PackageNotFoundError as exc:
            raise BrowserManagerError("playwright_missing", "required Playwright package is not installed") from exc
        except Exception as exc:
            raise BrowserManagerError("playwright_invalid", "Playwright package version is unavailable") from exc
        if observed_playwright != PLAYWRIGHT_VERSION:
            raise BrowserManagerError("playwright_version_mismatch", "installed Playwright version is not approved")

        selector_boundary = self.__policy.selector.parent
        try:
            _secure_file(self.__policy.selector, selector_boundary, self.__policy.owner_uid, "browser runtime selector")
        except BrowserManagerError as exc:
            if exc.code == "browser_runtime_missing":
                raise BrowserManagerError("browser_runtime_selector_missing", "browser runtime selector is missing") from exc
            raise BrowserManagerError("browser_runtime_selector_invalid", str(exc)) from exc
        selector = _json_object(
            self.__policy.selector,
            "browser_runtime_selector_invalid",
            "browser runtime selector",
        )
        if frozenset(selector) != _SELECTOR_KEYS or selector.get("schema_version") != _SCHEMA_VERSION:
            raise BrowserManagerError("browser_runtime_selector_invalid", "browser runtime selector schema is invalid")
        generation = _string(selector, "generation", _GENERATION, "browser_runtime_selector_invalid")
        expected_receipt_sha256 = _string(
            selector,
            "receipt_sha256",
            _SHA256,
            "browser_runtime_selector_invalid",
        )

        generation_root = self.__policy.runtime_root / generation
        _secure_directory(generation_root, self.__policy.runtime_root, self.__policy.owner_uid, "browser runtime generation")
        receipt_path = generation_root / "installation-receipt.json"
        try:
            _secure_file(receipt_path, generation_root, self.__policy.owner_uid, "browser installation receipt")
        except BrowserManagerError as exc:
            if exc.code == "browser_runtime_missing":
                raise BrowserManagerError("browser_receipt_missing", "browser installation receipt is missing") from exc
            raise BrowserManagerError("browser_receipt_invalid", str(exc)) from exc
        actual_receipt_sha256 = _sha256(receipt_path)
        if actual_receipt_sha256 != expected_receipt_sha256:
            raise BrowserManagerError("browser_receipt_mismatch", "browser installation receipt digest does not match selector")
        receipt = _json_object(receipt_path, "browser_receipt_invalid", "browser installation receipt")
        if frozenset(receipt) != _RECEIPT_KEYS or receipt.get("schema_version") != _SCHEMA_VERSION:
            raise BrowserManagerError("browser_receipt_invalid", "browser installation receipt schema is invalid")
        receipt_generation = _string(receipt, "generation", _GENERATION, "browser_receipt_invalid")
        if receipt_generation != generation:
            raise BrowserManagerError("browser_receipt_mismatch", "browser receipt generation does not match selector")

        installed_at = receipt.get("installed_at")
        if not isinstance(installed_at, str):
            raise BrowserManagerError("browser_receipt_invalid", "browser receipt installed_at is invalid")
        try:
            installed_timestamp = datetime.fromisoformat(installed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrowserManagerError("browser_receipt_invalid", "browser receipt installed_at is invalid") from exc
        if installed_timestamp.tzinfo is None or installed_timestamp.utcoffset() is None:
            raise BrowserManagerError("browser_receipt_invalid", "browser receipt installed_at must include a timezone")
        installer_release = _string(receipt, "installer_release", _RELEASE, "browser_receipt_invalid")
        if not installer_release.startswith("dispatch-installer-"):
            raise BrowserManagerError("browser_receipt_invalid", "browser receipt installer release is not approved")

        expected_platform = self.__policy.supported_platform
        platform_fields = {
            "platform_system": expected_platform.system,
            "distribution": expected_platform.distribution,
            "distribution_version": expected_platform.distribution_version,
            "architecture": expected_platform.architecture,
        }
        for key, expected in platform_fields.items():
            if receipt.get(key) != expected:
                raise BrowserManagerError("browser_receipt_mismatch", f"browser receipt {key} is not approved")
        expected_versions = {
            "playwright_version": PLAYWRIGHT_VERSION,
            "browser_family": BROWSER_FAMILY,
            "browser_version": CHROMIUM_VERSION,
            "playwright_revision": PLAYWRIGHT_REVISION,
        }
        for key, expected in expected_versions.items():
            if receipt.get(key) != expected:
                raise BrowserManagerError("browser_receipt_mismatch", f"browser receipt {key} is not approved")

        playwright_module, _ = _verified_receipt_file(
            receipt,
            generation_root,
            self.__policy.owner_uid,
            relative_key="playwright_module_relative_path",
            size_key="playwright_module_size",
            digest_key="playwright_module_sha256",
            label="Playwright Python module",
        )
        try:
            observed_playwright_module = self.__package_module_resolver()
        except Exception as exc:
            raise BrowserManagerError("playwright_invalid", "Playwright package location is unavailable") from exc
        if observed_playwright_module != playwright_module:
            raise BrowserManagerError(
                "playwright_package_mismatch",
                "importable Playwright package is not the installer-approved package",
            )
        playwright_driver_executable, playwright_driver_executable_sha256 = _verified_receipt_file(
            receipt,
            generation_root,
            self.__policy.owner_uid,
            relative_key="playwright_driver_executable_relative_path",
            size_key="playwright_driver_executable_size",
            digest_key="playwright_driver_executable_sha256",
            label="Playwright Node driver",
            executable=True,
        )
        playwright_driver_cli, _ = _verified_receipt_file(
            receipt,
            generation_root,
            self.__policy.owner_uid,
            relative_key="playwright_driver_cli_relative_path",
            size_key="playwright_driver_cli_size",
            digest_key="playwright_driver_cli_sha256",
            label="Playwright driver CLI",
        )

        executable_relative = _relative_path(receipt.get("executable_relative_path"), "executable_relative_path")
        executable = generation_root / executable_relative
        executable_details = _secure_file(executable, generation_root, self.__policy.owner_uid, "Chromium executable")
        executable_size = receipt.get("executable_size")
        if not isinstance(executable_size, int) or isinstance(executable_size, bool) or executable_size <= 0:
            raise BrowserManagerError("browser_receipt_invalid", "browser receipt executable_size is invalid")
        if executable_details.st_size != executable_size:
            raise BrowserManagerError("browser_executable_mismatch", "Chromium executable size does not match receipt")
        executable_sha256 = _string(receipt, "executable_sha256", _SHA256, "browser_receipt_invalid")
        if _sha256(executable) != executable_sha256:
            raise BrowserManagerError("browser_executable_mismatch", "Chromium executable digest does not match receipt")
        if not executable_details.st_mode & stat.S_IXUSR:
            raise BrowserManagerError("browser_runtime_unsafe", "Chromium executable is not executable by its owner")

        tree_relative = _relative_path(receipt.get("tree_manifest_relative_path"), "tree_manifest_relative_path")
        tree_manifest = generation_root / tree_relative
        _secure_file(tree_manifest, generation_root, self.__policy.owner_uid, "browser tree manifest")
        tree_manifest_sha256 = _string(receipt, "tree_manifest_sha256", _SHA256, "browser_receipt_invalid")
        if _sha256(tree_manifest) != tree_manifest_sha256:
            raise BrowserManagerError("browser_tree_mismatch", "browser tree manifest digest does not match receipt")

        for key in ("os_dependencies_verified", "sandbox_verified", "launch_probe_passed"):
            if receipt.get(key) is not True:
                raise BrowserManagerError("browser_receipt_incomplete", f"browser receipt {key} is not verified")
        sandbox_policy_id = _string(receipt, "sandbox_policy_id", _POLICY_ID, "browser_receipt_invalid")
        if sandbox_policy_id != SANDBOX_POLICY_ID:
            raise BrowserManagerError("browser_receipt_mismatch", "browser sandbox policy is not approved")

        if full_tree:
            self._verify_tree(generation_root, tree_manifest, receipt_path)

        return VerifiedBrowserInstallation(
            identity=BrowserRuntimeIdentity(
                generation=generation,
                executable=executable,
                executable_sha256=executable_sha256,
                control_executable=playwright_driver_executable,
                control_executable_sha256=playwright_driver_executable_sha256,
            ),
            receipt=receipt_path,
            receipt_sha256=actual_receipt_sha256,
            tree_manifest=tree_manifest,
            tree_manifest_sha256=tree_manifest_sha256,
            installed_at=installed_at,
            installer_release=installer_release,
            sandbox_policy_id=sandbox_policy_id,
            playwright_module=playwright_module,
            playwright_driver_cli=playwright_driver_cli,
        )

    def _verify_tree(self, generation_root: Path, tree_manifest: Path, receipt_path: Path) -> None:
        payload = _json_object(tree_manifest, "browser_tree_invalid", "browser tree manifest")
        if frozenset(payload) != _TREE_KEYS or payload.get("schema_version") != _SCHEMA_VERSION:
            raise BrowserManagerError("browser_tree_invalid", "browser tree manifest schema is invalid")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise BrowserManagerError("browser_tree_invalid", "browser tree manifest files are invalid")
        expected_paths: set[str] = set()
        for key, entry in files.items():
            relative = _relative_path(key, "tree member")
            if not isinstance(entry, dict) or frozenset(entry) != _TREE_FILE_KEYS:
                raise BrowserManagerError("browser_tree_invalid", "browser tree member declaration is invalid")
            size = entry.get("size")
            digest = entry.get("sha256")
            mode = entry.get("mode")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise BrowserManagerError("browser_tree_invalid", "browser tree member size is invalid")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise BrowserManagerError("browser_tree_invalid", "browser tree member digest is invalid")
            if mode not in {"0444", "0555", "0644", "0755"}:
                raise BrowserManagerError("browser_tree_invalid", "browser tree member mode is invalid")
            member = generation_root / relative
            details = _secure_file(member, generation_root, self.__policy.owner_uid, "browser tree member")
            if details.st_size != size or _sha256(member) != digest or stat.S_IMODE(details.st_mode) != int(mode, 8):
                raise BrowserManagerError("browser_tree_mismatch", "browser tree member does not match manifest")
            expected_paths.add(relative.as_posix())

        excluded = {
            receipt_path.relative_to(generation_root).as_posix(),
            tree_manifest.relative_to(generation_root).as_posix(),
        }
        observed_paths: set[str] = set()
        for directory, directories, filenames in os.walk(generation_root, followlinks=False):
            directory_path = Path(directory)
            _secure_directory(directory_path, generation_root, self.__policy.owner_uid, "browser generation tree")
            for name in list(directories):
                candidate = directory_path / name
                if candidate.is_symlink():
                    raise BrowserManagerError("browser_runtime_unsafe", "browser generation contains a symlink")
            for name in filenames:
                candidate = directory_path / name
                relative = candidate.relative_to(generation_root).as_posix()
                if relative in excluded:
                    continue
                _secure_file(candidate, generation_root, self.__policy.owner_uid, "browser generation member")
                observed_paths.add(relative)
        if observed_paths != expected_paths:
            raise BrowserManagerError("browser_tree_mismatch", "browser generation member set does not match manifest")


__all__ = [
    "BROWSER_FAMILY",
    "BrowserRuntimeAuthority",
    "BrowserRuntimeIdentity",
    "BrowserRuntimePolicy",
    "CHROMIUM_VERSION",
    "PLAYWRIGHT_REVISION",
    "PLAYWRIGHT_VERSION",
    "SANDBOX_POLICY_ID",
    "RuntimePlatform",
    "VerifiedBrowserInstallation",
    "current_platform",
]
