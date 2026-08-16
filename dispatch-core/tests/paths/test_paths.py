from __future__ import annotations

from pathlib import Path

import pytest

from paths import DispatchPaths, PathConfigError, require_within

ROOT = Path(__file__).resolve().parents[3]


def test_defaults_follow_a_different_temporary_home_without_creating_paths(tmp_path: Path) -> None:
    home = tmp_path / "users" / "portable-user"
    paths = DispatchPaths.from_environment({"HOME": str(home)}, code_root=ROOT)

    assert paths.home == home
    assert paths.code == ROOT
    assert paths.config == home / ".dispatch" / "config"
    assert paths.secrets == home / ".dispatch" / "secrets"
    assert paths.data == home / ".dispatch" / "data"
    assert paths.state == home / ".dispatch" / "state"
    assert paths.cache == home / ".dispatch" / "cache"
    assert paths.logs == home / ".dispatch" / "logs"
    assert paths.runtime == home / ".dispatch" / "run"
    assert paths.build_output("dispatch-core") == paths.cache / "build" / "dispatch-core"
    assert not home.exists()


def test_dispatch_home_and_owner_environment_are_explicit(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path / "home"),
        "DISPATCH_HOME": str(tmp_path / "dispatch-home"),
        "XDG_RUNTIME_DIR": str(tmp_path / "ignored-xdg-runtime"),
    }
    paths = DispatchPaths.from_environment(env, code_root=ROOT)
    owner = paths.owner_environment("handbook")

    assert paths.config == tmp_path / "dispatch-home" / "config"
    assert paths.runtime == tmp_path / "dispatch-home" / "run"
    assert owner["DISPATCH_CODE_ROOT"] == str(ROOT)
    assert owner["DISPATCH_HANDBOOK_DATA_ROOT"] == str(tmp_path / "dispatch-home" / "data" / "handbook")
    assert owner["DISPATCH_OWNER_STATE_ROOT"] == str(tmp_path / "dispatch-home" / "state" / "handbook")
    assert owner["DISPATCH_OWNER_SECRETS_ROOT"] == str(tmp_path / "dispatch-home" / "secrets" / "handbook")


def test_individual_roots_can_override_dispatch_home(tmp_path: Path) -> None:
    paths = DispatchPaths.from_environment(
        {
            "HOME": str(tmp_path / "home"),
            "DISPATCH_HOME": str(tmp_path / "dispatch-home"),
            "DISPATCH_DATA_ROOT": str(tmp_path / "separate-data"),
        },
        code_root=ROOT,
    )

    assert paths.config == tmp_path / "dispatch-home" / "config"
    assert paths.data == tmp_path / "separate-data"


def test_existing_dispatch_home_must_be_private(tmp_path: Path) -> None:
    dispatch_home = tmp_path / "dispatch-home"
    dispatch_home.mkdir(mode=0o777)
    dispatch_home.chmod(0o777)
    with pytest.raises(PathConfigError, match="private user-owned"):
        DispatchPaths.from_environment(
            {"HOME": str(tmp_path / "home"), "DISPATCH_HOME": str(dispatch_home)},
            code_root=ROOT,
        )


def test_relative_traversal_colliding_and_source_owned_roots_are_rejected(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    bad_environments = (
        {"HOME": home, "DISPATCH_DATA_ROOT": "relative/data"},
        {"HOME": home, "DISPATCH_STATE_ROOT": str(tmp_path / "state" / ".." / "escape")},
        {
            "HOME": home,
            "DISPATCH_CONFIG_ROOT": str(tmp_path / "shared"),
            "DISPATCH_DATA_ROOT": str(tmp_path / "shared"),
        },
        {"HOME": home, "DISPATCH_CACHE_ROOT": str(ROOT / "generated-cache")},
        {"HOME": home, "DISPATCH_BUILD_OUTPUT": str(ROOT / "generated-build")},
    )
    for env in bad_environments:
        with pytest.raises(PathConfigError):
            DispatchPaths.from_environment(env, code_root=ROOT)

    paths = DispatchPaths.from_environment({"HOME": home}, code_root=ROOT)
    with pytest.raises(PathConfigError):
        paths.owner_root("data", "../escape")
    with pytest.raises(PathConfigError):
        paths.owner_root("unknown", "handbook")


def test_symlink_parent_alias_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    code = tmp_path / "code"
    home.mkdir()
    real.mkdir()
    alias.symlink_to(real, target_is_directory=True)
    code.mkdir()
    with pytest.raises(PathConfigError, match="symlink alias"):
        DispatchPaths.from_environment(
            {"HOME": str(home), "DISPATCH_CONFIG_ROOT": str(alias / "config")},
            code_root=code,
        )


def test_nested_primary_roots_are_rejected(tmp_path: Path) -> None:
    base = tmp_path / "private"
    env = {
        "HOME": str(tmp_path / "home"),
        "DISPATCH_DATA_ROOT": str(base),
        "DISPATCH_STATE_ROOT": str(base / "state"),
    }
    with pytest.raises(PathConfigError, match="cannot overlap"):
        DispatchPaths.from_environment(environ=env, code_root=ROOT)


def test_owner_root_rejects_existing_symlink_alias(tmp_path: Path) -> None:
    paths = DispatchPaths.from_environment(environ={"HOME": str(tmp_path / "home")}, code_root=ROOT)
    paths.data.mkdir(parents=True)
    real = paths.data / "real-owner"
    real.mkdir()
    (paths.data / "handbook").symlink_to(real, target_is_directory=True)

    with pytest.raises(PathConfigError, match="symlink alias"):
        paths.owner_root("data", "handbook")


def test_existing_symlink_cannot_escape_a_declared_root(tmp_path: Path) -> None:
    root = tmp_path / "private-data"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    allowed = root / "handbook" / "index.sqlite3"
    assert require_within(allowed, root, "handbook index") == allowed
    with pytest.raises(PathConfigError):
        require_within(root / "escape" / "index.sqlite3", root, "handbook index")
