from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

import browser_manager.provisioning as provisioning_module
from browser_manager.provisioning import (
    BrowserProvisioningError,
    browser_install_command,
    browser_smoke_command,
    provision_managed_browser,
    system_dependency_install_command,
)
from browser_manager.providers import BrowserProviderRegistry
from browser_manager.versioning import BrowserVersionError, target_browser_version


def completed(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr=stderr)


def staged_python(tmp_path: Path) -> Path:
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o700)
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    package = site / "playwright" / "driver" / "package"
    package.mkdir(parents=True)
    for directory in (
        tmp_path / "venv",
        site.parent.parent,
        site.parent,
        site,
        site / "playwright",
        package.parent,
        package,
    ):
        directory.chmod(0o700)
    (site / "playwright" / "__init__.py").write_text("", encoding="utf-8")
    manifest = package / "browsers.json"
    manifest.write_text(
        json.dumps(
            {
                "browsers": [
                    {
                        "name": "chromium",
                        "revision": "1234567",
                        "browserVersion": "151.0.7922.34",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    metadata = site / "playwright-1.62.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: playwright\nVersion: 1.62.0\n", encoding="utf-8")
    metadata.chmod(0o600)
    return python


def install_fake_browser(cache: Path) -> Path:
    executable = cache / "chromium-1234567" / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True)
    cache.chmod(0o700)
    executable.write_text("chromium", encoding="utf-8")
    executable.chmod(0o700)
    provisioning_module.write_generation_digest(executable.parent.parent)
    return executable


def is_browser_install(values: tuple[str, ...]) -> bool:
    return "env" in values and "-m" in values and "playwright" in values and "install" in values


def test_non_linux_provisioning_fails_before_runner_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(provisioning_module.sys, "platform", "darwin")
    with pytest.raises(BrowserProvisioningError) as error:
        provision_managed_browser(
            python=tmp_path / "venv" / "bin" / "python",
            active_cache=tmp_path / "active",
            staging_cache=tmp_path / "staging",
            legacy_cache=None,
            run=lambda command, _cwd=None: calls.append(tuple(command)) or completed(),
        )
    assert error.value.code == "browser_platform_unsupported"
    assert calls == []
    assert not (tmp_path / "staging").exists()


def test_version_identity_is_derived_from_staged_playwright(tmp_path: Path) -> None:
    identity = target_browser_version(staged_python(tmp_path))
    assert identity.safe_data() == {
        "playwright_version": "1.62.0",
        "browser_family": "chromium",
        "chromium_revision": "1234567",
        "chromium_version": "151.0.7922.34",
    }


def test_version_metadata_fifo_and_oversized_manifest_fail_without_reading(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    metadata = site / "playwright-1.62.0.dist-info" / "METADATA"
    metadata.unlink()
    os.mkfifo(metadata, mode=0o600)
    with pytest.raises(BrowserVersionError) as fifo_error:
        target_browser_version(python)
    assert fifo_error.value.code == "playwright_invalid"

    metadata.unlink()
    metadata.write_text("Name: playwright\nVersion: 1.62.0\n", encoding="utf-8")
    manifest = site / "playwright" / "driver" / "package" / "browsers.json"
    manifest.write_bytes(b"{" + b" " * (64 * 1024) + b"}")
    with pytest.raises(BrowserVersionError) as size_error:
        target_browser_version(python)
    assert size_error.value.code == "playwright_invalid"

    manifest.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    with pytest.raises(BrowserVersionError) as depth_error:
        target_browser_version(python)
    assert depth_error.value.code == "playwright_invalid"


def test_aliased_playwright_distribution_metadata_fails_closed(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    outside = tmp_path / "outside-dist-info"
    outside.mkdir()
    (outside / "METADATA").write_text("Name: playwright\nVersion: 9.9.9\n", encoding="utf-8")
    (site / "arbitrary-alias.dist-info").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BrowserVersionError) as error:
        target_browser_version(python)
    assert error.value.code == "playwright_invalid"


def test_cache_creation_is_private_under_permissive_umask(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "browser-manager" / "playwright"
    previous = os.umask(0)
    try:
        assert provisioning_module._cache_root(root, create=True) == root
    finally:
        os.umask(previous)
    for path in (tmp_path / "nested", tmp_path / "nested" / "browser-manager", root):
        assert path.stat().st_mode & 0o777 == 0o700


def test_cache_creation_rejects_world_writable_existing_ancestor(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(BrowserProvisioningError) as error:
        provisioning_module._cache_root(unsafe / "browser-manager" / "playwright", create=True)
    assert error.value.code == "browser_cache_unsafe"
    assert not (unsafe / "browser-manager").exists()


def test_provisioning_subprocesses_start_from_a_scrubbed_environment(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    cache = tmp_path / "cache"
    executable = cache / "chromium-123" / "chrome"
    for command in (
        browser_install_command(python, cache),
        system_dependency_install_command(python),
        browser_smoke_command(python, cache, executable),
    ):
        env_index = command.index("env")
        assert command[env_index : env_index + 2] == ("env", "-i")
        assert "PATH=/usr/bin:/bin" in command
        assert all("TOKEN=" not in value and "API_KEY=" not in value for value in command)


def test_fresh_provision_downloads_user_browser_without_unneeded_system_install(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    staging = tmp_path / "staging"
    commands: list[tuple[str, ...]] = []

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="libnss3.so => /usr/lib/libnss3.so\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=active,
        staging_cache=staging,
        legacy_cache=None,
        run=run,
    )
    assert result.status == "installed"
    assert result.replacement_required is True
    assert not any("install-deps" in command for command in commands)
    ldd_command = next(command for command in commands if any(Path(value).name == "ldd" for value in command))
    smoke_command = next(command for command in commands if any("chromium_sandbox=True" in value for value in command))
    assert ldd_command[:4] == (str(Path("/usr/bin/timeout").resolve()), "--signal=TERM", "--kill-after=2s", "15s")
    assert smoke_command[:4] == (str(Path("/usr/bin/timeout").resolve()), "--signal=TERM", "--kill-after=5s", "30s")
    assert not list(staging.parent.glob(".smoke-*"))
    assert result.installation_record(active)["chromium_revision"] == "1234567"


def test_missing_libraries_are_installed_once_and_rescanned(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    staging = tmp_path / "staging"
    dependency_installs = 0
    scans = 0

    def run(command, cwd=None):
        nonlocal dependency_installs, scans
        values = tuple(str(value) for value in command)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            scans += 1
            return completed(
                stdout=("libnss3.so => not found\n" if scans == 1 else "libnss3.so => /usr/lib/libnss3.so\n"),
                returncode=(1 if scans == 1 else 0),
            )
        if "install-deps" in values:
            dependency_installs += 1
            return completed()
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=active,
        staging_cache=staging,
        legacy_cache=None,
        run=run,
    )
    assert result.status == "installed"
    assert dependency_installs == 1
    assert scans == 2


def test_exact_active_generation_is_reused_and_verified(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    commands: list[tuple[str, ...]] = []

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="ready\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=active,
        staging_cache=tmp_path / "staging",
        legacy_cache=None,
        run=run,
    )
    assert result.status == "reused"
    assert result.replacement_required is False
    assert not any(is_browser_install(command) for command in commands)


def test_changed_playwright_revision_stages_replacement_beside_active_cache(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    old = active / "chromium-9999999" / "chrome-linux64" / "chrome"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    old.chmod(0o700)
    staging = tmp_path / "staging"

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="ready\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=active,
        staging_cache=staging,
        legacy_cache=None,
        run=run,
    )
    assert result.status == "installed"
    assert result.replacement_required is True
    assert old.read_text(encoding="utf-8") == "old"
    assert (staging / "chromium-1234567" / "chrome-linux64" / "chrome").is_file()


def test_matching_legacy_cache_is_staged_for_transactional_migration(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir(mode=0o700)
    install_fake_browser(legacy)
    staging = tmp_path / "staging"
    commands: list[tuple[str, ...]] = []

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="ready\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=tmp_path / "active",
        staging_cache=staging,
        legacy_cache=legacy,
        run=run,
    )
    assert result.status == "migrated"
    assert result.replacement_required is True
    assert (staging / "chromium-1234567" / "chrome-linux64" / "chrome").is_file()
    assert (legacy / "chromium-1234567" / "chrome-linux64" / "chrome").is_file()
    assert not any(is_browser_install(command) for command in commands)


def test_missing_libraries_without_install_authority_fail_closed(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    staging = tmp_path / "staging"

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="libgbm.so => not found\n", returncode=1)
        raise AssertionError(values)

    with pytest.raises(BrowserProvisioningError) as error:
        provision_managed_browser(
            python=python,
            active_cache=tmp_path / "active",
            staging_cache=staging,
            legacy_cache=None,
            run=run,
            install_system_dependencies=False,
        )
    assert error.value.code == "browser_host_prerequisite_required"


def test_failed_system_dependency_install_stops_before_smoke(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    staging = tmp_path / "staging"
    smoke_attempted = False

    def run(command, cwd=None):
        nonlocal smoke_attempted
        values = tuple(str(value) for value in command)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="libgbm.so => not found\n", returncode=1)
        if "install-deps" in values:
            return completed(returncode=1)
        if any("chromium_sandbox=True" in value for value in values):
            smoke_attempted = True
            return completed()
        raise AssertionError(values)

    with pytest.raises(BrowserProvisioningError) as error:
        provision_managed_browser(
            python=python,
            active_cache=tmp_path / "active",
            staging_cache=staging,
            legacy_cache=None,
            run=run,
        )
    assert error.value.code == "browser_system_dependencies_failed"
    assert smoke_attempted is False


def test_failed_sandbox_smoke_cleans_private_smoke_home(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    staging = tmp_path / "staging"

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if is_browser_install(values):
            install_fake_browser(staging)
            return completed()
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="ready\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed(returncode=1)
        raise AssertionError(values)

    with pytest.raises(BrowserProvisioningError) as error:
        provision_managed_browser(
            python=python,
            active_cache=tmp_path / "active",
            staging_cache=staging,
            legacy_cache=None,
            run=run,
        )
    assert error.value.code == "browser_sandbox_verification_failed"
    assert not list(staging.parent.glob(".smoke-*"))


def test_provider_registry_reserves_future_contracts_without_activating_them() -> None:
    data = BrowserProviderRegistry().safe_data()
    assert [item["id"] for item in data] == ["managed-playwright", "persistent-cdp", "external-cdp"]
    assert data[0]["implemented"] is True
    assert data[0]["persistent_profiles"] is True
    assert data[1]["implemented"] is False
    assert data[2]["implemented"] is False


def test_pre_digest_generation_is_adopted_through_staging(tmp_path: Path) -> None:
    """Generations provisioned before digest recording existed adopt cleanly.

    Regression: #43 made reuse demand a digest marker, which permanently
    locked every pre-#43 install out of `dispatch update`
    (browser_digest_missing). Missing markers now migrate through staging;
    genuine tampering (mismatched digest) still fails closed.
    """
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    marker = active / "chromium-1234567" / ".dispatch-content-sha256"
    marker.unlink()
    staging = tmp_path / "staging"
    commands: list[tuple[str, ...]] = []

    def run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if any(Path(value).name == "ldd" for value in values):
            return completed(stdout="ready\n")
        if any("chromium_sandbox=True" in value for value in values):
            return completed()
        raise AssertionError(values)

    result = provision_managed_browser(
        python=python,
        active_cache=active,
        staging_cache=staging,
        legacy_cache=None,
        run=run,
    )
    assert result.status == "adopted"
    assert result.replacement_required is True
    # No re-download: the installed generation is reused, not replaced.
    assert not any(is_browser_install(command) for command in commands)
    # The staged copy is digest-marked and verifies; the active generation
    # is untouched until the caller's transactional swap activates staging.
    staged_generation = staging / "chromium-1234567"
    assert (staged_generation / ".dispatch-content-sha256").is_file()
    assert provisioning_module.verify_generation_digest(staging, target_browser_version(python))
    assert not marker.exists()
    assert (active / "chromium-1234567" / "chrome-linux64" / "chrome").is_file()


def test_reuse_fails_closed_when_generation_content_was_tampered_with(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    executable = active / "chromium-1234567" / "chrome-linux64" / "chrome"
    executable.chmod(0o700)
    executable.write_text("trojan", encoding="utf-8")
    executable.chmod(0o700)

    def run(command, cwd=None):
        raise AssertionError(("provisioning must not proceed", command))

    with pytest.raises(BrowserProvisioningError) as error:
        provision_managed_browser(
            python=python,
            active_cache=active,
            staging_cache=tmp_path / "staging",
            legacy_cache=None,
            run=run,
        )
    assert error.value.code == "browser_digest_mismatch"


def test_tampered_executable_after_install_is_detected_by_digest_roundtrip(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    generation = active / "chromium-1234567"
    pinned = provisioning_module.verify_generation_digest(active, target_browser_version(python))
    assert pinned == (generation / ".dispatch-content-sha256").read_text(encoding="utf-8").strip()
    executable = generation / "chrome-linux64" / "chrome"
    executable.write_text("swapped", encoding="utf-8")
    with pytest.raises(BrowserProvisioningError) as error:
        provisioning_module.verify_generation_digest(active, target_browser_version(python))
    assert error.value.code == "browser_digest_mismatch"


def test_digest_marker_tampering_is_rejected(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    generation = active / "chromium-1234567"
    identity = target_browser_version(python)

    marker = generation / ".dispatch-content-sha256"
    for payload in ("", "z" * 64, "a" * 65):
        marker.write_text(payload + "\n", encoding="utf-8")
        with pytest.raises(BrowserProvisioningError) as error:
            provisioning_module.verify_generation_digest(active, identity)
        assert error.value.code == "browser_digest_unsafe"

    marker.write_text("0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(BrowserProvisioningError) as mismatch:
        provisioning_module.verify_generation_digest(active, identity)
    assert mismatch.value.code == "browser_digest_mismatch"


def test_symlink_inside_generation_fails_the_digest_walk(tmp_path: Path) -> None:
    python = staged_python(tmp_path)
    active = tmp_path / "active"
    active.mkdir(mode=0o700)
    install_fake_browser(active)
    generation = active / "chromium-1234567"
    outside = tmp_path / "outside.so"
    outside.write_text("hostile", encoding="utf-8")
    (generation / "chrome-linux64" / "libevil.so").symlink_to(outside)
    identity = target_browser_version(python)
    with pytest.raises(BrowserProvisioningError) as error:
        provisioning_module.verify_generation_digest(active, identity)
    assert error.value.code == "browser_runtime_unsafe"


def test_installed_and_migrated_generations_carry_a_valid_digest(tmp_path: Path) -> None:
    python = staged_python(tmp_path)

    def run_for(target: Path):
        def run(command, cwd=None):
            values = tuple(str(value) for value in command)
            if is_browser_install(values):
                install_fake_browser(target)
                return completed()
            if any(Path(value).name == "ldd" for value in values):
                return completed(stdout="ready\n")
            if any("chromium_sandbox=True" in value for value in values):
                return completed()
            raise AssertionError(values)

        return run

    staging = tmp_path / "staging"
    installed = provision_managed_browser(
        python=python,
        active_cache=tmp_path / "active",
        staging_cache=staging,
        legacy_cache=None,
        run=run_for(staging),
    )
    assert installed.status == "installed"
    provisioning_module.verify_generation_digest(staging, installed.version)

    legacy = tmp_path / "legacy"
    legacy.mkdir(mode=0o700)
    install_fake_browser(legacy)
    legacy.joinpath("chromium-1234567", ".dispatch-content-sha256").unlink()
    migrated_staging = tmp_path / "migrated-staging"
    migrated = provision_managed_browser(
        python=python,
        active_cache=tmp_path / "migrated-active",
        staging_cache=migrated_staging,
        legacy_cache=legacy,
        run=run_for(migrated_staging),
    )
    assert migrated.status == "migrated"
    assert provisioning_module.verify_generation_digest(migrated_staging, migrated.version) != ""
