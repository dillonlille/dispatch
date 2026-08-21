import sys
import types

import pytest
from enum import Enum

from companion_bridge.config import AmazonConfig
from companion_bridge.managed_session import ManagedCompanionSessionProvider, ManagedSessionError, probe_companion_context, snapshot_session


class Page:
    url = "https://logistics.amazon.com/dspconsolev2"
    def __init__(self, authenticated=True): self.gotos = []; self.authenticated = authenticated
    def goto(self, url, *, wait_until, timeout): self.gotos.append((url, wait_until, timeout))
    def evaluate(self, expression):
        if "navigator.userAgent" in expression: return "fixture-agent"
        if "csrf-token" in expression: return "fixture-csrf"
        return {"ok": True, "status": 200, "url": "https://logistics.amazon.com/companion/platform/api/context", "contentType": "application/json", "body": {"authenticated": self.authenticated}}


class Context:
    def cookies(self, urls=None): return [{"name": "session", "value": "fixture-cookie", "domain": "logistics.amazon.com", "path": "/"}]


class Session:
    def __init__(self): self.page = Page(); self.context = Context()


class AuthResult:
    authenticated = True
    status = "login_success"
    manual_action = None


class Lease:
    def __init__(self): self.session = Session(); self.activated = False; self.released = False
    def activate(self): self.activated = True
    def release(self):
        self.released = True
        return types.SimpleNamespace(state=LeaseState.CLOSED)


class LeaseState(Enum):
    CLOSED = "closed"


def install_core_fixtures(monkeypatch, lease, requests, auth_calls):
    class Purpose: AUTHENTICATION = "authentication"
    class Request:
        def __init__(self, **kwargs): requests.append(kwargs)
    class Browser:
        def __init__(self, paths): pass
        def acquire(self, request): return lease
    class Authentication:
        def __init__(self, paths): pass
        def authenticate(self, session, alias): auth_calls.append((session, alias)); return AuthResult()
    monkeypatch.setitem(sys.modules, "browser_manager", types.SimpleNamespace(BrowserLeaseRequest=Request, BrowserManager=Browser, BrowserPurpose=Purpose, LeaseState=LeaseState))
    monkeypatch.setitem(sys.modules, "authentication", types.SimpleNamespace(AuthenticationManager=Authentication))


def test_exact_probe_and_snapshot_are_fixture_only() -> None:
    page = Page()
    proof = probe_companion_context(page, "https://logistics.amazon.com/companion/platform/api/context")
    assert proof.status == 200 and proof.key_count == 1
    material = snapshot_session(page, Context(), "https://logistics.amazon.com/dspconsolev2")
    assert material.user_agent == "fixture-agent" and material.csrf_token == "fixture-csrf" and len(material.cookies) == 1


def test_provider_authenticates_snapshots_then_releases_lease(monkeypatch) -> None:
    lease, requests, auth_calls = Lease(), [], []
    install_core_fixtures(monkeypatch, lease, requests, auth_calls)
    provider = ManagedCompanionSessionProvider(
        AmazonConfig(),
        browser_manager=sys.modules["browser_manager"].BrowserManager(object()),
        authentication_manager=sys.modules["authentication"].AuthenticationManager(object()),
    )
    material = provider.snapshot()
    assert lease.activated and lease.released
    assert requests[0]["plugin_id"] == "companion-bridge"
    assert requests[0]["realm"] == "amazon-operations"
    assert auth_calls and material.csrf_token == "fixture-csrf"


def test_profile_aware_manager_ignores_stale_legacy_alias(monkeypatch) -> None:
    lease, requests, auth_calls = Lease(), [], []
    install_core_fixtures(monkeypatch, lease, requests, auth_calls)
    observed = {}

    class Broker:
        profile = "new-profile"
        account_alias = "new-profile"

        def authenticate(self, session):
            observed["authenticated_session"] = session
            return AuthResult()

    class Authentication:
        def profile_for_plugin(self, plugin_id, provider):
            observed["profile_for_plugin"] = (plugin_id, provider)
            return "new-profile"

        def for_plugin(self, plugin_id, provider, profile):
            observed["for_plugin"] = (plugin_id, provider, profile)
            return Broker()

    provider = ManagedCompanionSessionProvider(
        AmazonConfig(auth_account_alias="old-profile"),
        browser_manager=sys.modules["browser_manager"].BrowserManager(object()),
        authentication_manager=Authentication(),
    )

    provider.snapshot()

    assert observed["profile_for_plugin"] == ("companion-bridge", "amazon-operations")
    assert observed["for_plugin"] == (
        "companion-bridge",
        "amazon-operations",
        "new-profile",
    )
    assert requests[0]["account_alias"] == "new-profile"


def test_context_rejects_wrong_endpoint_without_page_evaluation() -> None:
    with pytest.raises(ManagedSessionError):
        probe_companion_context(Page(), "https://logistics.amazon.com/other")


def test_context_rejects_empty_body_and_query_redirect() -> None:
    class EmptyPage(Page):
        def evaluate(self, expression):
            return {"ok": True, "status": 200, "url": "https://logistics.amazon.com/companion/platform/api/context", "contentType": "application/json", "body": {}}

    class QueryPage(Page):
        def evaluate(self, expression):
            return {"ok": True, "status": 200, "url": "https://logistics.amazon.com/companion/platform/api/context?unexpected=1", "contentType": "application/json", "body": {"authenticated": True}}

    with pytest.raises(ManagedSessionError):
        probe_companion_context(EmptyPage(), AmazonConfig().context_endpoint)
    with pytest.raises(ManagedSessionError):
        probe_companion_context(QueryPage(), AmazonConfig().context_endpoint)


def test_provider_fails_closed_when_release_is_quarantined(monkeypatch) -> None:
    lease, requests, auth_calls = Lease(), [], []
    install_core_fixtures(monkeypatch, lease, requests, auth_calls)
    lease.release = lambda: types.SimpleNamespace(state="quarantined")
    provider = ManagedCompanionSessionProvider(
        AmazonConfig(),
        browser_manager=sys.modules["browser_manager"].BrowserManager(object()),
        authentication_manager=sys.modules["authentication"].AuthenticationManager(object()),
    )
    with pytest.raises(ManagedSessionError, match="quarantined"):
        provider.snapshot()
