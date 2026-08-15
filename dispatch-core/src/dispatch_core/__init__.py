"""Portable Dispatch Core primitives."""
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

from .paths import DispatchPaths, PathConfigError, require_within

__version__ = "1.0.0"

__all__ = ["DispatchPaths", "PathConfigError", "require_within"]
