"""User-owned Playwright and Chromium runtime discovery.

Browser Manager deliberately does not install, update, copy, or activate a
browser.  The user-owned Playwright installation is the authority: Playwright
resolves its bundled Chromium from the private Dispatch browser cache and this
module records only the paths and versions needed to launch and clean up an
owned process tree.
"""
from __future__ import annotations


from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import stat


from .models import BrowserManagerError


BROWSER_FAMILY = "chromium"


@dataclass(frozen=True, slots=True)
class BrowserRuntimePolicy:
    """The per-user Playwright browser cache used by Dispatch."""

    browsers_path: Path


@dataclass(frozen=True, slots=True)
class BrowserRuntimeIdentity:
    """Stable, non-content identity for process ownership and lease recovery."""

    playwright_version: str
    chromium_version: str | None
    executable: Path
    control_executable: Path

    def __post_init__(self) -> None:
        if not isinstance(self.playwright_version, str) or not self.playwright_version:
            raise BrowserManagerError(
                "browser_runtime_identity_invalid",
                "Playwright version is missing",
            )
        if self.chromium_version is not None and (
            not isinstance(self.chromium_version, str) or not self.chromium_version
        ):
            raise BrowserManagerError(
                "browser_runtime_identity_invalid",
                "Chromium version is invalid",
            )
        for path, label in (
            (self.executable, "browser runtime executable"),
            (self.control_executable, "browser control executable"),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise BrowserManagerError(
                    "browser_runtime_identity_invalid",
                    f"{label} must be absolute",
                )


@dataclass(frozen=True, slots=True)
class VerifiedBrowserInstallation:
    identity: BrowserRuntimeIdentity
    playwright_module: Path
    browsers_path: Path
    executable_device: int
    executable_inode: int



def user_browser_cache() -> Path:
    """Return the only browser cache accepted by Browser Manager."""

    configured_cache = os.environ.get("DISPATCH_CACHE_ROOT")
    if configured_cache:
        cache_root = Path(configured_cache).expanduser()
    else:
        configured_home = os.environ.get("DISPATCH_HOME")
        dispatch_root = Path(configured_home).expanduser() if configured_home else Path.home() / ".dispatch"
        if not dispatch_root.is_absolute():
            raise BrowserManagerError(
                "browser_cache_path_invalid",
                "DISPATCH_HOME must be absolute",
            )
        cache_root = dispatch_root / "cache"
    if not cache_root.is_absolute():
        raise BrowserManagerError(
            "browser_cache_path_invalid",
            "DISPATCH_CACHE_ROOT must be absolute",
        )
    return cache_root / "browser-manager" / "playwright"


def installed_playwright_module() -> Path:
    """Locate the installed Playwright Python package."""

    try:
        distribution = importlib.metadata.distribution("playwright")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BrowserManagerError(
            "playwright_missing",
            "Playwright is not installed for the current user",
        ) from exc
    except Exception as exc:
        raise BrowserManagerError(
            "playwright_invalid",
            "Playwright package metadata is unavailable",
        ) from exc
    module = Path(distribution.locate_file("playwright/__init__.py"))
    try:
        return module.resolve(strict=True)
    except OSError as exc:
        raise BrowserManagerError(
            "playwright_invalid",
            "installed Playwright package location is unavailable",
        ) from exc


def installed_playwright_version() -> str:
    try:
        version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BrowserManagerError(
            "playwright_missing",
            "Playwright is not installed for the current user",
        ) from exc
    except Exception as exc:
        raise BrowserManagerError(
            "playwright_invalid",
            "Playwright package version is unavailable",
        ) from exc
    if not version:
        raise BrowserManagerError("playwright_invalid", "Playwright package version is empty")
    return version



def _driver_executable(module: Path) -> Path:
    name = "node.exe" if os.name == "nt" else "node"
    return (module.parent / "driver" / name).resolve(strict=False)


def _bounded_json(path: Path, *, maximum: int = 64 * 1024) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_size > maximum
        ):
            return None
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            return None
        payload = json.loads(data.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    finally:
        os.close(descriptor)
    return payload if isinstance(payload, dict) else None


def _chromium_descriptor(module: Path) -> tuple[str, str | None] | None:
    root = module.parent
    manifest = root / "driver" / "package" / "browsers.json"
    try:
        current = root
        for part in manifest.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                return None
        resolved = manifest.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        if resolved != manifest:
            return None
    except (OSError, ValueError):
        return None
    payload = _bounded_json(manifest)
    if payload is None:
        return None
    browsers = payload.get("browsers")
    if not isinstance(browsers, list):
        return None
    chromium = [browser for browser in browsers if isinstance(browser, dict) and browser.get("name") == BROWSER_FAMILY]
    if len(chromium) != 1:
        return None
    revision = chromium[0].get("revision")
    version = chromium[0].get("browserVersion")
    if not isinstance(revision, str) or not revision.isdigit():
        return None
    return revision, version if isinstance(version, str) and version else None


def _chromium_version(module: Path) -> str | None:
    descriptor = _chromium_descriptor(module)
    return None if descriptor is None else descriptor[1]


def _cache_directory(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise BrowserManagerError(
                "browser_runtime_unsafe",
                "Dispatch Playwright browser cache cannot use symlink ancestors",
            )
        if candidate.exists():
            details = candidate.lstat()
            if not stat.S_ISDIR(details.st_mode) or details.st_uid not in {0, os.geteuid()}:
                raise BrowserManagerError(
                    "browser_runtime_unsafe",
                    "Dispatch Playwright browser cache ancestry is unsafe",
                )
            writable = details.st_mode & 0o022
            trusted_sticky_root = details.st_uid == 0 and bool(details.st_mode & stat.S_ISVTX)
            if writable and not trusted_sticky_root:
                raise BrowserManagerError(
                    "browser_runtime_unsafe",
                    "Dispatch Playwright browser cache ancestry is group/world writable",
                )
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BrowserManagerError(
            "browser_runtime_missing",
            "Dispatch Playwright browser cache is missing",
        ) from exc
    except OSError as exc:
        raise BrowserManagerError(
            "browser_runtime_unsafe",
            "Dispatch Playwright browser cache cannot be inspected",
        ) from exc
    try:
        details = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise BrowserManagerError(
            "browser_runtime_unsafe",
            "Dispatch Playwright browser cache cannot be inspected",
        ) from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o022
    ):
        raise BrowserManagerError(
            "browser_runtime_unsafe",
            "Dispatch Playwright browser cache is not private",
        )
    return resolved


def _runtime_file(path: Path, boundary: Path, label: str, *, executable: bool) -> Path:
    try:
        unresolved = Path(os.path.abspath(path))
        if not unresolved.exists() and not unresolved.is_symlink():
            raise FileNotFoundError(unresolved)
        relative = unresolved.relative_to(boundary)
        candidate = boundary
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise BrowserManagerError("browser_runtime_unsafe", f"{label} cannot contain symlinks")
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(boundary)
        details = resolved.stat(follow_symlinks=False)
    except BrowserManagerError:
        raise
    except FileNotFoundError as exc:
        raise BrowserManagerError("browser_runtime_missing", f"{label} is missing") from exc
    except (OSError, ValueError) as exc:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} is outside the browser cache") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o022:
        raise BrowserManagerError("browser_runtime_unsafe", f"{label} is not a private regular file")
    if executable and not os.access(resolved, os.X_OK):
        raise BrowserManagerError("browser_runtime_missing", f"{label} is not executable")
    return resolved


