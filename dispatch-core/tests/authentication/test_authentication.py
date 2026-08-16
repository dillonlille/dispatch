from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from authentication import AuthenticationError, AuthenticationManager
from browser_manager import ManagedBrowserSession
from paths import DispatchPaths


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
