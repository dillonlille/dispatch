"""Bounded Amazon and Paycom login workflow over a trusted Browser Manager session."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dispatch_core.browser_manager import ManagedBrowserSession

if TYPE_CHECKING:
    from . import AuthenticationManager


AMAZON_LOGIN = ("#ap_email", "#ap_password", "#signInSubmit")
PAYCOM_LOGIN = ("#clientcode", "#username", "#password", "#btnSubmit")

_SNAPSHOT_SCRIPT = r"""() => {
  const visible = element => !!element && !element.disabled && element.offsetParent !== null;
  const selectors = [
    '#ap_email', '#ap_password', '#signInSubmit',
    '#auth-mfa-otpcode', 'input[name="otpCode"]',
    '#auth-captcha-image', 'input[name="guess"]',
    '#clientcode', '#username', '#password', '#btnSubmit'
  ];
  const loginNames = new Set(['clientcode', 'username', 'password', 'email']);
  const challengeIndices = [];
  for (const element of document.querySelectorAll('input')) {
    if (!visible(element) || loginNames.has(String(element.name || '').toLowerCase())) continue;
    const labels = [element.name || '', element.id || '', element.getAttribute('aria-label') || '', element.placeholder || ''];
    if (element.id) {
      const label = document.querySelector('label[for="' + CSS.escape(element.id) + '"]');
      if (label) labels.push(label.innerText || label.textContent || '');
    }
    for (const label of labels) {
      const match = String(label).trim().match(/^(?:(?:enter|unique)\s+)?(?:paycom\s+)?(?:security\s+)?pin(?:\s*(?:number|no\.?|#))?\s*([1-5])\s*[:?]?$/i)
        || String(label).trim().match(/^(?:security[_-]?)?pin[_-]?([1-5])$/i);
      if (match) challengeIndices.push(Number(match[1]));
    }
  }
  return {
    url: location.href,
    text: (document.body && document.body.innerText || '').slice(0, 12000),
    present: selectors.filter(selector => {
      try { return visible(document.querySelector(selector)); } catch { return false; }
    }),
    challengeIndices: Array.from(new Set(challengeIndices)).sort((a, b) => a - b)
  };
}"""

_PAYCOM_CHALLENGE_SCRIPT = r"""payload => {
  const visible = element => !!element && !element.disabled && element.offsetParent !== null;
  const loginNames = new Set(['clientcode', 'username', 'password']);
  const detected = [];
  for (const element of document.querySelectorAll('input')) {
    if (!visible(element) || loginNames.has(String(element.name || '').toLowerCase())) continue;
    const labels = [element.name || '', element.id || '', element.getAttribute('aria-label') || '', element.placeholder || ''];
    if (element.id) {
      const label = document.querySelector('label[for="' + CSS.escape(element.id) + '"]');
      if (label) labels.push(label.innerText || label.textContent || '');
    }
    const indices = [];
    for (const label of labels) {
      const match = String(label).trim().match(/^(?:(?:enter|unique)\s+)?(?:paycom\s+)?(?:security\s+)?pin(?:\s*(?:number|no\.?|#))?\s*([1-5])\s*[:?]?$/i)
        || String(label).trim().match(/^(?:security[_-]?)?pin[_-]?([1-5])$/i);
      if (match) indices.push(Number(match[1]));
    }
    const unique = Array.from(new Set(indices));
    if (unique.length === 1) detected.push({ field: element, index: unique[0] });
  }
  const observed = detected.map(item => item.index).sort((a, b) => a - b);
  if (detected.length !== 2 || observed.join(',') !== payload.indices.join(',') || new Set(observed).size !== 2) {
    return { status: 'manual_verification_required' };
  }
  const forms = new Set(detected.map(item => item.field.form));
  if (forms.size !== 1 || forms.has(null)) return { status: 'manual_verification_required' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (const item of detected) {
    const value = payload.values[String(item.index)];
    if (typeof value !== 'string' || !value) return { status: 'manual_verification_required' };
    setter.call(item.field, value);
    item.field.dispatchEvent(new Event('input', { bubbles: true }));
    item.field.dispatchEvent(new Event('change', { bubbles: true }));
    if (item.field.value !== value) return { status: 'manual_verification_required' };
  }
  try { detected[0].field.form.requestSubmit(); }
  catch { return { status: 'manual_verification_required' }; }
  return { status: 'security_factors_submitted' };
}"""


@dataclass(frozen=True)
class AuthenticationResult:
    realm: str
    account_alias: str
    status: str
    authenticated: bool
    manual_action: str | None = None

    def safe_data(self) -> dict[str, Any]:
        return {
            "realm": self.realm,
            "account_alias": self.account_alias,
            "status": self.status,
            "authenticated": self.authenticated,
            "manual_action": self.manual_action,
        }


def _result(
    session: ManagedBrowserSession,
    account_alias: str,
    status: str,
    *,
    authenticated: bool = False,
    manual_action: str | None = None,
) -> AuthenticationResult:
    return AuthenticationResult(session.realm, account_alias, status, authenticated, manual_action)


def _snapshot(page: Any) -> dict[str, Any] | None:
    try:
        value = page.evaluate(_SNAPSHOT_SCRIPT)
    except Exception:
        return None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("url"), str)
        or not isinstance(value.get("text"), str)
        or not isinstance(value.get("present"), list)
        or not all(isinstance(item, str) for item in value["present"])
        or not isinstance(value.get("challengeIndices"), list)
        or not all(type(item) is int for item in value["challengeIndices"])
    ):
        return None
    return value


def _approved(realm: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or port is not None:
        return False
    if realm == "amazon-operations":
        return (parsed.hostname == "logistics.amazon.com" and parsed.path.startswith("/dspconsolev2")) or (
            parsed.hostname == "www.amazon.com" and parsed.path == "/ap/signin"
        )
    return parsed.hostname == "www.paycomonline.net" and parsed.path.startswith("/v4/cl/")


def _at_landing(session: ManagedBrowserSession, url: str) -> bool:
    expected = urlsplit(session.landing_url)
    current = urlsplit(url)
    return current.scheme == expected.scheme and current.hostname == expected.hostname and current.path == expected.path


def _classify(session: ManagedBrowserSession, snapshot: dict[str, Any]) -> tuple[str, list[int]]:
    url = snapshot["url"]
    if not _approved(session.realm, url):
        return "unapproved_page", []
    text = snapshot["text"].lower()
    present = set(snapshot["present"])
    challenges = sorted(set(snapshot["challengeIndices"]))

    if re.search(r"account.{0,30}(locked|disabled)|too many.{0,20}attempt", text):
        return "account_locked", []
    if re.search(r"invalid.{0,30}(client|user|password|credential)|incorrect.{0,20}(password|login)|security (?:pin|answer).{0,40}(not correct|incorrect|invalid)", text):
        return "invalid_credentials", []
    if "#auth-captcha-image" in present or 'input[name="guess"]' in present or re.search(r"\bcaptcha\b|enter the characters", text):
        return "captcha_required", []
    if "#auth-mfa-otpcode" in present or 'input[name="otpCode"]' in present or re.search(
        r"multi[- ]factor|two[- ]step|two[- ]factor|one[- ]time|verification code|approve sign[- ]in|verify your identity",
        text,
    ):
        return "mfa_required", []
    if session.realm == "paycom-client" and challenges:
        return ("security_pins_required", challenges) if len(challenges) == 2 and all(1 <= item <= 5 for item in challenges) else ("unknown_challenge", [])
    required = AMAZON_LOGIN[:2] if session.realm == "amazon-operations" else PAYCOM_LOGIN[:3]
    if all(selector in present for selector in required):
        return "logged_out", []
    if _at_landing(session, url):
        return "authenticated", []
    return "unknown", []


def _wait(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


def _submit_login(page: Any, selectors: tuple[str, ...], values: tuple[str, ...]) -> bool:
    try:
        for selector, value in zip(selectors[:-1], values, strict=True):
            page.locator(selector).fill(value, timeout=5000)
        page.locator(selectors[-1]).click(timeout=5000)
    except Exception:
        return False
    _wait(page)
    return True


def authenticate(
    manager: "AuthenticationManager",
    session: ManagedBrowserSession,
    account_alias: str = "default",
    *,
    resume: bool = False,
) -> AuthenticationResult:
    """Authenticate only a fixed installed realm; never accepts a URL or selector."""

    credential = manager.credentials_for_session(session, account_alias)
    page = session.page
    if not resume:
        try:
            page.goto(session.landing_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return _result(session, account_alias, "auth_unavailable")

    submitted_login = False
    submitted_challenge = False
    canonical_retry = False
    for _ in range(5):
        snapshot = _snapshot(page)
        if snapshot is None:
            return _result(session, account_alias, "auth_unavailable")
        state, challenge_indices = _classify(session, snapshot)
        if state == "authenticated":
            return _result(
                session,
                account_alias,
                "login_success" if submitted_login or submitted_challenge or resume else "already_authenticated",
                authenticated=True,
            )
        if state in {"account_locked", "invalid_credentials"}:
            return _result(session, account_alias, state)
        if state == "captcha_required":
            return _result(session, account_alias, state, manual_action="complete_captcha")
        if state == "mfa_required":
            return _result(session, account_alias, state, manual_action="complete_mfa")
        if state in {"unapproved_page", "unknown_challenge"}:
            return _result(session, account_alias, "manual_verification_required", manual_action="inspect_page")
        if state == "logged_out" and not submitted_login:
            if session.realm == "amazon-operations":
                values = (credential.values["username"], credential.values["password"])
                submitted_login = _submit_login(page, AMAZON_LOGIN, values)
            else:
                values = (
                    credential.values["client_code"],
                    credential.values["username"],
                    credential.values["password"],
                )
                submitted_login = _submit_login(page, PAYCOM_LOGIN, values)
            if not submitted_login:
                return _result(session, account_alias, "auth_unavailable")
            continue
        if state == "security_pins_required" and not submitted_challenge:
            values = {str(index): credential.values[f"security_pin_{index}"] for index in challenge_indices}
            try:
                outcome = page.evaluate(
                    _PAYCOM_CHALLENGE_SCRIPT,
                    {"indices": challenge_indices, "values": values},
                )
            except Exception:
                outcome = None
            if not isinstance(outcome, dict) or outcome.get("status") != "security_factors_submitted":
                return _result(session, account_alias, "manual_verification_required", manual_action="inspect_page")
            submitted_challenge = True
            _wait(page)
            continue
        if state == "unknown" and not canonical_retry:
            try:
                page.goto(session.landing_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                return _result(session, account_alias, "auth_unavailable")
            canonical_retry = True
            continue
        return _result(session, account_alias, "manual_verification_required", manual_action="inspect_page")
    return _result(session, account_alias, "manual_verification_required", manual_action="inspect_page")


__all__ = ["AuthenticationResult", "authenticate"]
