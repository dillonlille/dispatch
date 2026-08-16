"""Private credential storage and bounded Authentication status for Dispatch Core."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
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


_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MAX_VAULT_SIZE = 1024 * 1024


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


DEFAULT_AUTH_REALMS = (
    AuthenticationRealm(
        id="amazon-operations",
        landing_url="https://logistics.amazon.com/dspconsolev2",
        credential_fields=("username", "password"),
    ),
    AuthenticationRealm(
        id="paycom-client",
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
_REALMS = {realm.id: realm for realm in DEFAULT_AUTH_REALMS}


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
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
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
            key = _safe_private_file(self.key_file, maximum_size=128)
            try:
                Fernet(key)
            except (TypeError, ValueError) as exc:
                raise AuthenticationError("auth_store_invalid", "authentication vault key is invalid") from exc
            return key
        if vault_present:
            raise AuthenticationError("auth_store_invalid", "authentication vault key is missing")
        if not create:
            return None
        key = Fernet.generate_key()
        _atomic_private_file(self.key_file, key)
        return key

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
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
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

    def put(self, realm_id: str, account_alias: str, values: Mapping[str, str]) -> None:
        with self._locked():
            payload = self._load()
            accounts = payload["accounts"].setdefault(realm_id, {})
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
        root = paths.owner_root("secrets", "dispatch-core") / "authentication"
        self._store = EncryptedCredentialStore(root)

    @property
    def store_root(self) -> Path:
        return self._store.root

    @staticmethod
    def realms() -> tuple[AuthenticationRealm, ...]:
        return DEFAULT_AUTH_REALMS

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
        removed = self._store.remove(realm_id, account_alias)
        return {
            "realm": realm_id,
            "account_alias": account_alias,
            "status": "removed" if removed else "not_enrolled",
        }


from .workflow import AuthenticationResult


__all__ = [
    "AuthenticationError",
    "AuthenticationManager",
    "AuthenticationRealm",
    "AuthenticationResult",
    "CredentialSet",
    "DEFAULT_AUTH_REALMS",
    "EncryptedCredentialStore",
]
