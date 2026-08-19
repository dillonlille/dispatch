"""Paycom's fixed fourteen-day Sunday-through-Saturday periods."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ANCHOR = date(2026, 7, 26)


class PeriodError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Period:
    start: str
    end: str
    dates: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.start}_{self.end}"


def _parse(value: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise PeriodError("invalid_period")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PeriodError("invalid_period") from exc
    if parsed.isoformat() != value:
        raise PeriodError("invalid_period")
    return parsed


def from_bounds(start: str, end: str) -> Period:
    first, last = _parse(start), _parse(end)
    if first.weekday() != 6 or last.weekday() != 5 or last - first != timedelta(days=13):
        raise PeriodError("invalid_period")
    return Period(start, end, tuple((first + timedelta(days=index)).isoformat() for index in range(14)))


def period_from_end(end: str) -> Period:
    last = _parse(end)
    return from_bounds((last - timedelta(days=13)).isoformat(), end)


def period_containing(value: str) -> Period:
    target = _parse(value)
    first = _ANCHOR + timedelta(days=((target - _ANCHOR).days // 14) * 14)
    return from_bounds(first.isoformat(), (first + timedelta(days=13)).isoformat())


def parse_period_key(key: str) -> Period:
    if not isinstance(key, str) or key.count("_") != 1:
        raise PeriodError("invalid_period")
    return from_bounds(*key.split("_"))


def validate_code(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"^[A-Za-z0-9]{4}$", code):
        raise ValueError("invalid_employee_code")
    return code


def canonical_timecard_url(employee_code: str, period: Period) -> str:
    validate_code(employee_code)
    parsed = parse_period_key(period.key)
    return f"https://www.paycomonline.net/v4/cl/web.php/timecard/index?firstrefno={employee_code}&perioddates={parsed.key}&formtype=SUMMARY"


def build_timecard_url(employee_code: str, period: Period, variant: int = 1) -> str:
    if variant not in {1, 2}:
        raise ValueError("navigation_policy_violation")
    return f"{canonical_timecard_url(employee_code, period)}&dispatch_timecards={variant}"


def is_captured_timecard_url(value: str, *, employee_code: str, period: Period) -> bool:
    from urllib.parse import parse_qs, urlsplit

    try:
        parsed = urlsplit(value)
        expected = urlsplit(build_timecard_url(employee_code, period, 1))
    except (ValueError, TypeError):
        return False
    if parsed.scheme != expected.scheme or parsed.hostname != expected.hostname or parsed.port is not None or parsed.username or parsed.password or parsed.path != expected.path or parsed.fragment:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {"firstrefno", "perioddates", "formtype", "dispatch_timecards"} or any(len(items) != 1 for items in query.values()):
        return False
    return query["firstrefno"][0] == employee_code and query["perioddates"][0] == period.key and query["formtype"][0] == "SUMMARY" and query["dispatch_timecards"][0] in {"1", "2"}


__all__ = ["Period", "PeriodError", "build_timecard_url", "canonical_timecard_url", "from_bounds", "is_captured_timecard_url", "parse_period_key", "period_containing", "period_from_end", "validate_code"]
