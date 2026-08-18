"""Install-time provisioning for the Dispatch-managed Chromium generation.

The installer calls this module from the exact staged checkout. Runtime Browser
Manager code remains read-only and consumes only the verified active result.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from typing import Any

try:
    from .versioning import BrowserVersionError, BrowserVersionIdentity, target_browser_version
except ImportError:  # supports exact file loading by the source installer
    _versioning_path = Path(__file__).with_name("versioning.py")
    try:
        _versioning_details = _versioning_path.stat(follow_symlinks=False)
    except OSError as exc:  # pragma: no cover - loader boundary
        raise ImportError("Browser Manager version policy is missing") from exc
    if (
        _versioning_path.is_symlink()
        or not stat.S_ISREG(_versioning_details.st_mode)
        or _versioning_details.st_uid != os.geteuid()
        or _versioning_details.st_nlink != 1
    ):
        raise ImportError("Browser Manager version policy is unsafe")
    _versioning_spec = importlib.util.spec_from_file_location("_dispatch_browser_versioning", _versioning_path)
    if _versioning_spec is None or _versioning_spec.loader is None:  # pragma: no cover - import machinery boundary
        raise
    _versioning = importlib.util.module_from_spec(_versioning_spec)
    sys.modules[_versioning_spec.name] = _versioning
    _versioning_spec.loader.exec_module(_versioning)
    BrowserVersionError = _versioning.BrowserVersionError
    BrowserVersionIdentity = _versioning.BrowserVersionIdentity
    target_browser_version = _versioning.target_browser_version


RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


class BrowserProvisioningError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrowserProvisioningResult:
    status: str
    version: Any
    active_cache: Path
    staged_cache: Path | None
    missing_libraries: tuple[str, ...]

    @property
    def replacement_required(self) -> bool:
        return self.staged_cache is not None

    def installation_record(self, cache: Path) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "active",
            **self.version.safe_data(),
            "cache": str(cache),
            "contains_secrets": False,
        }

    def safe_data(self) -> dict[str, object]:
        return {
            "status": self.status,
            **self.version.safe_data(),
            "cache": str(self.active_cache),
            "replacement_required": self.replacement_required,
            "host_libraries": "ready" if not self.missing_libraries else "missing",
        }


def _cache_root(path: Path, *, create: bool = False) -> Path:
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BrowserProvisioningError("browser_cache_unsafe", "browser cache cannot use symlink ancestors")
        if not current.exists():
            if not create:
                continue
            current.mkdir(mode=0o700)
            current.chmod(0o700)
            continue
        details = current.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid not in {0, os.geteuid()}:
            raise BrowserProvisioningError("browser_cache_unsafe", "browser cache ancestry is unsafe")
        writable = details.st_mode & 0o022
        trusted_sticky_root = details.st_uid == 0 and bool(details.st_mode & stat.S_ISVTX)
        if writable and not trusted_sticky_root:
            raise BrowserProvisioningError("browser_cache_unsafe", "browser cache ancestry is group/world writable")
    if path.is_symlink() or not path.is_dir():
        raise BrowserProvisioningError("browser_cache_missing", "managed browser cache is missing")
    details = path.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise BrowserProvisioningError("browser_cache_unsafe", "managed browser cache is not private and user-owned")
    return path


def _runtime_file(path: Path, boundary: Path) -> Path:
    try:
        unresolved = Path(os.path.abspath(path))
        relative = unresolved.relative_to(boundary)
        current = boundary
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise BrowserProvisioningError("browser_runtime_unsafe", "browser executable cannot contain symlinks")
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(boundary)
        details = resolved.stat(follow_symlinks=False)
    except BrowserProvisioningError:
        raise
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise BrowserProvisioningError("browser_runtime_missing", "managed Chromium executable is missing") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or details.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise BrowserProvisioningError("browser_runtime_unsafe", "managed Chromium executable is unsafe")
    return resolved


def inspect_managed_cache(cache: Path, version: Any) -> Path:
    root = _cache_root(cache)
    present = [candidate for candidate in version.executable_candidates(root) if candidate.exists() or candidate.is_symlink()]
    if len(present) != 1:
        raise BrowserProvisioningError("browser_runtime_missing", "managed Chromium revision is incomplete")
    return _runtime_file(present[0], root)


def browser_install_command(python: Path, cache: Path) -> tuple[str, ...]:
    timeout = _approved_system_tool(Path("/usr/bin/timeout"), "timeout")
    return (
        timeout,
        "--signal=TERM",
        "--kill-after=10s",
        "600s",
        "env",
        "-i",
        f"HOME={cache.parent}",
        "PATH=/usr/bin:/bin",
        f"PLAYWRIGHT_BROWSERS_PATH={cache}",
        str(python),
        "-m",
        "playwright",
        "install",
        "chromium",
    )


def system_dependency_install_command(python: Path) -> tuple[str, ...]:
    timeout = _approved_system_tool(Path("/usr/bin/timeout"), "timeout")
    return (
        timeout,
        "--signal=TERM",
        "--kill-after=10s",
        "600s",
        "env",
        "-i",
        f"HOME={python.parent.parent.parent}",
        "PATH=/usr/bin:/bin",
        str(python),
        "-m",
        "playwright",
        "install-deps",
        "chromium",
    )


def _approved_system_tool(path: Path, label: str) -> str:
    try:
        lexical = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path("/usr"))
        details = resolved.stat(follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise BrowserProvisioningError("browser_host_tool_missing", f"{label} is unavailable") from exc
    if lexical.st_uid != 0 or (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise BrowserProvisioningError("browser_host_tool_unsafe", f"{label} is unsafe")
    return str(resolved)


def _missing_libraries(executable: Path, *, run: RunCommand) -> tuple[str, ...]:
    timeout = _approved_system_tool(Path("/usr/bin/timeout"), "timeout")
    ldd = _approved_system_tool(Path("/usr/bin/ldd"), "ldd")
    completed = run((timeout, "--signal=TERM", "--kill-after=2s", "15s", ldd, str(executable)), None)
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    missing = sorted(
        {
            line.strip().split()[0]
            for line in output.splitlines()
            if "=> not found" in line and line.strip().split()
        }
    )
    if completed.returncode != 0 and not missing:
        raise BrowserProvisioningError("browser_dependency_scan_failed", "Chromium shared-library inspection failed")
    return tuple(item[:128] for item in missing[:128])


def browser_smoke_command(
    python: Path,
    cache: Path,
    executable: Path,
    *,
    home: Path | None = None,
) -> tuple[str, ...]:
    program = """
