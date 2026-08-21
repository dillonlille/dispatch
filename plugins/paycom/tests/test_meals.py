from __future__ import annotations

from datetime import datetime, timezone

import _bootstrap  # noqa: F401  (sys.path bootstrap for bare pytest runs)

from dispatch_paycom.meals.comparison import (
    clock_duration,
    collection_freshness,
    flex_meal_evidence,
    minute_delta,
    normalize_name,
    parse_clock,
    paycom_lunch_pairs,
)


def test_parse_clock_noon_and_midnight() -> None:
    assert parse_clock("12:00 PM") == 720
    assert parse_clock("12:30 PM") == 750
    assert parse_clock("12:00 AM") == 0
    assert parse_clock("1:15 PM") == 795
    assert parse_clock("11:59 AM") == 719
    assert parse_clock("12:60 PM") is None
    assert parse_clock("13:00 PM") is None
    assert parse_clock("noon") is None
    assert parse_clock(720) is None


def test_clock_duration_wraps_midnight_and_bounds() -> None:
    assert clock_duration(720, 750) == 30
    assert clock_duration(1380, 60) == 120  # 11 PM -> 1 AM
    assert clock_duration(None, 60) is None
    assert clock_duration(60, None) is None
    assert clock_duration(600, 1500) is None  # longer than the 240-minute bound


def test_minute_delta_wraps_at_half_day() -> None:
    assert minute_delta(750, 720) == 30
    assert minute_delta(690, 720) == -30
    assert minute_delta(721, 720) == 1
    assert minute_delta(0, 1440 - 5) == 5


def test_normalize_name_folds_case_punctuation_and_marks() -> None:
    assert normalize_name("Smith, Alex") == "smith alex"
    assert normalize_name("José García") == "jose garcia"


def test_collection_freshness_accepts_offset_space_and_z_timestamps() -> None:
    now = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    # Explicit UTC-offset timestamps must not get a spurious trailing "Z".
    result = collection_freshness("2026-07-26T18:00:00-07:00", now)
    assert result["stale"] is False and float(result["ageHours"]) >= 0  # type: ignore[call-overload]
    # Space-separated Zulu timestamps are normalized.
    result = collection_freshness("2026-07-26 22:15:00Z", now)
    assert result["stale"] is False
    # Already-Zulu timestamps parse unchanged.
    result = collection_freshness("2026-07-26T22:15:00Z", now)
    assert result["stale"] is False
    # Garbage timestamps fail closed.
    try:
        collection_freshness("not-a-timestamp", now)
    except ValueError as exc:
        assert str(exc) == "schema_invalid"
    else:
        raise AssertionError("expected schema_invalid")


def test_flex_meal_evidence_classifies_rows() -> None:
    complete = {"mealIndex": 1, "mealOut": "12:00 PM", "mealIn": "12:30 PM", "flexMinutes": 30, "parseWarning": None}
    assert flex_meal_evidence(complete)["status"] == "complete"
    warned = dict(complete, parseWarning="rounded to nearest minute")
    assert flex_meal_evidence(warned)["status"] == "warning"
    mismatched = dict(complete, flexMinutes=45)
    assert flex_meal_evidence(mismatched)["status"] == "invalid"
    no_meal = {"mealIndex": None, "mealOut": None, "mealIn": None, "flexMinutes": None, "parseWarning": "no meal break found"}
    assert flex_meal_evidence(no_meal)["status"] == "none"


def test_paycom_lunch_pairs_pairs_out_then_in() -> None:
    punches = [
        {"kind": "OUT LUNCH", "displayTime": "12:00 PM", "actualTime": None, "provenanceAvailable": False, "changeRequestStatus": None, "approved": 0},
        {"kind": "IN LUNCH", "displayTime": "12:30 PM", "actualTime": None, "provenanceAvailable": False, "changeRequestStatus": None, "approved": 0},
    ]
    pairs = paycom_lunch_pairs(punches)
    assert len(pairs) == 1
    assert pairs[0]["out"] == "12:00 PM" and pairs[0]["in"] == "12:30 PM"
    assert pairs[0]["durationMinutes"] == 30
    assert pairs[0]["evidenceStatus"] == "display_only"


def test_paycom_lunch_pairs_rejects_unknown_display_time() -> None:
    punches = [{"kind": "OUT LUNCH", "displayTime": "whenever", "actualTime": None, "provenanceAvailable": False, "changeRequestStatus": None, "approved": 0}]
    try:
        paycom_lunch_pairs(punches)
    except ValueError as exc:
        assert str(exc) == "schema_invalid"
    else:
        raise AssertionError("expected schema_invalid")
