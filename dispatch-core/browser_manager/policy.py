"""Installed, model-independent browser realm policy."""
from __future__ import annotations

from collections.abc import Iterable

from provider_catalog import PROVIDER_CATALOG

from .models import BrowserLeaseRequest, BrowserManagerError, BrowserPurpose, BrowserRealm


DEFAULT_REALMS = tuple(
    BrowserRealm(
        id=provider.id,
        landing_url=provider.landing_url,
        purposes=frozenset(
            {
                BrowserPurpose.AUTHENTICATION,
                BrowserPurpose.COLLECTION,
                BrowserPurpose.HEALTHCHECK,
            }
        ),
    )
    for provider in PROVIDER_CATALOG
)


class RealmRegistry:
    """Immutable realm lookup; callers cannot add URLs through a lease request."""

    def __init__(self, realms: Iterable[BrowserRealm] = DEFAULT_REALMS) -> None:
        values: dict[str, BrowserRealm] = {}
        for realm in realms:
            if realm.id in values:
                raise BrowserManagerError("invalid_browser_policy", f"duplicate realm: {realm.id}")
            values[realm.id] = realm
        if not values:
            raise BrowserManagerError("invalid_browser_policy", "at least one browser realm is required")
        self._values = values

    def resolve(self, request: BrowserLeaseRequest) -> BrowserRealm:
        realm = self._values.get(request.realm)
        if realm is None:
            raise BrowserManagerError("unknown_browser_realm", "requested browser realm is not installed")
        if request.purpose not in realm.purposes:
            raise BrowserManagerError("browser_purpose_denied", "requested purpose is not permitted for the realm")
        return realm

    def get(self, realm_id: str) -> BrowserRealm:
        realm = self._values.get(realm_id)
        if realm is None:
            raise BrowserManagerError("unknown_browser_realm", "stored browser realm is not installed")
        return realm

    def safe_data(self) -> list[dict[str, object]]:
        return [
            {
                "id": realm.id,
                "landing_url": realm.landing_url,
                "purposes": sorted(item.value for item in realm.purposes),
                "default_mode": realm.default_mode.value,
                "launch_timeout_seconds": realm.launch_timeout_seconds,
                "lease_timeout_seconds": realm.lease_timeout_seconds,
            }
            for realm in sorted(self._values.values(), key=lambda item: item.id)
        ]


__all__ = ["DEFAULT_REALMS", "RealmRegistry"]
