from __future__ import annotations

import json
from pathlib import Path
import stat
import types

import pytest

from authentication import AuthenticationError, AuthenticationManager
from browser_manager import ManagedBrowserSession
from paths import DispatchPaths


class FakeKeyring:
    """In-memory stand-in for the OS Secret Service."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.available = True
        self.fail_store = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType("authentication.keyring")
        module.available = lambda: self.available
        module.load = lambda: next(iter(self.items.values()), None)
        module.store = self._store
        module.delete = self._delete
        monkeypatch.setitem(__import__("sys").modules, "authentication.keyring", module)

    def _store(self, key: bytes) -> None:
        if self.fail_store:
            raise RuntimeError("synthetic keyring failure")
        self.items["vault-key"] = key

    def _delete(self) -> None:
        self.items.clear()


def manager(tmp_path: Path) -> AuthenticationManager:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = DispatchPaths.from_environment(
        {"HOME": str(home)},
        code_root=Path(__file__).resolve().parents[3],
    )
    return AuthenticationManager(paths)


def test_status_is_non_mutating_and_zero_credentials_is_ready(tmp_path: Path) -> None:
    authentication = manager(tmp_path)

    status = authentication.status()

    assert status["backend"] == "ready"
    assert status["configured"] is False
    assert {item["status"] for item in status["realms"]} == {"not_enrolled"}
    assert not authentication.store_root.exists()


def test_enroll_encrypts_credentials_and_returns_only_safe_status(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    values = {
        "username": "synthetic-user",
        "password": "synthetic-password-not-a-secret",
    }

    result = authentication.enroll("amazon-operations", "default", values)

    assert result == {
        "realm": "amazon-operations",
        "account_alias": "default",
        "status": "configured",
    }
    assert authentication.credentials("amazon-operations").values == values
    assert authentication.status("amazon-operations")["realms"][0]["status"] == "configured"
    assert stat.S_IMODE(authentication.store_root.stat().st_mode) == 0o700
    for name in ("vault.key", "credentials.enc", "credentials.lock"):
        assert stat.S_IMODE((authentication.store_root / name).stat().st_mode) == 0o600
    stored_bytes = b"".join(path.read_bytes() for path in authentication.store_root.iterdir())
    assert values["username"].encode() not in stored_bytes
    assert values["password"].encode() not in stored_bytes
    assert values["username"] not in json.dumps(authentication.status())


def test_invalid_fields_fail_closed_and_remove_is_idempotent(tmp_path: Path) -> None:
    authentication = manager(tmp_path)

    with pytest.raises(AuthenticationError) as failure:
        authentication.enroll("amazon-operations", "default", {"username": "synthetic-user"})
    assert failure.value.code == "invalid_credentials"

    authentication.enroll(
        "amazon-operations",
        "default",
        {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"},
    )
    with pytest.raises(AuthenticationError) as duplicate:
        authentication.enroll(
            "amazon-operations",
            "default",
            {"username": "replacement-user", "password": "replacement-password"},
        )
    assert duplicate.value.code == "profile_exists"


def test_vault_key_prefers_keyring_and_removes_disk_copy(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    authentication = manager(tmp_path)

    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    assert len(ring.items) == 1
    assert not (authentication.store_root / "vault.key").exists()
    assert authentication.credentials("amazon-operations").values == values


def test_vault_falls_back_to_disk_without_a_keyring(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.available = False
    ring.install(monkeypatch)
    authentication = manager(tmp_path)

    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    assert not ring.items
    key_file = authentication.store_root / "vault.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert authentication.credentials("amazon-operations").values == values


def test_keyring_failure_during_creation_falls_back_to_disk(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.fail_store = True
    ring.install(monkeypatch)
    authentication = manager(tmp_path)

    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    assert not ring.items
    assert (authentication.store_root / "vault.key").exists()
    assert authentication.credentials("amazon-operations").values == values


def test_existing_disk_key_is_used_when_no_keyring_holds_it(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    # Enroll with no keyring available so the key lands on disk...
    ring.available = False
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)
    # ...then make the keyring appear and verify the disk key still decrypts.
    ring.available = True

    assert authentication.credentials("amazon-operations").values == values


def test_disk_key_still_works_end_to_end_after_keyring_appears(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    ring.available = False
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)
    ring.available = True

    assert authentication.credentials("amazon-operations").values["username"] == "synthetic-user"
    assert authentication.remove("amazon-operations")["status"] == "removed"
    assert authentication.remove("amazon-operations")["status"] == "not_enrolled"
    with pytest.raises(AuthenticationError) as missing:
        authentication.credentials("amazon-operations")
    assert missing.value.code == "credentials_not_enrolled"


def test_browser_session_is_bound_to_the_canonical_realm_landing_page(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll(
        "amazon-operations",
        "default",
        {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"},
    )

    page = type("Page", (), {"url": "https://logistics.amazon.com/dspconsolev2"})()
    session = ManagedBrowserSession(
        lease_id="synthetic-lease",
        realm="amazon-operations",
        landing_url="https://logistics.amazon.com/dspconsolev2",
        page=page,
        context=object(),
    )
    assert authentication.verify_landing(session)["status"] == "verified"
    assert authentication.credentials_for_session(session).realm == "amazon-operations"

    page.url = "https://example.invalid/"
    assert authentication.verify_landing(session)["status"] == "not_at_landing"
