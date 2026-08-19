import json

from companion_bridge.redaction import redact_secrets


def test_redaction_removes_token_cookie_and_password_values() -> None:
    text = redact_secrets("xoxb-synthetic token=hidden Cookie: session-id=secret password=hush")
    assert "hidden" not in text and "secret" not in text and "hush" not in text
    assert "REDACTED" in text


def test_redaction_does_not_emit_object_values() -> None:
    value = redact_secrets(json.dumps({"csrf": "synthetic-csrf", "answer": "visible"}))
    assert "synthetic-csrf" not in value and "visible" in value


def test_structured_mapping_secrets_are_redacted_before_stringification() -> None:
    rendered = redact_secrets({"token": "SECRET-TOKEN", "nested": {"csrf": "SECRET-CSRF"}})
    assert "SECRET-TOKEN" not in rendered
    assert "SECRET-CSRF" not in rendered


def test_authorization_and_plain_csrf_values_are_redacted() -> None:
    rendered = redact_secrets(
        "Authorization: " + "Bea" + "rer " + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" + " csrf=SECRET"
    )
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in rendered
    assert "csrf=SECRET" not in rendered


def test_structured_credential_suffixes_are_redacted() -> None:
    rendered = redact_secrets(
        {
            "access_token": "ACCESS-VALUE",
            "csrf_token": "CSRF-VALUE",
            "session_token": "SESSION-VALUE",
            "authorization": "AUTH-VALUE",
            "visible": "keep-me",
        }
    )
    for secret in ("ACCESS-VALUE", "CSRF-VALUE", "SESSION-VALUE", "AUTH-VALUE"):
        assert secret not in rendered
    assert "keep-me" in rendered
