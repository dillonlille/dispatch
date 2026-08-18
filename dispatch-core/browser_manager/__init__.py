"""Core-owned browser lifecycle, isolation, leases, and recovery."""
from .manager import BrowserManager, ManagedLease
from .models import (
    BrowserLease,
    BrowserLeaseRequest,
    BrowserManagerError,
    BrowserMode,
    BrowserPurpose,
    BrowserRealm,
    LeaseState,
    ManagedBrowserSession,
)
from .policy import DEFAULT_REALMS, RealmRegistry
from .providers import (
    BrowserProvider,
    BrowserProviderDescriptor,
    BrowserProviderKind,
    BrowserProviderRegistry,
)


__all__ = [
    "BrowserLease",
    "BrowserLeaseRequest",
    "BrowserManager",
    "BrowserManagerError",
    "BrowserMode",
    "BrowserProvider",
    "BrowserProviderDescriptor",
    "BrowserProviderKind",
    "BrowserProviderRegistry",
    "BrowserPurpose",
    "BrowserRealm",
    "DEFAULT_REALMS",
    "LeaseState",
    "ManagedBrowserSession",
    "ManagedLease",
    "RealmRegistry",
]
