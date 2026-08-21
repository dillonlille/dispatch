"""Core-owned authentication and browser provider policy.

The catalog is intentionally small and closed in v1. Plugins may declare only a
provider identifier that Core already knows; URLs, selectors, and credential
fields remain Core-owned policy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    id: str
    public_id: str
    display_name: str
    landing_url: str
    credential_fields: tuple[str, ...]


PROVIDER_CATALOG = (
    ProviderPolicy(
        id="amazon-operations",
        public_id="amazon",
        display_name="Amazon Operations",
        landing_url="https://logistics.amazon.com/dspconsolev2",
        credential_fields=("username", "password"),
    ),
    ProviderPolicy(
        id="paycom-client",
        public_id="paycom",
        display_name="Paycom",
        landing_url="https://www.paycomonline.net/v4/cl/web.php/client-landing/arc",
        credential_fields=(
            "client_code",
            "username",
            "password",
            "security_pin_1",
            "security_pin_2",
            "security_pin_3",
            "security_pin_4",
            "security_pin_5",
        ),
    ),
)

PROVIDERS_BY_ID = {item.id: item for item in PROVIDER_CATALOG}
PROVIDERS_BY_PUBLIC_ID = {item.public_id: item for item in PROVIDER_CATALOG}

# This is the Core-owned policy for the built-in service/auth boundaries. The
# install-validated plugin metadata must agree with these provider IDs.
BUILTIN_PLUGIN_PROVIDERS = {
    "companion-bridge": "amazon-operations",
    "paycom": "paycom-client",
}


def provider_policy(provider_id: str) -> ProviderPolicy:
    try:
        return PROVIDERS_BY_ID[provider_id]
    except KeyError as exc:
        raise KeyError(provider_id) from exc


def provider_from_input(value: str) -> ProviderPolicy:
    """Resolve a public profile type or the legacy internal provider ID."""
    policy = PROVIDERS_BY_PUBLIC_ID.get(value) or PROVIDERS_BY_ID.get(value)
    if policy is None:
        raise KeyError(value)
    return policy


__all__ = [
    "BUILTIN_PLUGIN_PROVIDERS",
    "PROVIDER_CATALOG",
    "PROVIDERS_BY_ID",
    "PROVIDERS_BY_PUBLIC_ID",
    "ProviderPolicy",
    "provider_policy",
    "provider_from_input",
]
