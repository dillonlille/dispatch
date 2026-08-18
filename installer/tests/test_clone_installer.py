from __future__ import annotations

import hashlib
import importlib
import fcntl
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from dispatch_installer.browser_lock import (
    acquire_browser_generation_lock,
    assert_no_unresolved_browser_leases,
)
from dispatch_installer.cli import main as installer_main
from dispatch_installer.layout import (
    InstallLayout,
    InstallerError,
    atomic_json,
    ensure_private_directory,
    installation_lock,
    read_installation,
    read_json,
)
import dispatch_installer.lifecycle as lifecycle_runtime
import dispatch_installer.setup as setup_runtime
browser_lock_runtime = importlib.import_module("dispatch_installer.browser_lock")
cli_runtime = importlib.import_module("dispatch_installer.cli")
layout_runtime = importlib.import_module("dispatch_installer.layout")
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.lifecycle import install_from_clone
from dispatch_installer.repository import (
    REPOSITORY_URL,
    assert_checkout_clean,
    canonical_record_has_remote_authority,
    checkout_existing,
    clone_repository,
    current_commit,
    resolve_latest_release,
    verify_checkout_authority,
)
from dispatch_installer.setup import (
    configure_plugins,
    install_editable_source,
    load_plugin_config,
    migrate_legacy_plugin_config,
)
from dispatch_installer.service import (
    inspect_user_service,
    install_user_service,
    legacy_service_unit,
    legacy_service_unit_is_owned,
    remove_legacy_user_service,
    remove_user_service,
    service_unit,
    service_unit_is_owned,
)
from dispatch_installer.uninstall import plan_uninstall, uninstall
from dispatch_installer.user_command import inspect_user_command, install_user_command, launcher_script

uninstall_runtime = importlib.import_module("dispatch_installer.uninstall")


def completed(*, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), returncode, stdout=stdout, stderr="")


def fake_site_packages(python: Path) -> Path:
    candidates = sorted((python.parent.parent / "lib").glob("python*/site-packages"))
    site_packages = candidates[0] if candidates else python.parent.parent / "lib" / "python-test" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    for directory in (
        python.parent.parent,
        site_packages.parent.parent,
        site_packages.parent,
        site_packages,
    ):
        directory.chmod(0o700)
    playwright = site_packages / "playwright"
    package = playwright / "driver" / "package"
    package.mkdir(parents=True, exist_ok=True)
    (playwright / "__init__.py").write_text("", encoding="utf-8")
    (package / "browsers.json").write_text(
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
    metadata = site_packages / "playwright-1.62.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text("Name: playwright\nVersion: 1.62.0\n", encoding="utf-8")
    return site_packages


def browser_response(command) -> subprocess.CompletedProcess[str] | None:
    values = tuple(str(value) for value in command)
    if any("chromium_sandbox=True" in value for value in values):
        return completed()
    if (
        values
        and "env" in values
        and "-m" in values
        and "playwright" in values
        and "install" in values
        and any(value.startswith("PLAYWRIGHT_BROWSERS_PATH=") for value in values)
    ):
        cache_value = next(value.split("=", 1)[1] for value in values if value.startswith("PLAYWRIGHT_BROWSERS_PATH="))
        executable = Path(cache_value) / "chromium-1234567" / "chrome-linux64" / "chrome"
        executable.parent.mkdir(parents=True, exist_ok=True)
        Path(cache_value).chmod(0o700)
        executable.write_text("chromium", encoding="utf-8")
        executable.chmod(0o700)
        return completed()
    if any(Path(value).name == "ldd" for value in values):
        return completed(stdout="linux-vdso.so.1 => linux-vdso.so.1\n")
    if "install-deps" in values:
        return completed()
    return None


def editable_response(command) -> subprocess.CompletedProcess[str] | None:
    values = tuple(str(value) for value in command)
    if "--editable" not in values:
        return None
    site_packages = fake_site_packages(Path(values[0]))
    (site_packages / "__editable__.test.pth").write_text(values[-1] + "\n", encoding="utf-8")
    return completed()


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def write_browser_manager_project(core: Path) -> None:
    browser_manager = core / "browser_manager"
    browser_manager.mkdir(parents=True)
    for name in ("provisioning.py", "versioning.py"):
        shutil.copyfile(REPOSITORY_ROOT / "dispatch-core" / "browser_manager" / name, browser_manager / name)
    (core / "requirements.txt").write_text("", encoding="utf-8")


def write_test_project(
    source: Path, *, name: str = "dispatch-installer", package: str = "dispatch_installer"
) -> None:
    package_root = source / "src" / package
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[project]\n"
        f"name={name!r}\n"
        "version='1.0.0'\n"
        "dependencies=[]\n"
        "[tool.setuptools]\n"
        "package-dir={\"\"='src'}\n",
        encoding="utf-8",
    )


AUTHORITY_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def authority_response(command) -> subprocess.CompletedProcess[str] | None:
    values = tuple(str(value) for value in command)
    if values[-2:] == ("rev-parse", "--is-shallow-repository"):
        return completed(stdout="false\n")
    if values and values[-1] in {"HEAD", "FETCH_HEAD", "refs/remotes/origin/dev"} and "rev-parse" in values:
        return completed(stdout=f"{AUTHORITY_COMMIT}\n")
    if "symbolic-ref" in values:
        return completed(stdout="dev\n")
    return None


def make_layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return InstallLayout.from_environment({"HOME": str(home)})


