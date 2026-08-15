"""Dispatch installer primitives."""

from .core_release import activate_core_release, stage_core_wheel, verify_core_release
from .download import download_core_release_artifact
from .layout import InstallLayout, InstallerError
from .uninstall import plan_uninstall, uninstall as apply_uninstall

__version__ = "0.1.0"

__all__ = [
    "InstallLayout",
    "InstallerError",
    "activate_core_release",
    "apply_uninstall",
    "download_core_release_artifact",
    "plan_uninstall",
    "stage_core_wheel",
    "verify_core_release",
]
