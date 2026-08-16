from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import browser_manager.runtime_authority as authority_module
from browser_manager import BrowserManagerError
from browser_manager.runtime_authority import (
    BROWSER_FAMILY,
    BrowserRuntimeAuthority,
    BrowserRuntimeIdentity,
)


def fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BrowserRuntimeAuthority, Path, Path]:
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
                    {
                        "name": BROWSER_FAMILY,
                        "revision": "1234",
                        "browserVersion": "151.0.7922.34",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(authority_module, "user_browser_cache", lambda: cache)
    monkeypatch.setattr(authority_module, "installed_playwright_module", lambda: module)
    monkeypatch.setattr(authority_module, "installed_playwright_version", lambda: "1.62.0")
    return BrowserRuntimeAuthority(), chromium, module


def test_authority_resolves_user_cache_without_starting_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, _module = fixture(tmp_path, monkeypatch)
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
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/untrusted-playwright"
    assert inspection["ready"] is True
    assert inspection["browser_family"] == "chromium"
    assert inspection["chromium_version"] == "151.0.7922.34"
    assert inspection["chromium_executable"] == str(chromium.resolve())


def test_authority_rejects_symlinked_runtime_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, _module = fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside-chrome"
    outside.write_text("not a browser\n", encoding="utf-8")
    outside.chmod(0o700)
    chromium.unlink()
    chromium.symlink_to(outside)

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


def test_authority_rejects_missing_non_executable_and_invalid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, chromium, module = fixture(tmp_path, monkeypatch)
    chromium.unlink()
    with pytest.raises(BrowserManagerError) as missing:
        authority.load()
    assert missing.value.code == "browser_runtime_missing"

    authority, chromium, _module = fixture(tmp_path / "not-executable", monkeypatch)
    chromium.chmod(0o600)
    with pytest.raises(BrowserManagerError) as non_executable:
        authority.load()
    assert non_executable.value.code == "browser_runtime_missing"

    authority, _chromium, module = fixture(tmp_path / "invalid-manifest", monkeypatch)
    manifest = module.parent / "driver" / "package" / "browsers.json"
    manifest.write_text(
        json.dumps({"browsers": [{"name": BROWSER_FAMILY, "revision": "../escape"}]}),
        encoding="utf-8",
    )
    with pytest.raises(BrowserManagerError) as invalid:
        authority.load()
    assert invalid.value.code == "browser_runtime_missing"


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
