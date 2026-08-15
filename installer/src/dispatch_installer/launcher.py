from __future__ import annotations

import importlib
import json
import os
import sys

from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError
from .setup import active_plugin_paths, run_setup


def _error(code: str, message: str) -> int:
    print(
        json.dumps(
            {
                "ok": False,
                "action": "launch",
                "status": "error",
                "data": {},
                "freshness": None,
                "delivery": None,
                "error": {"code": code, "message": message[:256]},
            },
            sort_keys=True,
        )
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        layout = InstallLayout.from_environment()
        arguments = list(sys.argv[1:] if argv is None else argv)
        if arguments and arguments[0] == "setup":
            return run_setup(layout, arguments[1:])
        inspection = inspect_installation(layout)
        core = inspection["checks"]["core"]
        if core.get("status") != "ready":
            return _error("core_release_unavailable", "active Core release is missing or unsafe")
        release = layout.releases / str(core["release_id"])
        site_packages = release / "site-packages"
        if not site_packages.is_dir():
            raise InstallerError("core_site_packages_missing", "active Core package root is missing")
        os.environ.update(layout.core_environment(release))
        existing = os.environ.get("PYTHONPATH")
        plugin_paths = active_plugin_paths(layout)
        import_paths = [str(site_packages), *(str(path) for path in plugin_paths)]
        if existing:
            import_paths.append(existing)
        os.environ["PYTHONPATH"] = os.pathsep.join(import_paths)
        sys.path.insert(0, str(site_packages))
        for index, plugin_path in enumerate(plugin_paths, start=1):
            sys.path.insert(index, str(plugin_path))
        command_interface = importlib.import_module("dispatch_core.command_interface")
        return command_interface.main(arguments)
    except InstallerError as exc:
        return _error(exc.code, str(exc))
    except (KeyError, OSError, ImportError) as exc:
        return _error("core_launch_failed", str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
