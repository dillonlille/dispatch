"""Pure bounded meal and identity helpers used by the Paycom query."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import re
import unicodedata

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([AP]M)$", re.IGNORECASE)


def normalize_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.findall(r"[\w]+", without_marks.casefold(), flags=re.UNICODE))


def identity_name_key(value: str) -> str:
    return " ".join(sorted(normalize_name(value).split()))


def parse_clock(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).upper() == "PM":
        hour = 12 if hour == 12 else hour + 12
    elif hour == 12:
        hour = 0
    return hour * 60 + minute


def clock_duration(start: int | None, end: int | None) -> int | None:
    if start is None or end is None:
        return None
    duration = end - start
    if duration < 0:
        duration += 1440
    return duration if duration <= 240 else None


def minute_delta(left: int, right: int) -> int:
    value = left - right
    while value > 720:
        value -= 1440
    while value < -720:
        value += 1440
    return value


def flex_meal_evidence(row: Mapping[str, object]) -> dict[str, object]:
    index = row.get("mealIndex")
    has_meal = isinstance(index, int) and not isinstance(index, bool) and index >= 0
    if not has_meal:
        if row.get("mealOut") is not None or row.get("mealIn") is not None or row.get("flexMinutes") is not None:
            return {"status": "invalid", "out": None, "in": None, "minutes": None}
        warning = str(row.get("parseWarning") or "").strip().casefold()
        return {"status": "none" if warning == "no meal break found" else ("warning" if warning else "none"), "out": None, "in": None, "minutes": None}
    start = parse_clock(row.get("mealOut"))
    end = parse_clock(row.get("mealIn"))
    minutes = clock_duration(start, end)
    stored_minutes = row.get("flexMinutes")
    if start is None or end is None or not isinstance(stored_minutes, int) or isinstance(stored_minutes, bool) or not 0 <= stored_minutes <= 240 or minutes != stored_minutes:
        return {"status": "invalid", "out": None, "in": None, "minutes": None}
    return {"status": "warning" if row.get("parseWarning") else "complete", "out": row.get("mealOut"), "in": row.get("mealIn"), "minutes": minutes}


def paycom_lunch_pairs(punches: Iterable[Mapping[str, object]], *, maximum: int = 16) -> list[dict[str, object]]:
    events = [row for row in punches if str(row.get("kind") or "").strip().upper() in {"OUT LUNCH", "IN LUNCH"}]
    if len(events) > maximum:
        raise ValueError("observation_limit")

    def project(row: Mapping[str, object]) -> dict[str, object]:
        display = parse_clock(row.get("displayTime"))
        if display is None:
            raise ValueError("schema_invalid")
        actual = parse_clock(row.get("actualTime")) if row.get("provenanceAvailable") else None
        return {
            "time": row.get("actualTime") if actual is not None else row.get("displayTime"),
            "minutes": actual if actual is not None else display,
            "actual": actual is not None,
            "pending": row.get("changeRequestStatus") == "pending",
            "approved": row.get("changeRequestStatus") == "approved" or bool(row.get("approved")),
        }

    raw: list[dict[str, object | None]] = []
    current: dict[str, object | None] | None = None
    for event in events:
        value = project(event)
        is_out = str(event.get("kind") or "").strip().upper() == "OUT LUNCH"
        if is_out:
            if current is not None:
                raw.append(current)
            current = {"out": value, "in": None}
        elif current is not None and current.get("in") is None:
            current["in"] = value
            raw.append(current)
            current = None
        else:
            raw.append({"out": None, "in": value})
    if current is not None:
        raw.append(current)

    pairs: list[dict[str, object]] = []
    for pair in raw:
        values = [value for value in (pair.get("out"), pair.get("in")) if isinstance(value, dict)]
        pending = any(bool(value["pending"]) for value in values)
        display_only = any(not bool(value["actual"]) for value in values)
        approved = any(bool(value["approved"]) for value in values)
        out = pair.get("out")
        incoming = pair.get("in")
        out_minutes = out.get("minutes") if isinstance(out, dict) else None
        in_minutes = incoming.get("minutes") if isinstance(incoming, dict) else None
        pairs.append(
            {
                "out": out.get("time") if isinstance(out, dict) else None,
                "in": incoming.get("time") if isinstance(incoming, dict) else None,
                "durationMinutes": clock_duration(out_minutes, in_minutes),
                "evidenceStatus": "pending" if pending else "display_only" if display_only else "approved" if approved else "actual",
            }
        )
    return pairs


def collection_freshness(value: object, now: datetime) -> dict[str, object]:
    if not isinstance(value, str):
        raise ValueError("schema_invalid")
    normalized = value.replace(" ", "T", 1) + ("Z" if " " in value and not value.endswith("Z") else "")
    try:
        instant = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("schema_invalid") from exc
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=now.tzinfo)
    instant = instant.astimezone(now.tzinfo)
    age = max(0.0, (now - instant).total_seconds() / 3600)
    age_hours = int(age * 10 + 0.5) / 10
    collected = instant.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"collectedAt": collected, "ageHours": age_hours, "staleAfterHours": 24, "stale": age_hours > 24}
