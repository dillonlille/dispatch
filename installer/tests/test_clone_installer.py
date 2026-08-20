from __future__ import annotations

import contextlib
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
import dispatch_installer.launcher as launcher_runtime
import dispatch_installer.setup as setup_runtime
cli_runtime = importlib.import_module("dispatch_installer.cli")
layout_runtime = importlib.import_module("dispatch_installer.layout")
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.lifecycle import ensure_venv, install_from_clone, install_or_update
from dispatch_installer.repository import (
    DEVELOPMENT_BRANCH,
    LEGACY_DEVELOPMENT_BRANCH,
    REPOSITORY_URL,
    assert_checkout_clean,
    canonical_record_has_remote_authority,
    clone_repository,
    current_commit,
    local_checkout_matches_record,
    resolve_latest_release,
    verify_checkout_authority,
)
from dispatch_installer.setup import (
    configure_plugins,
    install_source_distribution,
    load_plugin_config,
    migrate_legacy_plugin_config,
    reconcile_plugin_services,
)
from dispatch_installer.service import (
    disable_plugin_service,
    enable_plugin_service,
    inspect_plugin_services,
    inspect_user_service,
    install_user_service,
    legacy_service_unit,
    legacy_service_unit_is_owned,
    remove_legacy_user_service,
    remove_plugin_service,
    remove_user_service,
    restore_systemd_service_state,
    plugin_service_path,
    plugin_service_receipt_path,
    plugin_service_unit,
    prepare_plugin_service,
    service_unit,
    service_unit_is_owned,
    status_plugin_service,
    stop_plugin_services_for_activation,
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
        "[build-system]\n"
        "requires=['setuptools==83.0.0']\n"
        "build-backend='setuptools.build_meta'\n"
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
    if values and values[-1] in {
        "HEAD",
        "FETCH_HEAD",
        f"refs/remotes/origin/{DEVELOPMENT_BRANCH}",
    } and "rev-parse" in values:
        return completed(stdout=f"{AUTHORITY_COMMIT}\n")
    if "symbolic-ref" in values:
        return completed(stdout=f"{DEVELOPMENT_BRANCH}\n")
    if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.remote"):
        return completed(stdout="origin\n")
    if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.merge"):
        return completed(stdout=f"refs/heads/{DEVELOPMENT_BRANCH}\n")
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


def test_read_installation_accepts_legacy_dev_ref_for_migration(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": LEGACY_DEVELOPMENT_BRANCH,
            "commit": "0123456789abcdef0123456789abcdef01234567",
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-19T00:00:00Z",
            "contains_secrets": False,
        },
    )

    record = read_installation(layout)
    assert record is not None
    assert record["channel"] == "dev"
    assert record["ref"] == LEGACY_DEVELOPMENT_BRANCH


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


def test_private_root_cannot_equal_or_contain_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    sentinel = home / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")

    for configured in (home, tmp_path):
        with pytest.raises(InstallerError) as error:
            InstallLayout.from_environment(
                {
                    "HOME": str(home),
                    "DISPATCH_CONFIG_ROOT": str(configured),
                }
            )
        assert error.value.code == "private_root_unsafe"

    assert sentinel.read_text(encoding="utf-8") == "preserve"


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


def test_layout_rejects_tilde_relative_configured_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with pytest.raises(InstallerError) as error:
        InstallLayout.from_environment(
            {"HOME": str(home), "DISPATCH_CONFIG_ROOT": "~/config"}
        )
    assert error.value.code == "path_not_absolute"


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


def test_installer_json_boundaries_normalize_deep_nesting(monkeypatch, tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    deep_json = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"

    installation = layout.installation_record
    installation.write_text(deep_json, encoding="utf-8")
    installation.chmod(0o600)
    monkeypatch.setattr(
        layout_runtime.json,
        "loads",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError()),
    )
    with pytest.raises(InstallerError) as installation_error:
        read_installation(layout)
    assert installation_error.value.code == "record_invalid"

    plugin_config = layout.config / "plugins.json"
    plugin_config.write_text(deep_json, encoding="utf-8")
    plugin_config.chmod(0o600)
    with pytest.raises(InstallerError) as config_error:
        load_plugin_config(layout)
    assert config_error.value.code == "plugin_config_invalid"

    with pytest.raises(InstallerError) as selection_error:
        lifecycle_runtime._selected_plugins(layout)
    assert selection_error.value.code == "plugin_config_invalid"


def test_github_json_boundaries_normalize_deep_nesting(monkeypatch) -> None:
    deep_json = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    monkeypatch.setattr(
        layout_runtime.json,
        "loads",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError()),
    )

    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = deep_json.encode()
    with pytest.raises(InstallerError) as release_error:
        resolve_latest_release(opener=Mock(return_value=response))
    assert release_error.value.code == "release_lookup_failed"

    dev_record: dict[str, object] = {
        "channel": "dev",
        "ref": DEVELOPMENT_BRANCH,
        "commit": "a" * 40,
    }
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = deep_json.encode()
    with pytest.raises(InstallerError) as authority_error:
        canonical_record_has_remote_authority(dev_record, opener=Mock(return_value=response))
    assert authority_error.value.code == "repository_authority_unavailable"


def test_remote_authority_binds_dev_and_stable_records() -> None:
    commit = "a" * 40

    def response(payload):
        value = Mock()
        value.__enter__ = Mock(return_value=value)
        value.__exit__ = Mock(return_value=False)
        value.read.return_value = json.dumps(payload).encode()
        return value

    dev_record: dict[str, object] = {"channel": "dev", "ref": DEVELOPMENT_BRANCH, "commit": commit}
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


