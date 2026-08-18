"""Provider contracts for current and future Browser Manager backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class BrowserProviderKind(StrEnum):
    MANAGED_PLAYWRIGHT = "managed-playwright"
    PERSISTENT_CDP = "persistent-cdp"
    EXTERNAL_CDP = "external-cdp"


@dataclass(frozen=True, slots=True)
class BrowserProviderDescriptor:
    id: str
    kind: BrowserProviderKind
    persistent_profiles: bool
    authentication_owner: str
    implemented: bool

    def safe_data(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "persistent_profiles": self.persistent_profiles,
            "authentication_owner": self.authentication_owner,
            "implemented": self.implemented,
        }


class BrowserProvider(ABC):
    """A Browser Manager-owned runtime provider; no collector owns this boundary."""

    @property
    @abstractmethod
    def descriptor(self) -> BrowserProviderDescriptor:
        raise NotImplementedError

    @abstractmethod
    def safe_status(self) -> dict[str, object]:
        raise NotImplementedError


class BrowserProviderFactory(Protocol):
    def __call__(self) -> BrowserProvider: ...


__all__ = [
    "BrowserProvider",
    "BrowserProviderDescriptor",
    "BrowserProviderFactory",
    "BrowserProviderKind",
]
