"""OS keyring access for the Dispatch credential vault.

Prefers the freedesktop Secret Service (gnome-keyring, KWallet, KeepAssistXC)
so the vault key never has to live on disk. When no Secret Service is
available (typical headless servers) callers fall back to the on-disk
0600 ``vault.key`` file; that fallback is a documented trust-model limit,
not a security boundary.
"""
from __future__ import annotations

from typing import Any

_SERVICE = "dispatch"
_SCHEMA = "dispatch-vault-key-v1"


def _connection() -> Any:
    import secretstorage  # imported lazily: optional dependency

    return secretstorage.dbus_init()


def _collection(conn: Any) -> Any:
    import secretstorage

    collection = secretstorage.get_default_collection(conn)
    if collection.is_locked():
        collection.unlock()  # prompts via the desktop agent when present
    if collection.is_locked():
        raise RuntimeError("keyring collection is locked")
    return collection


def available() -> bool:
    """True when a usable (unlocked or unlockable) Secret Service exists."""

    try:
        _collection(_connection())
        return True
    except Exception:
        return False


def store(key: bytes) -> None:
    collection = _collection(_connection())
    item = collection.search_items({"service": _SERVICE, "schema": _SCHEMA})
    for existing in item:
        existing.delete()
    collection.create_item(
        "Dispatch credential vault key",
        {"service": _SERVICE, "schema": _SCHEMA},
        key,
        replace=True,
    )


def load() -> bytes | None:
    collection = _collection(_connection())
    items = list(collection.search_items({"service": _SERVICE, "schema": _SCHEMA}))
    if not items:
        return None
    secret = items[0].get_secret()
    return bytes(secret)


def delete() -> None:
    collection = _collection(_connection())
    for item in collection.search_items({"service": _SERVICE, "schema": _SCHEMA}):
        item.delete()
