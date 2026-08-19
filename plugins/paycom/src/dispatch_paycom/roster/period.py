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
    offset = (target - _ANCHOR).days // 14
    first = _ANCHOR + timedelta(days=offset * 14)
    return from_bounds(first.isoformat(), (first + timedelta(days=13)).isoformat())


def parse_period_key(key: str) -> Period:
    if not isinstance(key, str) or key.count("_") != 1:
        raise PeriodError("invalid_period")
    start, end = key.split("_")
    return from_bounds(start, end)


__all__ = ["Period", "PeriodError", "from_bounds", "parse_period_key", "period_containing", "period_from_end"]
