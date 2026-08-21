from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from .config import AmazonConfig, PLUGIN_ID
from .redaction import redact_secrets


class ManagedSessionError(RuntimeError):
    """A bounded failure while obtaining a Core-owned authenticated snapshot."""


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str = "/"


@dataclass(frozen=True)
class SessionMaterial:
    """In-process-only request material; never serialize or log this object."""

    user_agent: str
    cookies: tuple[BrowserCookie, ...]
    csrf_token: str


@dataclass(frozen=True)
class ContextProof:
    status: int
    content_type: str
    response_url: str
    key_count: int


class PageLike(Protocol):
    url: str

    def goto(self, url: str, *, wait_until: str, timeout: int) -> Any: ...

    def evaluate(self, expression: str) -> Any: ...


class ContextLike(Protocol):
    def cookies(self, urls: str | list[str] | None = None) -> list[dict[str, Any]]: ...


class AuthResultLike(Protocol):
    authenticated: bool
    status: str
    manual_action: str | None


_CONTEXT_SCRIPT_TEMPLATE = """async () => {
  const endpoint = %s;
  const response = await fetch(endpoint, {
    credentials: 'include',
    headers: { accept: 'application/json, text/plain, */*' },
  });
  const contentType = response.headers.get('content-type') || '';
  const text = await response.text();
  let body = null;
  try { body = JSON.parse(text); } catch (_) { body = null; }
  return {
    ok: response.ok,
    status: response.status,
    url: response.url,
    contentType,
    body,
    redirected: response.redirected,
  };
}"""


def validate_companion_config(config: AmazonConfig) -> None:
    expected = {
        "companion_url": "/dspconsolev2",
        "context_endpoint": "/companion/platform/api/context",
        "stream_endpoint": "/companion/platform/api/conversations/stream",
    }
    for name, path in expected.items():
        value = getattr(config, name)
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != "logistics.amazon.com" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 443} or parsed.path.rstrip("/") != path:
            raise ManagedSessionError(f"{name} must target the installed Companion endpoint")


def probe_companion_context(page: PageLike, endpoint: str) -> ContextProof:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname != "logistics.amazon.com" or parsed.path.rstrip("/") != "/companion/platform/api/context" or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ManagedSessionError("context endpoint is not the exact Companion context endpoint")
    try:
        value = page.evaluate(_CONTEXT_SCRIPT_TEMPLATE % json.dumps(endpoint))
    except Exception as exc:
        raise ManagedSessionError("Companion context probe failed") from exc
    if not isinstance(value, dict):
        raise ManagedSessionError("Companion context probe returned an invalid object")
    status = _as_int(value.get("status"))
    content_type = str(value.get("contentType") or "")
    response_url = str(value.get("url") or "")
    body = value.get("body")
    final = urlsplit(response_url)
    if status is None or not 200 <= status < 300 or "json" not in content_type.lower() or not isinstance(body, dict) or not body:
        if _looks_like_login(value):
            raise ManagedSessionError("Companion authentication is required")
        raise ManagedSessionError("Companion context response is unavailable")
    if (
        final.scheme != "https"
        or final.hostname != "logistics.amazon.com"
        or final.path.rstrip("/") != "/companion/platform/api/context"
        or final.query
        or final.fragment
        or final.username
        or final.password
        or final.port not in {None, 443}
    ):
        raise ManagedSessionError("Companion context probe redirected away from its endpoint")
    authentication_fields = ("authenticated", "isAuthenticated", "loggedIn", "isLoggedIn")
    if any(body.get(key) is False for key in authentication_fields):
        raise ManagedSessionError("Companion authentication is required")
    if not any(body.get(key) is True for key in authentication_fields):
        raise ManagedSessionError("Companion context response did not prove authenticated identity")
    return ContextProof(status, content_type, response_url, len(body))


def snapshot_session(page: PageLike, context: ContextLike, companion_url: str) -> SessionMaterial:
    try:
        user_agent = str(page.evaluate("navigator.userAgent") or "")
        csrf = str(page.evaluate("(() => { const m = document.querySelector(\"meta[name='csrf-token']\"); return m && m.content ? m.content : ''; })()") or "")
        raw_cookies = context.cookies([companion_url])
    except Exception as exc:
        raise ManagedSessionError("authenticated Companion session snapshot failed") from exc
    cookies_list: list[BrowserCookie] = []
    for value in raw_cookies:
        cookie = _cookie(value)
        if cookie is not None:
            cookies_list.append(cookie)
    if not user_agent or not csrf or not cookies_list:
        raise ManagedSessionError("authenticated Companion session snapshot is incomplete")
    return SessionMaterial(user_agent=user_agent, cookies=tuple(cookies_list), csrf_token=csrf)


