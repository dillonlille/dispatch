"""Closed Browser Manager provider registry.

Only managed Playwright is implemented now. Persistent and external CDP kinds
are reserved contracts for later collector migration and are not activated.
"""
from __future__ import annotations

from .base import BrowserProviderDescriptor, BrowserProviderKind


MANAGED_PLAYWRIGHT = BrowserProviderDescriptor(
    id="managed-playwright",
    kind=BrowserProviderKind.MANAGED_PLAYWRIGHT,
    persistent_profiles=True,
    authentication_owner="browser-manager",
    implemented=True,
)

FUTURE_PERSISTENT_CDP = BrowserProviderDescriptor(
    id="persistent-cdp",
    kind=BrowserProviderKind.PERSISTENT_CDP,
    persistent_profiles=True,
    authentication_owner="provider",
    implemented=False,
)

FUTURE_EXTERNAL_CDP = BrowserProviderDescriptor(
    id="external-cdp",
    kind=BrowserProviderKind.EXTERNAL_CDP,
    persistent_profiles=True,
    authentication_owner="external",
    implemented=False,
)


class BrowserProviderRegistry:
    """Safe provider metadata without executable endpoints or profile paths."""

    def __init__(self) -> None:
        self._descriptors = (
            MANAGED_PLAYWRIGHT,
            FUTURE_PERSISTENT_CDP,
            FUTURE_EXTERNAL_CDP,
        )

    def safe_data(self) -> list[dict[str, object]]:
        return [descriptor.safe_data() for descriptor in self._descriptors]

    def implemented(self) -> tuple[BrowserProviderDescriptor, ...]:
        return tuple(descriptor for descriptor in self._descriptors if descriptor.implemented)


__all__ = [
    "BrowserProviderRegistry",
    "FUTURE_EXTERNAL_CDP",
    "FUTURE_PERSISTENT_CDP",
    "MANAGED_PLAYWRIGHT",
]
