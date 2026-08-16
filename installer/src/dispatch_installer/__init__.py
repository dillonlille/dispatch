"""Standard-library Dispatch clone installer."""

from .layout import InstallLayout, InstallerError
from .uninstall import plan_uninstall, uninstall

__version__ = "0.2.0"

__all__ = ["InstallLayout", "InstallerError", "plan_uninstall", "uninstall"]
