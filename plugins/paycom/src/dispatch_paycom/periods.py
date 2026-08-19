"""The fixed Sunday-through-Saturday fourteen-day Paycom period schedule."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re

_PERIOD_KEY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ANCHOR = date(2026, 7, 26)


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    start: str
    end: str
    key: str
    dates: tuple[str, ...]


def _parse(value: str) -> date:
    if type(value) is not str or len(value) != 10 or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise PeriodError("invalid_period")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PeriodError("invalid_period") from exc
    if parsed.isoformat() != value:
        raise PeriodError("invalid_period")
    return parsed


def _from_bounds(start: date, end: date) -> Period:
    if start.weekday() != 6 or end.weekday() != 5 or end - start != timedelta(days=13):
        raise PeriodError("invalid_period")
    dates = tuple((start + timedelta(days=index)).isoformat() for index in range(14))
    return Period(start.isoformat(), end.isoformat(), f"{start.isoformat()}_{end.isoformat()}", dates)


def period_from_end(end: str) -> Period:
    finish = _parse(end)
    return _from_bounds(finish - timedelta(days=13), finish)


def parse_period_key(key: str) -> Period:
    if type(key) is not str or _PERIOD_KEY_RE.fullmatch(key) is None:
        raise PeriodError("invalid_period")
    start, end = key.split("_", 1)
    return _from_bounds(_parse(start), _parse(end))


def period_containing(value: str) -> Period:
    target = _parse(value)
    offset = (target - _ANCHOR).days // 14
    start = _ANCHOR + timedelta(days=offset * 14)
    return _from_bounds(start, start + timedelta(days=13))


def shift_period(period: Period, days: int) -> Period:
    parsed = parse_period_key(period.key)
    shifted = _parse(parsed.start) + timedelta(days=days)
    return _from_bounds(shifted, shifted + timedelta(days=13))


def previous_period(period: Period) -> Period:
    return shift_period(period, -14)


def next_period(period: Period) -> Period:
    return shift_period(period, 14)
