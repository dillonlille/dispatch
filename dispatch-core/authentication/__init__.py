"""Private credential storage and bounded Authentication status for Dispatch Core."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from cryptography.fernet import Fernet, InvalidToken

from browser_manager import ManagedBrowserSession
from paths import DispatchPaths
from provider_catalog import (
    BUILTIN_PLUGIN_PROVIDERS,
    PROVIDER_CATALOG,
    ProviderPolicy,
    provider_from_input,
)


_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MAX_VAULT_SIZE = 1024 * 1024
_PROFILE_SCHEMA_VERSION = 1
_PROFILE_STATUSES = {"enrolled", "orphaned"}
_PROFILE_VERIFICATIONS = {"unverified", "verified"}


class AuthenticationError(RuntimeError):
    """A bounded Authentication failure that never contains a secret value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthenticationRealm:
    id: str
    landing_url: str
    credential_fields: tuple[str, ...]


DEFAULT_AUTH_REALMS = tuple(
    AuthenticationRealm(item.id, item.landing_url, item.credential_fields)
    for item in PROVIDER_CATALOG
)
_REALMS = {realm.id: realm for realm in DEFAULT_AUTH_REALMS}

# Compatibility name for callers that are moving from realm terminology. The
# public profile UX never serializes these policies or their URLs.
DEFAULT_AUTH_PROVIDERS = PROVIDER_CATALOG


