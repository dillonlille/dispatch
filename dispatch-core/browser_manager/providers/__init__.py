"""Browser Manager provider contracts and registry."""
from .base import (
    BrowserProvider,
    BrowserProviderDescriptor,
    BrowserProviderFactory,
    BrowserProviderKind,
)
from .registry import BrowserProviderRegistry


__all__ = [
    "BrowserProvider",
    "BrowserProviderDescriptor",
    "BrowserProviderFactory",
    "BrowserProviderKind",
    "BrowserProviderRegistry",
]
