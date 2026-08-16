"""Explicit built-in plugin setup from the checked-out source tree."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from .layout import (
    InstallLayout,
    InstallerError,
    assert_user_owned_directory,
    atomic_json,
    read_json,
)
from .service import service_unit_is_owned

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _plugin_id_map(layout: InstallLayout) -> dict[str, Path]:
    root = layout.clone / "plugins"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise InstallerError("plugins_root_unsafe", "cloned plugins directory is unsafe")
    if not root.exists():
        return {}
    result: dict[str, Path] = {}
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        manifest = directory / "pyproject.toml"
        if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise InstallerError("plugin_manifest_invalid", f"cannot read plugin metadata: {manifest}") from exc
        entry_points = project.get("project", {}).get("entry-points", {}).get("dispatch.plugins", {})
        if not isinstance(entry_points, dict):
            raise InstallerError("plugin_manifest_invalid", f"plugin entry points are invalid: {manifest}")
        ids = [value for value in entry_points if isinstance(value, str)]
        if not ids:
            ids = [directory.name]
        for plugin_id in ids:
            if plugin_id in result:
                raise InstallerError("plugin_duplicate", f"built-in plugin ID is duplicated: {plugin_id}")
            result[plugin_id] = directory
    return result


def available_plugins(layout: InstallLayout) -> list[str]:
    return sorted(_plugin_id_map(layout))


def _site_packages(layout: InstallLayout) -> Path:
    candidates = sorted((layout.venv / "lib").glob("python*/site-packages")) if (layout.venv / "lib").exists() else []
    if candidates:
        return candidates[-1]
    return layout.venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _plugin_config(layout: InstallLayout, selected: list[str]) -> dict[str, object]:
    site_packages = _site_packages(layout)
    catalog = _plugin_id_map(layout)
    plugins: list[dict[str, object]] = []
    for plugin_id in selected:
        project = tomllib.loads((catalog[plugin_id] / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = project.get("tool", {}).get("dispatch", {})
        capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise InstallerError("plugin_manifest_invalid", f"plugin capabilities are invalid: {plugin_id}")
        plugins.append(
            {
                "id": plugin_id,
                "source": str(catalog[plugin_id]),
                "site_packages": str(site_packages),
                "capabilities": capabilities,
            }
        )
    return {
        "schema_version": 1,
        "status": "complete",
        "selected_plugins": selected,
        "plugins": plugins,
        "contains_secrets": False,
    }


def configure_plugins(
    layout: InstallLayout,
    selected: Sequence[str],
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    selected_ids = list(selected)
    if len(selected_ids) != len(set(selected_ids)):
        raise InstallerError("plugin_duplicate", "a plugin cannot be selected twice")
    catalog = _plugin_id_map(layout)
    unknown = sorted(set(selected_ids) - set(catalog))
    if unknown:
        raise InstallerError("plugin_unknown", f"unknown built-in plugin: {unknown[0]}")
    service_present = layout.service_path.exists() or layout.service_path.is_symlink()
    if service_present and not service_unit_is_owned(layout):
        raise InstallerError("service_unit_unsafe", "Dispatch service unit is not Dispatch-owned")
    for plugin_id in selected_ids:
        source = catalog[plugin_id]
        completed = run(
            (
                str(layout.venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--editable",
                str(source),
            ),
            None,
        )
        if completed.returncode != 0:
            raise InstallerError("plugin_install_failed", f"could not install built-in plugin: {plugin_id}")
    config = _plugin_config(layout, selected_ids)
    atomic_json(layout.config / "plugins.json", config)
    if service_present:
        completed = run(("systemctl", "--user", "restart", "dispatch.service"), None)
        if completed.returncode != 0:
            raise InstallerError("service_restart_failed", "Dispatch service could not be restarted after setup")
    return {"status": "complete", "selected_plugins": selected_ids, "plugins": config["plugins"]}


def load_plugin_config(layout: InstallLayout) -> dict[str, object]:
    path = layout.config / "plugins.json"
    if not path.exists():
        return {"schema_version": 1, "plugins": [], "contains_secrets": False}
    try:
        payload = read_json(path)
    except InstallerError as exc:
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("contains_secrets") is not False
        or payload.get("status") != "complete"
        or not isinstance(payload.get("selected_plugins"), list)
        or not isinstance(payload.get("plugins"), list)
    ):
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid")
    return payload


def migrate_legacy_plugin_config(layout: InstallLayout) -> bool:
    """Import only a complete, non-secret legacy built-in selection."""
    current = layout.config / "plugins.json"
    legacy = layout.state / "install" / "setup.json"
    try:
        assert_user_owned_directory(legacy.parent, "legacy installation state")
    except InstallerError:
        return False
    if current.exists() or not legacy.is_file() or legacy.is_symlink():
        return False
    try:
        payload = read_json(legacy)
    except InstallerError:
        return False
    if not isinstance(payload, dict):
        return False
    selected = payload.get("selected_plugins")
    plugins = payload.get("plugins")
    product_version = payload.get("product_version")
    expected_fields = {
        "schema_version",
        "status",
        "product_version",
        "selected_plugins",
        "plugins",
        "contains_secrets",
    }
    plugin_fields = {"id", "package", "version", "release_id", "site_packages", "capabilities"}
    if (
        set(payload) != expected_fields
        or payload.get("schema_version") != 1
        or payload.get("status") != "complete"
        or payload.get("contains_secrets") is not False
        or not isinstance(product_version, str)
        or not product_version
        or len(product_version) > 128
        or not isinstance(selected, list)
        or not isinstance(plugins, list)
        or any(not isinstance(item, str) for item in selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(set(available_plugins(layout)))
        or len(plugins) != len(selected)
        or any(
            not isinstance(item, dict)
            or set(item) != plugin_fields
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("package"), str)
            or not isinstance(item.get("version"), str)
            or not isinstance(item.get("release_id"), str)
            or not isinstance(item.get("site_packages"), str)
            or not isinstance(item.get("capabilities"), list)
            or any(not isinstance(capability, str) for capability in item.get("capabilities", []))
            for item in plugins
        )
        or selected != [item["id"] for item in plugins]
    ):
        return False
    atomic_json(current, _plugin_config(layout, selected))
    return True


def run_setup(layout: InstallLayout, argv: list[str] | None = None, *, human: bool = True, run: RunCommand = _run) -> int:
    parser = argparse.ArgumentParser(prog="dispatch setup")
    parser.add_argument("--plugin", action="append", default=[], help="built-in plugin ID; may be repeated")
    parser.add_argument("--list", action="store_true", help="list built-in plugins")
    parser.add_argument("--yes", action="store_true", help="confirm the selected plugins")
    args = parser.parse_args(argv)
    plugins = available_plugins(layout)
    if args.list:
        payload = {"ok": True, "action": "setup", "status": "available", "plugins": plugins}
        print(json.dumps(payload, sort_keys=True))
        return 0
    selected = list(args.plugin)
    if not args.yes:
        if not human:
            print(json.dumps({"ok": False, "action": "setup", "status": "error", "error": {"code": "confirmation_required"}}))
            return 1
        print("Available built-in plugins:")
        for index, plugin_id in enumerate(plugins, start=1):
            print(f"  {index}. {plugin_id}")
        answer = input("Select plugin numbers separated by commas, or press Enter for Core only: ").strip()
        if answer:
            try:
                indexes = [int(value.strip()) for value in answer.split(",")]
                if any(index < 1 or index > len(plugins) for index in indexes):
                    raise ValueError
                selected = [plugins[index - 1] for index in indexes]
            except ValueError as exc:
                raise InstallerError("plugin_selection_invalid", "plugin selection is invalid") from exc
    result = configure_plugins(layout, selected, run=run)
    print(json.dumps({"ok": True, "action": "setup", **result}, sort_keys=True))
    return 0


__all__ = ["available_plugins", "configure_plugins", "load_plugin_config", "migrate_legacy_plugin_config", "run_setup"]