@dataclass(frozen=True)
class CredentialSet:
    """Trusted in-process credentials. Never serialize or include in model output."""

    realm: str
    account_alias: str
    values: Mapping[str, str] = field(repr=False, compare=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_slug(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _SLUG.fullmatch(value):
        raise AuthenticationError("invalid_auth_request", f"{label} must be a lowercase Dispatch slug")
    return value


def _realm(realm_id: str) -> AuthenticationRealm:
    value = _REALMS.get(realm_id)
    if value is None:
        raise AuthenticationError("unknown_auth_realm", "authentication realm is not installed")
    return value


def _provider(provider_id: str) -> ProviderPolicy:
    try:
        return provider_from_input(provider_id)
    except KeyError as exc:
        raise AuthenticationError("unknown_auth_provider", "authentication provider is not installed") from exc


def _safe_directory(path: Path, *, private: bool) -> os.stat_result:
    if path.is_symlink():
        raise AuthenticationError("auth_store_unsafe", "authentication directory cannot be a symlink")
    try:
        details = path.stat()
    except FileNotFoundError as exc:
        raise AuthenticationError("auth_store_missing", "authentication directory is absent") from exc
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise AuthenticationError("auth_store_unsafe", "authentication directory ownership is unsafe")
    mode = stat.S_IMODE(details.st_mode)
    if private and mode != 0o700:
        raise AuthenticationError("auth_store_unsafe", "authentication directory mode must be 0700")
    if not private and mode & 0o022:
        raise AuthenticationError("auth_store_unsafe", "authentication parent is group- or world-writable")
    return details


def _prepare_private_tree(root: Path) -> None:
    missing: list[Path] = []
    current = root
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    _safe_directory(current, private=False)
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _safe_directory(directory, private=True)
    _safe_directory(root, private=True)


def _safe_private_file(path: Path, *, maximum_size: int = _MAX_VAULT_SIZE) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthenticationError("auth_store_unsafe", "authentication file cannot be opened safely") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > maximum_size
        ):
            raise AuthenticationError("auth_store_unsafe", "authentication file ownership or mode is unsafe")
        data = bytearray()
        while len(data) <= maximum_size:
            chunk = os.read(descriptor, min(65536, maximum_size + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum_size:
            raise AuthenticationError("auth_store_unsafe", "authentication file exceeds size policy")
        if len(data) != details.st_size:
            raise AuthenticationError("auth_store_unsafe", "authentication file changed while it was read")
        return bytes(data)
    finally:
        os.close(descriptor)


def _atomic_private_file(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        _safe_private_file(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class EncryptedCredentialStore:
    """Small user-owned encrypted vault with atomic writes and a process lock."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.key_file = root / "vault.key"
        self.vault_file = root / "credentials.enc"
        self.lock_file = root / "credentials.lock"
        self.profile_file = root / "profiles.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _prepare_private_tree(self.root)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_file, flags, 0o600)
        except OSError as exc:
            raise AuthenticationError("auth_store_unsafe", "authentication lock file cannot be opened safely") from exc
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or details.st_nlink != 1
                or stat.S_IMODE(details.st_mode) != 0o600
            ):
                raise AuthenticationError("auth_store_unsafe", "authentication lock file is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _key(self, *, create: bool) -> bytes | None:
        key_present = self.key_file.exists() or self.key_file.is_symlink()
        vault_present = self.vault_file.exists() or self.vault_file.is_symlink()
        if key_present:
            disk_key = _safe_private_file(self.key_file, maximum_size=128)
            try:
                Fernet(disk_key)
            except (TypeError, ValueError) as exc:
                raise AuthenticationError("auth_store_invalid", "authentication vault key is invalid") from exc
            if vault_present and self._ring_has_key():
                ring_key = self._key_from_ring()
                if ring_key is not None and ring_key != disk_key:
                    # Two different keys exist; the disk copy wins today, but a
                    # silent divergence is how vaults get bricked. Fail loudly.
                    raise AuthenticationError(
                        "auth_store_ambiguous",
                        "vault key exists both in the OS keyring and on disk with different values",
                    )
            return disk_key
        if vault_present:
            if not self._ring_available():
                raise AuthenticationError(
                    "auth_store_key_unavailable",
                    "vault key lives in the OS keyring, which is currently unreachable",
                )
            ring_key = self._key_from_ring()
            if ring_key is not None:
                return ring_key
            raise AuthenticationError(
                "auth_store_invalid",
                "authentication vault key is missing from the OS keyring",
            )
        if not create:
            return None
        key = Fernet.generate_key()
        if not self._key_to_ring(key):
            _atomic_private_file(self.key_file, key)
        return key

    @staticmethod
    def _ring_available() -> bool:
        """True when an OS keyring is reachable at all.

        Distinguishes "no usable keyring exists" from "the keyring is
        temporarily unreachable", so a dbus hiccup cannot masquerade as data
        loss.
        """

        try:
            import importlib

            keyring_store = importlib.import_module("authentication.keyring")
            return keyring_store.available()
        except Exception:
            return False

    @classmethod
    def _ring_has_key(cls) -> bool:
        """True when the OS keyring is available AND holds a vault key."""

        try:
            import importlib

            keyring_store = importlib.import_module("authentication.keyring")
            if not keyring_store.available():
                return False
            return keyring_store.load() is not None
        except Exception:
            return False

    @staticmethod
    def _key_from_ring() -> bytes | None:
        """Best-effort read of the vault key from the OS keyring."""

        try:
            import importlib

            keyring_store = importlib.import_module("authentication.keyring")
            if not keyring_store.available():
                return None
            return keyring_store.load()
        except Exception:
            return None

    def _key_to_ring(self, key: bytes) -> bool:
        """Move the key into the OS keyring when one is available.

        Returns True when the keyring now holds the key; any pre-existing
        on-disk ``vault.key`` is removed so exactly one copy remains. Falls
        back to False (file storage) when no usable keyring exists.
        """

        try:
            import importlib

            keyring_store = importlib.import_module("authentication.keyring")
            if not keyring_store.available():
                return False
            keyring_store.store(key)
        except Exception:
            return False
        self.key_file.unlink(missing_ok=True)
        return True

    def rotate(self) -> dict[str, Any]:
        """Re-encrypt the vault under a freshly generated key.

        Creates a key (and its keyring/disk home) even when the vault is
        still empty, so rotation is safe to run at any time. All existing
        account records are preserved.
        """

        with self._locked():
            payload = self._load()
            key = Fernet.generate_key()
            if not self._key_to_ring(key):
                _atomic_private_file(self.key_file, key)
            if payload["accounts"]:
                self._write(payload)
            return {
                "status": "rotated",
                "accounts": sum(len(items) for items in payload["accounts"].values()),
            }

    def _load(self) -> dict[str, Any]:
        if not self.root.exists() and not self.root.is_symlink():
            return {"schema_version": 1, "accounts": {}}
        _safe_directory(self.root, private=True)
        key = self._key(create=False)
        if key is None:
            return {"schema_version": 1, "accounts": {}}
        if not self.vault_file.exists() and not self.vault_file.is_symlink():
            return {"schema_version": 1, "accounts": {}}
        token = _safe_private_file(self.vault_file)
        try:
            cleartext = Fernet(key).decrypt(token)
            payload = json.loads(cleartext.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AuthenticationError("auth_store_invalid", "authentication vault cannot be decrypted") from exc
        self._validate_payload(payload)
        return payload

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "accounts"}:
            raise AuthenticationError("auth_store_invalid", "authentication vault shape is invalid")
        if payload.get("schema_version") != 1 or not isinstance(payload.get("accounts"), dict):
            raise AuthenticationError("auth_store_invalid", "authentication vault version is invalid")
        for realm_id, accounts in payload["accounts"].items():
            policy = _realm(realm_id)
            if not isinstance(accounts, dict):
                raise AuthenticationError("auth_store_invalid", "authentication account map is invalid")
            for alias, record in accounts.items():
                _require_slug(alias, "account_alias")
                if (
                    not isinstance(record, dict)
                    or set(record) != {"updated_at", "values"}
                    or not isinstance(record.get("updated_at"), str)
                    or not isinstance(record.get("values"), dict)
                    or set(record["values"]) != set(policy.credential_fields)
                    or not all(isinstance(value, str) and value and "\x00" not in value for value in record["values"].values())
                ):
                    raise AuthenticationError("auth_store_invalid", "authentication account record is invalid")

    def _write(self, payload: dict[str, Any]) -> None:
        self._validate_payload(payload)
        key = self._key(create=True)
        assert key is not None
        cleartext = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        _atomic_private_file(self.vault_file, Fernet(key).encrypt(cleartext))

    @staticmethod
    def _validate_profile_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "profiles"}:
            raise AuthenticationError("auth_profile_store_invalid", "authentication profile registry shape is invalid")
        if payload.get("schema_version") != _PROFILE_SCHEMA_VERSION or not isinstance(payload.get("profiles"), dict):
            raise AuthenticationError("auth_profile_store_invalid", "authentication profile registry version is invalid")
        for profile, record in payload["profiles"].items():
            _require_slug(profile, "profile")
            if (
                not isinstance(record, dict)
                or set(record) != {"provider", "account_alias", "status", "verification", "updated_at", "bindings"}
                or not isinstance(record.get("provider"), str)
                or not isinstance(record.get("account_alias"), str)
                or not isinstance(record.get("status"), str)
                or record["status"] not in _PROFILE_STATUSES
                or not isinstance(record.get("verification"), str)
                or record["verification"] not in _PROFILE_VERIFICATIONS
                or not isinstance(record.get("updated_at"), str)
                or not isinstance(record.get("bindings"), list)
                or len(record["bindings"]) > 64
                or any(not isinstance(binding, str) or _SLUG.fullmatch(binding) is None for binding in record["bindings"])
                or len(set(record["bindings"])) != len(record["bindings"])
                or _SLUG.fullmatch(record["account_alias"]) is None
            ):
                raise AuthenticationError("auth_profile_store_invalid", "authentication profile record is invalid")

    def _load_profiles(self) -> dict[str, Any] | None:
        if not self.profile_file.exists() and not self.profile_file.is_symlink():
            return None
        raw = _safe_private_file(self.profile_file, maximum_size=256 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AuthenticationError("auth_profile_store_invalid", "authentication profile registry is invalid") from exc
        self._validate_profile_payload(payload)
        return payload

    @staticmethod
    def _profile_candidate(provider: str, alias: str, profiles: Mapping[str, Any]) -> str:
        candidate = alias
        if candidate not in profiles:
            return candidate
        record = profiles[candidate]
        if record.get("provider") == provider and record.get("account_alias") == alias:
            return candidate
        prefix = _provider(provider).public_id
        candidate = f"{prefix}-{alias}"
        if len(candidate) > 63:
            digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
            candidate = f"{candidate[:54].rstrip('-')}-{digest}"
        if candidate not in profiles:
            return candidate
        index = 2
        base = candidate[:60].rstrip("-")
        while f"{base}-{index}" in profiles:
            index += 1
        return f"{base}-{index}"

    def _reconciled_profiles(self, vault: dict[str, Any], registry: dict[str, Any] | None) -> dict[str, Any]:
        profiles = {} if registry is None else dict(registry["profiles"])
        for provider, accounts in vault["accounts"].items():
            for alias in accounts:
                if any(
                    record.get("provider") == provider and record.get("account_alias") == alias
                    for record in profiles.values()
                ):
                    continue
                profile = self._profile_candidate(provider, alias, profiles)
                profiles[profile] = {
                    "provider": provider,
                    "account_alias": alias,
                    "status": "enrolled",
                    "verification": "unverified",
                    "updated_at": _utc_now(),
                    "bindings": [],
                }
        for record in profiles.values():
            provider = record.get("provider")
            alias = record.get("account_alias")
            record["status"] = "enrolled" if vault["accounts"].get(provider, {}).get(alias) is not None else "orphaned"
        return {"schema_version": _PROFILE_SCHEMA_VERSION, "profiles": profiles}

    def profile_payload(self, *, persist: bool = False) -> dict[str, Any]:
        if persist:
            with self._locked():
                vault = self._load()
                registry = self._load_profiles()
                payload = self._reconciled_profiles(vault, registry)
                if registry != payload:
                    self.write_profile_payload(payload)
                return payload
        vault = self._load()
        registry = self._load_profiles()
        return self._reconciled_profiles(vault, registry)

    def write_profile_payload(self, payload: dict[str, Any]) -> None:
        self._validate_profile_payload(payload)
        data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(data) > 256 * 1024:
            raise AuthenticationError(
                "auth_profile_store_invalid",
                "authentication profile registry exceeds size policy",
            )
        _atomic_private_file(
            self.profile_file,
            data,
        )

    def put_profile(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        values: Mapping[str, str],
    ) -> None:
        with self._locked():
            vault = self._load()
            registry = self._reconciled_profiles(vault, self._load_profiles())
            # Existence is enforced here, under the lock, so two concurrent
            # enrollments can never silently overwrite one another (the
            # manager-level pre-check alone is advisory).
            existing = registry["profiles"].get(profile)
            if existing is not None:
                raise AuthenticationError("profile_exists", "authentication profile already exists")
            for record in registry["profiles"].values():
                if record.get("provider") == provider and record.get("account_alias") == account_alias:
                    raise AuthenticationError(
                        "profile_exists",
                        "credential account is already enrolled as another profile",
                    )
            accounts = vault["accounts"].setdefault(provider, {})
            accounts[account_alias] = {"updated_at": _utc_now(), "values": dict(values)}
            self._write(vault)
            registry["profiles"][profile] = {
                "provider": provider,
                "account_alias": account_alias,
                "status": "enrolled",
                "verification": "unverified",
                "updated_at": _utc_now(),
                "bindings": [],
            }
            self.write_profile_payload(registry)

    def select_profile_for_plugin(self, profile: str, plugin_id: str, provider: str) -> dict[str, Any]:
        """Select exactly one compatible profile for a plugin in one projection write."""
        with self._locked():
            vault = self._load()
            registry = self._reconciled_profiles(vault, self._load_profiles())
            selected = registry["profiles"].get(profile)
            if selected is None:
                raise AuthenticationError("profile_not_found", "authentication profile is not enrolled")
            if selected["status"] != "enrolled":
                raise AuthenticationError("profile_orphaned", "authentication profile has no usable encrypted record")
            if selected["provider"] != provider:
                raise AuthenticationError("profile_provider_mismatch", "authentication profile is enrolled for another provider")
            now = _utc_now()
            for record in registry["profiles"].values():
                bindings = [value for value in record["bindings"] if value != plugin_id]
                if record is selected:
                    bindings.append(plugin_id)
                if bindings != record["bindings"]:
                    record["bindings"] = bindings
                    record["updated_at"] = now
            self.write_profile_payload(registry)
            return dict(selected)

    def retain_plugin_bindings(self, selected_plugins: set[str]) -> None:
        """Drop bindings for deselected plugins without deleting any profile."""
        with self._locked():
            vault = self._load()
            registry = self._reconciled_profiles(vault, self._load_profiles())
            changed = False
            now = _utc_now()
            for record in registry["profiles"].values():
                bindings = [value for value in record["bindings"] if value in selected_plugins]
                if bindings != record["bindings"]:
                    record["bindings"] = bindings
                    record["updated_at"] = now
                    changed = True
            if changed:
                self.write_profile_payload(registry)

    def remove_profile(self, profile: str) -> tuple[bool, dict[str, Any] | None]:
        with self._locked():
            vault = self._load()
            registry = self._reconciled_profiles(vault, self._load_profiles())
            record = registry["profiles"].get(profile)
            if record is None:
                return False, None
            if record["bindings"]:
                raise AuthenticationError(
                    "profile_in_use",
                    "authentication profile is still selected by a plugin",
                )
            if record["status"] == "orphaned":
                # The registry may outlive a credential account when an earlier
                # compound removal was interrupted. With no bindings and no
                # matching vault record, retry safely removes only the stale
                # non-secret projection.
                provider, alias = record["provider"], record["account_alias"]
                if vault["accounts"].get(provider, {}).get(alias) is not None:
                    raise AuthenticationError(
                        "profile_orphaned",
                        "authentication profile cannot be removed because its provider is unavailable",
                    )
                del registry["profiles"][profile]
                self.write_profile_payload(registry)
                return True, record
            provider, alias = record["provider"], record["account_alias"]
            accounts = vault["accounts"].get(provider, {})
            if alias not in accounts:
                raise AuthenticationError("profile_orphaned", "authentication profile has no matching encrypted record")
            del accounts[alias]
            if not accounts:
                vault["accounts"].pop(provider, None)
            del registry["profiles"][profile]
            self._write(vault)
            self.write_profile_payload(registry)
            return True, record

    def put(self, realm_id: str, account_alias: str, values: Mapping[str, str]) -> None:
        _realm(realm_id)
        _require_slug(account_alias, "account_alias")
        with self._locked():
            payload = self._load()
            accounts = payload["accounts"].setdefault(realm_id, {})
            if account_alias in accounts:
                raise AuthenticationError(
                    "profile_exists",
                    "credential account already exists; create and select a new named profile",
                )
            accounts[account_alias] = {"updated_at": _utc_now(), "values": dict(values)}
            self._write(payload)

    def get(self, realm_id: str, account_alias: str) -> dict[str, str]:
        payload = self._load()
        try:
            values = payload["accounts"][realm_id][account_alias]["values"]
        except KeyError as exc:
            raise AuthenticationError("credentials_not_enrolled", "credentials are not enrolled") from exc
        return dict(values)

    def remove(self, realm_id: str, account_alias: str) -> bool:
        with self._locked():
            payload = self._load()
            realm_accounts = payload["accounts"].get(realm_id, {})
            if account_alias not in realm_accounts:
                return False
            del realm_accounts[account_alias]
            if not realm_accounts:
                payload["accounts"].pop(realm_id, None)
            self._write(payload)
            return True

    def configured_accounts(self) -> set[tuple[str, str]]:
        payload = self._load()
        return {
            (realm_id, alias)
            for realm_id, accounts in payload["accounts"].items()
            for alias in accounts
        }


class AuthenticationManager:
    """Core Authentication boundary; secret values remain trusted and in-process."""

    def __init__(self, paths: DispatchPaths) -> None:
        self._paths = paths
        root = paths.owner_root("secrets", "dispatch-core") / "authentication"
        self._store = EncryptedCredentialStore(root)

    @property
    def store_root(self) -> Path:
        return self._store.root

    @staticmethod
    def realms() -> tuple[AuthenticationRealm, ...]:
        return tuple(DEFAULT_AUTH_REALMS)

    @staticmethod
    def providers() -> tuple[ProviderPolicy, ...]:
        """Return the closed Core-owned provider menu without URLs or selectors."""
        return tuple(PROVIDER_CATALOG)

    @staticmethod
    def provider(value: str) -> ProviderPolicy:
        return _provider(value)

    @staticmethod
    def _public_profile(profile: str, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            policy = _provider(str(record.get("provider")))
            profile_type = policy.public_id
            type_name = policy.display_name
        except AuthenticationError:
            profile_type = "unavailable"
            type_name = "Unavailable profile type"
        return {
            "profile": profile,
            "type": profile_type,
            "type_name": type_name,
            "status": record.get("status"),
            "verification": record.get("verification", "unverified"),
        }

    def profiles(self) -> list[dict[str, Any]]:
        payload = self._store.profile_payload()
        return [
            self._public_profile(profile, payload["profiles"][profile])
            for profile in sorted(payload["profiles"])
        ]

    def profile_status(self, profile: str) -> dict[str, Any]:
        _require_slug(profile, "profile")
        payload = self._store.profile_payload()
        record = payload["profiles"].get(profile)
        if record is None:
            raise AuthenticationError("profile_not_found", "authentication profile is not enrolled")
        return self._public_profile(profile, record)

    def compatible_profiles(self, provider: str) -> list[dict[str, Any]]:
        policy = _provider(provider)
        payload = self._store.profile_payload()
        return [
            self._public_profile(profile, record)
            for profile, record in sorted(payload["profiles"].items())
            if record.get("provider") == policy.id and record.get("status") == "enrolled"
        ]

    def _profile_record(self, profile: str, *, persist: bool = True) -> dict[str, Any]:
        _require_slug(profile, "profile")
        payload = self._store.profile_payload(persist=persist)
        record = payload["profiles"].get(profile)
        if record is None:
            raise AuthenticationError("profile_not_found", "authentication profile is not enrolled")
        return record

    def _require_plugin_provider(self, plugin_id: str, provider: str) -> None:
        _require_slug(plugin_id, "plugin_id")
        policy = _provider(provider)
        expected = BUILTIN_PLUGIN_PROVIDERS.get(plugin_id)
        if expected is not None and expected != policy.id:
            raise AuthenticationError("profile_provider_mismatch", "plugin does not permit this authentication provider")

    def bind_profile(self, profile: str, plugin_id: str, provider: str) -> dict[str, Any]:
        self._require_plugin_provider(plugin_id, provider)
        updated = self._store.select_profile_for_plugin(profile, plugin_id, _provider(provider).id)
        return self._public_profile(profile, updated)

    def retain_plugin_bindings(self, selected_plugins: set[str]) -> None:
        for plugin_id in selected_plugins:
            _require_slug(plugin_id, "plugin_id")
        self._store.retain_plugin_bindings(selected_plugins)

    def profile_for_plugin(self, plugin_id: str, provider: str) -> str:
        self._require_plugin_provider(plugin_id, provider)
        provider_id = _provider(provider).id
        payload = self._store.profile_payload()
        matches = [
            profile
            for profile, record in payload["profiles"].items()
            if plugin_id in record.get("bindings", [])
        ]
        if len(matches) > 1:
            raise AuthenticationError("profile_binding_ambiguous", "plugin has more than one selected authentication profile")
        if not matches:
            raise AuthenticationError(
                "profile_required",
                f"add or select an enrolled authentication profile for {plugin_id}",
            )
        record = payload["profiles"][matches[0]]
        if record["provider"] != provider_id:
            raise AuthenticationError("profile_provider_mismatch", "selected authentication profile uses another provider")
        if record["status"] != "enrolled":
            raise AuthenticationError("profile_orphaned", "selected authentication profile has no usable encrypted record")
        return matches[0]

    def account_alias_for_profile(self, profile: str, provider: str | None = None) -> str:
        record = self._profile_record(profile)
        if provider is not None and record["provider"] != _provider(provider).id:
            raise AuthenticationError("profile_provider_mismatch", "authentication profile is enrolled for another provider")
        if record["status"] != "enrolled":
            raise AuthenticationError("profile_orphaned", "authentication profile has no usable encrypted record")
        return str(record["account_alias"])

    def profile_credentials(self, profile: str, provider: str | None = None) -> CredentialSet:
        record = self._profile_record(profile)
        actual_provider = str(record["provider"])
        if provider is not None and actual_provider != _provider(provider).id:
            raise AuthenticationError("profile_provider_mismatch", "authentication profile is enrolled for another provider")
        if record["status"] != "enrolled":
            raise AuthenticationError("profile_orphaned", "authentication profile has no usable encrypted record")
        return self.credentials(actual_provider, str(record["account_alias"]))

    def enroll_profile(
        self,
        profile: str,
        provider: str,
        values: Mapping[str, str],
        *,
        plugin_id: str | None = None,
    ) -> dict[str, Any]:
        _require_slug(profile, "profile")
        policy = _provider(provider)
        provider = policy.id
        if plugin_id is not None:
            self._require_plugin_provider(plugin_id, provider)
        if set(values) != set(policy.credential_fields):
            raise AuthenticationError("invalid_credentials", "credential fields do not match the provider policy")
        normalized: dict[str, str] = {}
        for name in policy.credential_fields:
            value = values[name]
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                raise AuthenticationError("invalid_credentials", "a credential value is empty or invalid")
            normalized[name] = value
        existing = self._store.profile_payload().get("profiles", {}).get(profile)
        if existing is not None:
            # The store enforces this again under its lock; the early check
            # just gives the common case a precise error before prompts/IO.
            raise AuthenticationError("profile_exists", "authentication profile already exists")
        account_alias = profile
        self._store.put_profile(profile, provider, account_alias, normalized)
        if plugin_id is not None:
            self.bind_profile(profile, plugin_id, provider)
        return {
            "profile": profile,
            "type": policy.public_id,
            "type_name": policy.display_name,
            "status": "enrolled",
            "verification": "unverified",
        }

    def remove_profile(self, profile: str) -> dict[str, Any]:
        _require_slug(profile, "profile")
        removed, record = self._store.remove_profile(profile)
        public = None if record is None else self._public_profile(profile, record)
        return {
            "profile": profile,
            "type": None if public is None else public["type"],
            "type_name": None if public is None else public["type_name"],
            "status": "removed" if removed else "not_enrolled",
        }

    def for_plugin(self, plugin_id: str, provider: str, profile: str) -> "PluginAuthenticationBroker":
        self._require_plugin_provider(plugin_id, provider)
        provider = _provider(provider).id
        record = self._profile_record(profile)
        if plugin_id not in record.get("bindings", []):
            raise AuthenticationError(
                "profile_not_authorized",
                "authentication profile is not selected for this plugin",
            )
        self.account_alias_for_profile(profile, provider)
        return PluginAuthenticationBroker(self._paths, plugin_id, provider, profile)

    def authenticate_profile(self, session: ManagedBrowserSession, profile: str) -> "AuthenticationResult":
        provider = _realm(session.realm).id
        alias = self.account_alias_for_profile(profile, provider)
        return self.authenticate(session, alias)

    def resume_profile(self, session: ManagedBrowserSession, profile: str) -> "AuthenticationResult":
        provider = _realm(session.realm).id
        alias = self.account_alias_for_profile(profile, provider)
        return self.resume(session, alias)

    def status(self, realm_id: str | None = None, account_alias: str = "default") -> dict[str, Any]:
        _require_slug(account_alias, "account_alias")
        if realm_id is not None:
            _realm(realm_id)
        configured = self._store.configured_accounts()
        selected = [value for value in DEFAULT_AUTH_REALMS if realm_id is None or value.id == realm_id]
        return {
            "backend": "ready",
            "configured": any((value.id, account_alias) in configured for value in selected),
            "account_alias": account_alias,
            "realms": [
                {
                    "id": value.id,
                    "landing_url": value.landing_url,
                    "status": "configured" if (value.id, account_alias) in configured else "not_enrolled",
                }
                for value in selected
            ],
        }

    def enroll(self, realm_id: str, account_alias: str, values: Mapping[str, str]) -> dict[str, Any]:
        policy = _realm(realm_id)
        _require_slug(account_alias, "account_alias")
        if (realm_id, account_alias) in self._store.configured_accounts():
            raise AuthenticationError(
                "profile_exists",
                "credential account already exists; create and select a new named profile",
            )
        if set(values) != set(policy.credential_fields):
            raise AuthenticationError("invalid_credentials", "credential fields do not match the realm policy")
        normalized: dict[str, str] = {}
        for name in policy.credential_fields:
            value = values[name]
            if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                raise AuthenticationError("invalid_credentials", "a credential value is empty or invalid")
            normalized[name] = value
        self._store.put(realm_id, account_alias, normalized)
        return {"realm": realm_id, "account_alias": account_alias, "status": "configured"}

    def credentials(self, realm_id: str, account_alias: str = "default") -> CredentialSet:
        policy = _realm(realm_id)
        _require_slug(account_alias, "account_alias")
        values = self._store.get(policy.id, account_alias)
        return CredentialSet(policy.id, account_alias, MappingProxyType(values))

    def credentials_for_session(
        self,
        session: ManagedBrowserSession,
        account_alias: str = "default",
    ) -> CredentialSet:
        policy = _realm(session.realm)
        if session.landing_url != policy.landing_url:
            raise AuthenticationError("auth_session_invalid", "browser session landing authority differs")
        return self.credentials(policy.id, account_alias)

    def verify_landing(self, session: ManagedBrowserSession) -> dict[str, str]:
        policy = _realm(session.realm)
        if session.landing_url != policy.landing_url:
            raise AuthenticationError("auth_session_invalid", "browser session landing authority differs")
        current_url = str(getattr(session.page, "url", ""))
        return {
            "realm": policy.id,
            "landing_url": policy.landing_url,
            "status": "verified" if current_url == policy.landing_url else "not_at_landing",
        }

    def authenticate(
        self,
        session: ManagedBrowserSession,
        account_alias: str = "default",
    ) -> "AuthenticationResult":
        from .workflow import authenticate

        return authenticate(self, session, account_alias)

    def resume(
        self,
        session: ManagedBrowserSession,
        account_alias: str = "default",
    ) -> "AuthenticationResult":
        from .workflow import authenticate

        return authenticate(self, session, account_alias, resume=True)

    def remove(self, realm_id: str, account_alias: str = "default") -> dict[str, Any]:
        _realm(realm_id)
        _require_slug(account_alias, "account_alias")
        payload = self._store.profile_payload()
        matches = [
            profile
            for profile, record in payload["profiles"].items()
            if record.get("provider") == realm_id and record.get("account_alias") == account_alias
        ]
        if len(matches) > 1:
            raise AuthenticationError("profile_binding_ambiguous", "credential account has multiple profile records")
        if matches:
            removed = self.remove_profile(matches[0])["status"] == "removed"
        else:
            removed = self._store.remove(realm_id, account_alias)
        return {
            "realm": realm_id,
            "account_alias": account_alias,
            "status": "removed" if removed else "not_enrolled",
        }

    def rotate_vault(self) -> dict[str, Any]:
        """Rotate the vault key, re-encrypting every stored credential."""

        return self._store.rotate()


class PluginAuthenticationBroker:
    """Plugin-scoped authentication facade; raw vault records stay Core-owned."""

    def __init__(self, paths: DispatchPaths, plugin_id: str, provider: str, profile: str) -> None:
        self._paths = paths
        self.plugin_id = plugin_id
        self.provider = provider
        self.profile = profile

    @property
    def account_alias(self) -> str:
        return AuthenticationManager(self._paths).account_alias_for_profile(self.profile, self.provider)

    def authenticate(self, session: ManagedBrowserSession) -> "AuthenticationResult":
        if session.realm != self.provider:
            raise AuthenticationError("profile_provider_mismatch", "managed browser session uses another provider")
        return AuthenticationManager(self._paths).authenticate_profile(session, self.profile)

    def resume(self, session: ManagedBrowserSession) -> "AuthenticationResult":
        if session.realm != self.provider:
            raise AuthenticationError("profile_provider_mismatch", "managed browser session uses another provider")
        return AuthenticationManager(self._paths).resume_profile(session, self.profile)

from .workflow import AuthenticationResult


__all__ = [
    "AuthenticationError",
    "AuthenticationManager",
    "PluginAuthenticationBroker",
    "AuthenticationRealm",
    "AuthenticationResult",
    "CredentialSet",
    "DEFAULT_AUTH_REALMS",
    "DEFAULT_AUTH_PROVIDERS",
    "EncryptedCredentialStore",
]
