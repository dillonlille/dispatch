from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

import authentication as authentication_runtime
from authentication import AuthenticationError, AuthenticationManager
from paths import DispatchPaths


def manager(tmp_path: Path) -> AuthenticationManager:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return AuthenticationManager(
        DispatchPaths.from_environment(
            {"HOME": str(home)},
            code_root=Path(__file__).resolve().parents[3],
        )
    )


def amazon_values() -> dict[str, str]:
    return {"username": "synthetic-user", "password": "synthetic-password"}


def paycom_values() -> dict[str, str]:
    return {
        "client_code": "synthetic-client",
        "username": "synthetic-user",
        "password": "synthetic-password",
        **{f"security_pin_{index}": f"synthetic-pin-{index}" for index in range(1, 6)},
    }


def test_named_profiles_are_lowercase_global_and_provider_compatible(tmp_path: Path) -> None:
    authentication = manager(tmp_path)

    authentication.enroll_profile("operations", "amazon-operations", amazon_values(), plugin_id="companion-bridge")
    assert authentication.profile_for_plugin("companion-bridge", "amazon-operations") == "operations"
    assert authentication.profile_status("operations") == {
        "profile": "operations",
        "type": "amazon",
        "type_name": "Amazon Operations",
        "status": "enrolled",
        "verification": "unverified",
    }

    with pytest.raises(AuthenticationError) as mismatch:
        authentication.bind_profile("operations", "companion-bridge", "paycom-client")
    assert mismatch.value.code == "profile_provider_mismatch"
    with pytest.raises(AuthenticationError) as duplicate:
        authentication.enroll_profile("operations", "paycom-client", paycom_values())
    # A taken profile name always reports profile_exists, regardless of the
    # provider involved; existence is enforced atomically inside the store.
    assert duplicate.value.code == "profile_exists"
    with pytest.raises(AuthenticationError) as existing:
        authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    assert existing.value.code == "profile_exists"
    with pytest.raises(AuthenticationError) as invalid:
        authentication.enroll_profile("Not-A-Slug", "amazon-operations", amazon_values())
    assert invalid.value.code == "invalid_auth_request"