def test_layout_has_clone_based_final_roots(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    assert {path.name for path in layout.dispatch_home.iterdir()} == {
        "config", "secrets", "data", "state", "cache", "logs", "run"
    }
    assert layout.clone == layout.dispatch_home / "dispatch"
    assert layout.installation_record.name == "installation.json"


def test_custom_private_roots_project_consistently_into_launcher_and_service(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = {"HOME": str(home)}
    for name, variable in (
        ("config", "DISPATCH_CONFIG_ROOT"),
        ("secrets", "DISPATCH_SECRETS_ROOT"),
        ("data", "DISPATCH_DATA_ROOT"),
        ("state", "DISPATCH_STATE_ROOT"),
        ("cache", "DISPATCH_CACHE_ROOT"),
        ("logs", "DISPATCH_LOGS_ROOT"),
        ("runtime", "DISPATCH_RUNTIME_ROOT"),
    ):
        environment[variable] = str(home / "private-roots" / name)
    layout = InstallLayout.from_environment(environment)
    layout.prepare()
    launcher = launcher_script(layout).decode("utf-8")
    for variable, value in environment.items():
        if variable != "HOME":
            assert f"export {variable}={value}" in launcher
    assert layout.browser_cache == home / "private-roots" / "cache" / "browser-manager" / "playwright"
    assert str(layout.browser_cache) in service_unit(layout).decode("utf-8")


def test_layout_rejects_symlinked_dispatch_home_ancestor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    real = tmp_path / "real"
    real.mkdir()
    (home / "linked").symlink_to(real, target_is_directory=True)
    with pytest.raises(InstallerError) as error:
        InstallLayout.from_environment(
            {"HOME": str(home), "DISPATCH_HOME": str(home / "linked" / "dispatch")}
        )
    assert error.value.code == "path_symlink"


def test_layout_rejects_control_characters_in_configured_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with pytest.raises(InstallerError) as error:
        InstallLayout.from_environment(
            {"HOME": str(home), "DISPATCH_HOME": str(home / "bad\nroot")}
        )
    assert error.value.code == "path_control_character"


def test_python_layout_rejects_writable_home_ancestor_and_staging(tmp_path: Path) -> None:
    writable_home = tmp_path / "writable-home"
    writable_home.mkdir(mode=0o700)
    writable_home.chmod(0o777)
    with pytest.raises(InstallerError) as home_error:
        InstallLayout.from_environment({"HOME": str(writable_home)})
    assert home_error.value.code == "directory_unsafe"
    writable_home.chmod(0o700)

    home = tmp_path / "safe-home"
    home.mkdir(mode=0o700)
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    with pytest.raises(InstallerError) as ancestor_error:
        InstallLayout.from_environment(
            {"HOME": str(home), "DISPATCH_HOME": str(unsafe_parent / "dispatch")}
        )
    assert ancestor_error.value.code == "directory_unsafe"
    unsafe_parent.chmod(0o700)

    layout = InstallLayout.from_environment({"HOME": str(home)})
    layout.prepare()
    staging = layout.dispatch_home / ".install-tmp"
    staging.mkdir(mode=0o700)
    staging.chmod(0o777)
    with pytest.raises(InstallerError) as staging_error:
        lifecycle_runtime._prepare_temporary_root(layout)
    assert staging_error.value.code == "staging_unsafe"
    assert stat.S_IMODE(staging.stat().st_mode) == 0o777
    staging.chmod(0o700)


def test_browser_provisioner_loader_rejects_symlinked_checkout_ancestor(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    outside = tmp_path / "outside-browser-manager"
    outside.mkdir()
    marker = outside / "executed"
    (outside / "provisioning.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    dispatch_core = layout.clone / "dispatch-core"
    dispatch_core.mkdir()
    (dispatch_core / "browser_manager").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallerError) as error:
        getattr(lifecycle_runtime, "_load_browser_provisioning")(layout)
    assert error.value.code == "browser_provisioner_unsafe"
    assert not marker.exists()


def test_installer_generation_lock_refuses_active_shared_browser_lease(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    locks = layout.state / "browser-manager"
    ensure_private_directory(locks, "Browser Manager state")
    lock_path = locks / "generation.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(InstallerError) as error:
            acquire_browser_generation_lock(layout)
        assert error.value.code == "browser_generation_busy"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_generation_lock_rejects_parent_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    locks = layout.state / "browser-manager"
    locks.mkdir(mode=0o700)
    displaced = layout.state / "browser-manager-original"
    outside = tmp_path / "outside-locks"
    outside.mkdir(mode=0o700)
    original_open = browser_lock_runtime.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "generation.lock" and dir_fd is not None and not swapped:
            locks.rename(displaced)
            locks.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(browser_lock_runtime.os, "open", racing_open)
    with pytest.raises(InstallerError) as error:
        acquire_browser_generation_lock(layout)
    assert error.value.code == "browser_generation_lock_unsafe"
    assert not (outside / "generation.lock").exists()


def test_durable_quarantined_lease_blocks_generation_mutation_without_live_lock(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    database = layout.data / "db" / "browser-manager" / "browser-manager.sqlite3"
    database.parent.mkdir(parents=True)
    database.parent.chmod(0o700)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE leases (lease_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        connection.execute("INSERT INTO leases VALUES (?, ?)", ("a" * 32, "quarantined"))
        connection.commit()
    finally:
        connection.close()
    database.chmod(0o600)

    with pytest.raises(InstallerError) as error:
        assert_no_unresolved_browser_leases(layout)
    assert error.value.code == "browser_reconciliation_required"


def test_release_resolver_ignores_drafts_and_prereleases() -> None:
    payload = [
        {"tag_name": "v-old", "draft": False, "prerelease": False, "published_at": "2025-01-01T00:00:00Z"},
        {"tag_name": "v-preview", "draft": False, "prerelease": True, "published_at": "2027-01-01T00:00:00Z"},
        {"tag_name": "v-draft", "draft": True, "prerelease": False, "published_at": "2028-01-01T00:00:00Z"},
        {"tag_name": "v-new", "draft": False, "prerelease": False, "published_at": "2026-01-01T00:00:00Z"},
    ]
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(payload).encode()
    assert resolve_latest_release(opener=Mock(return_value=response)) == "v-new"


def test_remote_authority_binds_dev_and_stable_records() -> None:
    commit = "a" * 40

    def response(payload):
        value = Mock()
        value.__enter__ = Mock(return_value=value)
        value.__exit__ = Mock(return_value=False)
        value.read.return_value = json.dumps(payload).encode()
        return value

    dev_record: dict[str, object] = {"channel": "dev", "ref": "dev", "commit": commit}
    dev_opener = Mock(
        return_value=response(
            {
                "status": "ahead",
                "base_commit": {"sha": commit},
                "merge_base_commit": {"sha": commit},
            }
        )
    )
    assert canonical_record_has_remote_authority(dev_record, opener=dev_opener) is True

    release = {
        "tag_name": "v1.2.3",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-08-16T00:00:00Z",
    }

    def stable_opener(request, timeout=20):
        assert timeout == 20
        if "/releases" in request.full_url:
            return response([release])
        return response({"sha": commit})

    stable_record: dict[str, object] = {"channel": "stable", "ref": "v1.2.3", "commit": commit}
    assert canonical_record_has_remote_authority(stable_record, opener=stable_opener) is True


def test_json_invalid_command_returns_one_error_document(capsys: pytest.CaptureFixture[str]) -> None:
    result = installer_main(["--json", "invalid-command"])
    assert result == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "arguments_invalid"


def test_cli_reports_committed_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_runtime,
        "_lifecycle",
        lambda *_args, **_kwargs: {
            "status": "installed_cleanup_incomplete",
            "cleanup_error_code": "post_activation_cleanup_failed",
        },
    )
    result = installer_main(["--dispatch-home", str(tmp_path / "dispatch"), "install", "--yes"])
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["status"] == "installed_cleanup_incomplete"
    assert payload["error"]["code"] == "post_activation_cleanup_failed"


def test_update_refuses_missing_installation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = installer_main(["--dispatch-home", str(tmp_path / "dispatch"), "update"])
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "installation_missing"


def test_clone_fetches_published_tag_explicitly_and_detaches_stable(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(command))
        return completed()

    destination = tmp_path / "clone"
    clone_repository(destination, channel="stable", ref="v1.2.3", run=fake_run)
    assert commands[0][0:5] == ("git", "clone", "--no-checkout", "--depth", "1")
    assert commands[1][-3:] == ("origin", "tag", "v1.2.3")
    assert commands[2][-3:] == ("checkout", "--detach", "refs/tags/v1.2.3")


def test_dev_clone_is_complete_and_update_is_fast_forward_only(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(command))
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    destination = tmp_path / "clone"
    clone_repository(destination, channel="dev", ref="dev", run=fake_run)
    assert "--depth" not in commands[0]
    (destination / ".git").mkdir(parents=True)
    checkout_existing(destination, channel="dev", ref="dev", run=fake_run)
    assert any(
        command[-1] == "refs/heads/dev:refs/remotes/origin/dev" for command in commands
    )
    assert any(REPOSITORY_URL in command for command in commands if "fetch" in command)
    assert any(command[-3:] == ("merge", "--ff-only", "origin/dev") for command in commands)

    commands.clear()
    checkout_existing(destination, channel="stable", ref="1.2.3", run=fake_run)
    assert any(command[-3:] == (REPOSITORY_URL, "tag", "1.2.3") for command in commands)
    assert any(command[-2:] == ("--detach", "refs/tags/1.2.3") for command in commands)


def test_checkout_clean_rejects_ignored_files(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    subprocess.run(("git", "init", "-q", "-b", "dev", str(clone)), check=True)
    subprocess.run(("git", "-C", str(clone), "config", "user.email", "tests@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(clone), "config", "user.name", "Dispatch Tests"), check=True)
    (clone / ".gitignore").write_text("*.egg-info/\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(clone), "add", ".gitignore"), check=True)
    subprocess.run(("git", "-C", str(clone), "commit", "-m", "base"), check=True, capture_output=True)

    assert_checkout_clean(clone)
    metadata = clone / "generated.egg-info"
    metadata.mkdir()
    (metadata / "PKG-INFO").write_text("generated", encoding="utf-8")
    with pytest.raises(InstallerError) as error:
        assert_checkout_clean(clone)
    assert error.value.code == "clone_dirty"


def test_real_git_switches_shallow_stable_clone_to_tracking_dev(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    subprocess.run(("git", "init", "-b", "main", str(upstream)), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(upstream), "config", "user.name", "Dispatch Test"), check=True)
    subprocess.run(("git", "-C", str(upstream), "config", "user.email", "dispatch@example.invalid"), check=True)
    (upstream / "source.txt").write_text("base\n")
    subprocess.run(("git", "-C", str(upstream), "add", "source.txt"), check=True)
    subprocess.run(("git", "-C", str(upstream), "commit", "-m", "base"), check=True, capture_output=True)
    (upstream / "source.txt").write_text("main\n")
    subprocess.run(("git", "-C", str(upstream), "commit", "-am", "main"), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(upstream), "tag", "1.0.0"), check=True)
    subprocess.run(("git", "-C", str(upstream), "checkout", "-b", "dev"), check=True, capture_output=True)
    (upstream / "source.txt").write_text("dev\n")
    subprocess.run(("git", "-C", str(upstream), "commit", "-am", "dev"), check=True, capture_output=True)
    expected = subprocess.run(
        ("git", "-C", str(upstream), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    clone = tmp_path / "clone"
    subprocess.run(
        (
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--depth",
            "1",
            "--branch",
            "main",
            upstream.as_uri(),
            str(clone),
        ),
        check=True,
    )
    subprocess.run(("git", "-C", str(clone), "fetch", "--depth", "1", "origin", "tag", "1.0.0"), check=True)
    subprocess.run(("git", "-C", str(clone), "checkout", "--detach", "refs/tags/1.0.0"), check=True)
    checkout_existing(clone, channel="dev", ref="dev", repository_url=upstream.as_uri())

    branch = subprocess.run(
        ("git", "-C", str(clone), "branch", "--show-current"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "dev"
    assert current_commit(clone) == expected
    history_count = subprocess.run(
        ("git", "-C", str(clone), "rev-list", "--count", "dev"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert history_count == "3"

    (clone / "local.txt").write_text("local")
    subprocess.run(("git", "-C", str(clone), "add", "local.txt"), check=True, capture_output=True)
    subprocess.run(
        ("git", "-C", str(clone), "-c", "user.name=Dispatch Test", "-c", "user.email=test@example.invalid", "commit", "-m", "local"),
        check=True,
        capture_output=True,
    )
    with pytest.raises(InstallerError, match="local commits"):
        checkout_existing(clone, channel="dev", ref="dev", repository_url=upstream.as_uri())


def test_staged_dev_checkout_must_match_github_fetch_head(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[-2:] == ("rev-parse", "--is-shallow-repository"):
            return completed(stdout="false\n")
        if values[-2:] == ("rev-parse", "HEAD"):
            return completed(stdout=f"{AUTHORITY_COMMIT}\n")
        if values[-2:] == ("rev-parse", "FETCH_HEAD"):
            return completed(stdout=f"{'f' * 40}\n")
        if "symbolic-ref" in values:
            return completed(stdout="dev\n")
        return completed()

    with pytest.raises(InstallerError, match="exactly track"):
        verify_checkout_authority(clone, channel="dev", ref="dev", run=fake_run)


def test_install_from_staged_clone_writes_atomic_record(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    legacy_marker = layout.legacy_browser_cache / "unrelated-marker"
    legacy_marker.parent.mkdir(mode=0o700)
    legacy_marker.write_text("preserve", encoding="utf-8")
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python")
    layout.venv_python.chmod(0o700)

    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(str(value) for value in command))
        if tuple(command[1:3]) == ("-m", "venv"):
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python")
            python.chmod(0o700)
            fake_site_packages(python)
        response = authority_response(command)
        if response is not None:
            return response
        return browser_response(command) or editable_response(command) or completed()

    result = install_from_clone(
        layout,
        source,
        channel="dev",
        ref="dev",
        run=fake_run,
    )
    assert result["status"] == "installed"
    record = read_installation(layout)
    assert record is not None
    assert record["channel"] == "dev"
    assert record["ref"] == "dev"
    assert str(record["commit"]).startswith("01234567")
    assert layout.clone.is_dir()
    assert layout.command_path.is_file()
    browser_result = result["browser"]
    assert isinstance(browser_result, dict)
    assert browser_result["status"] == "installed"
    assert browser_result["chromium_revision"] == "1234567"
    browser_record = read_json(layout.browser_installation_record)
    assert browser_record["cache"] == str(layout.browser_cache)
    assert browser_record["playwright_version"] == "1.62.0"
    assert browser_record["contains_secrets"] is False
    assert (layout.browser_cache / "chromium-1234567" / "chrome-linux64" / "chrome").is_file()
    assert legacy_marker.read_text(encoding="utf-8") == "preserve"
    assert not any("--editable" in command for command in commands)
    assert not any("install-deps" in command for command in commands)
    assert any(any("chromium_sandbox=True" in value for value in command) for command in commands)
    assert (fake_site_packages(layout.venv_python) / "__dispatch__.dispatch_installer.pth").is_file()


def test_setup_installs_selected_plugin_editable_and_writes_config(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = layout.clone / "plugins" / "handbook"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        "[project]\nname='handbook'\nversion='1'\n[project.entry-points.\"dispatch.plugins\"]\nhandbook='x:y'\n[tool.dispatch]\nid='handbook'\ncapabilities=['read_local_data']\n"
    )
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python")
    fake_site_packages(layout.venv_python)
    calls: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        calls.append(tuple(str(value) for value in command))
        return browser_response(command) or editable_response(command) or completed()

    result = configure_plugins(layout, ["handbook"], run=fake_run)
    config = json.loads((layout.config / "plugins.json").read_text())
    assert result["selected_plugins"] == ["handbook"]
    assert config["plugins"][0]["id"] == "handbook"
    assert config["status"] == "complete"
    assert config["selected_plugins"] == ["handbook"]
    assert config["plugins"][0]["capabilities"] == ["read_local_data"]
    assert calls == []
    assert (fake_site_packages(layout.venv_python) / "__dispatch__.handbook.pth").is_file()


def test_legacy_plugin_selection_migrates_only_when_complete(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    plugin = layout.clone / "plugins" / "handbook"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        "[project]\nname='handbook'\nversion='1'\n"
        "[project.entry-points.\"dispatch.plugins\"]\nhandbook='x:y'\n"
        "[tool.dispatch]\nid='handbook'\ncapabilities=['read_local_data']\n"
    )
    legacy = layout.state / "install" / "setup.json"
    ensure_private_directory(legacy.parent, "service directory")
    legacy.parent.chmod(0o700)
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "selected_plugins": ["handbook"],
                "contains_secrets": False,
            }
        )
    )
    legacy.chmod(0o600)
    assert migrate_legacy_plugin_config(layout) is False
    assert not (layout.config / "plugins.json").exists()
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "product_version": "0.0.7",
                "selected_plugins": ["handbook"],
                "plugins": [
                    {
                        "id": "handbook",
                        "package": "dispatch-handbook",
                        "version": "1",
                        "release_id": "legacy-release",
                        "site_packages": "/legacy/site-packages",
                        "capabilities": ["read_local_data"],
                    }
                ],
                "contains_secrets": False,
            }
        )
    )
    assert migrate_legacy_plugin_config(layout) is True
    migrated = json.loads((layout.config / "plugins.json").read_text())
    assert migrated["selected_plugins"] == ["handbook"]
    assert migrated["plugins"][0]["capabilities"] == ["read_local_data"]


def test_plugin_config_rejects_symlink_and_unowned_service_before_mutation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    outside = tmp_path / "outside-plugins.json"
    outside.write_text('{"schema_version":1,"contains_secrets":false}')
    outside.chmod(0o600)
    config = layout.config / "plugins.json"
    config.symlink_to(outside)
    with pytest.raises(InstallerError) as load_error:
        load_plugin_config(layout)
    assert load_error.value.code == "plugin_config_invalid"
    config.unlink()

    ensure_private_directory(layout.service_directory, "service directory")
    layout.service_path.write_text("unrelated")
    layout.service_path.chmod(0o600)
    with pytest.raises(InstallerError) as setup_error:
        configure_plugins(layout, [], run=lambda *_: completed())
    assert setup_error.value.code == "service_unit_unsafe"
    assert not config.exists()


def test_failed_activation_restores_prior_checkout_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    (layout.clone / "old.txt").write_text("old")
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("old-python")
    old_browser = layout.cache / "browser" / "old-browser"
    old_browser.parent.mkdir()
    old_browser.parent.chmod(0o700)
    old_browser.write_text("old-browser")

    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    (source / "new.txt").write_text("new")

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("new-python")
            python.chmod(0o700)
            fake_site_packages(python)
        if values == ("systemctl", "--user", "restart", "dispatch.service"):
            return completed(returncode=1)
        if values == ("systemctl", "--user", "disable", "--now", "dispatch.service"):
            return completed(returncode=1)
        response = authority_response(command)
        if response is not None:
            return response
        return browser_response(command) or editable_response(command) or completed()

    real_restore = lifecycle_runtime._restore_directory
    interrupted: set[Path] = set()

    def interrupt_rollback_once(path: Path, backup: Path | None) -> None:
        if path in {layout.venv, layout.clone} and path not in interrupted:
            interrupted.add(path)
            raise KeyboardInterrupt
        real_restore(path, backup)

    monkeypatch.setattr(lifecycle_runtime, "_restore_directory", interrupt_rollback_once)

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert error.value.code == "activation_rollback_failed"
    assert (layout.clone / "old.txt").read_text() == "old"
    assert not (layout.clone / "new.txt").exists()
    assert layout.venv_python.read_text() == "old-python"
    assert old_browser.read_text() == "old-browser"
    assert not layout.browser_cache.exists()
    assert not layout.browser_installation_record.exists()
    assert not layout.installation_record.exists()
    assert not layout.command_path.exists()
    assert not layout.service_path.exists()
    assert interrupted == {layout.venv, layout.clone}


def test_post_return_browser_swap_interrupt_restores_active_generation_and_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    (layout.clone / "old.txt").write_text("old", encoding="utf-8")
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("old-python", encoding="utf-8")
    old_browser = layout.browser_cache / "chromium-9999999" / "chrome-linux64" / "chrome"
    old_browser.parent.mkdir(parents=True)
    layout.browser_manager_cache.chmod(0o700)
    layout.browser_cache.chmod(0o700)
    old_browser.write_text("old-browser", encoding="utf-8")
    old_browser.chmod(0o700)
    old_record = {
        "schema_version": 1,
        "status": "active",
        "playwright_version": "1.61.0",
        "browser_family": "chromium",
        "chromium_revision": "9999999",
        "chromium_version": "150.0.0.0",
        "cache": str(layout.browser_cache),
        "contains_secrets": False,
    }
    atomic_json(layout.browser_installation_record, old_record)

    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    (source / "new.txt").write_text("new", encoding="utf-8")

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("new-python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        response = authority_response(command)
        if response is not None:
            return response
        return browser_response(command) or editable_response(command) or completed()

    original_swap = lifecycle_runtime._swap_directory

    def interrupt_after_browser_return(replacement, target, *, state=None):
        result = original_swap(replacement, target, state=state)
        if target == layout.browser_cache:
            raise KeyboardInterrupt("post-browser-return")
        return result

    monkeypatch.setattr(lifecycle_runtime, "_swap_directory", interrupt_after_browser_return)
    with pytest.raises(KeyboardInterrupt):
        install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert (layout.clone / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (layout.clone / "new.txt").exists()
    assert layout.venv_python.read_text(encoding="utf-8") == "old-python"
    assert old_browser.read_text(encoding="utf-8") == "old-browser"
    assert not (layout.browser_cache / "chromium-1234567").exists()
    assert read_json(layout.browser_installation_record) == old_record


def test_update_dirty_preflight_never_runs_destructive_rollback(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    user_file = layout.clone / "user.ignored"
    user_file.write_text("preserve", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if "status" in values:
            return completed(stdout="!! user.ignored\n")
        return completed()

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._update_existing(
            layout,
            channel="dev",
            ref="dev",
            run=fake_run,
            now=lambda: datetime(2026, 8, 16, tzinfo=UTC),
        )
    assert error.value.code == "clone_dirty"
    assert user_file.read_text(encoding="utf-8") == "preserve"
    assert not any(command[:2] == ("git", "clean") for command in commands)
    assert not any(command[:3] == ("git", "reset", "--hard") for command in commands)


def test_update_post_gate_file_is_preserved_and_fails_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    user_file = layout.clone / "arrived-after-preflight"
    old_commit = "1" * 40
    status_calls = 0

    def fail_checkout(*_args, **_kwargs) -> None:
        user_file.write_text("preserve", encoding="utf-8")
        raise InstallerError("injected_update_failure", "injected")

    def fake_run(command, cwd=None):
        nonlocal status_calls
        values = tuple(str(value) for value in command)
        if "status" in values:
            status_calls += 1
            return completed(stdout="" if status_calls == 1 else "?? arrived-after-preflight\n")
        if values[-2:] == ("rev-parse", "HEAD"):
            return completed(stdout=old_commit + "\n")
        return completed()

    monkeypatch.setattr(lifecycle_runtime, "checkout_existing", fail_checkout)
    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._update_existing(
            layout,
            channel="dev",
            ref="dev",
            run=fake_run,
            now=lambda: datetime(2026, 8, 16, tzinfo=UTC),
        )
    assert error.value.code == "checkout_rollback_failed"
    assert user_file.read_text(encoding="utf-8") == "preserve"


def test_install_caller_restores_checkout_after_post_return_swap_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    (layout.clone / "marker").write_text("old", encoding="utf-8")
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    (source / "marker").write_text("new", encoding="utf-8")
    original = lifecycle_runtime._swap_directory

    def interrupt_after_return(replacement, target, *, state=None):
        result = original(replacement, target, state=state)
        if target == layout.clone:
            raise KeyboardInterrupt("post-return")
        return result

    monkeypatch.setattr(lifecycle_runtime, "_swap_directory", interrupt_after_return)
    with pytest.raises(KeyboardInterrupt):
        install_from_clone(layout, source, channel="dev", ref="dev", run=lambda *_args, **_kwargs: completed())
    assert (layout.clone / "marker").read_text(encoding="utf-8") == "old"
    assert not list(layout.dispatch_home.glob(".dispatch.previous-*"))


def test_swap_directory_restores_active_generation_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0
    interruption = KeyboardInterrupt("swap")

    def interrupt_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interruption
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle_runtime.os, "replace", interrupt_second_replace)
    with pytest.raises(KeyboardInterrupt) as error:
        lifecycle_runtime._swap_directory(replacement, target)
    assert error.value is interruption
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not replacement.exists()


def test_swap_directory_recovers_post_mutation_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0
    interruption = KeyboardInterrupt("post-mutation")

    def interrupt_after_promotion(source, destination):
        nonlocal calls
        calls += 1
        result = real_replace(source, destination)
        if calls == 2:
            raise interruption
        return result

    monkeypatch.setattr(lifecycle_runtime.os, "replace", interrupt_after_promotion)
    with pytest.raises(KeyboardInterrupt) as error:
        lifecycle_runtime._swap_directory(replacement, target)
    assert error.value is interruption
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not replacement.exists()
    assert not list(tmp_path.glob(".active.previous-*"))


def test_swap_directory_recovers_post_mutation_active_backup_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    interruption = KeyboardInterrupt("after active backup")
    real_replace = os.replace
    calls = 0

    def interrupt_after_first_replace(source, destination):
        nonlocal calls
        calls += 1
        result = real_replace(source, destination)
        if calls == 1:
            raise interruption
        return result

    monkeypatch.setattr(lifecycle_runtime.os, "replace", interrupt_after_first_replace)
    with pytest.raises(KeyboardInterrupt) as error:
        lifecycle_runtime._swap_directory(replacement, target)
    assert error.value is interruption
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not replacement.exists()
    assert not list(tmp_path.glob(".active.previous-*"))


def test_swap_directory_retries_post_mutation_rollback_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_after_first_rollback_move(source, destination):
        nonlocal calls
        calls += 1
        result = real_replace(source, destination)
        if calls == 3:
            raise OSError("post-mutation rollback failure")
        return result

    real_chmod = Path.chmod

    def fail_chmod(path: Path, mode: int) -> None:
        real_chmod(path, mode)
        if path == target:
            raise OSError("post-promotion failure")

    monkeypatch.setattr(lifecycle_runtime.os, "replace", fail_after_first_rollback_move)
    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(OSError, match="post-promotion failure"):
        lifecycle_runtime._swap_directory(replacement, target)
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not replacement.exists()
    assert not list(tmp_path.glob(".active.previous-*"))


def test_swap_directory_persistent_rollback_failure_keeps_active_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_rollback_before_mutation(source, destination):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise OSError("persistent rollback failure")
        return real_replace(source, destination)

    real_chmod = Path.chmod

    def fail_chmod(path: Path, mode: int) -> None:
        real_chmod(path, mode)
        if path == target:
            raise OSError("post-promotion failure")

    monkeypatch.setattr(lifecycle_runtime.os, "replace", fail_rollback_before_mutation)
    monkeypatch.setattr(Path, "chmod", fail_chmod)
    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._swap_directory(replacement, target)
    assert error.value.code == "directory_swap_rollback_failed"
    assert (target / "marker").read_text(encoding="utf-8") == "new"
    assert any((path / "marker").read_text(encoding="utf-8") == "old" for path in tmp_path.glob(".active.previous-*"))


def test_complete_rollback_bounds_persistent_terminal_interrupt() -> None:
    attempts = 0

    def interrupt() -> None:
        nonlocal attempts
        attempts += 1
        raise KeyboardInterrupt("persistent")

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._complete_rollback(interrupt)
    assert error.value.code == "rollback_persistently_interrupted"
    assert attempts == 3


def test_restore_directory_uses_rename_fallback_when_replace_and_copy_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "marker").write_text("old", encoding="utf-8")

    monkeypatch.setattr(lifecycle_runtime.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))
    monkeypatch.setattr(
        lifecycle_runtime.shutil,
        "copytree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy")),
    )
    with pytest.raises(OSError, match="replace"):
        lifecycle_runtime._restore_directory(target, backup)
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not backup.exists()


def test_restore_directory_preserves_displaced_generation_and_user_file(tmp_path: Path) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (target / "user-file").write_text("preserve", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")

    lifecycle_runtime._restore_directory(target, backup)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    displaced = next(tmp_path.glob(".active.failed-*"))
    assert (displaced / "marker").read_text(encoding="utf-8") == "new"
    assert (displaced / "user-file").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("post_mutation", [False, True])
def test_restore_directory_failed_displaced_move_keeps_active_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, post_mutation: bool
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_displaced_move(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            if post_mutation:
                real_replace(source, destination)
            raise OSError("displaced move failed")
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle_runtime.os, "replace", fail_displaced_move)
    with pytest.raises(OSError):
        lifecycle_runtime._restore_directory(target, backup)
    assert target.is_dir()
    assert (target / "marker").read_text(encoding="utf-8") == "old"


def test_restore_directory_exchange_interruption_never_removes_active_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")
    interruption = KeyboardInterrupt("post-exchange")
    real_exchange = getattr(lifecycle_runtime, "_exchange_directories")

    def interrupt_after_exchange(left: Path, right: Path) -> None:
        real_exchange(left, right)
        raise interruption

    monkeypatch.setattr(lifecycle_runtime, "_exchange_directories", interrupt_after_exchange)
    with pytest.raises(KeyboardInterrupt) as error:
        lifecycle_runtime._restore_directory(target, backup)
    assert error.value is interruption
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    displaced = next(tmp_path.glob(".active.failed-*"))
    assert (displaced / "marker").read_text(encoding="utf-8") == "new"


def test_restore_directory_retry_after_exchange_interruption_keeps_prior_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")
    real_exchange = getattr(lifecycle_runtime, "_exchange_directories")
    calls = 0

    def interrupt_after_exchange(left: Path, right: Path) -> None:
        nonlocal calls
        calls += 1
        real_exchange(left, right)
        raise KeyboardInterrupt("post-exchange")

    monkeypatch.setattr(lifecycle_runtime, "_exchange_directories", interrupt_after_exchange)
    lifecycle_runtime._complete_rollback(
        lambda: lifecycle_runtime._restore_directory(target, backup)
    )
    assert calls == 1
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    displaced = next(tmp_path.glob(".active.failed-*"))
    assert (displaced / "marker").read_text(encoding="utf-8") == "new"


def test_restore_directory_persistent_cleanup_failure_keeps_active_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / "backup"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (target / "user-file").write_text("preserve", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")

    def fail_every_cleanup_move(_source, _destination):
        raise OSError("persistent cleanup failure")

    monkeypatch.setattr(lifecycle_runtime.os, "replace", fail_every_cleanup_move)
    with pytest.raises(OSError):
        lifecycle_runtime._restore_directory(target, backup)
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (backup / "marker").read_text(encoding="utf-8") == "new"
    assert (backup / "user-file").read_text(encoding="utf-8") == "preserve"


def test_update_checkout_rollback_defers_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    marker = layout.clone / "generation.txt"
    marker.write_text("old")
    old_commit = "1" * 40
    rollback_attempts = 0
    clean_commands: list[tuple[str, ...]] = []

    def fail_checkout(*_args, **_kwargs) -> None:
        marker.write_text("new")
        raise InstallerError("injected_update_failure", "injected")

    def fake_run(command, cwd=None):
        nonlocal rollback_attempts
        values = tuple(str(value) for value in command)
        if values[-2:] == ("rev-parse", "HEAD"):
            return completed(stdout=old_commit + "\n")
        if values[:3] == ("git", "reset", "--hard"):
            rollback_attempts += 1
            if rollback_attempts == 1:
                raise KeyboardInterrupt
            marker.write_text("old")
            return completed()
        if values[:2] == ("git", "clean"):
            clean_commands.append(values)
        return completed()

    monkeypatch.setattr(lifecycle_runtime, "checkout_existing", fail_checkout)
    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._update_existing(
            layout,
            channel="dev",
            ref="dev",
            run=fake_run,
            now=lambda: datetime(2026, 8, 16, tzinfo=UTC),
        )
    assert error.value.code == "injected_update_failure"
    assert rollback_attempts == 2
    assert clean_commands == []
    assert marker.read_text() == "old"


def test_activation_conflict_never_disables_unrelated_service(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    ensure_private_directory(layout.service_directory, "service directory")
    layout.service_path.write_text("[Service]\nExecStart=/usr/bin/unrelated\n")
    layout.service_path.chmod(0o600)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python")
            python.chmod(0o700)
            fake_site_packages(python)
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert error.value.code == "service_conflict"
    assert not any(command[:4] == ("systemctl", "--user", "disable", "--now") for command in commands)
    assert "unrelated" in layout.service_path.read_text()


def test_activation_never_enables_unowned_legacy_service(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    legacy = layout.service_directory / "dispatch-core.service"
    ensure_private_directory(legacy.parent, "service directory")
    legacy.write_text("[Service]\nExecStart=/usr/bin/unrelated\n")
    legacy.chmod(0o600)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(str(value) for value in command))
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert error.value.code == "legacy_service_unsafe"
    assert not any(command[:4] == ("systemctl", "--user", "enable", "--now") for command in commands)
    assert legacy.exists()


def test_post_activation_cleanup_failure_is_reported_without_checkout_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    legacy = layout.service_directory / "dispatch-core.service"
    ensure_private_directory(legacy.parent, "service directory")
    legacy.write_text("owned legacy", encoding="utf-8")
    legacy.chmod(0o600)

    monkeypatch.setattr(lifecycle_runtime, "legacy_service_unit_is_owned", lambda _layout: True)
    monkeypatch.setattr(lifecycle_runtime, "stop_legacy_user_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        lifecycle_runtime,
        "remove_legacy_user_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(InstallerError("legacy_service_reload_failed", "synthetic")),
    )

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    result = install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert result["status"] == "installed_cleanup_incomplete"
    assert result["cleanup_error_code"] == "post_activation_cleanup_failed"
    assert layout.clone.is_dir()
    assert layout.venv_python.is_file()
    assert legacy.exists()


def test_repository_staging_runs_inside_installation_root_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    observed: list[bool] = []

    def inspect_stage(*_args, **_kwargs):
        observed.append(layout_runtime._ACTIVE_INSTALLATION_ROOT.get() is not None)
        raise RuntimeError("stage inspected")

    monkeypatch.setattr(lifecycle_runtime, "_stage_repository", inspect_stage)
    with pytest.raises(RuntimeError, match="stage inspected"):
        lifecycle_runtime.install_or_update(layout, channel="dev")
    assert observed == [True]


def test_update_keyboard_interrupt_restores_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    marker = layout.clone / "marker"
    marker.write_text("old")

    def interrupted_checkout(*args, **kwargs):
        marker.write_text("new")
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle_runtime, "checkout_existing", interrupted_checkout)

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[-2:] == ("rev-parse", "HEAD"):
            return completed(stdout=f"{AUTHORITY_COMMIT}\n")
        if "reset" in values and "--hard" in values:
            marker.write_text("old")
        return completed()

    with pytest.raises(KeyboardInterrupt):
        lifecycle_runtime._update_existing(
            layout,
            channel="dev",
            ref="dev",
            run=fake_run,
            now=lambda: lifecycle_runtime.datetime.now(lifecycle_runtime.UTC),
        )
    assert marker.read_text() == "old"


def test_update_root_swap_does_not_run_rollback_against_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    displaced = layout.dispatch_home.parent / "dispatch-update-original"
    outside = tmp_path / "outside-update"
    (outside / "dispatch").mkdir(parents=True)
    commands: list[tuple[str, ...]] = []
    working_directories: list[Path | None] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        working_directories.append(cwd)
        if values[-2:] == ("rev-parse", "HEAD"):
            return completed(stdout=f"{AUTHORITY_COMMIT}\n")
        return completed()

    def swap_checkout(*_args, **_kwargs):
        layout.dispatch_home.rename(displaced)
        layout.dispatch_home.symlink_to(outside, target_is_directory=True)
        raise KeyboardInterrupt("checkout interrupted")

    monkeypatch.setattr(lifecycle_runtime, "checkout_existing", swap_checkout)
    with pytest.raises(KeyboardInterrupt):
        lifecycle_runtime._update_existing(
            layout,
            channel="dev",
            ref="dev",
            run=fake_run,
            now=lambda: datetime(2026, 8, 14, tzinfo=UTC),
        )
    assert any("reset" in command for command in commands)
    reset_cwds = [cwd for command, cwd in zip(commands, working_directories, strict=True) if "reset" in command]
    assert reset_cwds and all(cwd is not None and str(cwd).startswith("/proc/") for cwd in reset_cwds)
    assert not (outside / "rollback-marker").exists()


def test_repair_revalidates_github_authority_and_recorded_commit(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / ".git").mkdir(parents=True)
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": "dev",
            "commit": "f" * 40,
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-16T00:00:00Z",
            "contains_secrets": False,
        },
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime.repair_existing(layout, run=fake_run)
    assert error.value.code == "installation_commit_mismatch"
    assert any(REPOSITORY_URL in command for command in commands if "fetch" in command)
    assert not any("pip" in command for command in commands)


def test_uninstall_preserves_user_data_and_purge_removes_root(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    for name in ("config", "secrets", "data", "state", "logs"):
        (getattr(layout, name) / "keep.txt").write_text(name)
    layout.clone.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "dev", str(layout.clone)), check=True)
    subprocess.run(("git", "-C", str(layout.clone), "remote", "add", "origin", REPOSITORY_URL), check=True)
    subprocess.run(("git", "-C", str(layout.clone), "config", "user.email", "tests@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(layout.clone), "config", "user.name", "Dispatch Tests"), check=True)
    (layout.clone / "managed.txt").write_text("managed")
    subprocess.run(("git", "-C", str(layout.clone), "add", "managed.txt"), check=True)
    subprocess.run(("git", "-C", str(layout.clone), "commit", "-q", "-m", "managed"), check=True)
    commit = subprocess.run(
        ("git", "-C", str(layout.clone), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ("git", "-C", str(layout.clone), "update-ref", "refs/remotes/origin/dev", commit),
        check=True,
    )
    (layout.venv / "bin").mkdir(parents=True)
    (layout.venv_python).write_text("python")
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": "dev",
            "commit": commit,
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-16T00:00:00Z",
            "contains_secrets": False,
        },
    )
    atomic_json(
        layout.browser_installation_record,
        {
            "schema_version": 1,
            "status": "active",
            "playwright_version": "1.62.0",
            "browser_family": "chromium",
            "chromium_revision": "1234567",
            "chromium_version": "151.0.7922.34",
            "cache": str(layout.browser_cache),
            "contains_secrets": False,
        },
    )
    install_user_command(layout)
    previous_launcher = launcher_script(layout).replace(b"umask 077\n", b"", 1)
    layout.command_path.write_bytes(previous_launcher)
    layout.command_path.chmod(0o700)
    (layout.clone / ".git").chmod(0o700)
    previous_plan = plan_uninstall(layout, verify_authority=lambda _record: True)
    assert previous_plan["blockers"] == []
    metadata = layout.clone / ".git"
    metadata.chmod(0o777)
    report = inspect_installation(layout)
    assert report["checks"]["clone"]["git"] == "unsafe"
    metadata.chmod(0o700)
    blocked = plan_uninstall(layout, verify_authority=lambda _record: False)
    assert blocked["status"] == "blocked"
    assert (layout.clone / "managed.txt").read_text() == "managed"
    with pytest.raises(InstallerError) as authority_error:
        uninstall(layout, verify_authority=lambda _record: False)
    assert authority_error.value.code == "uninstall_blocked"
    assert (layout.clone / "managed.txt").read_text() == "managed"
    result = uninstall(layout, verify_authority=lambda _record: True)
    assert result["status"] == "uninstalled"
    for name in ("config", "secrets", "data", "state", "logs"):
        assert (getattr(layout, name) / "keep.txt").exists()
    assert not layout.clone.exists()
    assert not layout.venv.exists()
    assert not layout.installation_record.exists()
    assert not layout.browser_installation_record.exists()
    result = uninstall(layout, purge=True)
    assert result["status"] == "purged"
    assert not layout.dispatch_home.exists()


def test_stale_uninstall_receipt_cannot_authorize_fresh_managed_assets(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    marker = layout.clone / "fresh-managed"
    marker.parent.mkdir()
    marker.write_text("preserve", encoding="utf-8")
    for purge in (False, True):
        plan = plan_uninstall(layout, purge=purge)
        assert plan["status"] == "blocked"
        blockers = plan["blockers"]
        assert isinstance(blockers, list)
        assert any("stale uninstall receipt" in str(item) for item in blockers)
        with pytest.raises(InstallerError) as error:
            uninstall(layout, purge=purge)
        assert error.value.code == "uninstall_blocked"
        assert marker.read_text(encoding="utf-8") == "preserve"


def test_purge_removes_valid_external_private_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = {"HOME": str(home)}
    for name, variable in (
        ("config", "DISPATCH_CONFIG_ROOT"),
        ("secrets", "DISPATCH_SECRETS_ROOT"),
        ("data", "DISPATCH_DATA_ROOT"),
        ("state", "DISPATCH_STATE_ROOT"),
        ("cache", "DISPATCH_CACHE_ROOT"),
        ("logs", "DISPATCH_LOGS_ROOT"),
        ("runtime", "DISPATCH_RUNTIME_ROOT"),
    ):
        environment[variable] = str(home / "external" / name)
    layout = InstallLayout.from_environment(environment)
    layout.prepare()
    shutil.rmtree(layout.cache)
    shutil.rmtree(layout.run)
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    roots = (layout.config, layout.secrets, layout.data, layout.state, layout.logs)
    all_roots = (*roots, layout.cache, layout.run)
    for root in roots:
        (root / "marker").write_text("remove", encoding="utf-8")

    result = uninstall(layout, purge=True)
    assert result["status"] == "purged"
    assert not layout.dispatch_home.exists()
    assert all(not root.exists() for root in all_roots)


def test_uninstall_refuses_active_browser_generation_before_mutation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    shutil.rmtree(layout.cache)
    shutil.rmtree(layout.run)
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    (layout.config / "preserve").write_text("config", encoding="utf-8")
    locks = layout.state / "browser-manager"
    ensure_private_directory(locks, "Browser Manager state")
    lock_path = locks / "generation.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(InstallerError) as error:
            uninstall(layout)
        assert error.value.code == "browser_generation_busy"
        assert (layout.config / "preserve").read_text(encoding="utf-8") == "config"
        assert not layout.cache.exists()
        assert not layout.run.exists()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_uninstall_rolls_back_post_mutation_directory_stage_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    marker = layout.clone / "marker"
    marker.parent.mkdir()
    marker.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    original_stage = uninstall_runtime._stage_directory
    interrupted = False

    def interrupt_after_stage(stage):
        nonlocal interrupted
        original_stage(stage)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-stage")

    monkeypatch.setattr(uninstall_runtime, "_stage_directory", interrupt_after_stage)
    with pytest.raises(KeyboardInterrupt):
        uninstall(layout, run=lambda *_args, **_kwargs: completed())
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not list(layout.dispatch_home.glob(".dispatch.uninstall-*"))


def test_uninstall_reports_persistent_post_commit_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.clone / "marker").parent.mkdir()
    (layout.clone / "marker").write_text("sensitive", encoding="utf-8")
    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        uninstall_runtime,
        "_discard_directory",
        lambda _stage: (_ for _ in ()).throw(OSError("persistent cleanup")),
    )
    with pytest.raises(InstallerError) as error:
        uninstall(layout, run=lambda *_args, **_kwargs: completed())
    assert error.value.code == "uninstall_cleanup_failed"


def test_uninstall_rolls_back_current_service_after_later_mutation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python", encoding="utf-8")
    install_user_command(layout)
    install_user_service(layout, activate=False)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(str(value) for value in command))
        return completed()

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        uninstall_runtime,
        "remove_legacy_user_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected later failure")),
    )
    with pytest.raises(RuntimeError, match="injected later failure"):
        uninstall(layout, run=fake_run)
    assert layout.command_path.exists()
    assert inspect_user_command(layout)["status"] == "ready"
    assert layout.service_path.exists()
    assert service_unit_is_owned(layout)
    assert ("systemctl", "--user", "enable", "--now", "dispatch.service") in commands


def test_uninstall_restores_staged_directories_after_late_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    for path in (layout.clone, layout.venv, layout.cache, layout.run):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        (path / "marker").write_text(path.name, encoding="utf-8")
    layout.venv_python.parent.mkdir(parents=True, exist_ok=True)
    layout.venv_python.write_text("python", encoding="utf-8")
    install_user_command(layout)
    install_user_service(layout, activate=False)

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        uninstall_runtime,
        "_unlink_browser_installation_record",
        lambda _layout: (_ for _ in ()).throw(RuntimeError("late record failure")),
    )
    with pytest.raises(RuntimeError, match="late record failure"):
        uninstall(layout, run=lambda *_args, **_kwargs: completed())
    for path in (layout.clone, layout.venv, layout.cache, layout.run):
        assert (path / "marker").exists()
    assert inspect_user_command(layout)["status"] == "ready"
    assert service_unit_is_owned(layout)


def test_uninstall_reports_service_rollback_failure_when_generation_lock_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.state / "uninstall.json",
        {
            "schema_version": 1,
            "status": "uninstalled",
            "dispatch_home": str(layout.dispatch_home),
            "contains_secrets": False,
        },
    )
    install_user_service(layout, activate=False)
    lock_path = layout.state / "browser-manager" / "generation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.chmod(0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values == ("systemctl", "--user", "stop", "dispatch.service"):
            return completed()
        if values == ("systemctl", "--user", "enable", "--now", "dispatch.service"):
            return completed(returncode=1)
        return completed()

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(InstallerError) as error:
            uninstall(layout, run=fake_run)
        assert error.value.code == "service_rollback_failed"
        assert layout.service_path.exists()
        assert service_unit_is_owned(layout)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_uninstall_rejects_symlinked_browser_record_parent_without_external_mutation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    outside = tmp_path / "outside-browser-state"
    outside.mkdir(mode=0o700)
    external_record = outside / "installation.json"
    external_record.write_text('{"preserve":true}\n', encoding="utf-8")
    external_record.chmod(0o600)
    (layout.state / "browser-manager").symlink_to(outside, target_is_directory=True)

    plan = plan_uninstall(layout)
    assert plan["status"] == "blocked"
    blockers = plan["blockers"]
    assert isinstance(blockers, list)
    assert any("symlink" in str(item) for item in blockers)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)
    assert error.value.code == "uninstall_blocked"
    assert external_record.read_text(encoding="utf-8") == '{"preserve":true}\n'


def test_uninstall_rejects_invalid_provenance_before_removal(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    marker = layout.clone / "preserve"
    marker.parent.mkdir()
    marker.write_text("unrelated")
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": "https://example.invalid/unrelated.git",
            "channel": "dev",
            "ref": "dev",
            "commit": AUTHORITY_COMMIT,
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "not-a-timestamp",
            "contains_secrets": False,
        },
    )
    for purge in (False, True):
        with pytest.raises(InstallerError) as error:
            uninstall(layout, purge=purge)
        assert error.value.code == "uninstall_blocked"
        assert marker.read_text() == "unrelated"

    (layout.clone / ".git").mkdir()
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": "dev",
            "commit": "f" * 40,
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-16T00:00:00Z",
            "contains_secrets": False,
        },
    )
    for purge in (False, True):
        with pytest.raises(InstallerError) as error:
            uninstall(layout, purge=purge)
        assert error.value.code == "uninstall_blocked"
        assert marker.read_text() == "unrelated"


