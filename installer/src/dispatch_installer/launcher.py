"""Dispatch launcher: route installer-owned commands or execute cloned Core."""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from .cli import main as installer_main
from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError
from .setup import load_plugin_config, run_setup

_LIFECYCLE = {"doctor", "verify", "setup", "plugin-service", "uninstall", "update", "repair", "channel", "switch-channel"}

_HELP = """Usage: dispatch <command> [options]

Core commands:
  health, authentication, browser, collection, service

Lifecycle commands:
  setup, plugin-service, update, repair, channel, doctor, verify, uninstall

Run 'dispatch <command> --help' for command-specific help.
"""


def _prepare_core_environment(layout: InstallLayout) -> list[Path]:
    code_root = layout.clone / "dispatch-core"
    if not code_root.is_dir():
        raise InstallerError("core_missing", "cloned Dispatch Core is missing")
    package_root = code_root
    config = load_plugin_config(layout)
    plugins = config.get("plugins", [])
    if not isinstance(plugins, list):
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid")
    plugin_ids: list[str] = []
    plugin_paths: list[str] = []
    for plugin in plugins:
        if not isinstance(plugin, dict) or not isinstance(plugin.get("id"), str) or not isinstance(plugin.get("site_packages"), str):
            raise InstallerError("plugin_config_invalid", "plugin configuration is invalid")
        plugin_ids.append(str(plugin["id"]))
        plugin_paths.append(str(plugin["site_packages"]))
    values = {
        "DISPATCH_HOME": str(layout.dispatch_home),
        "DISPATCH_CODE_ROOT": str(layout.clone),
        "DISPATCH_CONFIG_ROOT": str(layout.config),
        "DISPATCH_SECRETS_ROOT": str(layout.secrets),
        "DISPATCH_DATA_ROOT": str(layout.data),
        "DISPATCH_STATE_ROOT": str(layout.state),
        "DISPATCH_CACHE_ROOT": str(layout.cache),
        "DISPATCH_LOGS_ROOT": str(layout.logs),
        "DISPATCH_RUNTIME_ROOT": str(layout.run),
        "DISPATCH_ACTIVE_PLUGINS": ",".join(plugin_ids),
        "DISPATCH_PLUGIN_PATHS": os.pathsep.join(plugin_paths),
        "PLAYWRIGHT_BROWSERS_PATH": str(layout.browser_cache),
    }
    os.environ.update(values)
    paths = [package_root, *(Path(path) for path in plugin_paths)]
    for path in reversed(paths):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return paths


def _run_core(arguments: list[str], *, prog: str = "dispatch") -> int:
    layout = InstallLayout.from_environment()
    _prepare_core_environment(layout)
    interface = importlib.import_module("command_interface")
    return int(interface.main(arguments, prog=prog))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    arguments = [argument for argument in arguments if argument != "--json"]
    if not arguments or arguments in (["--help"], ["-h"]):
        print(_HELP, end="")
        return 0
    if arguments[0] in _LIFECYCLE:
        cli_arguments = ["--json", *arguments] if json_output else arguments
        return installer_main(cli_arguments)
    try:
        return _run_core(arguments)
    except (InstallerError, ImportError, OSError, ValueError) as exc:
        payload = {"ok": False, "action": arguments[0], "status": "error", "data": {}, "error": {"code": "launch_failed", "message": str(exc)[:256]}}
        print(json.dumps(payload, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