class ManagedCompanionSessionProvider:
    """Acquire Core managers per request and release the lease before HTTP streaming."""

    def __init__(
        self,
        config: AmazonConfig,
        *,
        browser_manager: Any | None = None,
        authentication_manager: Any | None = None,
        browser_factory: Callable[[Any], Any] | None = None,
        auth_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self.browser_manager = browser_manager
        self.authentication_manager = authentication_manager
        self.browser_factory = browser_factory
        self.auth_factory = auth_factory

    def snapshot(self) -> SessionMaterial:
        validate_companion_config(self.config)
        try:
            from authentication import AuthenticationManager
            from browser_manager import BrowserLeaseRequest, BrowserManager, BrowserPurpose, LeaseState
        except ImportError as exc:
            raise ManagedSessionError("Dispatch Core browser and authentication services are unavailable") from exc
        core_paths = None
        if self.browser_manager is None or self.authentication_manager is None:
            from paths import DispatchPaths

            core_paths = DispatchPaths.from_environment()
        browser = self.browser_manager or (
            self.browser_factory(core_paths) if self.browser_factory else BrowserManager(core_paths)
        )
        authentication = self.authentication_manager or (
            self.auth_factory(core_paths) if self.auth_factory else AuthenticationManager(core_paths)
        )
        legacy_alias = getattr(self.config, "auth_account_alias", "default")
        if hasattr(authentication, "profile_for_plugin"):
            try:
                profile = authentication.profile_for_plugin(PLUGIN_ID, "amazon-operations")
            except Exception as exc:
                raise ManagedSessionError("Companion authentication profile is not selected") from exc
        else:
            profile = legacy_alias
        broker = None
        if hasattr(authentication, "profile") and hasattr(authentication, "authenticate"):
            broker = authentication
            account_alias = broker.account_alias
        elif hasattr(authentication, "for_plugin"):
            try:
                broker = authentication.for_plugin(PLUGIN_ID, "amazon-operations", profile)
                account_alias = broker.account_alias
            except Exception as exc:
                raise ManagedSessionError("Companion authentication profile is not enrolled") from exc
        else:
            # Compatibility for older Core test doubles and pre-profile Core.
            account_alias = profile
        request = BrowserLeaseRequest(
            plugin_id=PLUGIN_ID,
            plugin_release="0.1.1",
            realm="amazon-operations",
            purpose=BrowserPurpose.AUTHENTICATION,
            account_alias=account_alias,
        )
        lease = browser.acquire(request)
        try:
            lease.activate()
            session = lease.session
            if broker is not None:
                result: AuthResultLike = broker.authenticate(session)
            else:
                result = authentication.authenticate(session, account_alias)
            if not result.authenticated:
                status = str(result.status)
                manual = str(result.manual_action or "")
                raise ManagedSessionError(redact_secrets(f"Companion authentication is not ready: {status} {manual}".strip()))
            page: PageLike = session.page
            page.goto(self.config.companion_url, wait_until="domcontentloaded", timeout=30000)
            probe_companion_context(page, self.config.context_endpoint)
            return snapshot_session(page, session.context, self.config.companion_url)
        finally:
            # The lease must end before the caller constructs the direct HTTP SSE client.
            released = lease.release()
            if getattr(released, "state", None) is not LeaseState.CLOSED:
                raise ManagedSessionError("Browser Manager quarantined the Companion lease during cleanup")


def _cookie(value: Any) -> BrowserCookie | None:
    if not isinstance(value, dict):
        return None
    name, raw, domain = str(value.get("name") or ""), value.get("value"), str(value.get("domain") or "logistics.amazon.com")
    path = str(value.get("path") or "/")
    if not name or raw is None or len(name) > 256 or len(str(raw)) > 8192:
        return None
    return BrowserCookie(name, str(raw), domain, path)


def _looks_like_login(value: dict[str, Any]) -> bool:
    url = str(value.get("url") or "").lower()
    body = value.get("body")
    return "amazon.com/ap/signin" in url or (isinstance(body, dict) and any(body.get(key) is False for key in ("authenticated", "isAuthenticated", "loggedIn", "isLoggedIn")))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
