from __future__ import annotations

import importlib
import json
import os
import sys

from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError


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
        os.environ["PYTHONPATH"] = str(site_packages) if not existing else f"{site_packages}{os.pathsep}{existing}"
        sys.path.insert(0, str(site_packages))
        command_interface = importlib.import_module("dispatch_core.command_interface")
        return command_interface.main(argv)
    except InstallerError as exc:
        return _error(exc.code, str(exc))
    except (KeyError, OSError, ImportError) as exc:
        return _error("core_launch_failed", str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