def test_plugin_service_cli_routes_explicit_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str]] = []

    def result(operation):
        return lambda layout, plugin_id: calls.append((operation, plugin_id)) or {
            "status": "ready" if operation == "status" else operation + "d",
            "plugin_id": plugin_id,
        }

    monkeypatch.setattr(cli_runtime, "status_plugin_service", result("status"))
    monkeypatch.setattr(cli_runtime, "enable_plugin_service", result("enable"))
    monkeypatch.setattr(cli_runtime, "disable_plugin_service", result("disable"))
    monkeypatch.setattr(cli_runtime, "installation_lock", lambda layout: contextlib.nullcontext())
    monkeypatch.setattr(cli_runtime, "read_installation", lambda layout: {"channel": "dev"})
    monkeypatch.setattr(cli_runtime, "selected_long_running_plugins", lambda layout: ["worker"])
    root = tmp_path / "dispatch"

    assert installer_main(["--dispatch-home", str(root), "plugin-service", "status", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert installer_main(["--dispatch-home", str(root), "plugin-service", "enable", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "enabled"
    assert installer_main(["--dispatch-home", str(root), "plugin-service", "disable", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    assert calls == [("status", "worker"), ("enable", "worker"), ("disable", "worker")]


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


def test_dev_clone_is_complete_and_tracks_main(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(command))
        return completed()

    destination = tmp_path / "clone"
    clone_repository(destination, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)

    assert len(commands) == 1
    assert "--depth" not in commands[0]
    assert commands[0][commands[0].index("--branch") + 1] == DEVELOPMENT_BRANCH
    assert commands[0][-1] == str(destination)


def test_checkout_clean_rejects_ignored_files(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    subprocess.run(("git", "init", "-q", "-b", DEVELOPMENT_BRANCH, str(clone)), check=True)
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


@pytest.mark.parametrize("branch", [DEVELOPMENT_BRANCH, LEGACY_DEVELOPMENT_BRANCH])
def test_local_checkout_record_rejects_noncanonical_origin(tmp_path: Path, branch: str) -> None:
    clone = tmp_path / "clone"
    subprocess.run(("git", "init", "-q", "-b", branch, str(clone)), check=True)
    (clone / ".git").chmod(0o700)
    subprocess.run(("git", "-C", str(clone), "config", "user.email", "tests@example.invalid"), check=True)
    subprocess.run(("git", "-C", str(clone), "config", "user.name", "Dispatch Tests"), check=True)
    subprocess.run(("git", "-C", str(clone), "remote", "add", "origin", REPOSITORY_URL), check=True)
    (clone / "source.txt").write_text("canonical\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(clone), "add", "source.txt"), check=True)
    subprocess.run(("git", "-C", str(clone), "commit", "-q", "-m", "canonical"), check=True)
    commit = current_commit(clone)
    subprocess.run(
        ("git", "-C", str(clone), "update-ref", f"refs/remotes/origin/{branch}", commit),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(clone), "config", f"branch.{branch}.remote", "origin"),
        check=True,
    )
    subprocess.run(
        (
            "git", "-C", str(clone), "config",
            f"branch.{branch}.merge", f"refs/heads/{branch}",
        ),
        check=True,
    )
    record: dict[str, object] = {"channel": "dev", "ref": branch, "commit": commit}

    assert local_checkout_matches_record(clone, record) is True

    subprocess.run(
        ("git", "-C", str(clone), "remote", "set-url", "origin", (tmp_path / "unrelated.git").as_uri()),
        check=True,
    )
    assert local_checkout_matches_record(clone, record) is False


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
            return completed(stdout=f"{DEVELOPMENT_BRANCH}\n")
        if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.remote"):
            return completed(stdout="origin\n")
        if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.merge"):
            return completed(stdout=f"refs/heads/{DEVELOPMENT_BRANCH}\n")
        return completed()

    with pytest.raises(InstallerError, match="exactly track"):
        verify_checkout_authority(clone, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)


def test_staged_dev_checkout_rejects_wrong_upstream_tracking(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[-2:] == ("rev-parse", "--is-shallow-repository"):
            return completed(stdout="false\n")
        if values[-2:] in {("rev-parse", "HEAD"), ("rev-parse", "FETCH_HEAD")}:
            return completed(stdout=f"{AUTHORITY_COMMIT}\n")
        if "symbolic-ref" in values:
            return completed(stdout=f"{DEVELOPMENT_BRANCH}\n")
        if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.remote"):
            return completed(stdout="malicious\n")
        if values[-2:] == ("--get", f"branch.{DEVELOPMENT_BRANCH}.merge"):
            return completed(stdout="refs/heads/wrong\n")
        return completed()

    with pytest.raises(InstallerError, match="exactly track origin/main"):
        verify_checkout_authority(
            clone,
            channel="dev",
            ref=DEVELOPMENT_BRANCH,
            run=fake_run,
        )


def test_install_from_staged_clone_writes_atomic_record(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": LEGACY_DEVELOPMENT_BRANCH,
            "commit": "fedcba9876543210fedcba9876543210fedcba98",
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-19T00:00:00Z",
            "contains_secrets": False,
        },
    )
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
        return browser_response(command) or completed()

    result = install_or_update(
        layout,
        source=source,
        channel="dev",
        run=fake_run,
    )
    assert result["status"] == "switched"
    record = read_installation(layout)
    assert record is not None
    assert record["channel"] == "dev"
    assert record["ref"] == DEVELOPMENT_BRANCH
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
    assert any("--force-reinstall" in command for command in commands)
    assert not any((layout.clone / "installer").rglob("*.egg-info"))


def test_setup_installs_selected_plugin_from_private_source_copy_and_writes_config(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    plugin = layout.clone / "plugins" / "handbook"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools==83.0.0']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='handbook'\nversion='1'\n"
        "[project.entry-points.\"dispatch.plugins\"]\nhandbook='x:y'\n"
        "[tool.dispatch]\nid='handbook'\ncapabilities=['read_local_data']\n"
    )
    (plugin / "x.py").write_text("def y(request):\n    return request\n", encoding="utf-8")
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python")
    fake_site_packages(layout.venv_python)
    calls: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        calls.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return browser_response(command) or completed()

    result = configure_plugins(layout, ["handbook"], run=fake_run)
    config = json.loads((layout.config / "plugins.json").read_text())
    assert result["selected_plugins"] == ["handbook"]
    assert config["plugins"][0]["id"] == "handbook"
    assert config["status"] == "complete"
    assert config["selected_plugins"] == ["handbook"]
    assert config["plugins"][0]["capabilities"] == ["read_local_data"]
    assert any("--force-reinstall" in command for command in calls)
    assert not any(plugin.rglob("*.egg-info"))


def test_plugin_setup_restores_main_service_after_stop_interruption(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("old-python", encoding="utf-8")
    layout.venv_python.chmod(0o700)
    old_marker = layout.venv / "old-marker"
    old_marker.write_text("preserve", encoding="utf-8")
    install_user_service(layout, activate=False)
    state = {"active": True, "enabled": True}

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("replacement", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if state["enabled"] else 1)
        if values == ("systemctl", "--user", "stop", "dispatch.service"):
            state["active"] = False
            raise KeyboardInterrupt("stop interrupted")
        if values[:3] == ("systemctl", "--user", "enable"):
            state["enabled"] = True
        if values[:3] == ("systemctl", "--user", "start"):
            state["active"] = True
        return completed()

    with pytest.raises(KeyboardInterrupt):
        configure_plugins(layout, [], run=fake_run)

    assert state == {"active": True, "enabled": True}
    assert old_marker.read_text(encoding="utf-8") == "preserve"


def test_plugin_setup_restores_venv_after_post_displacement_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    layout.venv.mkdir()
    old_marker = layout.venv / "old-marker"
    old_marker.write_text("preserve", encoding="utf-8")
    real_replace = lifecycle_runtime.os.replace
    interrupted = False

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("replacement", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return completed()

    def post_displacement_interrupt(source, destination):
        nonlocal interrupted
        result = real_replace(source, destination)
        if Path(source) == layout.venv and ".venv.failed-" in Path(destination).name and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("post-displacement")
        return result

    monkeypatch.setattr(
        setup_runtime,
        "_plugin_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(InstallerError("synthetic", "fail after swap")),
    )
    monkeypatch.setattr(lifecycle_runtime.os, "replace", post_displacement_interrupt)

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, [], run=fake_run)

    assert error.value.code == "synthetic"
    assert interrupted is True
    assert old_marker.read_text(encoding="utf-8") == "preserve"
    assert not list(layout.dispatch_home.glob(".venv.previous-*"))
    assert not list(layout.dispatch_home.glob(".venv.failed-*"))


def test_plugin_setup_reports_projection_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    _write_runtime_plugin(layout.clone, dependencies=[])
    layout.venv.mkdir()
    atomic_json(
        layout.config / "plugins.json",
        setup_runtime._plugin_config(layout, ["worker"]),
    )

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("replacement", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return completed(returncode=1 if values[:3] in {
            ("systemctl", "--user", "is-active"),
            ("systemctl", "--user", "is-enabled"),
        } else 0)

    original_plugin_config = setup_runtime._plugin_config
    config_calls = 0

    def fail_after_previous_config(*args, **kwargs):
        nonlocal config_calls
        config_calls += 1
        if config_calls > 1:
            raise InstallerError("synthetic", "fail after swap")
        return original_plugin_config(*args, **kwargs)

    monkeypatch.setattr(setup_runtime, "_plugin_config", fail_after_previous_config)
    monkeypatch.setattr(
        setup_runtime,
        "reconcile_plugin_services",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("projection rollback failed")),
    )

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, [], run=fake_run)

    assert error.value.code == "plugin_environment_rollback_failed"


def test_plugin_setup_cleanup_failure_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    layout.venv.mkdir()
    monkeypatch.setattr(
        lifecycle_runtime,
        "ensure_venv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("setup interrupted")),
    )
    monkeypatch.setattr(
        lifecycle_runtime,
        "_safe_remove",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, [], run=lambda *_args, **_kwargs: completed())

    assert error.value.code == "plugin_environment_cleanup_failed"


@pytest.mark.parametrize(
    ("entry_points", "source", "expected_code"),
    [
        (
            "[project.entry-points.\"dispatch.plugins\"]\nother='x:y'\n",
            "def y(request):\n    return request\n",
            "plugin_entry_point_invalid",
        ),
        (
            "[project.entry-points.\"dispatch.plugins\"]\nhandbook='x:missing'\n",
            "def y(request):\n    return request\n",
            "plugin_entry_point_invalid",
        ),
    ],
)
def test_setup_requires_one_same_id_source_callable_entry_point(
    tmp_path: Path,
    entry_points: str,
    source: str,
    expected_code: str,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    plugin = layout.clone / "plugins" / "handbook"
    plugin.mkdir(parents=True)
    (plugin / "pyproject.toml").write_text(
        "[project]\nname='handbook'\nversion='1'\n"
        + entry_points
        + "[tool.dispatch]\nid='handbook'\ncapabilities=['read_local_data']\n",
        encoding="utf-8",
    )
    (plugin / "x.py").write_text(source, encoding="utf-8")

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, ["handbook"], run=lambda *_: completed())

    assert error.value.code == expected_code
    assert not (layout.config / "plugins.json").exists()


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
    legacy_plugin_sentinel = layout.dispatch_home / "plugins" / "user-data.txt"
    legacy_release_sentinel = layout.dispatch_home / "releases" / "user-data.txt"
    for sentinel in (legacy_plugin_sentinel, legacy_release_sentinel):
        sentinel.parent.mkdir()
        sentinel.write_text("preserve", encoding="utf-8")
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
    assert legacy_plugin_sentinel.read_text(encoding="utf-8") == "preserve"
    assert legacy_release_sentinel.read_text(encoding="utf-8") == "preserve"
    assert not hasattr(lifecycle_runtime, "_remove_legacy_code")


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
        return browser_response(command) or completed()

    real_restore = lifecycle_runtime._restore_directory
    interrupted: set[Path] = set()

    def interrupt_rollback_once(path: Path, backup: Path | None) -> None:
        if path in {layout.venv, layout.clone} and path not in interrupted:
            interrupted.add(path)
            raise KeyboardInterrupt
        real_restore(path, backup)

    monkeypatch.setattr(lifecycle_runtime, "_restore_directory", interrupt_rollback_once)

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
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
        return browser_response(command) or completed()

    original_swap = lifecycle_runtime._swap_directory

    def interrupt_after_browser_return(replacement, target, *, state=None):
        result = original_swap(replacement, target, state=state)
        if target == layout.browser_cache:
            raise KeyboardInterrupt("post-browser-return")
        return result

    monkeypatch.setattr(lifecycle_runtime, "_swap_directory", interrupt_after_browser_return)
    with pytest.raises(KeyboardInterrupt):
        install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
    assert (layout.clone / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (layout.clone / "new.txt").exists()
    assert layout.venv_python.read_text(encoding="utf-8") == "old-python"
    assert old_browser.read_text(encoding="utf-8") == "old-browser"
    assert not (layout.browser_cache / "chromium-1234567").exists()
    assert read_json(layout.browser_installation_record) == old_record


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
        install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=lambda *_args, **_kwargs: completed())
    assert (layout.clone / "marker").read_text(encoding="utf-8") == "old"
    assert not list(layout.dispatch_home.glob(".dispatch.previous-*"))


def test_clone_backup_cleanup_failure_is_reported(
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
    real_remove = lifecycle_runtime._safe_remove

    monkeypatch.setattr(lifecycle_runtime, "verify_checkout_authority", lambda *_args, **_kwargs: AUTHORITY_COMMIT)
    monkeypatch.setattr(
        lifecycle_runtime,
        "_reconcile_installation",
        lambda *_args, **_kwargs: {"status": "installed"},
    )

    def fail_backup_cleanup(path):
        if ".dispatch.previous-" in Path(path).name:
            raise OSError("backup cleanup failed")
        return real_remove(path)

    monkeypatch.setattr(lifecycle_runtime, "_safe_remove", fail_backup_cleanup)

    result = install_from_clone(
        layout,
        source,
        channel="dev",
        ref=DEVELOPMENT_BRANCH,
        run=lambda *_args, **_kwargs: completed(),
    )

    assert result["status"] == "installed_cleanup_incomplete"
    assert result["cleanup_error_code"] == "post_activation_cleanup_failed"
    assert (layout.clone / "marker").read_text(encoding="utf-8") == "new"
    assert list(layout.dispatch_home.glob(".dispatch.previous-*"))


def test_fresh_swap_keeps_rollback_state_after_post_promotion_cleanup_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "marker").write_text("new", encoding="utf-8")
    state = lifecycle_runtime._SwapState()
    real_replace = lifecycle_runtime.os.replace
    real_remove = lifecycle_runtime._safe_remove
    cleanup_calls = 0

    def post_promotion_interrupt(source, destination):
        result = real_replace(source, destination)
        if Path(source) == replacement and Path(destination) == target:
            raise KeyboardInterrupt("post-promotion")
        return result

    def interrupted_cleanup(path):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise KeyboardInterrupt("cleanup interrupted")
        return real_remove(path)

    monkeypatch.setattr(lifecycle_runtime.os, "replace", post_promotion_interrupt)
    monkeypatch.setattr(lifecycle_runtime, "_safe_remove", interrupted_cleanup)

    with pytest.raises(KeyboardInterrupt):
        lifecycle_runtime._swap_directory(replacement, target, state=state)

    assert state.active is True
    assert (target / "marker").read_text(encoding="utf-8") == "new"
    lifecycle_runtime._complete_rollback(lambda: lifecycle_runtime._restore_directory(target, None))
    state.active = False
    assert not target.exists()


def test_directory_swap_and_restore_use_one_backup(tmp_path: Path) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    state = lifecycle_runtime._SwapState()

    backup = lifecycle_runtime._swap_directory(replacement, target, state=state)

    assert state.active is True
    assert backup is not None and backup.is_dir()
    assert (target / "marker").read_text(encoding="utf-8") == "new"
    lifecycle_runtime._restore_directory(target, backup)
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not backup.exists()
    assert not list(tmp_path.glob(".active.failed-*"))


def test_swap_directory_rejects_nonprivate_managed_target(tmp_path: Path) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir(mode=0o700)
    replacement.mkdir(mode=0o700)
    target.chmod(0o777)
    (target / "sentinel").write_text("preserve", encoding="utf-8")

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._swap_directory(replacement, target)

    assert error.value.code == "managed_directory_unsafe"
    assert (target / "sentinel").read_text(encoding="utf-8") == "preserve"
    assert replacement.is_dir()


def test_restore_directory_accepts_post_commit_replace_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "active"
    backup = tmp_path / ".active.previous"
    target.mkdir()
    backup.mkdir()
    (target / "marker").write_text("new", encoding="utf-8")
    (backup / "marker").write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def raise_after_restore(source, destination):
        nonlocal calls
        calls += 1
        result = real_replace(source, destination)
        if calls == 2:
            raise KeyboardInterrupt("post-restore")
        return result

    monkeypatch.setattr(lifecycle_runtime.os, "replace", raise_after_restore)
    lifecycle_runtime._restore_directory(target, backup)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not backup.exists()
    assert not list(tmp_path.glob(".active.failed-*"))


def test_swap_directory_recovers_interrupt_after_backup_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "active"
    replacement = tmp_path / "replacement"
    target.mkdir()
    replacement.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (replacement / "marker").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def interrupt_after_backup(source, destination):
        nonlocal calls
        calls += 1
        result = real_replace(source, destination)
        if calls == 1:
            raise KeyboardInterrupt("post-backup")
        return result

    monkeypatch.setattr(lifecycle_runtime.os, "replace", interrupt_after_backup)
    with pytest.raises(KeyboardInterrupt):
        lifecycle_runtime._swap_directory(replacement, target)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (replacement / "marker").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".active.previous-*"))


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


def test_update_refuses_dirty_checkout_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    monkeypatch.setattr(lifecycle_runtime, "read_installation", lambda _layout: {"channel": "dev"})
    monkeypatch.setattr(
        lifecycle_runtime,
        "assert_checkout_clean",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InstallerError("checkout_dirty", "checkout has local changes")
        ),
    )
    staged = False

    def forbidden_stage(*_args, **_kwargs):
        nonlocal staged
        staged = True
        raise AssertionError("dirty checkout must fail before staging")

    monkeypatch.setattr(lifecycle_runtime, "_stage_repository", forbidden_stage)

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime.install_or_update(layout, channel="dev", update_current=True)

    assert error.value.code == "checkout_dirty"
    assert staged is False


def test_install_refuses_unrecorded_existing_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    sentinel = layout.clone / "user-sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    source = tmp_path / "staged" / "dispatch"
    source.mkdir(parents=True)
    installed = False

    def forbidden_install(*_args, **_kwargs):
        nonlocal installed
        installed = True
        raise AssertionError("unrecorded clone must not be replaced")

    monkeypatch.setattr(lifecycle_runtime, "install_from_clone", forbidden_install)

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime.install_or_update(layout, channel="dev", source=source)

    assert error.value.code == "incomplete_installation"
    assert installed is False
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_explicit_source_update_refuses_dirty_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    source = tmp_path / "staged" / "dispatch"
    source.mkdir(parents=True)
    monkeypatch.setattr(lifecycle_runtime, "read_installation", lambda _layout: {"channel": "dev"})
    monkeypatch.setattr(
        lifecycle_runtime,
        "assert_checkout_clean",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            InstallerError("checkout_dirty", "checkout has local changes")
        ),
    )
    installed = False

    def forbidden_install(*_args, **_kwargs):
        nonlocal installed
        installed = True
        raise AssertionError("dirty checkout must not be replaced")

    monkeypatch.setattr(lifecycle_runtime, "install_from_clone", forbidden_install)

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime.install_or_update(
            layout,
            channel="dev",
            source=source,
            update_current=True,
        )

    assert error.value.code == "checkout_dirty"
    assert installed is False


def test_update_reconciles_from_fresh_staged_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    staged = tmp_path / "staging" / "dispatch"
    staged.mkdir(parents=True)
    work = staged.parent
    captured: dict[str, object] = {}
    monkeypatch.setattr(lifecycle_runtime, "read_installation", lambda _layout: {"channel": "dev"})
    monkeypatch.setattr(lifecycle_runtime, "assert_checkout_clean", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle_runtime, "_stage_repository", lambda *_args, **_kwargs: (staged, work))

    def fake_install(_layout, source, **kwargs):
        captured["source"] = source
        captured["channel"] = kwargs["channel"]
        return {"status": "installed"}

    monkeypatch.setattr(lifecycle_runtime, "install_from_clone", fake_install)
    monkeypatch.setattr(lifecycle_runtime, "_safe_remove", lambda path: captured.setdefault("removed", path))

    result = lifecycle_runtime.install_or_update(layout, channel="dev", update_current=True)

    assert result["status"] == "updated"
    assert captured == {
        "source": staged,
        "channel": "dev",
        "removed": work,
    }


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
        return authority_response(command) or browser_response(command) or completed()

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
    assert error.value.code == "service_conflict"
    assert not any(command[:4] == ("systemctl", "--user", "disable", "--now") for command in commands)
    assert "unrelated" in layout.service_path.read_text()


def test_activation_accepts_exact_service_without_secondary_receipt(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    source = layout.dispatch_home / ".install-tmp" / "candidate" / "dispatch"
    source.parent.parent.mkdir(mode=0o700)
    source.parent.mkdir(mode=0o700)
    (source / ".git").mkdir(parents=True)
    write_test_project(source / "installer")
    write_browser_manager_project(source / "dispatch-core")
    install_user_service(layout, activate=False)
    assert not (layout.state / "service.json").exists()
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
        return authority_response(command) or browser_response(command) or completed()

    result = install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)

    assert result["status"] == "installed"
    assert service_unit_is_owned(layout)
    assert ("systemctl", "--user", "stop", "dispatch.service") in commands


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
        return authority_response(command) or browser_response(command) or completed()

    with pytest.raises(InstallerError) as error:
        install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
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
        return authority_response(command) or browser_response(command) or completed()

    result = install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
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
            "ref": DEVELOPMENT_BRANCH,
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
        return authority_response(command) or browser_response(command) or completed()

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
    subprocess.run(("git", "init", "-q", "-b", DEVELOPMENT_BRANCH, str(layout.clone)), check=True)
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
        ("git", "-C", str(layout.clone), "update-ref", f"refs/remotes/origin/{DEVELOPMENT_BRANCH}", commit),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(layout.clone), "config", f"branch.{DEVELOPMENT_BRANCH}.remote", "origin"),
        check=True,
    )
    subprocess.run(
        (
            "git", "-C", str(layout.clone), "config",
            f"branch.{DEVELOPMENT_BRANCH}.merge", f"refs/heads/{DEVELOPMENT_BRANCH}",
        ),
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
            "ref": DEVELOPMENT_BRANCH,
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
    assert ("systemctl", "--user", "enable", "dispatch.service") in commands
    assert ("systemctl", "--user", "start", "dispatch.service") in commands


def test_uninstall_restores_service_when_stop_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python", encoding="utf-8")
    install_user_command(layout)
    install_user_service(layout, activate=False)
    state = {"active": True, "enabled": True}
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if state["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "stop"):
            state["active"] = False
            raise KeyboardInterrupt("stop interrupted")
        if values[:3] == ("systemctl", "--user", "enable"):
            state["enabled"] = True
        if values[:3] == ("systemctl", "--user", "start"):
            state["active"] = True
        return completed()

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])

    with pytest.raises(KeyboardInterrupt):
        uninstall(layout, run=fake_run)

    assert state == {"active": True, "enabled": True}
    assert layout.service_path.exists()
    assert ("systemctl", "--user", "start", "dispatch.service") in commands


def test_uninstall_status_failure_occurs_before_service_stop_and_browser_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    prepare_plugin_service(layout, "worker")
    acquired = False
    commands: list[tuple[str, ...]] = []

    def forbidden_lock(_layout):
        nonlocal acquired
        acquired = True
        raise AssertionError("browser lock must not be acquired")

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        uninstall_runtime,
        "status_plugin_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("status interrupted")),
    )
    monkeypatch.setattr(uninstall_runtime, "acquire_browser_generation_lock", forbidden_lock)

    with pytest.raises(KeyboardInterrupt):
        uninstall(
            layout,
            run=lambda command, _cwd=None: commands.append(tuple(str(value) for value in command)) or completed(),
        )

    assert acquired is False
    assert not any(command[:3] == ("systemctl", "--user", "stop") for command in commands)
    assert plugin_service_path(layout, "worker").exists()


