from __future__ import annotations

import importlib
import io
import json
import os
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout

from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError
from .output import emit, format_help
from .setup import active_plugins, run_setup


def _error(code: str, message: str, *, json_output: bool = False) -> int:
    emit(
        {
            "ok": False,
            "action": "launch",
            "status": "error",
            "data": {},
            "freshness": None,
            "delivery": None,
            "error": {"code": code, "message": message[:256]},
        },
        json_output=json_output,
    )
    return 1


def _run_structured(command: Callable[[], int], *, json_output: bool, action: str) -> int:
    captured = io.StringIO()
    captured_error = io.StringIO()
    try:
        with redirect_stdout(captured), redirect_stderr(captured_error):
            result = command()
    except SystemExit as exc:
        result = exc.code if isinstance(exc.code, int) else 1
        output = captured.getvalue()
        error_output = captured_error.getvalue()
        if not json_output:
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
            if error_output:
                print(error_output, end="" if error_output.endswith("\n") else "\n", file=sys.stderr)
            return result
        if result == 0:
            emit(
                {
                    "ok": True,
                    "action": action,
                    "status": "help",
                    "data": {"help": output.rstrip()},
                    "error": None,
                },
                json_output=True,
            )
            return 0
        diagnostic = next(
            (line.strip() for line in reversed(error_output.splitlines()) if line.strip()),
            "invalid command arguments",
        )
        if ": error: " in diagnostic:
            diagnostic = diagnostic.split(": error: ", 1)[1]
        emit(
            {
                "ok": False,
                "action": action,
                "status": "error",
                "data": {},
                "error": {"code": "invalid_arguments", "message": diagnostic[:256]},
            },
            json_output=True,
        )
        return result
    output = captured.getvalue()
    error_output = captured_error.getvalue()
    if not output.strip():
        if json_output:
            return _error(
                "structured_output_missing",
                "Dispatch command returned no structured output",
                json_output=True,
            )
        if error_output:
            print(error_output, end="" if error_output.endswith("\n") else "\n", file=sys.stderr)
        return result
    if json_output:
        try:
            payload = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return _error(
                "structured_output_invalid",
                "Dispatch command returned invalid structured output",
                json_output=True,
            )
        if not isinstance(payload, dict):
            return _error(
                "structured_output_invalid",
                "Dispatch command returned invalid structured output",
                json_output=True,
            )
        emit(payload, json_output=True)
        return result
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        print(output, end="" if output.endswith("\n") else "\n")
    else:
        if isinstance(payload, dict):
            emit(payload)
        else:
            print(output, end="" if output.endswith("\n") else "\n")
    if error_output:
        print(error_output, end="" if error_output.endswith("\n") else "\n", file=sys.stderr)
    return result


def main(argv: list[str] | None = None) -> int:
    json_output = False
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if "--json" in arguments:
            json_output = True
            arguments = [argument for argument in arguments if argument != "--json"]
        if not arguments:
            if json_output:
                return _error("action_required", "choose a Dispatch command", json_output=True)
            print(format_help())
            return 0
        if arguments == ["--help"] or arguments == ["-h"]:
            if json_output:
                emit(
                    {
                        "ok": True,
                        "action": "help",
                        "status": "help",
                        "data": {"help": format_help()},
                        "error": None,
                    },
                    json_output=True,
                )
                return 0
            print(format_help())
            return 0
        layout = InstallLayout.from_environment()
        if arguments and arguments[0] == "setup":
            if not json_output:
                return run_setup(layout, arguments[1:], human=True)
            return _run_structured(
                lambda: run_setup(layout, arguments[1:], human=False),
                json_output=True,
                action="setup",
            )
        if arguments and arguments[0] == "uninstall":
            from .cli import main as installer_main

            return _run_structured(
                lambda: installer_main(
                    ["--dispatch-home", str(layout.dispatch_home), *arguments[1:]],
                    prog="dispatch uninstall",
                    public_uninstall=True,
                ),
                json_output=json_output,
                action="uninstall",
            )
        inspection = inspect_installation(layout)
        core = inspection["checks"]["core"]
        if core.get("status") != "ready":
            return _error(
                "core_release_unavailable",
                "active Core release is missing or unsafe",
                json_output=json_output,
            )
        release = layout.releases / str(core["release_id"])
        site_packages = release / "site-packages"
        if not site_packages.is_dir():
            raise InstallerError("core_site_packages_missing", "active Core package root is missing")
        os.environ.update(layout.core_environment(release))
        existing = os.environ.get("PYTHONPATH")
        plugins = active_plugins(layout)
        plugin_paths = [path for _plugin_id, path in plugins]
        import_paths = [str(site_packages), *(str(path) for path in plugin_paths)]
        if existing:
            import_paths.append(existing)
        os.environ["PYTHONPATH"] = os.pathsep.join(import_paths)
        os.environ["DISPATCH_ACTIVE_PLUGINS"] = ",".join(plugin_id for plugin_id, _path in plugins)
        os.environ["DISPATCH_PLUGIN_PATHS"] = os.pathsep.join(str(path) for path in plugin_paths)
        sys.path.insert(0, str(site_packages))
        for index, plugin_path in enumerate(plugin_paths, start=1):
            sys.path.insert(index, str(plugin_path))
        command_interface = importlib.import_module("dispatch_core.command_interface")
        return _run_structured(
            lambda: command_interface.main(arguments, prog="dispatch"),
            json_output=json_output,
            action=arguments[0],
        )
    except InstallerError as exc:
        return _error(exc.code, str(exc), json_output=json_output)
    except (KeyError, OSError, ImportError) as exc:
        return _error("core_launch_failed", str(exc), json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
