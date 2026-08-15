"""Dispatch installer primitives."""

from .browser_runtime import (
    activate_browser_generation,
    inspect_browser_runtime,
    load_browser_runtime_manifest,
    rollback_browser_generation,
    stage_browser_runtime,
    verify_browser_generation,
)
from .core_release import activate_core_release, stage_core_wheel, verify_core_release
from .download import download_core_release_artifact
from .layout import InstallLayout, InstallerError
from .uninstall import plan_uninstall, uninstall as apply_uninstall

__version__ = "0.1.1"

__all__ = [
    "InstallLayout",
    "InstallerError",
    "activate_browser_generation",
    "activate_core_release",
    "apply_uninstall",
    "download_core_release_artifact",
    "inspect_browser_runtime",
    "load_browser_runtime_manifest",
    "plan_uninstall",
    "rollback_browser_generation",
    "stage_browser_runtime",
    "stage_core_wheel",
    "verify_core_release",
    "verify_browser_generation",
]
