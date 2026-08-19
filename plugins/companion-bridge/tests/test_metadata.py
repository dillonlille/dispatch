from pathlib import Path
import json
import os
import stat
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_metadata_declares_exact_plugin_and_auxiliary_entry_points():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["dispatch"]["id"] == "companion-bridge"
    assert project["project"]["entry-points"]["dispatch.plugins"] == {"companion-bridge": "companion_bridge.service:handle"}
    assert project["project"]["entry-points"]["dispatch.services"] == {"companion-bridge": "companion_bridge.foreground_service:run"}
    assert project["project"]["entry-points"]["dispatch.configurators"] == {"companion-bridge": "companion_bridge.configurator:configure"}
    assert all("==" in dependency and " @ " not in dependency for dependency in project["project"]["dependencies"])
    assert project["tool"]["dispatch"]["capabilities"] == [
        "read_local_data",
        "mutate_data",
        "network",
        "authentication",
        "direct_delivery",
        "long_running",
    ]


def test_safe_slack_manifest_has_socket_mode_and_no_urls_or_tokens():
    manifest = json.loads((ROOT / "slack-app-manifest.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["socket_mode_enabled"] is True
    rendered = json.dumps(manifest)
    assert "xox" not in rendered and "xapp" not in rendered and "https://" not in rendered


def test_lifecycle_scripts_are_owner_executable_and_not_writable():
    for name in ("test", "build", "verify", "health"):
        mode = stat.S_IMODE((ROOT / "scripts" / name).stat().st_mode)
        assert mode & stat.S_IXUSR
        assert not mode & 0o022


def test_source_tree_has_no_legacy_runtime_directories():
    assert not (ROOT / "runtime").exists()
    assert not (ROOT / "releases").exists()
    assert not (ROOT / "receipts").exists()