def test_dispatch_home_cannot_equal_or_contain_home(tmp_path: Path) -> None:
    home = tmp_path / "root" / "home"
    home.parent.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    for unsafe in (home, tmp_path / "root"):
        with pytest.raises(InstallerError, match="cannot equal HOME or contain HOME"):
            InstallLayout.from_environment({"HOME": str(home), "DISPATCH_HOME": str(unsafe)})


def test_dev_rejects_version() -> None:
    from dispatch_installer.lifecycle import resolve_ref

    with pytest.raises(InstallerError, match="only valid"):
        resolve_ref("dev", "v1.0.0")


def test_replacement_venv_accepts_real_stdlib_python_layout(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    write_browser_manager_project(layout.clone / "dispatch-core")
    write_test_project(layout.clone / "installer")
    destination = layout.dispatch_home / "replacement-venv"
    browser = layout.dispatch_home / "replacement-browser"

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            completed_venv = subprocess.run(values, cwd=cwd, check=False, capture_output=True, text=True)
            fake_site_packages(Path(values[-1]) / "bin" / "python")
            return completed_venv
        return browser_response(command) or editable_response(command) or completed()

    result = lifecycle_runtime.ensure_venv(
        layout,
        destination=destination,
        browser_cache=browser,
        run=fake_run,
    )
    assert result == destination
    assert (destination / "bin" / "python").is_file()


def test_direct_source_registration_writes_private_metadata_without_backend(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)

    def forbidden_run(command, cwd=None):
        raise AssertionError("a build backend must not run")

    original = os.umask(0o022)
    try:
        result = install_editable_source(python, source, run=forbidden_run)
        restored = os.umask(0o022)
        os.umask(restored)
    finally:
        os.umask(original)

    assert result.returncode == 0
    assert restored == 0o022
    pth = site_packages / "__dispatch__.dispatch_installer.pth"
    generation = next(site_packages.glob(".dispatch-direct-dispatch_installer-*"))
    assert pth.read_text(encoding="utf-8") == f"{source / 'src'}\n{generation}\n"
    dist_info = generation / "dispatch_installer-1.0.0.dist-info"
    assert json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))["url"] == source.as_uri()
    assert "dispatch-direct-source" in (dist_info / "INSTALLER").read_text(encoding="utf-8")
    record = (dist_info / "RECORD").read_text(encoding="utf-8")
    assert "__dispatch__.dispatch_installer.pth" in record
    assert not (source / "src" / "generated.egg-info").exists()


