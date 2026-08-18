"""Version identity for the Dispatch-managed Playwright Chromium runtime."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import email.parser
import json
import os
import re
import stat


class BrowserVersionError(RuntimeError):
    """The staged Playwright package does not describe one safe Chromium runtime."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrowserVersionIdentity:
    playwright_version: str
    chromium_revision: str
    chromium_version: str | None
    playwright_root: Path

    def safe_data(self) -> dict[str, object]:
        return {
            "playwright_version": self.playwright_version,
            "browser_family": "chromium",
            "chromium_revision": self.chromium_revision,
            "chromium_version": self.chromium_version,
        }

    def executable_candidates(self, cache: Path) -> tuple[Path, ...]:
        root = cache / f"chromium-{self.chromium_revision}"
        return (
            root / "chrome-linux64" / "chrome",
            root / "chrome-linux" / "chrome",
        )


def _private_directory(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    if path != absolute:
        raise BrowserVersionError("browser_package_unsafe", f"{label} is missing or unsafe")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise BrowserVersionError("browser_package_unsafe", f"{label} contains a symlink ancestor")
    if not path.is_dir():
        raise BrowserVersionError("browser_package_unsafe", f"{label} is missing or unsafe")
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
        raise BrowserVersionError("browser_package_unsafe", f"{label} is not user-owned")
    return path


def _read_regular_text(path: Path, *, maximum: int, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BrowserVersionError("playwright_invalid", f"{label} is unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or details.st_size > maximum
        ):
            raise BrowserVersionError("playwright_invalid", f"{label} is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise BrowserVersionError("playwright_invalid", f"{label} is oversized")
        return payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise BrowserVersionError("playwright_invalid", f"{label} is not UTF-8") from exc
    finally:
        os.close(descriptor)


def _site_packages(python: Path) -> Path:
    venv = python.parent.parent
    candidates = sorted((venv / "lib").glob("python*/site-packages"))
    if len(candidates) != 1:
        raise BrowserVersionError("playwright_missing", "staged Python has no unique site-packages directory")
    return _private_directory(candidates[0], "staged site-packages")


def _playwright_version(site_packages: Path) -> str:
    versions: list[str] = []
    for candidate in sorted(site_packages.glob("*.dist-info")):
        if candidate.is_symlink():
            raise BrowserVersionError("playwright_invalid", "staged distribution metadata is aliased")
        if not candidate.is_dir():
            continue
        metadata = candidate / "METADATA"
        try:
            payload = email.parser.Parser().parsestr(
                _read_regular_text(metadata, maximum=64 * 1024, label="distribution metadata")
            )
        except BrowserVersionError:
            continue
        if str(payload.get("Name", "")).strip().lower() != "playwright":
            continue
        version = str(payload.get("Version", "")).strip()
        if re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z]+){1,3}", version) is None:
            raise BrowserVersionError("playwright_invalid", "staged Playwright version is invalid")
        versions.append(version)
    if len(versions) != 1:
        raise BrowserVersionError("playwright_invalid", "staged Playwright metadata is missing or ambiguous")
    return versions[0]


def target_browser_version(python: Path) -> BrowserVersionIdentity:
    """Read the exact Playwright/Chromium pair from one staged virtualenv."""

    site_packages = _site_packages(python)
    playwright_root = _private_directory(site_packages / "playwright", "staged Playwright package")
    driver_package = _private_directory(
        playwright_root / "driver" / "package",
        "staged Playwright driver package",
    )
    manifest = driver_package / "browsers.json"
    try:
        payload = json.loads(
            _read_regular_text(manifest, maximum=64 * 1024, label="Playwright browser metadata")
        )
    except (BrowserVersionError, json.JSONDecodeError, RecursionError) as exc:
        raise BrowserVersionError("playwright_invalid", "Playwright browser metadata is unavailable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("browsers"), list):
        raise BrowserVersionError("playwright_invalid", "Playwright browser metadata is unsafe")
    chromium = [item for item in payload["browsers"] if isinstance(item, dict) and item.get("name") == "chromium"]
    if len(chromium) != 1:
        raise BrowserVersionError("playwright_invalid", "Playwright does not declare one Chromium runtime")
    revision = chromium[0].get("revision")
    browser_version = chromium[0].get("browserVersion")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9]+", revision) is None:
        raise BrowserVersionError("playwright_invalid", "Playwright Chromium revision is invalid")
    if browser_version is not None and (not isinstance(browser_version, str) or not browser_version):
        raise BrowserVersionError("playwright_invalid", "Playwright Chromium version is invalid")
    return BrowserVersionIdentity(
        playwright_version=_playwright_version(site_packages),
        chromium_revision=revision,
        chromium_version=browser_version,
        playwright_root=playwright_root,
    )


__all__ = ["BrowserVersionError", "BrowserVersionIdentity", "target_browser_version"]
