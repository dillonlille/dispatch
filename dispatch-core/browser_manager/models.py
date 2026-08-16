"""Typed Browser Manager contracts and lifecycle states."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Any
from urllib.parse import urlsplit


_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class BrowserManagerError(RuntimeError):
    """A bounded Browser Manager failure safe for Core status handling."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BrowserMode(StrEnum):
    HEADLESS = "headless"
    HEADED = "headed"


class BrowserPurpose(StrEnum):
    AUTHENTICATION = "authentication"
    COLLECTION = "collection"
    HEALTHCHECK = "healthcheck"


class LeaseState(StrEnum):
    REQUESTED = "requested"
    STARTING = "starting"
    READY = "ready"
    ACTIVE = "active"
    CLOSING = "closing"
    QUARANTINED = "quarantined"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATES = frozenset({LeaseState.CLOSED, LeaseState.CANCELLED, LeaseState.FAILED})
_ALLOWED_TRANSITIONS = {
    LeaseState.REQUESTED: frozenset(
        {LeaseState.STARTING, LeaseState.QUARANTINED, LeaseState.CANCELLED, LeaseState.FAILED}
    ),
    LeaseState.STARTING: frozenset(
        {LeaseState.READY, LeaseState.CLOSING, LeaseState.QUARANTINED, LeaseState.CANCELLED, LeaseState.FAILED}
    ),
    LeaseState.READY: frozenset(
        {LeaseState.ACTIVE, LeaseState.CLOSING, LeaseState.QUARANTINED, LeaseState.CANCELLED, LeaseState.FAILED}
    ),
    LeaseState.ACTIVE: frozenset(
        {LeaseState.CLOSING, LeaseState.QUARANTINED, LeaseState.CANCELLED, LeaseState.FAILED}
    ),
    LeaseState.CLOSING: frozenset(
        {LeaseState.CLOSED, LeaseState.QUARANTINED, LeaseState.CANCELLED, LeaseState.FAILED}
    ),
    LeaseState.QUARANTINED: frozenset({LeaseState.FAILED}),
    LeaseState.CLOSED: frozenset(),
    LeaseState.CANCELLED: frozenset(),
    LeaseState.FAILED: frozenset(),
}


def _slug(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _SLUG.fullmatch(value):
        raise BrowserManagerError("invalid_browser_request", f"{label} must be a lowercase Dispatch slug")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BrowserManagerError("invalid_browser_time", "browser timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise BrowserManagerError("invalid_browser_time", "stored browser timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BrowserManagerError("invalid_browser_time", "stored browser timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def require_transition(current: LeaseState, target: LeaseState) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise BrowserManagerError(
            "invalid_lease_transition",
            f"browser lease cannot transition from {current.value} to {target.value}",
        )


@dataclass(frozen=True)
class BrowserRealm:
    id: str
    landing_url: str
    purposes: frozenset[BrowserPurpose]
    default_mode: BrowserMode = BrowserMode.HEADLESS
    launch_timeout_seconds: int = 30
    lease_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        _slug(self.id, "realm")
        parsed = urlsplit(self.landing_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise BrowserManagerError("invalid_browser_policy", "realm landing URL must be canonical HTTPS")
        if not self.purposes:
            raise BrowserManagerError("invalid_browser_policy", "realm must permit at least one bounded purpose")
        if not 1 <= self.launch_timeout_seconds <= 300:
            raise BrowserManagerError("invalid_browser_policy", "launch timeout must be between 1 and 300 seconds")
        if not 30 <= self.lease_timeout_seconds <= 7200:
            raise BrowserManagerError("invalid_browser_policy", "lease timeout must be between 30 and 7200 seconds")


@dataclass(frozen=True)
class BrowserLeaseRequest:
    plugin_id: str
    plugin_release: str
    realm: str
    purpose: BrowserPurpose
    account_alias: str = "default"
    mode: BrowserMode | None = None

    def __post_init__(self) -> None:
        _slug(self.plugin_id, "plugin_id")
        _slug(self.realm, "realm")
        _slug(self.account_alias, "account_alias")
        if not isinstance(self.plugin_release, str) or not _RELEASE.fullmatch(self.plugin_release):
            raise BrowserManagerError("invalid_browser_request", "plugin_release is invalid")
        if not isinstance(self.purpose, BrowserPurpose):
            raise BrowserManagerError("invalid_browser_request", "purpose must be a bounded BrowserPurpose")
        if self.mode is not None and not isinstance(self.mode, BrowserMode):
            raise BrowserManagerError("invalid_browser_request", "mode must be headed or headless")

    @property
    def profile_key(self) -> str:
        return f"{self.realm}:{self.plugin_id}:{self.account_alias}"


@dataclass(frozen=True)
class BrowserLease:
    lease_id: str
    plugin_id: str
    plugin_release: str
    realm: str
    purpose: BrowserPurpose
    account_alias: str
    mode: BrowserMode
    state: LeaseState
    created_at: datetime
    expires_at: datetime

    def safe_data(self) -> dict[str, str]:
        return {
            "lease_id": self.lease_id,
            "plugin_id": self.plugin_id,
            "plugin_release": self.plugin_release,
            "realm": self.realm,
            "purpose": self.purpose.value,
            "account_alias": self.account_alias,
            "mode": self.mode.value,
            "state": self.state.value,
            "created_at": format_timestamp(self.created_at),
            "expires_at": format_timestamp(self.expires_at),
        }


@dataclass(frozen=True)
class ManagedBrowserSession:
    """Trusted in-process session handed to Authentication or an activated plugin."""

    lease_id: str
    realm: str
    landing_url: str
    page: Any = field(repr=False, compare=False)
    context: Any = field(repr=False, compare=False)


__all__ = [
    "BrowserLease",
    "BrowserLeaseRequest",
    "BrowserManagerError",
    "BrowserMode",
    "BrowserPurpose",
    "BrowserRealm",
    "LeaseState",
    "ManagedBrowserSession",
    "TERMINAL_STATES",
    "format_timestamp",
    "parse_timestamp",
    "require_transition",
    "utc_now",
]