def test_uninstall_rolls_back_plugin_service_after_later_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    prepare_plugin_service(layout, "worker")
    enable_plugin_service(layout, "worker", run=lambda *args, **kwargs: completed())
    commands: list[tuple[str, ...]] = []
    service_state = {"active": True, "enabled": True}

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if service_state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if service_state["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "disable"):
            service_state.update(active=False, enabled=False)
        if values[:3] == ("systemctl", "--user", "enable"):
            service_state.update(active=True, enabled=True)
        return completed()

    monkeypatch.setattr(uninstall_runtime, "_uninstall_blockers", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        uninstall_runtime,
        "_stage_directory",
        lambda _stage: (_ for _ in ()).throw(RuntimeError("injected after service removal")),
    )

    with pytest.raises(RuntimeError, match="injected after service removal"):
        uninstall(layout, run=fake_run)
    assert plugin_service_path(layout, "worker").exists()
    assert not plugin_service_receipt_path(layout, "worker").exists()
    assert status_plugin_service(layout, "worker", run=fake_run)["status"] == "ready"
    assert ("systemctl", "--user", "enable", "dispatch-plugin-worker.service") in commands
    assert ("systemctl", "--user", "start", "dispatch-plugin-worker.service") in commands


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
        if values == ("systemctl", "--user", "enable", "dispatch.service"):
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
            "ref": DEVELOPMENT_BRANCH,
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
            "ref": DEVELOPMENT_BRANCH,
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
        return browser_response(command) or completed()

    result = lifecycle_runtime.ensure_venv(
        layout,
        destination=destination,
        browser_cache=browser,
        run=fake_run,
    )
    assert result == destination
    assert (destination / "bin" / "python").is_file()


def test_source_install_uses_pip_from_private_copy_without_checkout_pollution(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    commands: list[tuple[str, ...]] = []
    staged_sources: list[Path] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        staged = Path(values[-1])
        staged_sources.append(staged)
        assert staged.is_dir()
        assert staged != source
        assert (staged / "pyproject.toml").is_file()
        return completed()

    result = install_source_distribution(python, source, no_deps=True, run=fake_run)

    assert result.returncode == 0
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == (str(python), "-m", "pip", "install")
    assert "--no-build-isolation" in command
    assert "--no-deps" in command
    assert "--force-reinstall" in command
    assert not any(source.rglob("*.egg-info"))
    assert not any(source.rglob("*.dist-info"))
    assert all(not staged.exists() for staged in staged_sources)


@pytest.mark.parametrize(
    "metadata_name",
    ["dispatch_installer.egg-info", "dispatch_installer-1.0.0.dist-info"],
)
def test_source_install_rejects_preexisting_checkout_metadata(
    tmp_path: Path,
    metadata_name: str,
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    stale = source / "src" / metadata_name
    stale.mkdir()
    python = tmp_path / "venv" / "bin" / "python"

    with pytest.raises(InstallerError) as error:
        install_source_distribution(python, source, no_deps=True, run=lambda *_: completed())

    assert error.value.code == "source_metadata_exists"
    assert stale.is_dir()


def test_source_install_requires_pinned_build_backend(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    manifest = source / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("setuptools==83.0.0", "setuptools>=68"),
        encoding="utf-8",
    )
    python = tmp_path / "venv" / "bin" / "python"

    with pytest.raises(InstallerError) as error:
        install_source_distribution(python, source, no_deps=True, run=lambda *_: completed())

    assert error.value.code == "source_build_backend_invalid"


def test_source_install_rejects_group_writable_project_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    manifest = source / "pyproject.toml"
    manifest.chmod(0o666)
    called = False

    def forbidden_run(command, cwd=None):
        nonlocal called
        called = True
        return completed()

    with pytest.raises(InstallerError) as error:
        install_source_distribution(
            tmp_path / "venv" / "bin" / "python",
            source,
            no_deps=True,
            run=forbidden_run,
        )

    assert error.value.code == "source_project_unsafe"
    assert called is False


def test_plugin_metadata_rejects_group_writable_source(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone, dependencies=[])
    (plugin / "src" / "dispatch_worker" / "__init__.py").chmod(0o666)

    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_metadata(plugin, expected_id="worker")

    assert error.value.code == "source_project_unsafe"


def test_temporary_staging_rejects_unsafe_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = tmp_path / "unsafe-tmp"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o777)
    monkeypatch.setattr(tempfile, "tempdir", str(unsafe))
    layout = make_layout(tmp_path)
    layout.prepare()
    source = tmp_path / "source"
    write_test_project(source)

    try:
        with pytest.raises(InstallerError) as lifecycle_error:
            lifecycle_runtime._prepare_temporary_root(layout)
        assert lifecycle_error.value.code == "directory_unsafe"

        with pytest.raises(InstallerError) as source_error:
            install_source_distribution(
                tmp_path / "venv" / "bin" / "python",
                source,
                no_deps=True,
                run=lambda *_args, **_kwargs: completed(),
            )
        assert source_error.value.code == "directory_unsafe"
        assert not list(unsafe.iterdir())
    finally:
        unsafe.chmod(0o700)


def test_source_install_propagates_interruption_and_removes_private_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    interruption = KeyboardInterrupt("stop")
    staged: Path | None = None

    def interrupt(command, cwd=None):
        nonlocal staged
        staged = Path(command[-1])
        assert staged.is_dir()
        raise interruption

    with pytest.raises(KeyboardInterrupt) as error:
        install_source_distribution(python, source, no_deps=True, run=interrupt)

    assert error.value is interruption
    assert staged is not None and not staged.exists()
    assert not any(source.rglob("*.egg-info"))


def test_source_install_reports_private_copy_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    staged: Path | None = None

    def fake_run(command, cwd=None):
        nonlocal staged
        staged = Path(command[-1])
        return completed()

    monkeypatch.setattr(
        setup_runtime.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    with pytest.raises(InstallerError) as error:
        install_source_distribution(python, source, no_deps=True, run=fake_run)

    assert error.value.code == "source_stage_cleanup_failed"
    assert staged is not None and staged.is_dir()
    monkeypatch.undo()
    shutil.rmtree(staged.parent)


def test_source_install_reports_primary_and_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    write_test_project(source)
    python = tmp_path / "venv" / "bin" / "python"
    staged: Path | None = None

    def interrupt(command, cwd=None):
        nonlocal staged
        staged = Path(command[-1])
        raise KeyboardInterrupt("install interrupted")

    monkeypatch.setattr(
        setup_runtime.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )
    with pytest.raises(InstallerError) as error:
        install_source_distribution(python, source, no_deps=True, run=interrupt)

    assert error.value.code == "source_stage_cleanup_failed"
    assert staged is not None and staged.is_dir()
    monkeypatch.undo()
    shutil.rmtree(staged.parent)


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
        install_from_clone(layout, outside, channel="dev", ref=DEVELOPMENT_BRANCH, run=lambda *_: completed())
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
    obsolete_record = layout.state / "service.json"
    atomic_json(
        obsolete_record,
        {
            "schema_version": 1,
            "unit": str(layout.service_path),
            "unit_sha256": hashlib.sha256(service_unit(layout)).hexdigest(),
            "service": "dispatch.service",
            "contains_secrets": False,
        },
    )
    install_user_service(layout, activate=False)
    assert not obsolete_record.exists()
    assert b"UMask=0077\n" in layout.service_path.read_bytes()
    layout.service_path.chmod(0o666)
    inspection = inspect_user_service(layout, run=lambda *_: completed())
    assert inspection["status"] == "unsafe"
    layout.service_path.chmod(0o600)
    content = layout.service_path.read_bytes()
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

    def fail_every_reload(command, _cwd=None):
        if tuple(command) == ("systemctl", "--user", "daemon-reload"):
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as rollback_error:
        remove_user_service(layout, run=fail_every_reload)
    assert rollback_error.value.code == "service_rollback_failed"
    assert layout.service_path.read_bytes() == content
    layout.service_path.write_text("changed")
    with pytest.raises(InstallerError) as remove_error:
        remove_user_service(layout, run=lambda *_: completed())
    assert remove_error.value.code == "service_unit_unsafe"
    assert layout.service_path.read_text() == "changed"


def test_malformed_obsolete_service_records_are_preserved_before_publication(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    main_record = layout.state / "service.json"
    atomic_json(main_record, {"unexpected": True})

    with pytest.raises(InstallerError) as main_error:
        install_user_service(layout, activate=False)

    assert main_error.value.code == "service_record_unsafe"
    assert json.loads(main_record.read_text(encoding="utf-8")) == {"unexpected": True}
    assert not layout.service_path.exists()

    plugin_record = plugin_service_receipt_path(layout, "worker")
    atomic_json(plugin_record, {"unexpected": True})
    with pytest.raises(InstallerError) as plugin_error:
        prepare_plugin_service(layout, "worker")

    assert plugin_error.value.code == "plugin_service_unsafe"
    assert json.loads(plugin_record.read_text(encoding="utf-8")) == {"unexpected": True}
    assert not plugin_service_path(layout, "worker").exists()


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


def test_uninstall_plan_blocks_nonprivate_managed_directory(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir(mode=0o700)
    layout.clone.chmod(0o777)
    (layout.clone / "sentinel").write_text("preserve", encoding="utf-8")

    plan = plan_uninstall(layout)

    blockers = plan["blockers"]
    assert isinstance(blockers, list)
    assert any("managed path is writable by group or other" in value for value in blockers)
    with pytest.raises(InstallerError):
        uninstall(layout)
    assert (layout.clone / "sentinel").read_text(encoding="utf-8") == "preserve"


def test_uninstall_plan_rejects_unrelated_browser_record(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(layout.browser_installation_record, {"unrelated": True})

    plan = plan_uninstall(layout)

    blockers = plan["blockers"]
    assert isinstance(blockers, list)
    assert any("browser installation record is invalid" in value for value in blockers)
    with pytest.raises(InstallerError):
        uninstall(layout)
    assert json.loads(layout.browser_installation_record.read_text(encoding="utf-8")) == {
        "unrelated": True
    }

    atomic_json(
        layout.browser_installation_record,
        {
            "schema_version": 1,
            "status": "active",
            "playwright_version": "",
            "browser_family": "chromium",
            "chromium_revision": "",
            "chromium_version": None,
            "cache": str(layout.browser_cache),
            "contains_secrets": False,
        },
    )
    malformed = plan_uninstall(layout)
    malformed_blockers = malformed["blockers"]
    assert isinstance(malformed_blockers, list)
    assert any("browser installation record is invalid" in value for value in malformed_blockers)


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


def test_legacy_setup_symlink_parent_is_never_migrated(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    external = tmp_path / "external-install-state"
    external.mkdir(mode=0o700)
    receipt = external / "setup.json"
    receipt.write_text("{}")
    receipt.chmod(0o600)
    (layout.state / "install").symlink_to(external, target_is_directory=True)
    assert migrate_legacy_plugin_config(layout) is False
    assert receipt.exists()


def test_preflight_config_restore_failure_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin_config = layout.config / "plugins.json"

    def migrate(_layout):
        atomic_json(
            plugin_config,
            {
                "schema_version": 1,
                "status": "complete",
                "selected_plugins": [],
                "plugins": [],
                "contains_secrets": False,
            },
        )
        return True

    monkeypatch.setattr(lifecycle_runtime, "migrate_legacy_plugin_config", migrate)
    monkeypatch.setattr(
        lifecycle_runtime,
        "_build_replacement_venv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("build interrupted")),
    )
    monkeypatch.setattr(
        lifecycle_runtime,
        "_restore_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("restore failed")),
    )

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._reconcile_installation(
            layout,
            channel="dev",
            ref=DEVELOPMENT_BRANCH,
            commit=AUTHORITY_COMMIT,
            run=lambda *_args, **_kwargs: completed(),
            now=lambda: datetime.now(UTC),
            status="installed",
        )

    assert error.value.code == "preflight_rollback_failed"
    assert plugin_config.exists()


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
        return authority_response(command) or browser_response(command) or completed()

    real_remove = lifecycle_runtime._safe_remove

    def interrupted_cleanup(path: Path) -> None:
        if path.name.startswith("dispatch-installer-"):
            raise KeyboardInterrupt
        real_remove(path)

    monkeypatch.setattr(lifecycle_runtime, "_safe_remove", interrupted_cleanup)
    result = install_from_clone(layout, source, channel="dev", ref=DEVELOPMENT_BRANCH, run=fake_run)
    assert result["status"] == "installed_cleanup_incomplete"
    assert result["cleanup_error_code"] == "post_activation_cleanup_failed"
    record = read_installation(layout)
    assert record is not None
    assert record["commit"] == AUTHORITY_COMMIT
    assert layout.venv_python.exists()
    assert layout.clone.exists()


def test_replacement_venv_cleanup_failure_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    monkeypatch.setattr(
        lifecycle_runtime,
        "ensure_venv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("build interrupted")),
    )
    monkeypatch.setattr(
        lifecycle_runtime,
        "_safe_remove",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._build_replacement_venv(layout, run=lambda *_args, **_kwargs: completed())

    assert error.value.code == "venv_stage_cleanup_failed"


def test_repository_stage_cleanup_failure_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    monkeypatch.setattr(
        lifecycle_runtime,
        "clone_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("clone interrupted")),
    )
    monkeypatch.setattr(
        lifecycle_runtime,
        "_safe_remove",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(InstallerError) as error:
        lifecycle_runtime._stage_repository(
            layout,
            channel="dev",
            ref=DEVELOPMENT_BRANCH,
            run=lambda *_args, **_kwargs: completed(),
        )

    assert error.value.code == "repository_stage_cleanup_failed"


def test_staged_work_cleanup_interrupt_is_reported(
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
    with pytest.raises(InstallerError) as error:
        lifecycle_runtime.install_or_update(layout, channel="dev")
    assert error.value.code == "repository_stage_cleanup_failed"
    assert work.exists()


def _write_runtime_plugin(root: Path, *, plugin_id: str = "worker", dependencies: list[str] | None = None) -> Path:
    plugin = root / "plugins" / plugin_id
    package = plugin / "src" / f"dispatch_{plugin_id}"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def handle(request):\n"
        "    return request\n\n"
        "def serve(context):\n"
        "    return None\n",
        encoding="utf-8",
    )
    deps = dependencies or []
    (plugin / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires=['setuptools==83.0.0']\n"
        "build-backend='setuptools.build_meta'\n"
        "[project]\n"
        f"name='dispatch-{plugin_id}'\n"
        "version='1.0.0'\n"
        f"dependencies={deps!r}\n"
        "[project.entry-points.\"dispatch.plugins\"]\n"
        f"{plugin_id}='dispatch_{plugin_id}:handle'\n"
        "[project.entry-points.\"dispatch.services\"]\n"
        f"{plugin_id}='dispatch_{plugin_id}:serve'\n"
        "[tool.dispatch]\n"
        f"id='{plugin_id}'\n"
        "capabilities=['long_running']\n",
        encoding="utf-8",
    )
    return plugin


def test_launcher_rejects_symlinked_core_source(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    external = tmp_path / "external-core"
    external.mkdir()
    (external / "command_interface.py").write_text("", encoding="utf-8")
    (layout.clone / "dispatch-core").symlink_to(external, target_is_directory=True)

    with pytest.raises(InstallerError) as error:
        launcher_runtime._prepare_core_environment(layout)

    assert error.value.code == "core_missing"


def test_launcher_rejects_forged_plugin_site_packages(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    core = layout.clone / "dispatch-core"
    core.mkdir()
    (core / "command_interface.py").write_text("", encoding="utf-8")
    _write_runtime_plugin(layout.clone, dependencies=[])
    site_packages = layout.venv / "lib" / "python-test" / "site-packages"
    site_packages.mkdir(parents=True)
    for path in (layout.venv, site_packages.parent.parent, site_packages.parent, site_packages):
        path.chmod(0o700)
    payload = setup_runtime._plugin_config(layout, ["worker"])
    plugins = payload["plugins"]
    assert isinstance(plugins, list) and isinstance(plugins[0], dict)
    external = tmp_path / "outside-plugin"
    external.mkdir()
    plugins[0]["site_packages"] = str(external)
    atomic_json(layout.config / "plugins.json", payload)

    with pytest.raises(InstallerError) as error:
        launcher_runtime._prepare_core_environment(layout)

    assert error.value.code == "plugin_config_invalid"
    assert str(external) not in os.environ.get("DISPATCH_PLUGIN_PATHS", "")


def test_replacement_venv_rejects_symlinked_core_source(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    outside = tmp_path / "outside-core"
    outside.mkdir()
    (outside / "requirements.txt").write_text("outside-package==1\n", encoding="utf-8")
    (layout.clone / "dispatch-core").symlink_to(outside, target_is_directory=True)
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
        return completed()

    with pytest.raises(InstallerError) as error:
        ensure_venv(
            layout,
            destination=tmp_path / "replacement",
            provision_browser=False,
            run=fake_run,
        )

    assert error.value.code == "requirements_missing"
    assert not any("outside-package==1" in command for command in commands)


def test_replacement_venv_rejects_group_writable_core_requirements(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    core = layout.clone / "dispatch-core"
    core.mkdir()
    requirements = core / "requirements.txt"
    requirements.write_text("outside-package==1\n", encoding="utf-8")
    requirements.chmod(0o666)
    write_test_project(layout.clone / "installer")
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
        return completed()

    with pytest.raises(InstallerError) as error:
        ensure_venv(
            layout,
            destination=tmp_path / "replacement",
            provision_browser=False,
            run=fake_run,
        )

    assert error.value.code == "source_project_unsafe"
    assert not any("outside-package==1" in command for command in commands)


def test_replacement_venv_rejects_invalid_plugin_callable_before_install(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    plugin = _write_runtime_plugin(layout.clone, dependencies=[])
    project = plugin / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            "worker='dispatch_worker:handle'",
            "worker='dispatch_worker:missing'",
        ),
        encoding="utf-8",
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return completed()

    with pytest.raises(InstallerError) as error:
        ensure_venv(
            layout,
            destination=tmp_path / "replacement",
            selected_plugins=["worker"],
            provision_browser=False,
            run=fake_run,
        )

    assert error.value.code == "plugin_entry_point_invalid"
    assert not any(
        "dispatch-source-dispatch-worker-" in command[-1]
        for command in commands
        if command
    )


def test_installer_rejects_plugin_entry_point_signature_mismatch(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone, dependencies=[])
    package = plugin / "src" / "dispatch_worker" / "__init__.py"
    package.write_text(
        package.read_text(encoding="utf-8").replace(
            "def serve(context):",
            "def serve():",
        ),
        encoding="utf-8",
    )

    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_metadata(plugin, expected_id="worker")

    assert error.value.code == "plugin_entry_point_invalid"


def test_plugin_dependency_metadata_requires_exact_pins(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(
        layout.clone,
        dependencies=["worker-runtime @ https://example.invalid/runtime.whl"],
    )
    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_dependencies(plugin, expected_id="worker")
    assert error.value.code == "plugin_dependency_invalid"


def _private_source_copy(source: Path, destination: Path) -> Path:
    copied = Path(shutil.copytree(source, destination))
    for path in (copied, *copied.rglob("*")):
        details = path.stat(follow_symlinks=False)
        path.chmod(stat.S_IMODE(details.st_mode) & ~0o022)
    return copied


def test_real_companion_source_metadata_is_installer_readable(tmp_path: Path) -> None:
    source = _private_source_copy(
        Path(__file__).resolve().parents[2] / "plugins" / "companion-bridge",
        tmp_path / "companion-bridge",
    )
    metadata = setup_runtime.plugin_metadata(source, expected_id="companion-bridge")
    assert metadata["long_running"] is True
    assert metadata["dependencies"]


def test_real_paycom_source_metadata_is_installer_readable(tmp_path: Path) -> None:
    source = _private_source_copy(
        Path(__file__).resolve().parents[2] / "plugins" / "paycom",
        tmp_path / "paycom",
    )
    metadata = setup_runtime.plugin_metadata(source, expected_id="paycom")
    assert metadata["collects"] is True
    assert metadata["long_running"] is False
    assert metadata["dependencies"] == []


def test_collect_capability_requires_one_matching_collector_entry_point(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone)
    project = plugin / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        "capabilities=['long_running']",
        "capabilities=['long_running','collect']",
    )
    project.write_text(text, encoding="utf-8")

    with pytest.raises(InstallerError) as missing:
        setup_runtime.plugin_metadata(plugin, expected_id="worker")
    assert missing.value.code == "plugin_collector_missing"

    project.write_text(
        text
        + "[project.entry-points.\"dispatch.collectors\"]\n"
        + "worker='dispatch_worker:collectors'\n",
        encoding="utf-8",
    )
    package = plugin / "src" / "dispatch_worker" / "__init__.py"
    package.write_text(
        package.read_text(encoding="utf-8") + "\ndef collectors():\n    return ()\n",
        encoding="utf-8",
    )
    metadata = setup_runtime.plugin_metadata(plugin, expected_id="worker")
    assert metadata["collects"] is True


def test_non_collecting_plugin_rejects_collector_entry_point(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone)
    project = plugin / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        + "[project.entry-points.\"dispatch.collectors\"]\n"
        + "worker='dispatch_worker:collectors'\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerError) as unexpected:
        setup_runtime.plugin_metadata(plugin, expected_id="worker")
    assert unexpected.value.code == "plugin_collector_unexpected"


def test_setup_rejects_unloadable_collector_target_before_mutation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone)
    package = plugin / "src" / "dispatch_worker" / "__init__.py"
    package.write_text(
        "def handle(request):\n    return {}\n\ndef serve(context):\n    return None\n",
        encoding="utf-8",
    )
    project = plugin / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        "capabilities=['long_running']",
        "capabilities=['long_running','collect']",
    )
    project.write_text(
        text
        + "[project.entry-points.\"dispatch.collectors\"]\n"
        + "worker='dispatch_worker:missing'\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, ["worker"])
    assert error.value.code == "plugin_entry_point_invalid"
    assert not (layout.config / "plugins.json").exists()


def test_replacement_venv_installs_plugin_dependencies_before_direct_registration(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("core==1\n", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    plugin = _write_runtime_plugin(layout.clone, dependencies=["worker-runtime==2.4.0"])
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return completed()

    replacement = tmp_path / "replacement"
    ensure_venv(
        layout,
        destination=replacement,
        selected_plugins=["worker"],
        provision_browser=False,
        run=fake_run,
    )

    dependency_command = next(command for command in commands if "worker-runtime==2.4.0" in command)
    assert dependency_command[:5] == (
        str(replacement / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
    )
    plugin_command = next(
        command
        for command in commands
        if "--force-reinstall" in command and "dispatch-source-dispatch-worker-" in command[-1]
    )
    assert commands.index(dependency_command) > commands.index(next(command for command in commands if "-r" in command))
    assert commands.index(plugin_command) > commands.index(dependency_command)


def test_plugin_dependency_failure_keeps_active_venv_and_selection(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    _write_runtime_plugin(layout.clone, dependencies=["worker-runtime==2.4.0"])
    layout.venv.mkdir()
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("old-venv", encoding="utf-8")
    marker = layout.venv / "active-marker"
    marker.write_text("preserve", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def failing_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("replacement", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        if "worker-runtime==2.4.0" in values:
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as error:
        configure_plugins(layout, ["worker"], run=failing_run)
    assert error.value.code == "plugin_dependencies_failed"
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (layout.config / "plugins.json").exists()
    assert not list(layout.dispatch_home.glob(".dispatch-plugin-*"))


def test_long_running_selection_prepares_secret_free_unit_and_deselection_removes_it(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    plugin = _write_runtime_plugin(layout.clone, dependencies=[])
    layout.venv_python.parent.mkdir(parents=True)
    layout.venv_python.write_text("python", encoding="utf-8")
    layout.venv_python.chmod(0o700)
    fake_site_packages(layout.venv_python)
    commands: list[tuple[str, ...]] = []
    service_state = {"active": False, "enabled": False}

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if service_state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if service_state["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "enable"):
            service_state.update(active=True, enabled=True)
        elif values[:3] == ("systemctl", "--user", "restart"):
            service_state["active"] = True
        elif values[:3] == ("systemctl", "--user", "disable"):
            service_state.update(active=False, enabled=False)
        elif values[:3] == ("systemctl", "--user", "stop"):
            service_state["active"] = False
        return completed()

    result = configure_plugins(layout, ["worker"], run=fake_run)
    assert result["services"]["prepared"][0]["status"] == "prepared"
    unit = plugin_service_path(layout, "worker")
    receipt = plugin_service_receipt_path(layout, "worker")
    content = unit.read_text(encoding="utf-8")
    assert "plugin serve \"worker\"" in content
    assert "password" not in content.lower()
    assert not receipt.exists()
    assert not any("--now" in command for command in commands)

    enabled = enable_plugin_service(layout, "worker", run=fake_run)
    assert enabled["status"] == "enabled"
    assert status_plugin_service(layout, "worker", run=fake_run)["status"] == "ready"
    disabled = disable_plugin_service(layout, "worker", run=fake_run)
    assert disabled["status"] == "disabled"
    configure_plugins(layout, [], run=fake_run)
    assert not unit.exists()
    assert not receipt.exists()


def test_plugin_service_enable_fails_before_systemd_when_health_is_degraded(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[:4] == (str(layout.command_path), "plugin", "health", "worker"):
            return completed(returncode=1)
        if values[:3] in {
            ("systemctl", "--user", "is-active"),
            ("systemctl", "--user", "is-enabled"),
        }:
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as error:
        enable_plugin_service(layout, "worker", run=fake_run)
    assert error.value.code == "plugin_service_not_ready"
    assert not any(
        len(command) > 2
        and command[0] == "systemctl"
        and command[2] in {"daemon-reload", "enable", "restart", "disable"}
        for command in commands
    )


def test_plugin_service_enable_interruption_restores_previous_state(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    state = {"active": False, "enabled": False}

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[:4] == (str(layout.command_path), "plugin", "health", "worker"):
            return completed()
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if state["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "enable"):
            state.update(active=True, enabled=True)
            raise KeyboardInterrupt("enable interrupted")
        if values[:3] == ("systemctl", "--user", "disable"):
            state.update(active=False, enabled=False)
        return completed()

    with pytest.raises(KeyboardInterrupt):
        enable_plugin_service(layout, "worker", run=fake_run)

    assert state == {"active": False, "enabled": False}
    assert plugin_service_path(layout, "worker").exists()


def test_plugin_service_activation_failure_restores_prepared_projection(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    before_unit = plugin_service_path(layout, "worker").read_bytes()
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[:3] in {
            ("systemctl", "--user", "is-active"),
            ("systemctl", "--user", "is-enabled"),
        }:
            return completed(returncode=1)
        if values[:3] == ("systemctl", "--user", "restart"):
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as error:
        enable_plugin_service(layout, "worker", run=fake_run)
    assert error.value.code == "plugin_service_activation_failed"
    assert plugin_service_path(layout, "worker").read_bytes() == before_unit
    assert not plugin_service_receipt_path(layout, "worker").exists()
    assert ("systemctl", "--user", "disable", "--now", "dispatch-plugin-worker.service") in commands


def test_activation_stops_active_disabled_plugin_service(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed()
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=1)
        return completed()

    states = stop_plugin_services_for_activation(layout, ["worker"], run=fake_run)
    assert states == [{"plugin_id": "worker", "active": True, "enabled": False}]
    assert ("systemctl", "--user", "stop", "dispatch-plugin-worker.service") in commands


def test_partial_plugin_service_stop_is_rolled_back(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    for plugin_id in ("alpha", "beta"):
        prepare_plugin_service(layout, plugin_id)
    states = {
        "alpha": {"active": True, "enabled": True},
        "beta": {"active": True, "enabled": True},
    }
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        commands.append(values)
        service = values[-1] if values else ""
        plugin_id = service.removeprefix("dispatch-plugin-").removesuffix(".service")
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if states[plugin_id]["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if states[plugin_id]["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "stop"):
            states[plugin_id]["active"] = False
            if plugin_id == "beta":
                raise KeyboardInterrupt("second stop interrupted")
        if values[:3] == ("systemctl", "--user", "enable"):
            states[plugin_id]["enabled"] = True
        if values[:3] == ("systemctl", "--user", "start"):
            states[plugin_id]["active"] = True
        return completed()

    with pytest.raises(KeyboardInterrupt):
        stop_plugin_services_for_activation(layout, ["alpha", "beta"], run=fake_run)

    assert states["alpha"] == {"active": True, "enabled": True}
    assert states["beta"] == {"active": True, "enabled": True}
    assert ("systemctl", "--user", "start", "dispatch-plugin-alpha.service") in commands
    assert ("systemctl", "--user", "start", "dispatch-plugin-beta.service") in commands


def test_reconcile_accepts_exact_plugin_unit_without_receipt(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    _write_runtime_plugin(layout.clone, dependencies=[])
    ensure_private_directory(layout.service_directory, "service directory")
    unit = plugin_service_path(layout, "worker")
    unit.write_bytes(plugin_service_unit(layout, "worker"))
    unit.chmod(0o600)

    result = reconcile_plugin_services(
        layout,
        ["worker"],
        run=lambda *args, **kwargs: completed(returncode=1),
    )
    assert result["status"] == "ready"
    prepared = result["prepared"]
    assert isinstance(prepared, list)
    assert prepared[0]["plugin_id"] == "worker"
    assert not plugin_service_receipt_path(layout, "worker").exists()


def test_reconcile_migrates_safe_receipt_only_plugin_state(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    _write_runtime_plugin(layout.clone, dependencies=[])
    content = plugin_service_unit(layout, "worker")
    receipt = plugin_service_receipt_path(layout, "worker")
    atomic_json(
        receipt,
        {
            "schema_version": 1,
            "plugin_id": "worker",
            "unit": str(plugin_service_path(layout, "worker")),
            "unit_sha256": hashlib.sha256(content).hexdigest(),
            "service": "dispatch-plugin-worker.service",
            "status": "prepared",
            "contains_secrets": False,
        },
    )

    result = reconcile_plugin_services(
        layout,
        ["worker"],
        run=lambda *args, **kwargs: completed(returncode=1),
    )

    assert result["status"] == "ready"
    assert plugin_service_path(layout, "worker").read_bytes() == content
    assert not receipt.exists()


def test_doctor_marks_malformed_plugin_config_unsafe(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    config = layout.config / "plugins.json"
    config.write_text("{not-json", encoding="utf-8")
    config.chmod(0o600)

    report = inspect_installation(layout)
    assert report["ok"] is False
    assert report["status"] == "unsafe"
    assert report["checks"]["plugins"]["status"] == "unsafe"


def test_plugin_capabilities_reject_unhashable_values_with_bounded_error(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    plugin = _write_runtime_plugin(layout.clone, dependencies=[])
    project = plugin / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            "capabilities=['long_running']",
            "capabilities=[{}]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_metadata(plugin, expected_id="worker")
    assert error.value.code == "plugin_manifest_invalid"


def test_replacement_venv_rejects_conflicting_plugin_pins(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    layout.clone.mkdir()
    (layout.clone / "dispatch-core").mkdir()
    (layout.clone / "dispatch-core" / "requirements.txt").write_text("", encoding="utf-8")
    write_test_project(layout.clone / "installer")
    _write_runtime_plugin(layout.clone, plugin_id="worker", dependencies=["shared-runtime==1.0.0"])
    _write_runtime_plugin(layout.clone, plugin_id="other", dependencies=["shared-runtime==2.0.0"])

    def fake_run(command, cwd=None):
        values = tuple(str(value) for value in command)
        if values[1:3] == ("-m", "venv"):
            python = Path(values[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("python", encoding="utf-8")
            python.chmod(0o700)
            fake_site_packages(python)
        return completed()

    with pytest.raises(InstallerError) as error:
        ensure_venv(
            layout,
            destination=tmp_path / "replacement",
            selected_plugins=["worker", "other"],
            provision_browser=False,
            run=fake_run,
        )
    assert error.value.code == "plugin_dependency_conflict"


def test_remove_plugin_service_rejects_symlinked_receipt_root(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    receipt_parent = layout.state / "plugins"
    receipt_parent.mkdir(mode=0o700)
    external = tmp_path / "external-receipts"
    external.mkdir(mode=0o700)
    services = receipt_parent / "services"
    services.symlink_to(external, target_is_directory=True)
    receipt = external / "worker.json"
    content = plugin_service_unit(layout, "worker")
    atomic_json(
        receipt,
        {
            "schema_version": 1,
            "plugin_id": "worker",
            "unit": str(plugin_service_path(layout, "worker")),
            "unit_sha256": hashlib.sha256(content).hexdigest(),
            "service": "dispatch-plugin-worker.service",
            "status": "prepared",
            "contains_secrets": False,
        },
    )

    with pytest.raises(InstallerError) as error:
        remove_plugin_service(layout, "worker", run=lambda *args, **kwargs: completed())
    assert error.value.code == "plugin_service_unsafe"
    assert receipt.exists()


def test_remove_plugin_service_restores_after_reload_interruption(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    state = {"active": True, "enabled": True}
    reloads = 0

    def fake_run(command, cwd=None):
        nonlocal reloads
        values = tuple(str(value) for value in command)
        if values[:3] == ("systemctl", "--user", "is-active"):
            return completed(returncode=0 if state["active"] else 1)
        if values[:3] == ("systemctl", "--user", "is-enabled"):
            return completed(returncode=0 if state["enabled"] else 1)
        if values[:3] == ("systemctl", "--user", "disable"):
            state.update(active=False, enabled=False)
        if values[:3] == ("systemctl", "--user", "daemon-reload"):
            reloads += 1
            if reloads == 1:
                raise KeyboardInterrupt("reload interrupted")
        if values[:3] == ("systemctl", "--user", "enable"):
            state["enabled"] = True
        if values[:3] == ("systemctl", "--user", "start"):
            state["active"] = True
        return completed()

    with pytest.raises(KeyboardInterrupt):
        remove_plugin_service(layout, "worker", run=fake_run)

    assert plugin_service_path(layout, "worker").read_bytes() == plugin_service_unit(layout, "worker")
    assert state == {"active": True, "enabled": True}
    assert reloads == 2


def test_remove_plugin_service_reports_failed_restart_rollback(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    enable_plugin_service(layout, "worker", run=lambda *args, **kwargs: completed())
    reloads = 0

    def fake_run(command, cwd=None):
        nonlocal reloads
        values = tuple(str(value) for value in command)
        if values[:3] == ("systemctl", "--user", "daemon-reload"):
            reloads += 1
            return completed(returncode=1 if reloads == 1 else 0)
        if values[:3] == ("systemctl", "--user", "enable"):
            return completed(returncode=1)
        return completed()

    with pytest.raises(InstallerError) as error:
        remove_plugin_service(layout, "worker", run=fake_run)
    assert error.value.code == "plugin_service_rollback_failed"
    assert plugin_service_path(layout, "worker").exists()
    assert not plugin_service_receipt_path(layout, "worker").exists()


def test_stopped_and_disabled_exact_service_is_prepared(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    prepare_plugin_service(layout, "worker")
    enable_plugin_service(layout, "worker", run=lambda *args, **kwargs: completed())

    status = status_plugin_service(
        layout,
        "worker",
        run=lambda *args, **kwargs: completed(returncode=1),
    )
    assert "receipt_status" not in status
    assert status["status"] == "prepared"


def test_required_plugin_service_cannot_be_missing(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    report = inspect_plugin_services(
        layout,
        ["worker"],
        run=lambda *args, **kwargs: completed(returncode=1),
    )
    assert report["status"] == "unsafe"
    services = report["services"]
    assert isinstance(services, dict)
    assert services["worker"]["status"] == "missing"


def test_service_state_restore_preserves_enabled_but_inactive(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        commands.append(tuple(str(value) for value in command))
        return completed()

    restore_systemd_service_state(
        "dispatch-plugin-worker.service",
        {"active": False, "enabled": True},
        run=fake_run,
    )
    assert commands == [
        ("systemctl", "--user", "enable", "dispatch-plugin-worker.service"),
        ("systemctl", "--user", "stop", "dispatch-plugin-worker.service"),
    ]


def test_main_service_ownership_is_derived_without_state_receipt(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    install_user_service(layout, activate=False)
    external = tmp_path / "external-state"
    layout.state.rename(external)
    layout.state.symlink_to(external, target_is_directory=True)

    assert service_unit_is_owned(layout) is True


def test_doctor_and_cli_bound_stale_plugin_selection(tmp_path: Path, monkeypatch, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    atomic_json(
        layout.config / "plugins.json",
        {
            "schema_version": 1,
            "status": "complete",
            "selected_plugins": ["missing-plugin"],
            "plugins": [],
            "contains_secrets": False,
        },
    )
    report = inspect_installation(layout)
    assert report["status"] == "unsafe"
    assert report["checks"]["plugins"]["status"] == "unsafe"

    monkeypatch.setattr(cli_runtime, "read_installation", lambda current: {"channel": "dev"})
    result = installer_main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "plugin-service",
            "status",
            "missing-plugin",
        ]
    )
    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "plugin_config_invalid"