import sys
from playwright.sync_api import sync_playwright

playwright = sync_playwright().start()
browser = None
try:
    browser = playwright.chromium.launch(
        executable_path=sys.argv[2],
        headless=True,
        chromium_sandbox=True,
    )
    page = browser.new_page()
    page.goto("about:blank")
finally:
    if browser is not None:
        browser.close()
    playwright.stop()
"""
    smoke_home = home or cache.parent
    timeout = _approved_system_tool(Path("/usr/bin/timeout"), "timeout")
    return (
        timeout,
        "--signal=TERM",
        "--kill-after=5s",
        "30s",
        "env",
        "-i",
        f"HOME={smoke_home}",
        "PATH=/usr/bin:/bin",
        f"PLAYWRIGHT_BROWSERS_PATH={cache}",
        str(python),
        "-I",
        "-B",
        "-c",
        program,
        str(cache),
        str(executable),
    )


def _verify_host(
    *,
    python: Path,
    cache: Path,
    executable: Path,
    run: RunCommand,
    install_system_dependencies: bool,
) -> tuple[str, ...]:
    missing = _missing_libraries(executable, run=run)
    if missing and install_system_dependencies:
        completed = run(system_dependency_install_command(python), None)
        if completed.returncode != 0:
            raise BrowserProvisioningError(
                "browser_system_dependencies_failed",
                "could not install missing Chromium system dependencies",
            )
        missing = _missing_libraries(executable, run=run)
    if missing:
        raise BrowserProvisioningError(
            "browser_host_prerequisite_required",
            "Chromium system libraries are missing: " + ", ".join(missing),
        )
    try:
        smoke_home = Path(tempfile.mkdtemp(prefix=".smoke-", dir=cache.parent))
        smoke_home.chmod(0o700)
    except OSError as exc:
        raise BrowserProvisioningError(
            "browser_smoke_staging_failed",
            "could not prepare private browser smoke-test state",
        ) from exc
    try:
        smoke = run(browser_smoke_command(python, cache, executable, home=smoke_home), None)
    finally:
        shutil.rmtree(smoke_home, ignore_errors=True)
    if smoke.returncode != 0:
        raise BrowserProvisioningError(
            "browser_sandbox_verification_failed",
            "managed Chromium could not complete a sandboxed smoke launch",
        )
    return missing


def _copy_legacy_cache(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise BrowserProvisioningError("browser_cache_unsafe", "browser staging target already exists")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    destination.chmod(0o700)


def provision_managed_browser(
    *,
    python: Path,
    active_cache: Path,
    staging_cache: Path,
    legacy_cache: Path | None,
    run: RunCommand,
    install_system_dependencies: bool = True,
) -> BrowserProvisioningResult:
    """Provision or safely reuse the Chromium revision matched to staged Playwright."""

    if not sys.platform.startswith("linux"):
        raise BrowserProvisioningError(
            "browser_platform_unsupported",
            "managed Browser Manager provisioning currently supports Linux only",
        )
    try:
        version = target_browser_version(python)
    except BrowserVersionError as exc:
        raise BrowserProvisioningError(exc.code, str(exc)) from exc

    try:
        active_executable = inspect_managed_cache(active_cache, version)
    except BrowserProvisioningError as exc:
        if exc.code not in {"browser_cache_missing", "browser_runtime_missing"}:
            raise
    else:
        missing = _verify_host(
            python=python,
            cache=active_cache,
            executable=active_executable,
            run=run,
            install_system_dependencies=install_system_dependencies,
        )
        return BrowserProvisioningResult("reused", version, active_cache, None, missing)

    if legacy_cache is not None and (legacy_cache.exists() or legacy_cache.is_symlink()):
        try:
            inspect_managed_cache(legacy_cache, version)
        except BrowserProvisioningError as exc:
            if exc.code not in {"browser_cache_missing", "browser_runtime_missing"}:
                raise
        else:
            _copy_legacy_cache(legacy_cache, staging_cache)
            executable = inspect_managed_cache(staging_cache, version)
            missing = _verify_host(
                python=python,
                cache=staging_cache,
                executable=executable,
                run=run,
                install_system_dependencies=install_system_dependencies,
            )
            return BrowserProvisioningResult("migrated", version, active_cache, staging_cache, missing)

    _cache_root(staging_cache, create=True)
    installed = run(browser_install_command(python, staging_cache), None)
    if installed.returncode != 0:
        raise BrowserProvisioningError("browser_install_failed", "could not install managed Playwright Chromium")
    executable = inspect_managed_cache(staging_cache, version)
    missing = _verify_host(
        python=python,
        cache=staging_cache,
        executable=executable,
        run=run,
        install_system_dependencies=install_system_dependencies,
    )
    return BrowserProvisioningResult("installed", version, active_cache, staging_cache, missing)


__all__ = [
    "BrowserProvisioningError",
    "BrowserProvisioningResult",
    "browser_install_command",
    "browser_smoke_command",
    "inspect_managed_cache",
    "provision_managed_browser",
    "system_dependency_install_command",
]
