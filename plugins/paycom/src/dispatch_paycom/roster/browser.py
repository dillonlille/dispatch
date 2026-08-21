"""Roster navigation and response capture over Core's managed Playwright page."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from .models import parse_roster_source
from .period import Period

ROSTER_URL = "https://www.paycomonline.net/v4/cl/web.php/timecardsearch/index?from=main_menu"
ROSTER_API_URL = "https://time-and-attendance.paycomonline.net/api/cl/timecard-search/employees"
PAYCOM_HOST = "www.paycomonline.net"


class RosterBrowserError(RuntimeError):
    pass


def _approved_paycom_url(value: str, *, path: str | None = None) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == PAYCOM_HOST and parsed.port is None and not parsed.username and not parsed.password and (path is None or parsed.path == path)


def is_roster_url(value: str) -> bool:
    return _approved_paycom_url(value, path="/v4/cl/web.php/timecardsearch/index") and urlsplit(value).query == "from=main_menu"


def is_roster_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == "time-and-attendance.paycomonline.net" and parsed.port is None and not parsed.username and not parsed.password and parsed.path == "/api/cl/timecard-search/employees" and not parsed.query and not parsed.fragment


def _request_codes(response: Any, period: Period) -> tuple[str, ...]:
    request = getattr(response, "request", None)
    if request is None or getattr(request, "method", None) != "POST" or not is_roster_api_url(str(getattr(request, "url", ""))):
        raise RosterBrowserError("roster_request_invalid")
    value = getattr(request, "post_data_json", None)
    try:
        body = value() if callable(value) else value
        if body is None:
            raw = getattr(request, "post_data", None)
            body = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise RosterBrowserError("roster_request_invalid") from exc
    if not isinstance(body, dict) or body.get("startDate") != period.start or body.get("endDate") != period.end:
        raise RosterBrowserError("roster_request_invalid")
    codes = body.get("eeCodes")
    if not isinstance(codes, list) or not 1 <= len(codes) <= 5_000 or any(not isinstance(code, str) or len(code) != 4 or not code.isalnum() for code in codes):
        raise RosterBrowserError("roster_request_invalid")
    normalized = tuple(sorted(code.upper() for code in codes))
    if len(set(normalized)) != len(normalized):
        raise RosterBrowserError("roster_request_invalid")
    return normalized


def _response_matches(response: Any, period: Period) -> bool:
    url = getattr(response, "url", "")
    status = getattr(response, "status", 200)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", ""))
    if not is_roster_api_url(url) or status != 200 or "json" not in content_type.lower():
        return False
    try:
        _request_codes(response, period)
    except RosterBrowserError:
        return False
    return True


def _response_codes(source: bytes) -> tuple[str, ...]:
    try:
        value = json.loads(source.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RosterBrowserError("roster_response_invalid") from exc
    codes = value.get("eeCodes") if isinstance(value, dict) else None
    if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes):
        raise RosterBrowserError("roster_response_invalid")
    normalized = tuple(sorted(code.upper() for code in codes))
    if len(set(normalized)) != len(normalized):
        raise RosterBrowserError("roster_response_invalid")
    return normalized


def _body(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, dict) and "body" in value:
        return _body(value["body"])
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":")).encode("utf-8")
    raise RosterBrowserError("roster_export_unavailable")


def capture_roster_export(page: Any, *, period: Period, target: str | None = None) -> bytes:
    if not isinstance(period, Period) or period.key != f"{period.start}_{period.end}":
        raise RosterBrowserError("roster_period_invalid")
    expect_response = getattr(page, "expect_response", None)
    if not callable(expect_response):
        raise RosterBrowserError("roster_export_unavailable")
    try:
        with expect_response(lambda response: _response_matches(response, period), timeout=60_000) as response_info:
            page.goto(ROSTER_URL, wait_until="domcontentloaded", timeout=30_000)
        response = response_info.value
        if not _response_matches(response, period):
            raise RosterBrowserError("roster_response_invalid")
        requested_codes = _request_codes(response, period)
        body_method = getattr(response, "body", None)
        if not callable(body_method):
            raise RosterBrowserError("roster_body_unavailable")
        source = _body(body_method())
        if _response_codes(source) != requested_codes:
            raise RosterBrowserError("roster_response_invalid")
    except RosterBrowserError:
        raise
    except Exception as exc:
        # A session-expiry redirect to the login page means the expected API
        # response never fires and expect_response times out; surface that as
        # an actionable re-authentication signal instead of a generic failure.
        current_url = getattr(page, "url", None)
        if isinstance(current_url, str) and current_url and not is_roster_url(current_url):
            raise RosterBrowserError("roster_authentication_required") from exc
        raise RosterBrowserError("roster_response_unavailable") from exc
    current_url = getattr(page, "url", None)
    if not isinstance(current_url, str) or not current_url:
        raise RosterBrowserError("roster_navigation_policy_violation")
    if not is_roster_url(current_url):
        raise RosterBrowserError("roster_navigation_policy_violation")
    if len(source) < 32 or len(source) > 2 * 1024 * 1024:
        raise RosterBrowserError("roster_export_invalid")
    try:
        parse_roster_source(source)
    except Exception as exc:
        raise RosterBrowserError("roster_export_invalid") from exc
    return source


__all__ = ["PAYCOM_HOST", "ROSTER_API_URL", "ROSTER_URL", "RosterBrowserError", "capture_roster_export", "is_roster_api_url", "is_roster_url"]
