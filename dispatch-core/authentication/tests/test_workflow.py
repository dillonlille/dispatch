from __future__ import annotations

from pathlib import Path

from dispatch_core.authentication import AuthenticationManager
from dispatch_core.browser_manager import ManagedBrowserSession
from dispatch_core.paths import DispatchPaths


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def fill(self, value: str, *, timeout: int) -> None:
        assert timeout == 5000
        self.page.filled[self.selector] = value

    def click(self, *, timeout: int) -> None:
        assert timeout == 5000
        if self.selector == "#signInSubmit":
            self.page.state = self.page.after_login
        elif self.selector == "#btnSubmit":
            self.page.state = "paycom_challenge"
        else:
            raise AssertionError(f"unexpected click: {self.selector}")


class FakePage:
    def __init__(self, realm: str, *, after_login: str = "landing") -> None:
        self.realm = realm
        self.after_login = after_login
        self.state = "blank"
        self.authenticated = False
        self.filled: dict[str, str] = {}
        self.challenge_payload: dict | None = None

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 30000
        if self.authenticated:
            self.state = "landing"
        elif self.realm == "amazon-operations":
            self.state = "amazon_login"
        else:
            self.state = "paycom_login"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state == "domcontentloaded"
        assert timeout == 5000

    def evaluate(self, script: str, payload: dict | None = None) -> dict:
        if payload is not None:
            assert self.state == "paycom_challenge"
            self.challenge_payload = payload
            self.authenticated = True
            self.state = "paycom_internal"
            return {"status": "security_factors_submitted"}
        snapshots = {
            "amazon_login": {
                "url": "https://www.amazon.com/ap/signin?openid.return_to=synthetic",
                "text": "Sign in",
                "present": ["#ap_email", "#ap_password", "#signInSubmit"],
                "challengeIndices": [],
            },
            "amazon_mfa": {
                "url": "https://www.amazon.com/ap/signin",
                "text": "Enter verification code",
                "present": ["#auth-mfa-otpcode"],
                "challengeIndices": [],
            },
            "amazon_captcha": {
                "url": "https://www.amazon.com/ap/signin",
                "text": "Enter the characters shown",
                "present": ["#auth-captcha-image", 'input[name="guess"]'],
                "challengeIndices": [],
            },
            "paycom_login": {
                "url": "https://www.paycomonline.net/v4/cl/cl-login.php",
                "text": "Client log in",
                "present": ["#clientcode", "#username", "#password", "#btnSubmit"],
                "challengeIndices": [],
            },
            "paycom_challenge": {
                "url": "https://www.paycomonline.net/v4/cl/security.php",
                "text": "Enter two security PINs",
                "present": [],
                "challengeIndices": [2, 5],
            },
            "paycom_bad_challenge": {
                "url": "https://www.paycomonline.net/v4/cl/security.php",
                "text": "Enter security PIN",
                "present": [],
                "challengeIndices": [2],
            },
            "paycom_internal": {
                "url": "https://www.paycomonline.net/v4/cl/app.php",
                "text": "Client services",
                "present": [],
                "challengeIndices": [],
            },
            "unapproved": {
                "url": "https://example.invalid/signin",
                "text": "Sign in",
                "present": ["#ap_email", "#ap_password", "#signInSubmit"],
                "challengeIndices": [],
            },
        }
        if self.state == "landing":
            url = (
                "https://logistics.amazon.com/dspconsolev2"
                if self.realm == "amazon-operations"
                else "https://www.paycomonline.net/v4/cl/web.php/client-landing/arc"
            )
            return {"url": url, "text": "Ready", "present": [], "challengeIndices": []}
        return snapshots[self.state]


def authentication(tmp_path: Path) -> AuthenticationManager:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    paths = DispatchPaths.from_environment(
        {"HOME": str(home)},
        code_root=Path(__file__).resolve().parents[3],
    )
    return AuthenticationManager(paths)


def session(page: FakePage) -> ManagedBrowserSession:
    landing = (
        "https://logistics.amazon.com/dspconsolev2"
        if page.realm == "amazon-operations"
        else "https://www.paycomonline.net/v4/cl/web.php/client-landing/arc"
    )
    return ManagedBrowserSession(
        lease_id="synthetic-lease",
        realm=page.realm,
        landing_url=landing,
        page=page,
        context=object(),
    )


def enroll_amazon(manager: AuthenticationManager) -> None:
    manager.enroll(
        "amazon-operations",
        "default",
        {"username": "synthetic-user", "password": "synthetic-password-not-a-secret"},
    )


def enroll_paycom(manager: AuthenticationManager) -> dict[str, str]:
    values = {
        "client_code": "synthetic-client",
        "username": "synthetic-user",
        "password": "synthetic-password-not-a-secret",
        **{f"security_pin_{index}": f"synthetic-pin-{index}" for index in range(1, 6)},
    }
    manager.enroll("paycom-client", "default", values)
    return values


def test_amazon_submits_fixed_fields_then_pauses_for_mfa_and_resumes(tmp_path: Path) -> None:
    manager = authentication(tmp_path)
    enroll_amazon(manager)
    page = FakePage("amazon-operations", after_login="amazon_mfa")
    managed_session = session(page)

    waiting = manager.authenticate(managed_session)

    assert waiting.safe_data() == {
        "realm": "amazon-operations",
        "account_alias": "default",
        "status": "mfa_required",
        "authenticated": False,
        "manual_action": "complete_mfa",
    }
    assert page.filled == {
        "#ap_email": "synthetic-user",
        "#ap_password": "synthetic-password-not-a-secret",
    }

    page.authenticated = True
    page.state = "landing"
    completed = manager.resume(managed_session)
    assert completed.status == "login_success"
    assert completed.authenticated is True


def test_amazon_captcha_and_unapproved_pages_fail_closed(tmp_path: Path) -> None:
    manager = authentication(tmp_path)
    enroll_amazon(manager)
    page = FakePage("amazon-operations", after_login="amazon_captcha")
    managed_session = session(page)

    assert manager.authenticate(managed_session).status == "captcha_required"
    page.state = "unapproved"
    result = manager.resume(managed_session)
    assert result.status == "manual_verification_required"
    assert result.manual_action == "inspect_page"


def test_paycom_submits_only_the_two_requested_security_pins(tmp_path: Path) -> None:
    manager = authentication(tmp_path)
    values = enroll_paycom(manager)
    page = FakePage("paycom-client")

    result = manager.authenticate(session(page))

    assert result.status == "login_success"
    assert result.authenticated is True
    assert page.filled == {
        "#clientcode": values["client_code"],
        "#username": values["username"],
        "#password": values["password"],
    }
    assert page.challenge_payload == {
        "indices": [2, 5],
        "values": {"2": values["security_pin_2"], "5": values["security_pin_5"]},
    }


def test_paycom_rejects_an_ambiguous_security_challenge(tmp_path: Path) -> None:
    manager = authentication(tmp_path)
    enroll_paycom(manager)
    page = FakePage("paycom-client")
    page.authenticated = False
    managed_session = session(page)
    page.state = "paycom_bad_challenge"

    result = manager.resume(managed_session)

    assert result.status == "manual_verification_required"
    assert page.challenge_payload is None
