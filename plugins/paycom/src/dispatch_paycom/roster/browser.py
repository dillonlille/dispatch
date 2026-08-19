"""Roster navigation and response capture over Core's managed Playwright page."""
from __future__ import annotations

from contextlib import contextmanager
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


def _response_matches(response: Any) -> bool:
    url = getattr(response, "url", "")
    status = getattr(response, "status", 200)
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", ""))
    return is_roster_api_url(url) and status == 200 and "json" in content_type.lower()


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
    except (TypeError, ValueError) as exc:
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


def _response_codes(source: bytes) -> tuple[str, ...]:
    try:
        value = json.loads(source.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
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


def _fallback_capture(page: Any) -> bytes:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise RosterBrowserError("roster_export_unavailable")
    try:
        value = evaluate("window.__dispatch_paycom_roster_export || null")
    except Exception as exc:
        raise RosterBrowserError("roster_export_unavailable") from exc
    if value is None:
        raise RosterBrowserError("roster_export_unavailable")
    return _body(value)


def capture_roster_export(page: Any, *, period: Period, target: str | None = None) -> bytes:
    if not isinstance(period, Period) or period.key != f"{period.start}_{period.end}":
        raise RosterBrowserError("roster_period_invalid")
    expect_response = getattr(page, "expect_response", None)
    if callable(expect_response):
        try:
            with expect_response(_response_matches, timeout=60_000) as response_info:
                page.goto(ROSTER_URL, wait_until="domcontentloaded", timeout=30_000)
            response = response_info.value
            if not _response_matches(response):
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
            raise RosterBrowserError("roster_response_unavailable") from exc
    else:
        try:
            result = page.goto(ROSTER_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            raise RosterBrowserError("roster_navigation_failed") from exc
        source = None
        body_method = getattr(result, "body", None)
        if callable(body_method):
            try:
                source = _body(body_method())
            except Exception:
                source = None
        if source is None:
            source = _fallback_capture(page)
    current_url = getattr(page, "url", None)
    if isinstance(current_url, str) and current_url and not is_roster_url(current_url):
        raise RosterBrowserError("roster_navigation_policy_violation")
    if len(source) < 32 or len(source) > 2 * 1024 * 1024:
        raise RosterBrowserError("roster_export_invalid")
    try:
        parse_roster_source(source)
    except Exception as exc:
        raise RosterBrowserError("roster_export_invalid") from exc
    return source


__all__ = ["PAYCOM_HOST", "ROSTER_API_URL", "ROSTER_URL", "RosterBrowserError", "capture_roster_export", "is_roster_api_url", "is_roster_url"]