def test_profile_enrollment_is_atomic_inside_the_store_lock(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    store = authentication._store

    # First enrollment wins...
    store.put_profile("race", "amazon-operations", "race", amazon_values())
    # ...a second enrollment of the same profile fails closed inside the
    # locked section, even when the manager-level advisory check is bypassed
    # by a concurrent caller (regression for the last-writer-wins race).
    with pytest.raises(AuthenticationError) as conflict:
        store.put_profile("race", "paycom-client", "race", paycom_values())
    assert conflict.value.code == "profile_exists"
    # The same credential account also cannot be enrolled under a new name.
    with pytest.raises(AuthenticationError) as alias:
        store.put_profile("other", "amazon-operations", "race", amazon_values())
    assert alias.value.code == "profile_exists"


def test_profile_registry_is_private_encrypted_and_secret_free(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    values = amazon_values()
    authentication.enroll_profile("operations", "amazon-operations", values)

    registry = authentication.store_root / "profiles.json"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    stored = b"".join(path.read_bytes() for path in authentication.store_root.iterdir())
    assert values["username"].encode() not in stored
    assert values["password"].encode() not in stored
    assert values["username"] not in json.dumps(authentication.profiles())


def test_legacy_vault_records_project_to_profiles_without_reentry(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll("amazon-operations", "default", amazon_values())

    assert authentication.profiles() == [
        {
            "profile": "default",
            "type": "amazon",
            "type_name": "Amazon Operations",
            "status": "enrolled",
            "verification": "unverified",
        }
    ]
    assert not (authentication.store_root / "profiles.json").exists()
    assert authentication.profile_credentials("default", "amazon-operations").values == amazon_values()
    assert (authentication.store_root / "profiles.json").exists()


def test_legacy_alias_collision_uses_a_bounded_public_profile_type_prefix(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll("amazon-operations", "default", amazon_values())
    authentication.enroll("paycom-client", "default", paycom_values())

    profiles = authentication.profiles()

    assert [item["profile"] for item in profiles] == ["default", "paycom-default"]
    assert {item["type"] for item in profiles} == {"amazon", "paycom"}
    assert all(len(item["profile"]) <= 63 for item in profiles)


def test_orphaned_registry_record_is_not_deleted_silently(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    payload = authentication._store.profile_payload()  # type: ignore[attr-defined]
    payload["profiles"]["operations"]["account_alias"] = "missing"
    authentication._store.write_profile_payload(payload)  # type: ignore[attr-defined]

    assert authentication.profile_status("operations")["status"] == "orphaned"
    assert authentication.remove_profile("operations")["status"] == "removed"
    assert (authentication.store_root / "credentials.enc").exists()
    assert authentication.profiles()[0]["profile"] == "amazon-operations"


def test_unknown_orphan_profile_does_not_hide_healthy_profiles(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    payload = authentication._store.profile_payload()  # type: ignore[attr-defined]
    payload["profiles"]["retired"] = {
        "provider": "retired-provider",
        "account_alias": "retired",
        "status": "orphaned",
        "verification": "unverified",
        "updated_at": "2026-01-01T00:00:00Z",
        "bindings": [],
    }
    authentication._store.write_profile_payload(payload)  # type: ignore[attr-defined]

    profiles = authentication.profiles()

    assert [item["profile"] for item in profiles] == ["operations", "retired"]
    assert profiles[0]["status"] == "enrolled"
    assert profiles[1]["type"] == "unavailable"
    assert profiles[1]["status"] == "orphaned"


def test_interrupted_profile_removal_is_retryable(monkeypatch, tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    original = authentication_runtime._atomic_private_file
    failed = False

    def fail_registry_once(path, data):
        nonlocal failed
        if path.name == "profiles.json" and not failed:
            failed = True
            raise OSError("synthetic registry publication failure")
        return original(path, data)

    monkeypatch.setattr(authentication_runtime, "_atomic_private_file", fail_registry_once)
    with pytest.raises(OSError):
        authentication.remove_profile("operations")
    monkeypatch.setattr(authentication_runtime, "_atomic_private_file", original)

    assert authentication.profile_status("operations")["status"] == "orphaned"
    assert authentication.remove_profile("operations")["status"] == "removed"
    assert authentication.profiles() == []


def test_profile_selection_is_exact_and_in_use_profile_cannot_be_removed(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile(
        "amazon-primary",
        "amazon-operations",
        amazon_values(),
        plugin_id="companion-bridge",
    )
    authentication.enroll_profile("amazon-backup", "amazon-operations", amazon_values())

    authentication.bind_profile("amazon-backup", "companion-bridge", "amazon-operations")

    assert authentication.profile_for_plugin("companion-bridge", "amazon-operations") == "amazon-backup"
    with pytest.raises(AuthenticationError) as in_use:
        authentication.remove_profile("amazon-backup")
    assert in_use.value.code == "profile_in_use"
    with pytest.raises(AuthenticationError) as compatibility_in_use:
        authentication.remove("amazon-operations", "amazon-backup")
    assert compatibility_in_use.value.code == "profile_in_use"
    assert authentication.remove_profile("amazon-primary")["status"] == "removed"

    authentication.retain_plugin_bindings(set())
    assert authentication.remove_profile("amazon-backup")["status"] == "removed"


def test_plugin_scoped_broker_rejects_unselected_profile(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())

    with pytest.raises(AuthenticationError) as unauthorized:
        authentication.for_plugin("companion-bridge", "amazon-operations", "operations")
    assert unauthorized.value.code == "profile_not_authorized"


def test_profile_registry_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    registry = authentication.store_root / "profiles.json"
    registry.unlink()
    os.mkfifo(registry, 0o600)

    with pytest.raises(AuthenticationError) as unsafe:
        authentication.profiles()
    assert unsafe.value.code == "auth_store_unsafe"


def test_profile_registry_write_is_size_bounded(tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    record = {
        "provider": "amazon-operations",
        "account_alias": "account",
        "status": "orphaned",
        "verification": "unverified",
        "updated_at": "2026-01-01T00:00:00Z",
        "bindings": [],
    }
    payload = {
        "schema_version": 1,
        "profiles": {f"profile-{index}": dict(record) for index in range(5000)},
    }

    with pytest.raises(AuthenticationError) as oversized:
        authentication._store.write_profile_payload(payload)  # type: ignore[attr-defined]
    assert oversized.value.code == "auth_profile_store_invalid"


def test_vault_json_recursion_is_a_stable_store_error(monkeypatch, tmp_path: Path) -> None:
    authentication = manager(tmp_path)
    authentication.enroll_profile("operations", "amazon-operations", amazon_values())
    monkeypatch.setattr(
        authentication_runtime.json,
        "loads",
        lambda _value: (_ for _ in ()).throw(RecursionError()),
    )

    with pytest.raises(AuthenticationError) as invalid:
        authentication.profiles()
    assert invalid.value.code == "auth_store_invalid"
