from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest
import dispatch_core.browser_manager as browser_manager_module

from dispatch_core.browser_manager import BrowserManager, BrowserManagerError
from dispatch_core.browser_manager.runtime import PlaywrightRuntime
from dispatch_core.browser_manager.runtime_authority import (
    BROWSER_FAMILY,
    BrowserRuntimeAuthority,
    BrowserRuntimePolicy,
    CHROMIUM_VERSION,
    PLAYWRIGHT_REVISION,
    PLAYWRIGHT_VERSION,
    RuntimePlatform,
)


PLATFORM = RuntimePlatform(
    system="linux",
    distribution="ubuntu",
    distribution_version="24.04",
    architecture="x86_64",
)


def encoded(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def authority_for_testing(
    policy: BrowserRuntimePolicy,
    *,
    platform_value: RuntimePlatform,
    playwright_module: Path,
    package_version: str = PLAYWRIGHT_VERSION,
) -> BrowserRuntimeAuthority:
    value = object.__new__(BrowserRuntimeAuthority)
    object.__setattr__(value, "_BrowserRuntimeAuthority__policy", policy)
    object.__setattr__(value, "_BrowserRuntimeAuthority__platform_resolver", lambda: platform_value)
    object.__setattr__(
        value,
        "_BrowserRuntimeAuthority__package_version_resolver",
        lambda unused_name: package_version,
    )
    object.__setattr__(
        value,
        "_BrowserRuntimeAuthority__package_module_resolver",
        lambda: playwright_module,
    )
    return value


def authority_policy(authority: BrowserRuntimeAuthority) -> BrowserRuntimePolicy:
    return object.__getattribute__(authority, "_BrowserRuntimeAuthority__policy")


def authority_module(authority: BrowserRuntimeAuthority) -> Path:
    resolver = object.__getattribute__(authority, "_BrowserRuntimeAuthority__package_module_resolver")
    return resolver()


def install_fixture(tmp_path: Path) -> tuple[BrowserRuntimeAuthority, dict[str, object], Path]:
    config_root = tmp_path / "config"
    runtime_root = tmp_path / "runtimes"
    generation = "chromium-151.0.7922.34-r1234"
    generation_root = runtime_root / generation
    executable = generation_root / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True, mode=0o755)
    config_root.mkdir(mode=0o755)
    for directory in (runtime_root, generation_root, executable.parent, config_root):
        directory.chmod(0o755)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o555)
    resource = executable.parent / "resources.pak"
    resource.write_bytes(b"trusted resource")
    resource.chmod(0o444)
    playwright_module = generation_root / "python" / "playwright" / "__init__.py"
    driver_executable = generation_root / "python" / "playwright" / "driver" / "node"
    driver_cli = generation_root / "python" / "playwright" / "driver" / "package" / "cli.js"
    playwright_module.parent.mkdir(parents=True)
    playwright_module.write_text("# verified Playwright fixture\n", encoding="utf-8")
    playwright_module.chmod(0o444)
    driver_executable.parent.mkdir(parents=True)
    driver_executable.write_bytes(b"verified node fixture\n")
    driver_executable.chmod(0o555)
    driver_cli.parent.mkdir(parents=True)
    driver_cli.write_text("// verified driver fixture\n", encoding="utf-8")
    driver_cli.chmod(0o444)

    tree = {
        "schema_version": 1,
        "files": {
            "chrome-linux64/chrome": {
                "size": executable.stat().st_size,
                "sha256": digest(executable),
                "mode": "0555",
            },
            "chrome-linux64/resources.pak": {
                "size": resource.stat().st_size,
                "sha256": digest(resource),
                "mode": "0444",
            },
            "python/playwright/__init__.py": {
                "size": playwright_module.stat().st_size,
                "sha256": digest(playwright_module),
                "mode": "0444",
            },
            "python/playwright/driver/node": {
                "size": driver_executable.stat().st_size,
                "sha256": digest(driver_executable),
                "mode": "0555",
            },
            "python/playwright/driver/package/cli.js": {
                "size": driver_cli.stat().st_size,
                "sha256": digest(driver_cli),
                "mode": "0444",
            },
        },
    }
    tree_path = generation_root / "tree-manifest.json"
    tree_path.write_bytes(encoded(tree))
    tree_path.chmod(0o444)

    receipt: dict[str, object] = {
        "schema_version": 1,
        "generation": generation,
        "installed_at": datetime(2026, 8, 14, tzinfo=timezone.utc).isoformat(),
        "installer_release": "dispatch-installer-1.0.0",
        "platform_system": PLATFORM.system,
        "distribution": PLATFORM.distribution,
        "distribution_version": PLATFORM.distribution_version,
        "architecture": PLATFORM.architecture,
        "playwright_version": PLAYWRIGHT_VERSION,
        "playwright_module_relative_path": "python/playwright/__init__.py",
        "playwright_module_size": playwright_module.stat().st_size,
        "playwright_module_sha256": digest(playwright_module),
        "playwright_driver_executable_relative_path": "python/playwright/driver/node",
        "playwright_driver_executable_size": driver_executable.stat().st_size,
        "playwright_driver_executable_sha256": digest(driver_executable),
        "playwright_driver_cli_relative_path": "python/playwright/driver/package/cli.js",
        "playwright_driver_cli_size": driver_cli.stat().st_size,
        "playwright_driver_cli_sha256": digest(driver_cli),
        "browser_family": BROWSER_FAMILY,
        "browser_version": CHROMIUM_VERSION,
        "playwright_revision": PLAYWRIGHT_REVISION,
        "executable_relative_path": "chrome-linux64/chrome",
        "executable_size": executable.stat().st_size,
        "executable_sha256": digest(executable),
        "tree_manifest_relative_path": tree_path.name,
        "tree_manifest_sha256": digest(tree_path),
        "source_manifest_sha256": "a" * 64,
        "os_dependencies_verified": True,
        "sandbox_verified": True,
        "sandbox_policy_id": "dispatch-chromium-apparmor-v1",
        "launch_probe_passed": True,
    }
    receipt_path = generation_root / "installation-receipt.json"
    receipt_path.write_bytes(encoded(receipt))
    receipt_path.chmod(0o444)
    for directory in (generation_root, *[path for path in generation_root.rglob("*") if path.is_dir()]):
        directory.chmod(0o555)
    selector = {
        "schema_version": 1,
        "generation": generation,
        "receipt_sha256": digest(receipt_path),
    }
    selector_path = config_root / "browser-runtime-active.json"
    selector_path.write_bytes(encoded(selector))
    selector_path.chmod(0o444)

    policy = BrowserRuntimePolicy(
        selector=selector_path,
        runtime_root=runtime_root,
        owner_uid=os.getuid(),
        supported_platform=PLATFORM,
    )
    authority = authority_for_testing(
        policy,
        platform_value=PLATFORM,
        playwright_module=playwright_module,
    )
    return authority, receipt, executable


