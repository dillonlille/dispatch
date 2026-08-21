from __future__ import annotations

import pytest

from authentication import AuthenticationError, AuthenticationResult
from browser_manager import LeaseState
from collection_manager import CollectionDisposition, CollectionManager, CollectionManagerError, CollectionReceipt, CollectorRegistration, CollectionRequest


class NoLaunchBrowser:
    def acquire(self, request):
        raise AssertionError("browser must not be launched during profile preflight")


class MissingProfileAuth:
    def profile_for_plugin(self, plugin_id: str, provider: str) -> str:
        raise AuthenticationError("profile_required", "profile required")

    def account_alias_for_profile(self, profile: str, provider: str) -> str:
        raise AssertionError("account lookup must not run after missing profile")


def registration() -> CollectorRegistration:
    return CollectorRegistration(
        "profile-collector",
        "paycom",
        "1.0.0",
        lambda _context: CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True),
        browser_realm="paycom-client",
        authentication_required=True,
    )


def test_authenticated_collection_fails_before_browser_when_profile_missing() -> None:
    manager = CollectionManager(NoLaunchBrowser(), MissingProfileAuth())  # type: ignore[arg-type]
    manager.register(registration())

    with pytest.raises(CollectionManagerError) as error:
        manager.run(CollectionRequest("profile-collector"))
    assert error.value.code == "authentication_profile_required"


class SelectedProfileAuth:
    def profile_for_plugin(self, plugin_id: str, provider: str) -> str:
        assert (plugin_id, provider) == ("paycom", "paycom-client")
        return "payroll"

    def account_alias_for_profile(self, profile: str, provider: str) -> str:
        assert (profile, provider) == ("payroll", "paycom-client")
        return "payroll"

    def authenticate(self, session, account_alias: str) -> AuthenticationResult:
        assert account_alias == "payroll"
        return AuthenticationResult("paycom-client", "payroll", "already_authenticated", True)


class Lease:
    def __init__(self):
        self.session = type("Session", (), {"realm": "paycom-client"})()
        self.closed = False

    def activate(self):
        return None

    def release(self):
        self.closed = True
        return type("Result", (), {"state": LeaseState.CLOSED})()


def test_selected_profile_resolves_to_internal_alias_without_collection_context_secret() -> None:
    lease = Lease()
    browser = type("Browser", (), {"acquire": lambda self, request: (setattr(self, "request", request) or lease)})()
    observed = {}
    registration_value = CollectorRegistration(
        "profile-collector",
        "paycom",
        "1.0.0",
        lambda context: (observed.update(context=context) or CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True)),
        browser_realm="paycom-client",
        authentication_required=True,
    )
    manager = CollectionManager(browser, SelectedProfileAuth())  # type: ignore[arg-type]
    manager.register(registration_value)

    result = manager.run(CollectionRequest("profile-collector"))

    assert result.status == "succeeded"
    assert getattr(browser, "request").account_alias == "payroll"
    assert observed["context"].session is lease.session
    assert not hasattr(observed["context"], "credentials")


def test_persisted_task_alias_is_not_replaced_by_a_later_plugin_binding() -> None:
    class PersistedAliasAuth:
        def profile_for_plugin(self, _plugin_id, _provider):
            raise AssertionError("durable tasks must not resolve a later profile binding")

        def authenticate(self, _session, account_alias):
            assert account_alias == "payroll"
            return AuthenticationResult("paycom-client", "payroll", "already_authenticated", True)

    lease = Lease()
    browser = type("Browser", (), {"acquire": lambda self, request: (setattr(self, "request", request) or lease)})()
    manager = CollectionManager(browser, PersistedAliasAuth())  # type: ignore[arg-type]
    manager.register(registration())

    result = manager._run_request(  # type: ignore[attr-defined]
        CollectionRequest("profile-collector", account_alias="payroll"),
        "0" * 32,
        None,
        resolve_profile=False,
    )

    assert result.status == "succeeded"
    assert getattr(browser, "request").account_alias == "payroll"


def test_explicit_legacy_account_alias_is_preserved() -> None:
    class LegacyAliasAuth:
        def credentials(self, provider, account_alias):
            assert (provider, account_alias) == ("paycom-client", "legacy-payroll")
            return object()

        def profile_for_plugin(self, _plugin_id, _provider):
            raise AssertionError("explicit legacy account must not resolve a named profile")

        def authenticate(self, _session, account_alias):
            assert account_alias == "legacy-payroll"
            return AuthenticationResult("paycom-client", account_alias, "already_authenticated", True)

    lease = Lease()
    browser = type("Browser", (), {"acquire": lambda self, request: (setattr(self, "request", request) or lease)})()
    manager = CollectionManager(browser, LegacyAliasAuth())  # type: ignore[arg-type]
    manager.register(registration())

    result = manager.run(CollectionRequest("profile-collector", account_alias="legacy-payroll"))

    assert result.status == "succeeded"
    assert getattr(browser, "request").account_alias == "legacy-payroll"
