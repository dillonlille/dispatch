"""Managed-page timecard navigation and bounded DOM projection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extraction import EXTRACTION_SCRIPT
from .models import validate_timecard_record
from .period import Period, build_timecard_url, is_captured_timecard_url, validate_code

class TimecardBrowserError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TimecardCapture:
    employee_code: str
    period_key: str
    source_url: str
    source_html: bytes
    record: dict[str, Any]


def _content(page: Any) -> bytes:
    content = getattr(page, "content", None)
    if not callable(content):
        raise TimecardBrowserError("timecard_source_unavailable")
    try:
        value = content()
    except Exception as exc:
        raise TimecardBrowserError("timecard_source_unavailable") from exc
    if isinstance(value, str):
        result = value.encode()
    elif isinstance(value, bytes):
        result = value
    else:
        raise TimecardBrowserError("timecard_source_unavailable")
    if not 1 <= len(result) <= 2 * 1024 * 1024:
        raise TimecardBrowserError("timecard_html_invalid")
    return result


def _projection(page: Any, config: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise TimecardBrowserError("timecard_projection_unavailable")
    try:
        try:
            value = evaluate(EXTRACTION_SCRIPT, config)
        except TypeError:
            value = evaluate(EXTRACTION_SCRIPT)
    except Exception as exc:
        raise TimecardBrowserError("timecard_projection_unavailable") from exc
    if not isinstance(value, dict):
        raise TimecardBrowserError("timecard_projection_unavailable")
    record = value.get("record", value)
    raw_html = value.get("sourceHtml", value.get("source_html"))
    if not isinstance(record, dict):
        raise TimecardBrowserError("timecard_projection_unavailable")
    if isinstance(raw_html, str):
        raw_html = raw_html.encode()
    elif not isinstance(raw_html, (bytes, bytearray)):
        raw_html = None
    return record, bytes(raw_html) if raw_html is not None else None


def capture_timecard(page: Any, *, employee_code: str, period: Period, variant: int = 1) -> TimecardCapture:
    validate_code(employee_code)
    target = build_timecard_url(employee_code, period, variant)
    try:
        response = page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        wait_for_load = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load):
            wait_for_load("load", timeout=60_000)
    except Exception as exc:
        raise TimecardBrowserError("timecard_navigation_failed") from exc
    current_url = getattr(page, "url", None)
    if isinstance(current_url, str) and current_url and current_url != target:
        raise TimecardBrowserError("timecard_navigation_policy_violation")
    response_html = None
    if response is not None:
        response_url = getattr(response, "url", target)
        response_status = getattr(response, "status", 0)
        response_headers = getattr(response, "headers", {}) or {}
        body = getattr(response, "body", None)
        if response_url != target or response_status != 200 or "text/html" not in str(response_headers.get("content-type", "")).lower() or not callable(body):
            raise TimecardBrowserError("timecard_response_invalid")
        try:
            response_html = body()
        except Exception as exc:
            raise TimecardBrowserError("timecard_source_unavailable") from exc
        if isinstance(response_html, str):
            response_html = response_html.encode()
        if not isinstance(response_html, bytes):
            raise TimecardBrowserError("timecard_source_unavailable")
    config = {
        "employeeCode": employee_code,
        "period": {"start": period.start, "end": period.end, "key": period.key, "dates": list(period.dates)},
        "sourceUrl": target,
    }
    record, evaluated_html = _projection(page, config)
    source_html = response_html or evaluated_html or _content(page)
    if len(source_html) < 1_024 or len(source_html) > 2 * 1024 * 1024 or b'id="tbltimesheet"' not in source_html or b'id="periodtotals"' not in source_html:
        raise TimecardBrowserError("timecard_html_invalid")
    try:
        validated = validate_timecard_record(record, employee_code=employee_code, period=period, source_url=target)
    except Exception as exc:
        raise TimecardBrowserError("timecard_projection_invalid") from exc
    return TimecardCapture(employee_code, period.key, target, source_html, validated)


__all__ = ["EXTRACTION_SCRIPT", "TimecardBrowserError", "TimecardCapture", "capture_timecard", "is_captured_timecard_url"]