def test_direct_source_rejects_preexisting_metadata_aliases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    alias = source / "src" / "alias.egg-info"
    alias.symlink_to(outside)

    with pytest.raises(InstallerError) as symlink_error:
        install_editable_source(Path("/venv/bin/python"), source)
    assert symlink_error.value.code == "editable_source_unsafe"
    assert outside.read_text(encoding="utf-8") == "preserve"

    alias.unlink()
    alias.write_text("metadata", encoding="utf-8")
    hardlink = tmp_path / "outside-hardlink"
    os.link(alias, hardlink)
    with pytest.raises(InstallerError) as hardlink_error:
        install_editable_source(Path("/venv/bin/python"), source)
    assert hardlink_error.value.code == "editable_source_unsafe"
    assert hardlink.read_text(encoding="utf-8") == "metadata"


def test_direct_source_rejects_site_packages_aliases(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    outside = tmp_path / "outside"
    outside.write_text("preserve", encoding="utf-8")
    pth = site_packages / "__dispatch__.dispatch_installer.pth"
    pth.symlink_to(outside)

    with pytest.raises(InstallerError) as error:
        install_editable_source(python, source)
    assert error.value.code == "editable_site_packages_unsafe"
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_direct_source_rejects_site_packages_ancestor_alias(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    outside = tmp_path / "outside-lib"
    (outside / "python3.11" / "site-packages").mkdir(parents=True)
    (python.parent.parent / "lib").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallerError) as error:
        install_editable_source(python, source)
    assert error.value.code == "editable_site_packages_unsafe"
    assert not list(outside.rglob("__dispatch__.*"))


def test_direct_source_write_interruption_propagates_without_temporary_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    interruption = KeyboardInterrupt("stop")
    real_write = setup_runtime._atomic_private_bytes_at
    calls = 0

    def interrupt_second(directory: int, name: str, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise interruption
        real_write(directory, name, payload)

    monkeypatch.setattr(setup_runtime, "_atomic_private_bytes_at", interrupt_second)
    with pytest.raises(KeyboardInterrupt) as error:
        install_editable_source(python, source)
    assert error.value is interruption
    assert not (site_packages / "__dispatch__.dispatch_installer.pth").exists()
    assert not list(site_packages.glob(".dispatch-direct-dispatch_installer-*"))
    all_bytes = b"".join(path.read_bytes() for path in site_packages.rglob("*") if path.is_file())
    assert b".dispatch-editable-" not in all_bytes


def test_direct_source_reinstall_interruption_keeps_previous_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    install_editable_source(python, source)
    pth = site_packages / "__dispatch__.dispatch_installer.pth"
    previous_pth = pth.read_bytes()
    previous_generation = Path(previous_pth.decode().splitlines()[-1])
    manifest = source / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("version='1.0.0'", "version='2.0.0'"),
        encoding="utf-8",
    )
    real_write = setup_runtime._atomic_private_bytes_at
    calls = 0

    def interrupt_second(directory: int, name: str, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("metadata")
        real_write(directory, name, payload)

    monkeypatch.setattr(setup_runtime, "_atomic_private_bytes_at", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        install_editable_source(python, source)
    assert pth.read_bytes() == previous_pth
    assert (previous_generation / "dispatch_installer-1.0.0.dist-info" / "RECORD").is_file()
    assert list(site_packages.glob(".dispatch-direct-dispatch_installer-*")) == [previous_generation]


def test_direct_source_post_commit_interruption_leaves_complete_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    interruption = KeyboardInterrupt("post-commit")
    real_write = setup_runtime._atomic_private_bytes_at

    def interrupt_after_pth(directory: int, name: str, payload: bytes) -> None:
        real_write(directory, name, payload)
        if name == "__dispatch__.dispatch_installer.pth":
            raise interruption

    monkeypatch.setattr(setup_runtime, "_atomic_private_bytes_at", interrupt_after_pth)
    with pytest.raises(KeyboardInterrupt) as error:
        install_editable_source(python, source)
    assert error.value is interruption
    pth = site_packages / "__dispatch__.dispatch_installer.pth"
    generation = Path(pth.read_text(encoding="utf-8").splitlines()[-1])
    assert generation.is_dir()
    assert next(generation.glob("*.dist-info/RECORD")).is_file()


def test_direct_source_reinstall_atomically_replaces_distribution(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    install_editable_source(python, source)
    manifest = source / "pyproject.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("version='1.0.0'", "version='2.0.0'"), encoding="utf-8")

    install_editable_source(python, source)

    generations = list(site_packages.glob(".dispatch-direct-dispatch_installer-*"))
    assert len(generations) == 1
    assert (generations[0] / "dispatch_installer-2.0.0.dist-info" / "RECORD").is_file()
    assert not list(site_packages.rglob("dispatch_installer-1.0.0.dist-info"))


def test_direct_source_reinstall_cleanup_interrupt_preserves_new_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    install_editable_source(python, source)
    manifest = source / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("version='1.0.0'", "version='2.0.0'"),
        encoding="utf-8",
    )
    real_remove = setup_runtime._remove_tree_at
    interruption = KeyboardInterrupt("cleanup")
    interrupted = False

    def interrupt_after_cleanup(parent: int, name: str) -> None:
        nonlocal interrupted
        real_remove(parent, name)
        if not interrupted:
            interrupted = True
            raise interruption

    monkeypatch.setattr(setup_runtime, "_remove_tree_at", interrupt_after_cleanup)
    with pytest.raises(KeyboardInterrupt) as error:
        install_editable_source(python, source)
    assert error.value is interruption
    pth = site_packages / "__dispatch__.dispatch_installer.pth"
    generation = Path(pth.read_text(encoding="utf-8").splitlines()[-1])
    assert (generation / "dispatch_installer-2.0.0.dist-info" / "RECORD").is_file()
    assert len(list(site_packages.glob(".dispatch-direct-dispatch_installer-*"))) == 1


def test_direct_source_rejects_legacy_distribution_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    stale = site_packages / "dispatch_installer-0.9.0.dist-info"
    stale.mkdir()
    (stale / "METADATA").write_text("Name: dispatch-installer\nVersion: 0.9.0\n", encoding="utf-8")

    with pytest.raises(InstallerError) as error:
        install_editable_source(python, source)
    assert error.value.code == "editable_stale_metadata"
    assert (stale / "METADATA").is_file()


def test_direct_source_rejects_hyphenated_legacy_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    egg_link = site_packages / "dispatch-installer.egg-link"
    egg_link.write_text("legacy\n", encoding="utf-8")

    with pytest.raises(InstallerError) as egg_error:
        install_editable_source(python, source)
    assert egg_error.value.code == "editable_stale_metadata"
    egg_link.unlink()

    outside = tmp_path / "legacy-dist"
    outside.mkdir()
    (outside / "METADATA").write_text("Name: dispatch-installer\nVersion: 0.9.0\n", encoding="utf-8")
    stale_alias = site_packages / "dispatch-installer-0.9.0.dist-info"
    stale_alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallerError) as dist_error:
        install_editable_source(python, source)
    assert dist_error.value.code == "editable_stale_metadata"
    assert stale_alias.is_symlink()
    stale_alias.unlink()

    arbitrary_alias = site_packages / "arbitrary-alias.dist-info"
    arbitrary_alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallerError) as arbitrary_error:
        install_editable_source(python, source)
    assert arbitrary_error.value.code == "editable_stale_metadata"
    assert arbitrary_alias.is_symlink()


def test_direct_source_site_packages_swap_cannot_escape_pinned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    site_packages = fake_site_packages(python)
    moved = tmp_path / "moved-site-packages"
    outside = tmp_path / "outside-site-packages"
    outside.mkdir(mode=0o700)
    real_write = setup_runtime._atomic_private_bytes_at
    swapped = False

    def swap_before_commit(directory: int, name: str, payload: bytes) -> None:
        nonlocal swapped
        if name == "__dispatch__.dispatch_installer.pth" and not swapped:
            swapped = True
            os.replace(site_packages, moved)
            site_packages.symlink_to(outside, target_is_directory=True)
        real_write(directory, name, payload)

    monkeypatch.setattr(setup_runtime, "_atomic_private_bytes_at", swap_before_commit)
    with pytest.raises(InstallerError) as error:
        install_editable_source(python, source)
    assert error.value.code == "editable_site_packages_unsafe"
    assert not (outside / "__dispatch__.dispatch_installer.pth").exists()
    assert not list(outside.glob(".dispatch-direct-*"))
    assert not (moved / "__dispatch__.dispatch_installer.pth").exists()
    assert not list(moved.glob(".dispatch-direct-*"))


def test_launcher_shell_quotes_custom_home_path(tmp_path: Path) -> None:
    home = tmp_path / 'home";touch INJECTED;#'
    home.mkdir(mode=0o700)
    layout = InstallLayout.from_environment({"HOME": str(home)})
    lines = launcher_script(layout).decode().splitlines()
    assert lines[2] == "umask 077"
    assert lines[3] == f"export DISPATCH_HOME={shlex.quote(str(layout.dispatch_home))}"
    words = shlex.split(lines[-1])
    assert words[:2] == ["exec", str(layout.venv_python)]
    assert words[2:] == ["-I", "-B", "-m", "dispatch_installer.launcher", "$@"]


def test_launcher_and_service_upgrade_previous_private_umask_format(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    install_user_command(layout)
    previous_launcher = launcher_script(layout).replace(b"umask 077\n", b"", 1)
    layout.command_path.write_bytes(previous_launcher)
    layout.command_path.chmod(0o700)
    assert inspect_user_command(layout)["status"] == "ready"
    install_user_command(layout)
    assert layout.command_path.read_bytes() == launcher_script(layout)

    install_user_service(layout, activate=False)
    previous_service = layout.service_path.read_bytes().replace(b"UMask=0077\n", b"", 1)
    layout.service_path.write_bytes(previous_service)
    layout.service_path.chmod(0o600)
    service_record = read_json(layout.state / "service.json")
    service_record["unit_sha256"] = hashlib.sha256(previous_service).hexdigest()
    atomic_json(layout.state / "service.json", service_record)
    assert service_unit_is_owned(layout)
    install_user_service(layout, activate=False)
    assert b"UMask=0077\n" in layout.service_path.read_bytes()

    clone_launcher = (
        "#!/bin/sh\nset -eu\numask 077\n"
        f"export DISPATCH_HOME={shlex.quote(str(layout.dispatch_home))}\n"
        f"exec {shlex.quote(str(layout.venv_python))} -I -B -m dispatch_installer.launcher \"$@\"\n"
    ).encode("utf-8")
    layout.command_path.write_bytes(clone_launcher)
    layout.command_path.chmod(0o700)
    assert inspect_user_command(layout)["status"] == "ready"
    install_user_command(layout)
    assert layout.command_path.read_bytes() == launcher_script(layout)

    clone_service = service_unit(layout).replace(
        str(layout.browser_cache).encode("utf-8"),
        str(layout.legacy_browser_cache).encode("utf-8"),
        1,
    )
    layout.service_path.write_bytes(clone_service)
    layout.service_path.chmod(0o600)
    service_record = read_json(layout.state / "service.json")
    service_record["unit_sha256"] = hashlib.sha256(clone_service).hexdigest()
    atomic_json(layout.state / "service.json", service_record)
    assert service_unit_is_owned(layout)
    install_user_service(layout, activate=False)
    assert str(layout.browser_cache).encode("utf-8") in layout.service_path.read_bytes()


def test_launcher_rejects_symlinked_public_directory_ancestor(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    outside = tmp_path / "outside-local"
    outside.mkdir()
    (layout.home / ".local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallerError) as error:
        install_user_command(layout)
    assert error.value.code == "path_symlink"
    assert not (outside / "bin" / "dispatch").exists()


def test_launcher_rejects_group_writable_public_directory(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.command_path.parent.mkdir(mode=0o700, parents=True)
    layout.command_path.parent.chmod(0o777)
    with pytest.raises(InstallerError) as error:
        install_user_command(layout)
    assert error.value.code == "directory_unsafe"


def test_installation_lock_rejects_root_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    displaced = layout.dispatch_home.parent / "dispatch-original"
    outside = tmp_path / "outside-install"
    outside.mkdir(mode=0o700)
    original_open = layout_runtime.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == ".install.lock" and dir_fd is not None and not swapped:
            layout.dispatch_home.rename(displaced)
            layout.dispatch_home.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(layout_runtime.os, "open", racing_open)
    with pytest.raises(InstallerError) as error:
        with installation_lock(layout):
            pass
    assert error.value.code == "installation_root_changed"
    assert not (outside / ".install.lock").exists()


def test_installation_lock_rejects_root_swap_immediately_after_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    displaced = layout.dispatch_home.parent / "dispatch-after-flock"
    outside = tmp_path / "outside-after-flock"
    outside.mkdir(mode=0o700)
    original_flock = layout_runtime.fcntl.flock
    swapped = False

    def racing_flock(descriptor, operation):
        nonlocal swapped
        result = original_flock(descriptor, operation)
        if operation & layout_runtime.fcntl.LOCK_EX and not swapped:
            layout.dispatch_home.rename(displaced)
            layout.dispatch_home.symlink_to(outside, target_is_directory=True)
            swapped = True
        return result

    monkeypatch.setattr(layout_runtime.fcntl, "flock", racing_flock)
    with pytest.raises(InstallerError) as error:
        with installation_lock(layout):
            pass
    assert error.value.code == "installation_root_changed"
    assert not (outside / ".install.lock").exists()


def test_installation_lock_detects_root_swap_before_lifecycle_mutation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = tmp_path / "staged-source"
    (source / ".git").mkdir(parents=True)
    displaced = layout.dispatch_home.parent / "dispatch-lifetime-original"
    outside = tmp_path / "outside-lifetime"
    outside.mkdir(mode=0o700)

    with installation_lock(layout):
        layout.dispatch_home.rename(displaced)
        layout.dispatch_home.symlink_to(outside, target_is_directory=True)
        with pytest.raises(InstallerError) as error:
            lifecycle_runtime._promote_clone(layout, source)
        assert error.value.code == "installation_root_changed"
    assert not (outside / "dispatch").exists()
    assert not (outside / ".install-tmp").exists()


def test_post_validation_root_swap_keeps_promotion_in_pinned_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    staging_root = lifecycle_runtime._prepare_temporary_root(layout)
    source = Path(tempfile.mkdtemp(prefix="candidate-", dir=staging_root)) / "dispatch"
    (source / ".git").mkdir(parents=True)
    displaced = layout.dispatch_home.parent / "dispatch-pinned-original"
    outside = tmp_path / "outside-pinned"
    outside.mkdir(mode=0o700)
    original_assert = lifecycle_runtime.assert_installation_root_current
    validation_count = 0

    def swap_after_validation(value=None):
        nonlocal validation_count
        original_assert(value)
        validation_count += 1
        if validation_count == 2:
            layout.dispatch_home.rename(displaced)
            layout.dispatch_home.symlink_to(outside, target_is_directory=True)

    with installation_lock(layout):
        monkeypatch.setattr(lifecycle_runtime, "assert_installation_root_current", swap_after_validation)
        lifecycle_runtime._promote_clone(layout, source)
    assert (displaced / "dispatch" / ".git").is_dir()
    assert not (outside / "dispatch").exists()


def test_installation_lock_rejects_symlink_without_chmod_target(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    target.chmod(0o644)
    layout.lock_path.symlink_to(target)
    with pytest.raises(InstallerError) as error:
        with installation_lock(layout):
            pass
    assert error.value.code == "lock_unsafe"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_text() == "preserve"


def test_install_rejects_symlinked_staging_root(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    (layout.dispatch_home / ".install-tmp").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, outside, channel="dev", ref="dev", run=lambda *_: completed())
    assert error.value.code == "staging_unsafe"


def test_doctor_rejects_symlinked_git_authority(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    layout.clone.chmod(0o700)
    external = tmp_path / "external-git"
    external.mkdir()
    (layout.clone / ".git").symlink_to(external, target_is_directory=True)
    report = inspect_installation(layout)
    assert report["ok"] is False
    assert report["status"] == "unsafe"
    assert report["checks"]["clone"]["git"] == "unsafe"

    (layout.clone / ".git").unlink()
    (layout.clone / ".git").mkdir()
    report = inspect_installation(layout)
    assert report["ok"] is False
    assert report["checks"]["clone"]["git"] == "unsafe"


def test_service_publication_and_removal_reject_unowned_unit(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    ensure_private_directory(layout.service_directory, "service directory")
    layout.service_directory.chmod(0o700)
    layout.service_path.write_text("[Unit]\nDescription=Unrelated\n")
    original = layout.service_path.read_bytes()
    with pytest.raises(InstallerError) as install_error:
        install_user_service(layout, activate=False)
    assert install_error.value.code == "service_conflict"
    assert layout.service_path.read_bytes() == original

    layout.service_path.unlink()
    install_user_service(layout, activate=False)
    assert b"UMask=0077\n" in layout.service_path.read_bytes()
    layout.service_path.chmod(0o666)
    inspection = inspect_user_service(layout, run=lambda *_: completed())
    assert inspection["status"] == "unsafe"
    layout.service_path.chmod(0o600)
    content = layout.service_path.read_bytes()
    receipt = layout.state / "service.json"
    reload_calls = 0

    def fail_initial_reload(command, _cwd=None):
        nonlocal reload_calls
        values = tuple(command)
        if values == ("systemctl", "--user", "daemon-reload"):
            reload_calls += 1
            return completed(returncode=1 if reload_calls == 1 else 0)
        return completed()

    with pytest.raises(InstallerError) as reload_error:
        remove_user_service(layout, run=fail_initial_reload)
    assert reload_error.value.code == "service_reload_failed"
    assert reload_calls == 2
    assert layout.service_path.read_bytes() == content
    assert receipt.exists()

    def fail_every_reload(command, _cwd=None):
        if tuple(command) == ("systemctl", "--user", "daemon-reload"):
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as rollback_error:
        remove_user_service(layout, run=fail_every_reload)
    assert rollback_error.value.code == "service_rollback_failed"
    assert layout.service_path.read_bytes() == content
    assert receipt.exists()
    layout.service_path.write_text("changed")
    forged = json.loads((layout.state / "service.json").read_text())
    import hashlib

    forged["unit_sha256"] = hashlib.sha256(b"changed").hexdigest()
    atomic_json(layout.state / "service.json", forged)
    with pytest.raises(InstallerError) as remove_error:
        remove_user_service(layout, run=lambda *_: completed())
    assert remove_error.value.code == "service_unit_unsafe"
    assert layout.service_path.read_text() == "changed"


def test_legacy_service_requires_matching_owned_receipt(tmp_path: Path) -> None:
    import hashlib

    layout = make_layout(tmp_path)
    layout.prepare()
    unit = layout.service_directory / "dispatch-core.service"
    ensure_private_directory(unit.parent, "service directory")
    unit.parent.chmod(0o700)
    content = legacy_service_unit(layout)
    unit.write_bytes(content)
    unit.chmod(0o600)
    receipt = layout.state / "install" / "service.json"
    receipt.parent.mkdir(parents=True)
    receipt.parent.chmod(0o700)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "unit": str(unit),
                "unit_sha256": hashlib.sha256(content).hexdigest(),
                "launcher": str(layout.dispatch_home / "bin" / "dispatch"),
                "service": "dispatch-core.service",
                "status": "active",
                "contains_secrets": False,
            }
        )
    )
    receipt.chmod(0o600)
    assert legacy_service_unit_is_owned(layout) is True
    malformed = json.loads(receipt.read_text())
    malformed.pop("status")
    malformed["extra"] = True
    atomic_json(receipt, malformed)
    assert legacy_service_unit_is_owned(layout) is False
    malformed.pop("extra")
    malformed["status"] = "active"
    atomic_json(receipt, malformed)
    assert legacy_service_unit_is_owned(layout) is True
    unit.write_text("changed")
    assert legacy_service_unit_is_owned(layout) is False
    unit.write_bytes(content)
    assert legacy_service_unit_is_owned(layout) is True

    unrelated = b"[Service]\nExecStart=/usr/bin/unrelated-daemon --delete-other-data\n"
    unit.write_bytes(unrelated)
    malformed["unit_sha256"] = hashlib.sha256(unrelated).hexdigest()
    atomic_json(receipt, malformed)
    commands: list[tuple[str, ...]] = []
    assert legacy_service_unit_is_owned(layout) is False
    with pytest.raises(InstallerError):
        remove_legacy_user_service(
            layout,
            run=lambda command, _cwd=None: commands.append(tuple(command)) or completed(),
        )
    assert commands == []

    unit.write_bytes(content)
    malformed["unit_sha256"] = hashlib.sha256(content).hexdigest()
    atomic_json(receipt, malformed)
    reload_calls = 0

    def fail_initial_reload(command, _cwd=None):
        nonlocal reload_calls
        values = tuple(command)
        if values == ("systemctl", "--user", "daemon-reload"):
            reload_calls += 1
            return completed(returncode=1 if reload_calls == 1 else 0)
        return completed()

    with pytest.raises(InstallerError) as reload_error:
        remove_legacy_user_service(layout, run=fail_initial_reload)
    assert reload_error.value.code == "legacy_service_reload_failed"
    assert reload_calls == 2
    assert unit.read_bytes() == content
    assert receipt.exists()

    def fail_every_reload(command, _cwd=None):
        values = tuple(command)
        if values == ("systemctl", "--user", "daemon-reload"):
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as rollback_error:
        remove_legacy_user_service(layout, run=fail_every_reload)
    assert rollback_error.value.code == "legacy_service_rollback_failed"
    assert unit.read_bytes() == content
    assert receipt.exists()
    remove_legacy_user_service(layout, run=lambda *_: completed())
    assert not unit.exists()
    assert not receipt.exists()


def test_legacy_service_symlinked_receipt_parent_is_never_touched(tmp_path: Path) -> None:
    import hashlib

    layout = make_layout(tmp_path)
    layout.prepare()
    unit = layout.service_directory / "dispatch-core.service"
    ensure_private_directory(unit.parent, "service directory")
    unit.parent.chmod(0o700)
    content = legacy_service_unit(layout)
    unit.write_bytes(content)
    unit.chmod(0o600)
    external = tmp_path / "external-legacy-state"
    external.mkdir(mode=0o700)
    receipt = external / "service.json"
    atomic_json(
        receipt,
        {
            "schema_version": 1,
            "unit": str(unit),
            "unit_sha256": hashlib.sha256(content).hexdigest(),
            "launcher": str(layout.dispatch_home / "bin" / "dispatch"),
            "service": "dispatch-core.service",
            "status": "active",
            "contains_secrets": False,
        },
    )
    (layout.state / "install").symlink_to(external, target_is_directory=True)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(str(value) for value in command))
        return completed()

    assert legacy_service_unit_is_owned(layout) is False
    with pytest.raises(InstallerError) as error:
        remove_legacy_user_service(layout, run=fake_run)
    assert error.value.code == "legacy_service_unsafe"
    assert commands == []
    assert unit.exists()
    assert receipt.exists()


def test_uninstall_plan_blocks_symlink_before_any_removal(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    install_user_command(layout)
    layout.clone.mkdir()
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    layout.cache.rmdir()
    layout.cache.symlink_to(outside, target_is_directory=True)
    plan = plan_uninstall(layout)
    assert plan["status"] == "blocked"
    blockers = plan["blockers"]
    assert isinstance(blockers, list)
    assert any("managed path is unsafe" in item for item in blockers)
    with pytest.raises(InstallerError) as error:
        uninstall(layout)
    assert error.value.code == "uninstall_blocked"
    assert layout.command_path.exists()
    assert layout.clone.exists()


def test_legacy_cleanup_requires_verified_setup_receipt(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    legacy_plugin = layout.dispatch_home / "plugins" / "user-data.txt"
    legacy_plugin.parent.mkdir()
    legacy_plugin.write_text("preserve")
    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._remove_legacy_code(layout, setup_migrated=False)
    assert error.value.code == "legacy_cleanup_unsafe"
    assert legacy_plugin.read_text() == "preserve"


def test_legacy_setup_symlink_parent_is_never_migrated_or_cleaned(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    external = tmp_path / "external-install-state"
    external.mkdir(mode=0o700)
    receipt = external / "setup.json"
    receipt.write_text("{}")
    receipt.chmod(0o600)
    (layout.state / "install").symlink_to(external, target_is_directory=True)
    assert migrate_legacy_plugin_config(layout) is False
    with pytest.raises(InstallerError):
        lifecycle_runtime._remove_legacy_code(layout, setup_migrated=True)
    assert receipt.exists()


def test_post_activation_legacy_cleanup_failure_keeps_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python")
            python.chmod(0o700)
            fake_site_packages(python)
        response = authority_response(command)
        if response is not None:
            return response
        return browser_response(command) or editable_response(command) or completed()

    monkeypatch.setattr(
        lifecycle_runtime,
        "_remove_legacy_code",
        lambda _layout, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    monkeypatch.setattr(lifecycle_runtime, "migrate_legacy_plugin_config", lambda _layout: True)
    legacy_path = layout.service_directory / "dispatch-core.service"
    ensure_private_directory(legacy_path.parent, "service directory")
    legacy_path.write_text("legacy")
    monkeypatch.setattr(lifecycle_runtime, "legacy_service_unit_is_owned", lambda _layout: True)
    monkeypatch.setattr(lifecycle_runtime, "stop_legacy_user_service", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(lifecycle_runtime, "remove_legacy_user_service", lambda *_args, **_kwargs: None)
    result = install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert result["status"] == "installed_cleanup_incomplete"
    assert result["cleanup_error_code"] == "post_activation_cleanup_failed"
    record = read_installation(layout)
    assert record is not None
    assert record["commit"] == "0123456789abcdef0123456789abcdef01234567"


def test_post_activation_work_cleanup_interrupt_keeps_new_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python")
            python.chmod(0o700)
            fake_site_packages(python)
        return authority_response(command) or browser_response(command) or editable_response(command) or completed()

    real_remove = lifecycle_runtime._safe_remove

    def interrupted_cleanup(path: Path) -> None:
        if path.name.startswith("dispatch-installer-"):
            raise KeyboardInterrupt
        real_remove(path)

    monkeypatch.setattr(lifecycle_runtime, "_safe_remove", interrupted_cleanup)
    result = install_from_clone(layout, source, channel="dev", ref="dev", run=fake_run)
    assert result["status"] == "installed_cleanup_incomplete"
    assert result["cleanup_error_code"] == "post_activation_cleanup_failed"
    record = read_installation(layout)
    assert record is not None
    assert record["commit"] == AUTHORITY_COMMIT
    assert layout.venv_python.exists()
    assert layout.clone.exists()


def test_staged_work_cleanup_interrupt_does_not_replace_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = make_layout(tmp_path)
    work = tmp_path / "staged-work"
    staged = work / "dispatch"
    staged.mkdir(parents=True)
    monkeypatch.setattr(lifecycle_runtime, "_stage_repository", lambda *_args, **_kwargs: (staged, work))
    monkeypatch.setattr(
        lifecycle_runtime,
        "install_from_clone",
        lambda *_args, **_kwargs: {"status": "installed"},
    )
    monkeypatch.setattr(
        lifecycle_runtime,
        "_safe_remove",
        lambda _path: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    result = lifecycle_runtime.install_or_update(layout, channel="dev")
    assert result["status"] == "installed"
    assert work.exists()
