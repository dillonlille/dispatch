from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import browser_manager.runtime_authority as authority_module
from browser_manager import BrowserManagerError
from browser_manager.runtime_authority import (
    BROWSER_FAMILY,
    BrowserRuntimeAuthority,
    BrowserRuntimeIdentity,
)


class FakePlaywright:
    def __init__(self, executable: Path) -> None:
        self.chromium = SimpleNamespace(executable_path=str(executable))
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePlaywrightContext:
    def __init__(self, playwright: FakePlaywright, observed: list[str | None]) -> None:
        self.playwright = playwright
        self.observed = observed

    def start(self) -> FakePlaywright:
        self.observed.append(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        return self.playwright


def fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable: Path | None = None,
) -> tuple[BrowserRuntimeAuthority, Path, FakePlaywright, list[str | None]]:
    cache = tmp_path / "home" / ".dispatch" / "cache" / "browser"
    chromium = cache / "chromium-1234" / "chrome-linux64" / "chrome"
    chromium.parent.mkdir(parents=True)
    chromium.write_text("#!/bin/sh\n", encoding="utf-8")
    chromium.chmod(0o700)
    cache.chmod(0o700)

    package = tmp_path / "venv" / "site-packages" / "playwright"
    driver = package / "driver"
    (driver / "package").mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("# fake playwright\n", encoding="utf-8")
    module.chmod(0o600)
    control = driver / "node"
    control.write_text("#!/bin/sh\n", encoding="utf-8")
    control.chmod(0o700)
    (driver / "package" / "browsers.json").write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": BROWSER_FAMILY, "browserVersion": "151.0.7922.34"}
                ]
            }
        ),
        encoding="utf-8",
    )

    selected = executable or chromium
    playwright = FakePlaywright(selected)
    observed: list[str | None] = []
    monkeypatch.setattr(authority_module, "user_browser_cache", lambda: cache)
    monkeypatch.setattr(authority_module, "installed_playwright_module", lambda: module)
    monkeypatch.setattr(authority_module, "installed_playwright_version", lambda: "1.62.0")
    monkeypatch.setattr(
        authority_module,
        "_load_sync_playwright",
        lambda: lambda: FakePlaywrightContext(playwright, observed),
    )
    return BrowserRuntimeAuthority(), chromium, playwright, observed


def test_authority_resolves_user_cache_and_reports_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, playwright, observed = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/untrusted-playwright")

    installation = authority.load()
    inspection = authority.inspect()

    assert installation.identity == BrowserRuntimeIdentity(
        playwright_version="1.62.0",
        chromium_version="151.0.7922.34",
        executable=chromium.resolve(),
        control_executable=(
            tmp_path / "venv" / "site-packages" / "playwright" / "driver" / "node"
        ).resolve(),
    )
    assert observed == [str((tmp_path / "home" / ".dispatch" / "cache" / "browser").resolve())] * 2
    assert playwright.stopped is True
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/untrusted-playwright"
    assert inspection["ready"] is True
    assert inspection["browser_family"] == "chromium"
    assert inspection["chromium_version"] == "151.0.7922.34"
    assert inspection["chromium_executable"] == str(chromium.resolve())


def test_authority_rejects_symlinked_runtime_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, playwright, _observed = fixture(tmp_path, monkeypatch)
    linked = chromium.with_name("chrome-link")
    linked.symlink_to(chromium)
    playwright.chromium.executable_path = str(linked)
    with pytest.raises(BrowserManagerError) as error:
        authority.load()
    assert error.value.code == "browser_runtime_unsafe"


def test_user_browser_cache_honors_custom_dispatch_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "custom-dispatch"
    monkeypatch.setenv("DISPATCH_HOME", str(custom))
    assert authority_module.user_browser_cache() == (custom / "cache" / "browser").resolve()

    monkeypatch.setenv("DISPATCH_HOME", "relative-dispatch")
    with pytest.raises(BrowserManagerError) as error:
        authority_module.user_browser_cache()
    assert error.value.code == "browser_cache_path_invalid"


def test_browser_cache_rejects_symlink_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "custom-dispatch"
    cache = custom / "cache"
    outside = tmp_path / "outside"
    cache.mkdir(parents=True)
    outside.mkdir()
    (cache / "browser").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("DISPATCH_HOME", str(custom))

    configured = authority_module.user_browser_cache()
    assert configured.is_symlink()
    with pytest.raises(BrowserManagerError) as error:
        authority_module._cache_directory(configured)
    assert error.value.code == "browser_runtime_unsafe"


def test_authority_rejects_missing_or_non_executable_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, _, _ = fixture(tmp_path, monkeypatch, executable=tmp_path / "missing-chrome")

    with pytest.raises(BrowserManagerError) as missing:
        authority.load()
    assert missing.value.code == "browser_runtime_missing"

    chromium.chmod(0o600)
    authority, _, _, _ = fixture(tmp_path / "not-executable", monkeypatch)
    executable = authority_module.user_browser_cache() / "chromium-1234" / "chrome-linux64" / "chrome"
    executable.chmod(0o600)
    with pytest.raises(BrowserManagerError) as non_executable:
        authority.load()
    assert non_executable.value.code == "browser_runtime_missing"


def test_authority_rejects_playwright_executable_outside_user_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _, _, _ = fixture(tmp_path, monkeypatch, executable=tmp_path / "outside-chrome")
    outside = tmp_path / "outside-chrome"
    outside.write_text("not a browser\n", encoding="utf-8")
    outside.chmod(0o700)

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()

    assert rejected.value.code == "browser_runtime_unsafe"


def test_inspect_reports_missing_playwright_without_mutating_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "home" / ".dispatch" / "cache" / "browser"
    cache.mkdir(parents=True)
    cache.chmod(0o700)
    monkeypatch.setattr(authority_module, "user_browser_cache", lambda: cache)

    def missing() -> str:
        raise BrowserManagerError("playwright_missing", "not installed")

    monkeypatch.setattr(authority_module, "installed_playwright_module", missing)
    monkeypatch.setattr(authority_module, "installed_playwright_version", missing)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/tmp/untrusted-playwright")

    inspection = BrowserRuntimeAuthority().inspect()

    assert inspection["installed"] is False
    assert inspection["ready"] is False
    assert inspection["error_code"] == "playwright_missing"
    assert inspection["playwright_browsers_path"] == str(cache)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/untrusted-playwright"