def _resolve_chromium(cache: Path, module: Path) -> Path:
    descriptor = _chromium_descriptor(module)
    if descriptor is None:
        raise BrowserManagerError("browser_runtime_missing", "Playwright Chromium metadata is invalid")
    revision, _version = descriptor
    root = cache / f"chromium-{revision}"
    for relative in (("chrome-linux64", "chrome"), ("chrome-linux", "chrome")):
        candidate = root.joinpath(*relative)
        if candidate.exists() or candidate.is_symlink():
            return _runtime_file(candidate, cache, "Chromium executable", executable=True)
    raise BrowserManagerError("browser_runtime_missing", "Playwright Chromium is not installed")


class BrowserRuntimeAuthority:
    """Discover the current user-owned Playwright Chromium installation."""

    __slots__ = ("__policy",)

    def __init__(self) -> None:
        self.__policy = BrowserRuntimePolicy(user_browser_cache())

    @classmethod
    def production(cls) -> "BrowserRuntimeAuthority":
        return cls()

    def _observed_playwright_version(self) -> str | None:
        try:
            return installed_playwright_version()
        except BrowserManagerError:
            return None

    def load(self, *, full_tree: bool = False) -> VerifiedBrowserInstallation:
        del full_tree  # Kept for the existing health/runtime call sites.
        module = installed_playwright_module()
        version = installed_playwright_version()
        cache = _cache_directory(self.__policy.browsers_path)
        executable = _resolve_chromium(cache, module)
        control_executable = _runtime_file(
            _driver_executable(module),
            module.parent,
            "Playwright control executable",
            executable=True,
        )
        identity = BrowserRuntimeIdentity(
            playwright_version=version,
            chromium_version=_chromium_version(module),
            executable=executable,
            control_executable=control_executable,
        )
        executable_details = executable.stat(follow_symlinks=False)
        return VerifiedBrowserInstallation(
            identity=identity,
            playwright_module=module,
            browsers_path=cache,
            executable_device=executable_details.st_dev,
            executable_inode=executable_details.st_ino,
        )

    def inspect(self, *, full_tree: bool = True) -> dict[str, object]:
        try:
            installation = self.load(full_tree=full_tree)
        except BrowserManagerError as exc:
            version = self._observed_playwright_version()
            cache = self.__policy.browsers_path
            return {
                "installed": version is not None,
                "configured": cache.exists() and cache.is_dir() and not cache.is_symlink(),
                "ready": False,
                "operational": False,
                "error_code": exc.code,
                "error_message": str(exc)[:256],
                "playwright_version": version,
                "chromium_revision": None,
                "chromium_version": None,
                "browser_family": BROWSER_FAMILY,
                "playwright_browsers_path": str(cache),
            }
        identity = installation.identity
        descriptor = _chromium_descriptor(installation.playwright_module)
        return {
            "installed": True,
            "configured": True,
            "ready": True,
            "operational": True,
            "error_code": None,
            "error_message": None,
            "playwright_version": identity.playwright_version,
            "chromium_revision": None if descriptor is None else descriptor[0],
            "chromium_version": identity.chromium_version,
            "browser_family": BROWSER_FAMILY,
            "playwright_browsers_path": str(installation.browsers_path),
            "chromium_executable": str(identity.executable),
            "playwright_control_executable": str(identity.control_executable),
        }


__all__ = [
    "BROWSER_FAMILY",
    "BrowserRuntimeAuthority",
    "BrowserRuntimeIdentity",
    "BrowserRuntimePolicy",
    "VerifiedBrowserInstallation",
    "installed_playwright_module",
    "installed_playwright_version",
    "user_browser_cache",
]
