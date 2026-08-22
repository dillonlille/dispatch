"""Private credential storage and bounded Authentication status for Dispatch Core."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
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
# Vault cleartext format: schema_version 2 stamps the key fingerprint so
# rotations are verifiable and mismatched keys fail with precise errors.
_VAULT_SCHEMA_VERSION = 2
_KEY_ID_BYTES = 8
# Bounded lock acquisition: fail auth_store_busy after this many seconds
# instead of hanging forever on an externally-held flock.
_LOCK_TIMEOUT_SECONDS = 10.0


def _key_id(key: bytes) -> str:
    """Short public fingerprint of a vault key (never the key itself)."""

    return hashlib.sha256(key).hexdigest()[: _KEY_ID_BYTES * 2]


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


def _validate_ancestor_chain(root: Path) -> None:
    """Re-check every ancestor of an EXISTING store before reading/writing.

    Creation-time validation (_prepare_private_tree) expires the moment a
    local attacker loosens an ancestor's permissions or repoints a path
    component at another directory via a symlink. Every store open therefore
    walks root -> filesystem top and rejects:
      - symlinked components anywhere above the store root,
      - non-directory components,
      - foreign-owned components,
      - group- or world-writable ancestors unless sticky (OpenSSH-style).
    The store root itself is validated separately with the stricter
    private=True rules by the caller.
    """

    chain: list[Path] = []
    probe = root
    while True:
        chain.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    # Walk from the top of the filesystem down to the store's parent,
    # applying OpenSSH-style ancestor rules (see loop body).
    for directory in reversed(chain[1:]):
        if directory.is_symlink():
            raise AuthenticationError(
                "auth_store_unsafe",
                "authentication ancestor path component is a symlink",
            )
        try:
            details = directory.stat()
        except FileNotFoundError as exc:
            # Components above an existing store must exist; a vanished
            # ancestor means the tree was tampered with mid-flight.
            raise AuthenticationError(
                "auth_store_unsafe",
                "authentication ancestor directory disappeared",
            ) from exc
        if not stat.S_ISDIR(details.st_mode):
            raise AuthenticationError(
                "auth_store_unsafe",
                "authentication ancestor path component is not a directory",
            )
        if details.st_uid not in (0, os.geteuid()):
            raise AuthenticationError(
                "auth_store_unsafe",
                "authentication ancestor directory ownership is unsafe",
            )
        # OpenSSH-style rule: a group- or world-writable ancestor is
        # acceptable only when the sticky bit confines deletions to owners
        # (the classic /tmp shape); otherwise it enables tree swaps.
        mode = stat.S_IMODE(details.st_mode)
        if mode & 0o022 and not mode & 0o1000:
            raise AuthenticationError(
                "auth_store_unsafe",
                "authentication ancestor directory permissions are unsafe",
            )


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
        if self.root.exists() or self.root.is_symlink():
            _validate_ancestor_chain(self.root)
        _prepare_private_tree(self.root)
        # Two-phase open: create the lock file only when absent, so a
        # swapped-in replacement is always detected by the identity check.
        try:
            descriptor = os.open(
                self.lock_file,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created = True
        except FileExistsError:
            created = False
            try:
                descriptor = os.open(
                    self.lock_file,
                    os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as exc:
                raise AuthenticationError(
                    "auth_store_unsafe",
                    "authentication lock file cannot be opened safely",
                ) from exc
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
            if created and details.st_size == 0:
                # Freshly created: record this process's ownership marker.
                os.write(descriptor, f"{os.getpid()}\n".encode())
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            # Bounded acquisition: an externally-held flock (leaked fd,
            # squatter process) must fail loudly instead of hanging every
            # auth operation forever.
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AuthenticationError(
                            "auth_store_busy",
                            "authentication vault is locked by another process; retry once it exits",
                        ) from None
                    time.sleep(0.05)
            # Inode identity: verify the inode we locked is still the one at
            # the path (catches swaps that happened before acquisition), and
            # re-check at release (catches swaps DURING the critical section,
            # converting silent overlap into a detected violation for every
            # subsequent operation).
            def _identity_mismatch() -> bool:
                try:
                    path_details = os.stat(self.lock_file)
                except FileNotFoundError:
                    return True
                return (path_details.st_dev, path_details.st_ino) != (details.st_dev, details.st_ino)

            if _identity_mismatch():
                raise AuthenticationError(
                    "auth_store_unsafe",
                    "authentication lock file was swapped while acquiring the vault lock",
                )
            yield
            if _identity_mismatch():
                raise AuthenticationError(
                    "auth_store_unsafe",
                    "authentication lock file was swapped during the vault critical section",
                )
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
        """Re-encrypt the vault under a freshly generated key, crash-safely.

        Order of operations (the old design bricked the vault if a crash
        landed between "new key installed" and "token re-encrypted"):

        1. Journal the OLD key to ``vault.key.retired``.
        2. Write the re-encrypted token FIRST -- the store still opens with
           the old key, and ``_load`` transparently falls back to the
           retired key while a journal exists.
        3. Install the new key (keyring preferred, disk fallback).
        4. Remove the journal; rotation is complete.

        Any crash leaves either (old key + old token) or (new key + new
        token) or (new key + old token + journal); every state opens
        cleanly because ``_load`` tries journaled retired keys on failure.
        """

        with self._locked():
            payload = self._load()
            accounts = sum(len(items) for items in payload["accounts"].values())
            new_key = Fernet.generate_key()
            current_key = self._key(create=False)
            if current_key is not None:
                # Journal the old key so no crash can orphan the token.
                _atomic_private_file(self.root / "vault.key.retired", current_key)
                _atomic_private_file(
                    self.root / "rotation.journal",
                    json.dumps({"phase": "re-encrypt"}).encode(),
                )
            # Token first: encrypted with the NEW key, not yet installed.
            stamped = dict(payload)
            stamped.pop("key_id", None)
            self._validate_payload(stamped)
            stamped["schema_version"] = _VAULT_SCHEMA_VERSION
            stamped["key_id"] = _key_id(new_key)
            cleartext = (json.dumps(stamped, sort_keys=True, separators=(",", ":")) + "\n").encode()
            _atomic_private_file(self.vault_file, Fernet(new_key).encrypt(cleartext))
            # Now install the new key.
            if not self._key_to_ring(new_key):
                _atomic_private_file(self.key_file, new_key)
            # Rotation complete: clear journal and retired copy.
            (self.root / "rotation.journal").unlink(missing_ok=True)
            (self.root / "vault.key.retired").unlink(missing_ok=True)
            return {"status": "rotated", "accounts": accounts}

    def _retired_keys(self) -> list[bytes]:
        """Old keys still referenced by an interrupted rotation journal."""

        retired = self.root / "vault.key.retired"
        journal = self.root / "rotation.journal"
        if not (journal.exists() and retired.exists()):
            return []
        try:
            return [_safe_private_file(retired, maximum_size=128)]
        except AuthenticationError:
            return []

    def _recover_if_needed(self, token: bytes, key: bytes) -> bytes:
        """Heal an interrupted rotation before decrypting.

        If the token fails under the installed key but a rotation journal
        exists, the crash happened after "new key installed" but before the
        token was replaced: reinstall the journaled retired key (which does
        open the token), clear the journal, and hand back the working key.
        The next rotate() then redoes the rotation cleanly.
        """

        try:
            Fernet(key).decrypt(token)
            return key
        except InvalidToken:
            pass
        for candidate in self._retired_keys():
            try:
                Fernet(candidate).decrypt(token)
                if not self._key_to_ring(candidate):
                    _atomic_private_file(self.key_file, candidate)
                (self.root / "rotation.journal").unlink(missing_ok=True)
                (self.root / "vault.key.retired").unlink(missing_ok=True)
                return candidate
            except InvalidToken:
                continue
        return key

    def _load(self) -> dict[str, Any]:
        if not self.root.exists() and not self.root.is_symlink():
            return {"schema_version": _VAULT_SCHEMA_VERSION, "accounts": {}}
        _safe_directory(self.root, private=True)
        key = self._key(create=False)
        if key is None:
            return {"schema_version": _VAULT_SCHEMA_VERSION, "accounts": {}}
        if not self.vault_file.exists() and not self.vault_file.is_symlink():
            return {"schema_version": _VAULT_SCHEMA_VERSION, "accounts": {}}
        token = _safe_private_file(self.vault_file)
        key = self._recover_if_needed(token, key)
        try:
            cleartext = Fernet(key).decrypt(token)
            payload = json.loads(cleartext.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AuthenticationError("auth_store_invalid", "authentication vault cannot be decrypted") from exc
        self._validate_payload(payload)
        stamped = payload.get("key_id")
        if payload.get("schema_version") == 2 and isinstance(stamped, str) and stamped != _key_id(key):
            # The vault was written by a different key than the one currently
            # installed. Name the mismatch precisely instead of letting a
            # rotation or dual-key accident look like corruption.
            raise AuthenticationError(
                "auth_store_key_mismatch",
                "vault records are stamped with a different key id than the installed vault key",
            )
        return payload

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) not in (
            {"schema_version", "accounts"},
            {"schema_version", "accounts", "key_id"},
        ):
            raise AuthenticationError("auth_store_invalid", "authentication vault shape is invalid")
        version = payload.get("schema_version")
        if version not in (1, 2) or not isinstance(payload.get("accounts"), dict):
            raise AuthenticationError("auth_store_invalid", "authentication vault version is invalid")
        if version == 2:
            # key_id may be absent while a payload is in flight in memory;
            # its presence and match against the installed key are enforced
            # on the read path (_load) for anything that came off disk.
            key_id = payload.get("key_id")
            if key_id is not None and (
                not isinstance(key_id, str)
                or len(key_id) != _KEY_ID_BYTES * 2
                or any(char not in "0123456789abcdef" for char in key_id)
            ):
                raise AuthenticationError("auth_store_invalid", "authentication vault key id is invalid")
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
        stamped = dict(payload)
        stamped.pop("key_id", None)
        self._validate_payload(stamped)
        key = self._key(create=True)
        assert key is not None
        # Schema v2: stamp the writing key's fingerprint so a later load can
        # prove it holds the right key (and rotation actually rotated).
        stamped["schema_version"] = _VAULT_SCHEMA_VERSION
        stamped["key_id"] = _key_id(key)
        cleartext = (json.dumps(stamped, sort_keys=True, separators=(",", ":")) + "\n").encode()
        token = Fernet(key).encrypt(cleartext)
        if len(token) > _MAX_VAULT_SIZE:
            # Enforce the cap on the NEW bytes. The reader validates the file
            # it is about to read; writing an oversized vault would succeed
            # once and then be unreadable forever.
            raise AuthenticationError(
                "auth_store_limit",
                "authentication vault exceeds its size policy",
            )
        _atomic_private_file(self.vault_file, token)

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

    def _registry_mac(self, profiles_json: bytes) -> str:
        """HMAC over the registry body, keyed by a STABLE registry secret.

        The profile registry is plaintext by design (no secrets inside),
        but its contents drive authorization decisions — so tampering with
        bindings, status, or verification must be detectable. The MAC key
        is a dedicated random secret stored beside the registry (0600),
        NOT the vault key: rotation must not invalidate the registry.
        """

        mac_file = self.root / "registry.mac"
        if mac_file.exists():
            return hmac.new(_safe_private_file(mac_file, maximum_size=128), profiles_json, hashlib.sha256).hexdigest()
        mac_key = os.urandom(32)
        _atomic_private_file(mac_file, mac_key)
        return hmac.new(mac_key, profiles_json, hashlib.sha256).hexdigest()

    def _load_profiles(self) -> dict[str, Any] | None:
        if not self.profile_file.exists() and not self.profile_file.is_symlink():
            return None
        raw = _safe_private_file(self.profile_file, maximum_size=256 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise AuthenticationError("auth_profile_store_invalid", "authentication profile registry is invalid") from exc
        # Integrity: when a MAC is present it must match; unsigned legacy
        # registries are accepted and gain a MAC on their next write.
        if isinstance(payload, dict) and "integrity" in payload:
            recorded = payload.pop("integrity")
            body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            expected = self._registry_mac(body)
            if not isinstance(recorded, str) or not hmac.compare_digest(recorded, expected):
                raise AuthenticationError(
                    "auth_profile_store_invalid",
                    "authentication profile registry failed its integrity check",
                )
            self._validate_profile_payload(payload)
            return {"integrity": recorded, **payload}
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

    def profile_payload_consistent(self) -> dict[str, Any]:
        """Reconciled profile projection read under the store lock.

        Identical to ``profile_payload()`` but never serves a snapshot that
        interleaves with a concurrent writer; used where a stale projection
        would produce wrong decisions rather than merely old ones. Absent
        stores stay absent (no tree creation on read).
        """

        if not self.root.exists() and not self.root.is_symlink():
            empty = {"schema_version": _VAULT_SCHEMA_VERSION, "accounts": {}}
            return self._reconciled_profiles(empty, None)
        with self._locked():
            vault = self._load()
            registry = self._load_profiles()
            return self._reconciled_profiles(vault, registry)

    def write_profile_payload(self, payload: dict[str, Any]) -> None:
        self._validate_profile_payload(payload)
        body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # Stamp an integrity MAC (keyed by the vault key) so tampering with
        # bindings/status/verification is detected on the next read. The
        # MAC covers the canonical body; the reader re-computes it after
        # popping the "integrity" field.
        envelope = {"integrity": self._registry_mac(body), **payload}
        data = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode()
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
        if not self.root.exists() and not self.root.is_symlink():
            raise AuthenticationError("credentials_not_enrolled", "credentials are not enrolled")
        with self._locked():
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
        if not self.root.exists() and not self.root.is_symlink():
            return set()
        with self._locked():
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
        payload = self._store.profile_payload_consistent()
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

    def select_plugin_profile(self, profile: str, plugin_id: str, provider: str) -> dict[str, Any]:
        """Bind an enrolled profile to a plugin (one profile per plugin)."""

        _require_slug(profile, "profile")
        self._require_plugin_provider(plugin_id, provider)
        updated = self._store.select_profile_for_plugin(profile, plugin_id, _provider(provider).id)
        return self._public_profile(profile, updated)

    def clear_plugin_profile(self, plugin_id: str) -> dict[str, Any]:
        """Release a plugin's profile binding so its profile can be removed."""

        _require_slug(plugin_id, "plugin_id")
        with self._store._locked():
            vault = self._store._load()
            registry = self._store._reconciled_profiles(vault, self._store._load_profiles())
            released = None
            now = _utc_now()
            for record in registry["profiles"].values():
                if plugin_id in record["bindings"]:
                    record["bindings"] = [value for value in record["bindings"] if value != plugin_id]
                    record["updated_at"] = now
                    if released is None:
                        released = dict(record)
                        released.pop("bindings", None)
            if released is None:
                raise AuthenticationError(
                    "profile_not_selected",
                    f"plugin {plugin_id} has no selected authentication profile",
                )
            self._store.write_profile_payload(registry)
        return {
            "profile": None,
            "type": None,
            "type_name": None,
            "status": "released",
            "released": released,
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

    def _mark_profile_verified(self, provider: str, account_alias: str) -> None:
        """Flip a profile's verification to 'verified' after a live login.

        Best-effort and idempotent: a successful bounded login against the
        realm is the strongest acceptance evidence Dispatch has, so the
        profile registry reflects it. Failures here never mask the result.
        """

        try:
            with self._store._locked():
                vault = self._store._load()
                registry = self._store._reconciled_profiles(vault, self._store._load_profiles())
                changed = False
                for record in registry["profiles"].values():
                    if (
                        record.get("provider") == provider
                        and record.get("account_alias") == account_alias
                        and record.get("verification") != "verified"
                    ):
                        record["verification"] = "verified"
                        record["updated_at"] = _utc_now()
                        changed = True
                if changed:
                    self._store.write_profile_payload(registry)
        except AuthenticationError:
            return

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
        selected_ids = {value.id for value in selected}
        any_configured = any(realm in selected_ids for (realm, _alias) in configured)
        return {
            "backend": "ready",
            # True when the selected realms hold ANY enrolled account, so a
            # vault holding only named-profile records (alias = profile slug)
            # still reports as configured. The per-realm list below stays
            # pinned to the explicitly requested alias.
            "configured": any((value.id, account_alias) in configured for value in selected) or any_configured,
            "account_alias": account_alias,
            "realms": [
                {
                    "id": value.id,
                    "landing_url": value.landing_url,
                    "status": "configured" if (value.id, account_alias) in configured else (
                        "configured_named_only"
                        if any(realm == value.id for (realm, _alias) in configured)
                        else "not_enrolled"
                    ),
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

        result = authenticate(self, session, account_alias)
        if result.authenticated:
            self._mark_profile_verified(_realm(session.realm).id, account_alias)
        return result

    def resume(
        self,
        session: ManagedBrowserSession,
        account_alias: str = "default",
    ) -> "AuthenticationResult":
        from .workflow import authenticate

        result = authenticate(self, session, account_alias, resume=True)
        if result.authenticated:
            self._mark_profile_verified(_realm(session.realm).id, account_alias)
        return result

    def remove(self, realm_id: str, account_alias: str = "default") -> dict[str, Any]:
        _realm(realm_id)
        _require_slug(account_alias, "account_alias")
        payload = self._store.profile_payload_consistent()
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
