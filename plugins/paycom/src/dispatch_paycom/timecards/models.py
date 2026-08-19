"""Bounded Paycom timecard DOM projection validation."""
from __future__ import annotations

from typing import Any
import math
import re

from .period import Period, is_captured_timecard_url, parse_period_key, validate_code

HEADERS = ("date", "paycode", "i1", "allocation1", "o1", "i2", "allocation2", "o2", "hours", "total_hours", "amount", "exception-points", "waiver", "comment", "missing-punch", "delete")
HEADERS_NO_WAIVER = tuple(item for item in HEADERS if item != "waiver")
_SLOTS = {"i1", "o1", "i2", "o2"}
_LABELS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_TIME = re.compile(r"^(0[1-9]|1[0-2]):[0-5][0-9] [AP]M$")


class TimecardRecordError(ValueError):
    pass


def _invalid(code: str = "timecard_invalid") -> None:
    raise TimecardRecordError(code)


def _bounded(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and len(value) <= maximum and not any(ord(char) < 32 and char not in "\t\n\r" for char in value) and "\x7f" not in value


def _number(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0)


def _generic_rows(value: Any, maximum: int = 500) -> bool:
    return isinstance(value, list) and len(value) <= maximum and all(isinstance(row, list) and len(row) <= 20 and all(_bounded(cell, 1000) for cell in row) for row in value)


def _punch(value: Any, ordinal: int, seen: set[str]) -> None:
    if not isinstance(value, dict) or value.get("ordinal") != ordinal or not isinstance(value.get("rowIndex"), int) or not 0 <= value["rowIndex"] <= 16 or value.get("slot") not in _SLOTS or not _bounded(value.get("displayTime"), 20) or not _bounded(value.get("kind"), 40) or not _bounded(value.get("actualTime"), 20) or not _bounded(value.get("roundedTime"), 20) or not _bounded(value.get("clockName"), 200) or not _bounded(value.get("clockCode"), 50) or not _bounded(value.get("comment"), 2000) or not isinstance(value.get("provenanceAvailable"), bool) or value.get("changeRequestStatus") not in {None, "approved", "pending", "rejected"} or not isinstance(value.get("approved"), bool) or value["approved"] != (value["changeRequestStatus"] == "approved"):
        _invalid()
    if value["provenanceAvailable"]:
        if value["kind"] not in {"IN DAY", "OUT LUNCH", "IN LUNCH", "OUT DAY"} or not _TIME.fullmatch(value["actualTime"]) or not _TIME.fullmatch(value["roundedTime"]):
            _invalid()
    elif any(value[field] != "" for field in ("kind", "actualTime", "roundedTime", "clockName", "clockCode", "comment")):
        _invalid()
    key = f"{value['rowIndex']}:{value['slot']}"
    if key in seen:
        _invalid("timecard_duplicate_punch")
    seen.add(key)


def validate_timecard_record(value: Any, *, employee_code: str, period: Period, source_url: str) -> dict[str, Any]:
    validate_code(employee_code)
    expected = parse_period_key(period.key)
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("sourceFormat") != "paycom-timecard-dom.v1" or value.get("employeeCode") != employee_code or value.get("periodStart") != expected.start or value.get("periodEnd") != expected.end or value.get("periodKey") != expected.key or value.get("sourceUrl") != source_url or value.get("pageTitle") != "Timecard Editor" or value.get("headers") not in (list(HEADERS), list(HEADERS_NO_WAIVER)) or not isinstance(value.get("days"), list) or len(value["days"]) != 14 or not isinstance(value.get("additionalRows"), list) or len(value["additionalRows"]) > 200:
        _invalid()
    for index, day in enumerate(value["days"]):
        if not isinstance(day, dict) or day.get("date") != expected.dates[index] or day.get("label") != _LABELS[index % 7] or not _bounded(day.get("payCode"), 100) or not _bounded(day.get("allocation1"), 300) or not _bounded(day.get("allocation2"), 300) or not _number(day.get("hours")) or not _number(day.get("totalHours")) or not _number(day.get("dollars")) or not _bounded(day.get("exceptionText"), 2000) or (day.get("waiverChecked") is not None and not isinstance(day.get("waiverChecked"), bool)) or not isinstance(day.get("comments"), list) or len(day["comments"]) > 20 or not all(_bounded(item, 2000) for item in day["comments"]) or not isinstance(day.get("unresolvedSlots"), list) or len(day["unresolvedSlots"]) > 32 or not all(isinstance(item, str) and (item in _SLOTS or re.fullmatch(r"(?:[1-9]|1[0-6]):(?:i1|o1|i2|o2)", item)) for item in day["unresolvedSlots"]) or len(set(day["unresolvedSlots"])) != len(day["unresolvedSlots"]) or not isinstance(day.get("missingPunch"), bool) or day["missingPunch"] != bool(day["unresolvedSlots"]) or not isinstance(day.get("punches"), list) or len(day["punches"]) > 32:
            _invalid()
        seen: set[str] = set()
        for ordinal, punch in enumerate(day["punches"], 1):
            _punch(punch, ordinal, seen)
    row_ids: set[str] = set()
    for row in value["additionalRows"]:
        if not isinstance(row, dict) or row.get("date") not in expected.dates or not isinstance(row.get("rowIndex"), int) or not 1 <= row["rowIndex"] <= 16 or f"{row['date']}:{row['rowIndex']}" in row_ids or not _bounded(row.get("rowClass"), 300) or not _bounded(row.get("payCode"), 2000) or not _bounded(row.get("allocation1"), 300) or not _bounded(row.get("allocation2"), 300) or not _number(row.get("hours")) or not _number(row.get("totalHours")) or not _number(row.get("dollars")) or not _bounded(row.get("exceptionText"), 2000) or (row.get("waiverChecked") is not None and not isinstance(row.get("waiverChecked"), bool)) or not isinstance(row.get("comments"), list) or not all(_bounded(item, 2000) for item in row["comments"]) or not isinstance(row.get("unresolvedSlots"), list) or not all(item in _SLOTS for item in row["unresolvedSlots"]) or not isinstance(row.get("punchOrdinals"), list) or not all(isinstance(item, int) and 1 <= item <= 32 for item in row["punchOrdinals"]):
            _invalid()
        row_ids.add(f"{row['date']}:{row['rowIndex']}")
        day = value["days"][expected.dates.index(row["date"])]
        for ordinal in row["punchOrdinals"]:
            if ordinal > len(day["punches"]) or day["punches"][ordinal - 1]["rowIndex"] != row["rowIndex"]:
                _invalid()
        if any(f"{row['rowIndex']}:{slot}" not in day["unresolvedSlots"] for slot in row["unresolvedSlots"]):
            _invalid()
    weekly = value.get("weeklyTotals")
    if not isinstance(weekly, list) or len(weekly) != 2 or not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0 for item in weekly) or not isinstance(value.get("periodTotalHours"), (int, float)) or isinstance(value["periodTotalHours"], bool) or not math.isfinite(value["periodTotalHours"]) or value["periodTotalHours"] < 0 or abs(value["periodTotalHours"] - sum(weekly)) > 0.011 or not _generic_rows(value.get("approvals")) or not _generic_rows(value.get("attestations")) or not _generic_rows(value.get("mealWaivers")):
        _invalid()
    if not is_captured_timecard_url(source_url, employee_code=employee_code, period=expected):
        _invalid("timecard_url_invalid")
    return value


__all__ = ["HEADERS", "HEADERS_NO_WAIVER", "TimecardRecordError", "validate_timecard_record"]
