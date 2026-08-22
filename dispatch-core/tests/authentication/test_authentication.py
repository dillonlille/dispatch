from __future__ import annotations

import json
from pathlib import Path
import stat
import types

import pytest

from cryptography.fernet import Fernet

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


def test_symlinked_vault_files_fail_closed(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.available = False
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    outside = tmp_path / "outside-target"
    outside.write_bytes(b"synthetic")
    key_path = authentication.store_root / "vault.key"
    key_path.unlink()
    key_path.symlink_to(outside)

    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_unsafe"


def test_world_readable_key_fails_closed(monkeypatch, tmp_path: Path) -> None:
    import os

    ring = FakeKeyring()
    ring.available = False
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    key_path = authentication.store_root / "vault.key"
    os.chmod(key_path, 0o644)
    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_unsafe"
    os.chmod(key_path, 0o600)
    # Restoring the mode recovers the store without re-enrolling.
    assert authentication.credentials("amazon-operations").values == values


def test_group_writable_store_directory_fails_closed(monkeypatch, tmp_path: Path) -> None:
    import os

    ring = FakeKeyring()
    ring.available = False
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    os.chmod(authentication.store_root, 0o750)
    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_unsafe"
    os.chmod(authentication.store_root, 0o700)
    assert authentication.credentials("amazon-operations").values == values


def test_divergent_dual_keys_fail_closed_instead_of_silent_disk_preference(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    ring.available = False
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)
    # A second key appears in the keyring while an older disk key exists.
    ring.available = True
    ring.items["vault-key"] = Fernet.generate_key()

    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_ambiguous"


def test_unreachable_keyring_with_ring_only_vault_is_reported_as_transient(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    # Enroll normally so the key lands in the (fake) OS keyring...
    authentication.enroll("amazon-operations", "default", values)
    assert not (authentication.store_root / "vault.key").exists()
    # ...then make the keyring disappear entirely (dbus down).
    ring.available = False

    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_key_unavailable"

    # Recovery is possible once the keyring returns.
    ring.available = True
    assert authentication.credentials("amazon-operations").values == values


def test_rotate_rekeys_the_vault_without_losing_accounts(monkeypatch, tmp_path: Path) -> None:
    ring = FakeKeyring()
    ring.install(monkeypatch)
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    result = authentication.rotate_vault()

    assert result == {"status": "rotated", "accounts": 1}
    assert len(ring.items) == 1
    # The old key must be gone from the ring; the new one decrypts the data.
    assert authentication.credentials("amazon-operations").values == values
    assert authentication.remove("amazon-operations")["status"] == "removed"


def test_vault_v2_stamps_key_id_and_upgrades_v1_payloads(monkeypatch, tmp_path: Path) -> None:
    import json as json_module
    import os

    from cryptography.fernet import Fernet as _Fernet

    from authentication import _key_id

    ring = FakeKeyring()
    ring.install(monkeypatch)
    ring.available = False
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    store_root = authentication.store_root
    # Create each level privately: parents=True with a umask would leave
    # intermediates group-writable, which the ancestor-chain guard rejects.
    missing = []
    probe = store_root
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for level in reversed(missing):
        level.mkdir(mode=0o700)

    # Write a v1-era vault token directly (pre-upgrade shape)...
    legacy_key = Fernet.generate_key()
    legacy_payload = {
        "schema_version": 1,
        "accounts": {
            "amazon-operations": {
                "default": {"updated_at": "2026-01-01T00:00:00Z", "values": dict(values)}
            }
        },
    }
    cleartext = (json_module.dumps(legacy_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (store_root / "credentials.enc").write_bytes(_Fernet(legacy_key).encrypt(cleartext))
    (store_root / "vault.key").write_bytes(legacy_key)
    os.chmod(store_root / "credentials.enc", 0o600)
    os.chmod(store_root / "vault.key", 0o600)

    # ...it still reads under v1 rules...
    assert authentication.credentials("amazon-operations").values == values

    # ...and the next write upgrades it to v2 with a matching key stamp.
    authentication.enroll(
        "paycom-client",
        "default",
        {
            "client_code": "synthetic-client",
            "username": "synthetic-user",
            "password": "synthetic-password-not-a-secret",
            **{f"security_pin_{index}": f"synthetic-pin-{index}" for index in range(1, 6)},
        },
    )
    installed = (store_root / "vault.key").read_bytes()
    stored = json_module.loads(_Fernet(installed).decrypt((store_root / "credentials.enc").read_bytes()))
    assert stored["schema_version"] == 2
    assert stored["key_id"] == _key_id(installed)
    assert stored["accounts"]["amazon-operations"]["default"]["values"] == values


def test_vault_load_detects_a_stamped_key_mismatch(monkeypatch, tmp_path: Path) -> None:
    import json as json_module

    from cryptography.fernet import Fernet as _Fernet

    from authentication import _key_id

    ring = FakeKeyring()
    ring.install(monkeypatch)
    ring.available = False
    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll("amazon-operations", "default", values)

    store_root = authentication.store_root
    installed_key = (store_root / "vault.key").read_bytes()
    payload = {
        "schema_version": 2,
        "key_id": _key_id(Fernet.generate_key()),
        "accounts": {
            "amazon-operations": {
                "default": {"updated_at": "2026-01-01T00:00:00Z", "values": dict(values)}
            }
        },
    }
    cleartext = (json_module.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (store_root / "credentials.enc").write_bytes(_Fernet(installed_key).encrypt(cleartext))

    with pytest.raises(AuthenticationError) as failure:
        authentication.credentials("amazon-operations")
    assert failure.value.code == "auth_store_key_mismatch"


def test_oversized_vault_write_is_refused_before_bricking_reads(monkeypatch, tmp_path: Path) -> None:
    """VULN-6 regression: the size cap must apply to the NEW token at write
    time, so a vault can never be written that its own reader will refuse."""
    ring = FakeKeyring()
    ring.install(monkeypatch)
    ring.available = False
    authentication = manager(tmp_path)
    big = "V" * 4096
    written = 0
    # Keep writing until the store refuses; the refusal must come from the
    # write path (auth_store_limit) BEFORE the oversized file lands.
    for index in range(500):
        try:
            authentication._store.put("amazon-operations", f"acct{index}", {"username": big, "password": big})
            written += 1
        except AuthenticationError as exc:
            assert exc.code == "auth_store_limit", f"unexpected error at {index}: {exc.code}"
            break
    else:  # pragma: no cover - cap never reached in 500 accounts
        pytest.fail("vault size cap never triggered")
    assert written > 0
    # The previously-written accounts remain readable — no brick.
    assert authentication.credentials("amazon-operations", "acct0").values["username"] == big


def test_successful_login_marks_profile_verified(tmp_path: Path) -> None:
    from tests.authentication.test_workflow import FakePage

    authentication = manager(tmp_path)
    values = {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"}
    authentication.enroll_profile("operations", "amazon-operations", values)
    assert authentication.profile_status("operations")["verification"] == "unverified"

    page = FakePage("amazon-operations")
    landing = "https://logistics.amazon.com/dspconsolev2"
    managed_session = ManagedBrowserSession(
        lease_id="synthetic-lease",
        realm="amazon-operations",
        landing_url=landing,
        page=page,
        context=object(),
    )

    result = authentication.authenticate_profile(managed_session, "operations")

    assert result.authenticated is True
    assert result.account_alias == "operations"
    assert authentication.profile_status("operations")["verification"] == "verified"


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