def refresh_receipt(authority: BrowserRuntimeAuthority, receipt: dict[str, object]) -> None:
    policy = authority_policy(authority)
    selector_path = policy.selector
    selector = json.loads(selector_path.read_text(encoding="utf-8"))
    receipt_path = policy.runtime_root / str(selector["generation"]) / "installation-receipt.json"
    receipt_path.chmod(0o644)
    receipt_path.write_bytes(encoded(receipt))
    receipt_path.chmod(0o444)
    selector["receipt_sha256"] = digest(receipt_path)
    selector_path.chmod(0o644)
    selector_path.write_bytes(encoded(selector))
    selector_path.chmod(0o444)


def test_valid_installer_receipt_and_tree_are_consumed_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    authority, _, executable = install_fixture(tmp_path)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "untrusted-cache"))

    installation = authority.load(full_tree=True)

    assert installation.identity.executable == executable
    assert installation.identity.generation == "chromium-151.0.7922.34-r1234"
    assert authority.inspect(full_tree=True)["ready"] is True
    assert not (tmp_path / "untrusted-cache").exists()


def test_playwright_control_files_and_loaded_module_location_are_bound(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    generation_root = executable.parents[1]
    control = generation_root / "python" / "playwright" / "driver" / "node"
    control.chmod(0o755)
    control.write_bytes(b"tampered node fixture\n")
    control.chmod(0o555)

    with pytest.raises(BrowserManagerError) as tampered:
        authority.load()
    assert tampered.value.code == "browser_runtime_mismatch"

    authority, _, _ = install_fixture(tmp_path / "second")
    object.__setattr__(
        authority,
        "_BrowserRuntimeAuthority__package_module_resolver",
        lambda: tmp_path / "untrusted" / "playwright" / "__init__.py",
    )
    with pytest.raises(BrowserManagerError) as misplaced:
        authority.load()
    assert misplaced.value.code == "playwright_package_mismatch"


def test_executable_tampering_fails_closed(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    executable.chmod(0o755)
    executable.write_bytes(b"#!/bin/sh\nexit 7\n")
    executable.chmod(0o555)

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()

    assert rejected.value.code == "browser_executable_mismatch"


def test_runtime_rechecks_the_complete_tree_immediately_before_launch(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    installation = authority.load(full_tree=True)
    runtime = object.__new__(PlaywrightRuntime)
    object.__setattr__(runtime, "_PlaywrightRuntime__authority", authority)
    object.__setattr__(runtime, "_PlaywrightRuntime__installation", installation)
    object.__setattr__(
        runtime,
        "_PlaywrightRuntime__launch_executable",
        installation.identity.executable,
    )
    resource = executable.parent / "resources.pak"
    resource.chmod(0o644)
    resource.write_bytes(b"tampered resource")
    resource.chmod(0o444)

    with pytest.raises(BrowserManagerError) as rejected:
        runtime._verified_for_launch()

    assert rejected.value.code == "browser_tree_mismatch"


def test_hard_linked_executable_fails_closed(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    os.link(executable, tmp_path / "linked-chromium")

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()

    assert rejected.value.code == "browser_runtime_unsafe"


def test_receipt_version_mismatch_fails_even_when_installer_files_are_well_formed(tmp_path: Path) -> None:
    authority, receipt, _ = install_fixture(tmp_path)
    receipt["playwright_revision"] = "9999"
    refresh_receipt(authority, receipt)

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()

    assert rejected.value.code == "browser_receipt_mismatch"


def test_unlisted_tree_member_fails_full_integrity_check(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    unexpected = executable.parent / "unexpected-library.so"
    executable.parent.chmod(0o755)
    unexpected.write_bytes(b"unexpected")
    unexpected.chmod(0o444)
    executable.parent.chmod(0o555)

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load(full_tree=True)

    assert rejected.value.code == "browser_tree_mismatch"


def test_unsafe_installer_owned_permissions_fail_closed(tmp_path: Path) -> None:
    authority, _, executable = install_fixture(tmp_path)
    executable.parent.parent.chmod(0o775)

    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()

    assert rejected.value.code == "browser_runtime_unsafe"


def test_missing_selector_is_reported_as_not_ready(tmp_path: Path) -> None:
    policy = BrowserRuntimePolicy(
        selector=tmp_path / "config" / "browser-runtime-active.json",
        runtime_root=tmp_path / "runtimes",
        owner_uid=os.getuid(),
        supported_platform=PLATFORM,
    )
    authority = authority_for_testing(
        policy,
        platform_value=PLATFORM,
        playwright_module=tmp_path / "playwright" / "__init__.py",
    )

    result = authority.inspect()

    assert result["ready"] is False
    assert result["error_code"] == "browser_runtime_selector_missing"


def test_wrong_playwright_package_version_fails_before_runtime_selection(tmp_path: Path) -> None:
    authority, _, _ = install_fixture(tmp_path)
    mismatched = authority_for_testing(
        authority_policy(authority),
        platform_value=PLATFORM,
        playwright_module=authority_module(authority),
        package_version="1.61.0",
    )

    with pytest.raises(BrowserManagerError) as rejected:
        mismatched.load()

    assert rejected.value.code == "playwright_version_mismatch"


def test_malformed_boolean_and_duplicate_key_receipts_fail_closed(tmp_path: Path) -> None:
    authority, receipt, _ = install_fixture(tmp_path)
    receipt["launch_probe_passed"] = 1
    refresh_receipt(authority, receipt)
    with pytest.raises(BrowserManagerError) as boolean_receipt:
        authority.load()
    assert boolean_receipt.value.code == "browser_receipt_incomplete"

    authority, _, _ = install_fixture(tmp_path / "duplicate")
    selector = authority_policy(authority).selector
    selector.chmod(0o644)
    selector.write_text(
        '{"schema_version":1,"schema_version":1,"generation":"chromium-151.0.7922.34-r1234",'
        f'"receipt_sha256":"{json.loads(selector.read_text())["receipt_sha256"]}"}}\n',
        encoding="utf-8",
    )
    selector.chmod(0o444)
    with pytest.raises(BrowserManagerError) as duplicate:
        authority.load()
    assert duplicate.value.code == "browser_runtime_selector_invalid"


def test_boolean_selector_schema_version_fails_closed(tmp_path: Path) -> None:
    authority, _, _ = install_fixture(tmp_path)
    selector = authority_policy(authority).selector
    payload = json.loads(selector.read_text())
    payload["schema_version"] = True
    selector.chmod(0o644)
    selector.write_bytes(encoded(payload))
    selector.chmod(0o444)
    with pytest.raises(BrowserManagerError) as rejected:
        authority.load()
    assert rejected.value.code == "browser_runtime_selector_invalid"


def test_public_manager_constructor_has_no_runtime_or_realm_override() -> None:
    parameters = inspect.signature(BrowserManager).parameters

    assert list(parameters) == ["paths"]
    assert "runtime" not in parameters
    assert "realms" not in parameters
    assert "clock" not in parameters
    assert not hasattr(browser_manager_module, "PlaywrightRuntime")
    assert not hasattr(BrowserManager, "_for_testing")
    assert not hasattr(PlaywrightRuntime, "_for_testing")
    assert not hasattr(BrowserRuntimeAuthority, "_for_testing")
    assert "runtime" not in BrowserManager.__slots__
    assert "realms" not in BrowserManager.__slots__
    assert "clock" not in BrowserManager.__slots__
    assert BrowserManager.layout.fset is None
    assert BrowserManager.store.fset is None
    assert BrowserManager.maximum_browsers.fset is None
    assert "_authority" not in PlaywrightRuntime.__slots__
    assert "_installation" not in PlaywrightRuntime.__slots__
    assert "_launch_executable" not in PlaywrightRuntime.__slots__
    assert "policy" not in BrowserRuntimeAuthority.__slots__
    assert "_platform_resolver" not in BrowserRuntimeAuthority.__slots__
    assert list(inspect.signature(PlaywrightRuntime).parameters) == []
    assert not inspect.signature(BrowserRuntimeAuthority).parameters
