"""Small, read-only Paycom meal comparison and integrity query."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .contracts import bound_result, validate_date, validate_request
from .meals import (
    collection_freshness,
    flex_meal_evidence,
    identity_name_key,
    minute_delta,
    parse_clock,
    paycom_lunch_pairs,
)
from .paths import PaycomPaths, coerce_paths
from .periods import PeriodError, period_from_end
from .storage import ReadOnlyDatabase, StorageError, open_read_only

PAYCOM_TIMEZONE = "America/Los_Angeles"
PACIFIC = ZoneInfo(PAYCOM_TIMEZONE)
MAX_REPORTS = 8
MAX_DRIVERS = 128
MAX_EMPLOYEES = 1_000
MAX_DAYS = 20_000
MAX_PUNCHES = 20_000
MAX_IDENTITY_MAPPINGS = 1_000
MAX_REVIEW_PUNCHES = 4


class QueryError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


ROSTER_TABLES = ("collection_runs", "active_snapshots", "employees")
TIMECARD_TABLES = ("runs", "active_periods", "employee_timecards", "days", "punches")
MEAL_TABLES = ("meal_break_gap_reports", "meal_break_gap_rows")
IDENTITY_TABLES = ("identity_crosswalk",)

ROSTER_COLUMNS = {
    "collection_runs": ("run_id", "target", "collected_at", "source_sha256", "employee_count", "active_employee_count", "active_driver_count"),
    "active_snapshots": ("target", "run_id"),
    "employees": ("run_id", "employee_code", "employee_name", "status", "department_code", "department_desc", "delivery_station_code", "delivery_station_desc", "position_title", "pay_class", "pay_type", "primary_supervisor", "is_active", "is_active_driver"),
}
TIMECARD_COLUMNS = {
    "runs": ("id", "period_start", "period_end", "period_key", "roster_revision", "roster_source_sha256", "employee_count", "day_count", "punch_count", "missing_day_count", "collected_at"),
    "active_periods": ("period_end", "run_id"),
    "employee_timecards": ("run_id", "employee_code", "employee_name", "period_total_hours"),
    "days": ("run_id", "employee_code", "work_date", "hours", "total_hours", "comments_json", "missing_punch", "unresolved_slots_json"),
    "punches": ("run_id", "employee_code", "work_date", "ordinal", "kind", "display_time", "actual_time", "provenance_available", "change_request_status", "approved", "comment"),
}
MEAL_COLUMNS = {
    "meal_break_gap_reports": ("id", "report_date", "collected_at", "driver_count", "total_driver_count", "meal_break_count", "row_count", "status"),
    "meal_break_gap_rows": ("report_id", "report_date", "transporter_id", "delivery_associate_name", "route_code", "meal_index", "meal_start_time", "meal_end_time", "meal_length_min", "parse_warning"),
}
IDENTITY_COLUMNS = {
    "identity_crosswalk": ("transporter_id", "employee_code", "effective_start", "effective_end", "approved_at", "approved_by", "evidence_json"),
}


def _mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _stored_text(value: object, maximum: int = 160) -> str:
    if type(value) is not str:
        raise QueryError("schema_invalid", "A stored Paycom text value is invalid.")
    result = value.strip()
    if not result or len(result) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in result):
        raise QueryError("schema_invalid", "A stored Paycom text value is invalid.")
    return result


def _stored_date(value: object) -> str:
    try:
        return validate_date(value)
    except Exception as exc:
        raise QueryError("schema_invalid", "A stored Paycom date is invalid.") from exc


def _stored_json_array(value: object, *, maximum: int, item_maximum: int | None = None) -> list[Any]:
    if not isinstance(value, str):
        raise QueryError("schema_invalid", "A stored Paycom JSON value is invalid.")
    try:
        result = json.loads(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise QueryError("schema_invalid", "A stored Paycom JSON value is invalid.") from exc
    if not isinstance(result, list) or len(result) > maximum:
        raise QueryError("schema_invalid", "A stored Paycom JSON value is invalid.")
    if item_maximum is not None and any(type(item) is not str or len(item) > item_maximum for item in result):
        raise QueryError("schema_invalid", "A stored Paycom JSON value is invalid.")
    return result


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise QueryError("schema_invalid", "A stored Paycom boolean is invalid.")


def _integer(value: object, minimum: int = 0, maximum: int = 2_000_000) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise QueryError("schema_invalid", "A stored Paycom count is invalid.")
    return value


def _number(value: object, minimum: float = 0, maximum: float = 336) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        raise QueryError("schema_invalid", "A stored Paycom number is invalid.")
    return float(value)


def _now(clock: Any) -> datetime:
    try:
        instant = clock()
    except Exception as exc:
        raise QueryError("unavailable", "The Paycom clock is unavailable.") from exc
    if not isinstance(instant, datetime):
        raise QueryError("unavailable", "The Paycom clock is unavailable.")
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def _pacific_date(instant: datetime) -> str:
    return instant.astimezone(PACIFIC).date().isoformat()


def _add_days(value: str, count: int) -> str:
    return (date.fromisoformat(value) + timedelta(days=count)).isoformat()


def _freshness(value: object, now: datetime) -> dict[str, object]:
    try:
        return collection_freshness(value, now)
    except ValueError as exc:
        raise QueryError("schema_invalid", "A stored Paycom collection timestamp is invalid.") from exc


def _safe_count(rows: list[Any], maximum: int, what: str) -> list[Any]:
    if len(rows) > maximum:
        raise QueryError("observation_limit", f"The Paycom {what} population exceeds the safe bound.")
    return rows


class PaycomQuery:
    """Read-only domain implementation. It never creates or writes a database."""

    def __init__(self, paths: PaycomPaths | Mapping[str, Any] | Any | None = None, clock: Any | None = None) -> None:
        self.paths = coerce_paths(paths)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._databases: dict[str, ReadOnlyDatabase | None] = {}

    def close(self) -> None:
        failure: QueryError | None = None
        for name, database in tuple(self._databases.items()):
            if database is None:
                continue
            try:
                database.close()
            except StorageError as exc:
                failure = QueryError(exc.code, exc.message)
        self._databases.clear()
        if failure:
            raise failure

    def __enter__(self) -> "PaycomQuery":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def _open(self, name: str, tables: tuple[str, ...], columns: dict[str, tuple[str, ...]]) -> ReadOnlyDatabase | None:
        if name in self._databases:
            return self._databases[name]
        path = self.paths.as_dict()[name]
        if not path.exists() and not path.is_symlink():
            self._databases[name] = None
            return None
        try:
            database = open_read_only(path, required_tables=tables)
            database.require_columns(columns)
        except StorageError as exc:
            if exc.code == "not_loaded":
                self._databases[name] = None
                return None
            raise QueryError(exc.code, exc.message) from exc
        self._databases[name] = database
        return database

    def _roster(self) -> ReadOnlyDatabase | None:
        return self._open("roster", ROSTER_TABLES, ROSTER_COLUMNS)

    def _timecards(self) -> ReadOnlyDatabase | None:
        return self._open("timecards", TIMECARD_TABLES, TIMECARD_COLUMNS)

    def _meals(self) -> ReadOnlyDatabase | None:
        return self._open("meals", MEAL_TABLES, MEAL_COLUMNS)

    def _identity(self) -> ReadOnlyDatabase | None:
        return self._open("identity", IDENTITY_TABLES, IDENTITY_COLUMNS)

    @staticmethod
    def _check_database(database: ReadOnlyDatabase | None) -> bool:
        return database is not None and database.quick_ok()

    def _latest_roster_run(self, database: ReadOnlyDatabase) -> dict[str, Any] | None:
        row = database.execute(
            """SELECT r.run_id AS runId, r.target, r.collected_at AS collectedAt,
                      r.source_sha256 AS sourceSha256, r.employee_count AS employeeCount,
                      r.active_employee_count AS activeEmployees, r.active_driver_count AS activeDrivers
               FROM active_snapshots a JOIN collection_runs r ON r.run_id=a.run_id
              ORDER BY a.target DESC LIMIT 1"""
        ).fetchone()
        return _mapping(row) if row is not None else None

    def _roster_run(self, database: ReadOnlyDatabase, run_id: object) -> dict[str, Any] | None:
        row = database.execute(
            """SELECT run_id AS runId, target, collected_at AS collectedAt,
                      source_sha256 AS sourceSha256, employee_count AS employeeCount,
                      active_employee_count AS activeEmployees, active_driver_count AS activeDrivers
                 FROM collection_runs WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        return _mapping(row) if row is not None else None

    def _roster_rows(self, database: ReadOnlyDatabase, run_id: object) -> list[dict[str, Any]]:
        rows = _safe_count(
            [_mapping(row) for row in database.execute(
                """SELECT employee_code AS employeeCode, employee_name AS employeeName,
                          status, department_code AS departmentCode, department_desc AS department,
                          delivery_station_code AS stationCode, delivery_station_desc AS station,
                          position_title AS positionTitle, pay_class AS payClass, pay_type AS payType,
                          primary_supervisor AS primarySupervisor, is_active_driver AS isActiveDriver
                     FROM employees WHERE run_id=? AND is_active=1
                     ORDER BY employee_name COLLATE NOCASE, employee_code LIMIT ?""",
                (run_id, MAX_EMPLOYEES + 1),
            ).fetchall()],
            MAX_EMPLOYEES,
            "employees",
        )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            row["employeeCode"] = _stored_text(row["employeeCode"], 4).upper()
            row["employeeName"] = _stored_text(row["employeeName"], 160)
            if row["employeeCode"] in seen:
                raise QueryError("schema_invalid", "The roster contains duplicate employee codes.")
            seen.add(row["employeeCode"])
            row["isActiveDriver"] = _bool(row["isActiveDriver"])
            result.append(row)
        return result

    def _run_for(self, timecards: ReadOnlyDatabase, *, period_end: str | None = None, work_date: str | None = None) -> dict[str, Any] | None:
        if period_end is not None:
            row = timecards.execute(
                """SELECT r.* FROM active_periods a JOIN runs r ON r.id=a.run_id
                    WHERE r.period_end=? LIMIT 1""", (period_end,)
            ).fetchone()
        elif work_date is not None:
            row = timecards.execute(
                """SELECT r.* FROM active_periods a JOIN runs r ON r.id=a.run_id
                    WHERE r.period_start<=? AND r.period_end>=?
                    ORDER BY r.period_end DESC LIMIT 1""", (work_date, work_date)
            ).fetchone()
        else:
            row = timecards.execute(
                """SELECT r.* FROM active_periods a JOIN runs r ON r.id=a.run_id
                    ORDER BY r.period_end DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        run = _mapping(row)
        try:
            period = period_from_end(_stored_date(run["period_end"]))
        except (KeyError, PeriodError) as exc:
            raise QueryError("schema_invalid", "The loaded Paycom period is invalid.") from exc
        if run.get("period_start") != period.start or run.get("period_key") != period.key:
            raise QueryError("schema_invalid", "The loaded Paycom period is not a canonical fourteen-day period.")
        for key in ("employee_count", "day_count", "punch_count", "missing_day_count"):
            _integer(run.get(key), 0, MAX_DAYS if key != "employee_count" else MAX_EMPLOYEES)
        if run["employee_count"] < 1 or run["day_count"] != run["employee_count"] * 14:
            raise QueryError("schema_invalid", "The loaded Paycom period counts are incomplete.")
        if work_date is not None and not period.start <= work_date <= period.end:
            raise QueryError("wrong_period", "The requested date is outside the loaded Paycom period.")
        return run

    def _period_projection(self, database: ReadOnlyDatabase, run: Mapping[str, Any]) -> dict[str, int]:
        run_id = run["id"]
        employees = int(database.execute("SELECT COUNT(*) FROM employee_timecards WHERE run_id=?", (run_id,)).fetchone()[0])
        days = int(database.execute("SELECT COUNT(*) FROM days WHERE run_id=?", (run_id,)).fetchone()[0])
        punches = int(database.execute("SELECT COUNT(*) FROM punches WHERE run_id=?", (run_id,)).fetchone()[0])
        incomplete = int(database.execute("SELECT COUNT(*) FROM days WHERE run_id=? AND missing_punch=1", (run_id,)).fetchone()[0])
        return {"employees": employees, "days": days, "punches": punches, "incompleteDays": incomplete}

    def _period_evidence(self, database: ReadOnlyDatabase, run: Mapping[str, Any], date_start: str | None = None, date_end: str | None = None) -> list[dict[str, Any]]:
        args: list[Any] = [run["id"]]
        range_sql = ""
        if date_start is not None and date_end is not None:
            range_sql = " AND work_date>=? AND work_date<=?"
            args.extend([date_start, date_end])
        cards = _safe_count(
            [_mapping(row) for row in database.execute(
                """SELECT employee_code AS employeeCode, employee_name AS employeeName,
                          period_total_hours AS periodHours
                     FROM employee_timecards WHERE run_id=? ORDER BY employee_code LIMIT ?""",
                (run["id"], MAX_EMPLOYEES + 1),
            ).fetchall()],
            MAX_EMPLOYEES,
            "timecard employees",
        )
        if len(cards) != run["employee_count"]:
            raise QueryError("schema_invalid", "The timecard employee projection is incomplete.")
        by_code: dict[str, dict[str, Any]] = {}
        for card in cards:
            code = _stored_text(card["employeeCode"], 4).upper()
            if code in by_code:
                raise QueryError("schema_invalid", "The timecard projection contains duplicate employees.")
            by_code[code] = {"employeeCode": code, "employeeName": _stored_text(card["employeeName"]), "periodHours": _number(card["periodHours"], 0, 336), "days": [], "punches": []}
        day_rows = _safe_count(
            [_mapping(row) for row in database.execute(
                f"""SELECT employee_code AS employeeCode, work_date AS date,
                           hours, total_hours AS totalHours, comments_json AS commentsJson,
                           missing_punch AS missingPunch, unresolved_slots_json AS unresolvedSlotsJson
                      FROM days WHERE run_id=?{range_sql}
                      ORDER BY employee_code, work_date LIMIT ?""",
                tuple(args + [MAX_DAYS + 1]),
            ).fetchall()],
            MAX_DAYS,
            "timecard days",
        )
        for row in day_rows:
            code = _stored_text(row["employeeCode"], 4).upper()
            target = by_code.get(code)
            if target is None:
                raise QueryError("schema_invalid", "A timecard day has no employee card.")
            day = {
                "date": _stored_date(row["date"]),
                "hours": None if row["hours"] is None else _number(row["hours"]),
                "totalHours": None if row["totalHours"] is None else _number(row["totalHours"]),
                "comments": _stored_json_array(row["commentsJson"], maximum=20, item_maximum=2_000),
                "missingPunch": _bool(row["missingPunch"]),
                "unresolvedSlots": _stored_json_array(row["unresolvedSlotsJson"], maximum=64, item_maximum=64),
            }
            if day["missingPunch"] != bool(day["unresolvedSlots"]):
                raise QueryError("schema_invalid", "Missing-punch evidence is not explicit.")
            target["days"].append(day)
        punch_args = list(args)
        punch_rows = _safe_count(
            [_mapping(row) for row in database.execute(
                f"""SELECT employee_code AS employeeCode, work_date AS date, ordinal,
                           kind, display_time AS displayTime, actual_time AS actualTime,
                           provenance_available AS provenanceAvailable,
                           change_request_status AS changeRequestStatus, approved, comment
                      FROM punches WHERE run_id=?{range_sql}
                      ORDER BY employee_code, work_date, ordinal LIMIT ?""",
                tuple(punch_args + [MAX_PUNCHES + 1]),
            ).fetchall()],
            MAX_PUNCHES,
            "timecard punches",
        )
        for row in punch_rows:
            code = _stored_text(row["employeeCode"], 4).upper()
            target = by_code.get(code)
            if target is None:
                raise QueryError("schema_invalid", "A timecard punch has no employee card.")
            row["employeeCode"] = code
            row["date"] = _stored_date(row["date"])
            row["kind"] = _stored_text(row["kind"], 80)
            row["displayTime"] = _stored_text(row["displayTime"], 32)
            row["actualTime"] = None if row["actualTime"] is None else _stored_text(row["actualTime"], 32)
            row["provenanceAvailable"] = _bool(row["provenanceAvailable"])
            row["approved"] = _bool(row["approved"])
            if row["changeRequestStatus"] not in (None, "pending", "approved"):
                raise QueryError("schema_invalid", "The timecard punch change request status is invalid.")
            row["comment"] = None if row["comment"] is None or row["comment"] == "" else _stored_text(row["comment"], 2_000)
            target["punches"].append(row)
        if date_start is not None and date_end is not None:
            expected = [value for value in period_from_end(run["period_end"]).dates if date_start <= value <= date_end]
        else:
            expected = list(period_from_end(run["period_end"]).dates)
        for evidence in by_code.values():
            actual_dates = [row["date"] for row in evidence["days"]]
            if actual_dates != expected:
                raise QueryError("schema_invalid", "The timecard day projection is incomplete or out of order.")
        return list(by_code.values())

    def _identity_mappings(self, database: ReadOnlyDatabase) -> dict[str, dict[str, Any]]:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        if version != 1 or not database.quick_ok():
            raise QueryError("schema_invalid", "The identity crosswalk is invalid.")
        rows = _safe_count(
            [_mapping(row) for row in database.execute(
                """SELECT transporter_id AS transporterId, employee_code AS employeeCode,
                          effective_start AS effectiveStart, effective_end AS effectiveEnd,
                          approved_at AS approvedAt, approved_by AS approvedBy, evidence_json AS evidenceJson
                     FROM identity_crosswalk ORDER BY transporter_id LIMIT ?""", (MAX_IDENTITY_MAPPINGS + 1,)
            ).fetchall()],
            MAX_IDENTITY_MAPPINGS,
            "identity mappings",
        )
        allowed_evidence = {"dcr_transporter_full_name", "route_transporter_name", "historical_lunch_alignment"}
        result: dict[str, dict[str, Any]] = {}
        codes: set[str] = set()
        for row in rows:
            transporter = _stored_text(row["transporterId"])
            code = _stored_text(row["employeeCode"], 4).upper()
            if re.fullmatch(r"[A-Za-z0-9]{4}", code) is None or code in codes or transporter in result:
                raise QueryError("schema_invalid", "The identity crosswalk is not one-to-one.")
            start = _stored_date(row["effectiveStart"])
            end = None if row["effectiveEnd"] is None else _stored_date(row["effectiveEnd"])
            if end is not None and end < start:
                raise QueryError("schema_invalid", "The identity crosswalk has an invalid effective range.")
            approved_at = row["approvedAt"]
            if not isinstance(approved_at, str) or "T" not in approved_at:
                raise QueryError("schema_invalid", "The identity crosswalk approval timestamp is invalid.")
            _stored_text(row["approvedBy"], 80)
            try:
                evidence = json.loads(row["evidenceJson"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise QueryError("schema_invalid", "The identity crosswalk evidence is invalid.") from exc
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4 or len(set(evidence)) != len(evidence) or any(item not in allowed_evidence for item in evidence):
                raise QueryError("schema_invalid", "The identity crosswalk evidence is invalid.")
            result[transporter] = {"employeeCode": code, "effectiveStart": start, "effectiveEnd": end}
            codes.add(code)
        return result

    def _meal_rows(self, database: ReadOnlyDatabase, work_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        reports = [_mapping(row) for row in database.execute(
            """SELECT id, collected_at AS collectedAt, driver_count AS driverCount,
                      total_driver_count AS totalDriverCount, meal_break_count AS mealBreakCount,
                      row_count AS rowCount, status
                 FROM meal_break_gap_reports WHERE report_date=? ORDER BY id LIMIT ?""", (work_date, MAX_REPORTS + 1)
        ).fetchall()]
        if len(reports) > MAX_REPORTS:
            raise QueryError("observation_limit", "The Meal Break Gaps report count exceeds the safe bound.")
        for report in reports:
            if report["status"] not in {"ok", "partial"}:
                raise QueryError("schema_invalid", "The Meal Break Gaps report status is invalid.")
            for key in ("driverCount", "totalDriverCount", "mealBreakCount", "rowCount"):
                _integer(report[key], 0, MAX_DRIVERS)
            if report["totalDriverCount"] < report["driverCount"] or report["rowCount"] != report["driverCount"]:
                raise QueryError("schema_invalid", "The Meal Break Gaps population counts are inconsistent.")
            projected = database.execute(
                """SELECT COUNT(*) AS rows,
                          SUM(CASE WHEN meal_index IS NOT NULL THEN 1 ELSE 0 END) AS meals
                     FROM meal_break_gap_rows WHERE report_id=?""", (report["id"],)
            ).fetchone()
            if int(projected["rows"]) != report["rowCount"] or int(projected["meals"] or 0) != report["mealBreakCount"]:
                raise QueryError("schema_invalid", "The Meal Break Gaps report projection is incomplete.")
        if not reports:
            return [], []
        report_ids = {report["id"] for report in reports}
        rows = _safe_count(
            [_mapping(row) for row in database.execute(
                """SELECT report_id AS reportId, transporter_id AS transporterId,
                          delivery_associate_name AS driverName, route_code AS routeCode,
                          meal_index AS mealIndex, meal_start_time AS mealOut,
                          meal_end_time AS mealIn, meal_length_min AS flexMinutes,
                          parse_warning AS parseWarning
                     FROM meal_break_gap_rows WHERE report_date=?
                     ORDER BY delivery_associate_name COLLATE NOCASE, transporter_id LIMIT ?""", (work_date, MAX_DRIVERS * MAX_REPORTS + 1)
            ).fetchall()],
            MAX_DRIVERS * MAX_REPORTS,
            "Meal Break Gaps drivers",
        )
        expected_rows = sum(report["rowCount"] for report in reports)
        if len(rows) != expected_rows or any(row["reportId"] not in report_ids for row in rows):
            raise QueryError("schema_invalid", "The Meal Break Gaps population is incomplete.")
        seen: set[str] = set()
        for row in rows:
            row["transporterId"] = _stored_text(row["transporterId"])
            row["driverName"] = _stored_text(row["driverName"])
            row["routeCode"] = _stored_text(row["routeCode"], 80)
            if row["transporterId"] in seen:
                raise QueryError("schema_invalid", "The Meal Break Gaps population has duplicate transporter IDs.")
            seen.add(row["transporterId"])
        return reports, rows

    def _review_context(self, evidence: Mapping[str, Any], work_date: str) -> dict[str, Any]:
        day = next((row for row in evidence["days"] if row["date"] == work_date), None)
        punches = [row for row in evidence["punches"] if row["date"] == work_date]
        lunch = [row for row in punches if row["kind"].upper() in {"OUT LUNCH", "IN LUNCH"}]
        approved_edit = any(row["changeRequestStatus"] == "approved" or row["approved"] for row in lunch)
        actual_ins = sorted((row for row in punches if row["kind"].upper() == "IN DAY" and row["provenanceAvailable"] and parse_clock(row["actualTime"]) is not None), key=lambda row: row.get("ordinal", 0))
        actual_outs = sorted((row for row in punches if row["kind"].upper() == "OUT DAY" and row["provenanceAvailable"] and parse_clock(row["actualTime"]) is not None), key=lambda row: row.get("ordinal", 0))
        raw_hours = day.get("totalHours") if day else None
        day_hours = None if raw_hours is None else round(float(raw_hours) * 100) / 100
        missing_types = {"i1": "IN DAY", "o1": "OUT LUNCH", "i2": "IN LUNCH", "o2": "OUT DAY"}
        missing = [] if day is None else list(dict.fromkeys(missing_types.get(str(value).lower().split(":")[-1]) for value in day["unresolvedSlots"] if str(value).lower().split(":")[-1] in missing_types))
        unresolved_all = [
            {"time": row["displayTime"] if parse_clock(row["displayTime"]) is not None else None, "kind": row["kind"], "status": "pending" if row["changeRequestStatus"] == "pending" else "display_only"}
            for row in punches if row["changeRequestStatus"] == "pending" or not row["provenanceAvailable"]
        ]
        comment = next((" ".join(str(row["comment"]).split()) for row in lunch if row["comment"] and str(row["comment"]).strip()), None)
        truncated = bool(comment and len(comment) > 160)
        return {
            "dayHours": day_hours,
            "firstClockIn": actual_ins[0]["actualTime"] if actual_ins else None,
            "paycomClockOut": actual_outs[-1]["actualTime"] if actual_outs else None,
            "missingPunchTypes": missing,
            "unresolvedPunches": unresolved_all[:MAX_REVIEW_PUNCHES],
            "unresolvedPunchCount": len(unresolved_all),
            "unresolvedPunchesTruncated": len(unresolved_all) > MAX_REVIEW_PUNCHES,
            "approvedEdit": approved_edit,
            "changeNote": f"{comment[:157]}…" if truncated else comment,
            "changeNoteTruncated": truncated,
        }

    def _paycom_context(self, work_date: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, dict[str, Any]] | None, dict[str, dict[str, Any]] | None, dict[str, dict[str, Any]] | None]:
        roster = self._roster()
        timecards = self._timecards()
        identity = self._identity()
        if roster is None or timecards is None or identity is None:
            return None, None, None, None, None
        if not roster.quick_ok() or not timecards.quick_ok() or not identity.quick_ok():
            raise QueryError("schema_invalid", "A Paycom database integrity check failed.")
        run = self._run_for(timecards, work_date=work_date)
        if run is None:
            return None, None, None, None, None
        roster_run = self._roster_run(roster, run["roster_revision"])
        if roster_run is None or roster_run["sourceSha256"] != run["roster_source_sha256"] or roster_run["activeEmployees"] != run["employee_count"]:
            raise QueryError("schema_invalid", "Roster and timecard revisions are not bound.")
        roster_rows = self._roster_rows(roster, run["roster_revision"])
        if len(roster_rows) != run["employee_count"]:
            raise QueryError("schema_invalid", "The bound roster is incomplete.")
        evidence = self._period_evidence(timecards, run, work_date, work_date)
        projection = self._period_projection(timecards, run)
        if projection != {"employees": run["employee_count"], "days": run["day_count"], "punches": run["punch_count"], "incompleteDays": run["missing_day_count"]}:
            raise QueryError("schema_invalid", "The timecard projection counts are inconsistent.")
        roster_codes = sorted(row["employeeCode"] for row in roster_rows)
        evidence_codes = sorted(row["employeeCode"] for row in evidence)
        if roster_codes != evidence_codes:
            raise QueryError("schema_invalid", "The roster and timecard populations differ.")
        mappings = self._identity_mappings(identity)
        by_code = {row["employeeCode"]: row for row in roster_rows}
        by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in roster_rows:
            by_name[identity_name_key(row["employeeName"])].append(row)
        by_evidence = {row["employeeCode"]: row for row in evidence}
        source = {"loaded": True, "periodStart": run["period_start"], "periodEnd": run["period_end"], "run": run}
        return source, mappings, by_code, by_name, by_evidence

    def meal_comparison(self, values: Mapping[str, Any]) -> dict[str, Any]:
        instant = _now(self.clock)
        today = _pacific_date(instant)
        work_date = values.get("work_date") or (_add_days(today, -1) if values.get("relative_scope") == "yesterday" else today)
        meals = self._meals()
        if meals is None:
            return {"ok": True, "status": "not_loaded", "timezone": PAYCOM_TIMEZONE, "workDate": work_date, "flexSource": {"loaded": False}, "paycomSource": {"loaded": False}}
        if not meals.quick_ok():
            raise QueryError("schema_invalid", "The Meal Break Gaps database integrity check failed.")
        reports, sources = self._meal_rows(meals, work_date)
        if not reports:
            return {"ok": True, "status": "not_loaded", "timezone": PAYCOM_TIMEZONE, "workDate": work_date, "flexSource": {"loaded": False}, "paycomSource": {"loaded": False}}
        paycom_source, mappings, by_code, by_name, evidence_by_code = self._paycom_context(work_date)
        summary: dict[str, int] = {
            "drivers": len(sources), "identityMatched": 0, "identityUnmatched": 0, "identityAmbiguous": 0, "identityNotEvaluated": 0,
            "exactMatches": 0, "closeMatches": 0, "minorDifferences": 0, "different": 0, "flexOnly": 0, "paycomOnly": 0,
            "noLunchEvidence": 0, "multiplePaycomPairs": 0, "incompletePaycomLunch": 0, "unresolvedPaycomEvidence": 0,
            "invalidFlexEvidence": 0, "paycomNotLoaded": 0,
        }
        count_field = {
            "exact_match": "exactMatches", "close_match": "closeMatches", "minor_difference": "minorDifferences", "different": "different",
            "flex_only": "flexOnly", "paycom_only": "paycomOnly", "no_lunch_evidence": "noLunchEvidence", "multiple_paycom_pairs": "multiplePaycomPairs",
            "incomplete_paycom_lunch": "incompletePaycomLunch", "unresolved_paycom_evidence": "unresolvedPaycomEvidence", "invalid_flex_evidence": "invalidFlexEvidence", "paycom_not_loaded": "paycomNotLoaded",
        }
        results: list[dict[str, Any]] = []
        for source_row in sources:
            flex = flex_meal_evidence(source_row)
            person = None
            evidence = None
            identity_status: str
            identity_source: str | None = None
            if paycom_source is None:
                identity_status = "not_evaluated"
                summary["identityNotEvaluated"] += 1
                comparison_status = "paycom_not_loaded"
            else:
                mapping = mappings.get(source_row["transporterId"]) if mappings is not None else None
                approved = bool(mapping and work_date >= mapping["effectiveStart"] and (mapping["effectiveEnd"] is None or work_date <= mapping["effectiveEnd"]))
                if approved:
                    person = by_code.get(mapping["employeeCode"]) if by_code is not None else None
                    matches = [person] if person is not None else []
                    identity_source = "approved_crosswalk" if person is not None else None
                else:
                    matches = (by_name or {}).get(identity_name_key(source_row["driverName"]), [])
                    person = matches[0] if len(matches) == 1 else None
                    identity_source = "exact_name" if person is not None else None
                if not matches:
                    identity_status = "unmatched"
                    summary["identityUnmatched"] += 1
                    comparison_status = "identity_unmatched"
                elif len(matches) > 1:
                    identity_status = "ambiguous"
                    summary["identityAmbiguous"] += 1
                    comparison_status = "identity_ambiguous"
                else:
                    identity_status = "matched"
                    summary["identityMatched"] += 1
                    evidence = (evidence_by_code or {}).get(person["employeeCode"])
                    if evidence is None:
                        raise QueryError("schema_invalid", "A matched employee has no timecard evidence.")
                    pairs = paycom_lunch_pairs([row for row in evidence["punches"] if row["date"] == work_date])
                    flex_invalid = flex["status"] == "invalid" or (flex["status"] == "warning" and flex["out"] is None)
                    if flex_invalid:
                        comparison_status = "invalid_flex_evidence"
                    elif len(pairs) > 1:
                        comparison_status = "multiple_paycom_pairs"
                    elif len(pairs) == 1 and (pairs[0]["out"] is None or pairs[0]["in"] is None or pairs[0]["durationMinutes"] is None):
                        comparison_status = "incomplete_paycom_lunch"
                    elif len(pairs) == 1 and pairs[0]["evidenceStatus"] not in {"actual", "approved"}:
                        comparison_status = "unresolved_paycom_evidence"
                    elif not pairs:
                        comparison_status = "no_lunch_evidence" if flex["out"] is None else "flex_only"
                    elif flex["out"] is None:
                        comparison_status = "paycom_only"
                    else:
                        pair = pairs[0]
                        values_clock = [parse_clock(flex["out"]), parse_clock(flex["in"]), parse_clock(pair["out"]), parse_clock(pair["in"])]
                        if any(value is None for value in values_clock):
                            raise QueryError("schema_invalid", "A comparable meal timestamp is invalid.")
                        out_delta = minute_delta(values_clock[2], values_clock[0])
                        in_delta = minute_delta(values_clock[3], values_clock[1])
                        largest = max(abs(out_delta), abs(in_delta))
                        comparison_status = "exact_match" if largest == 0 else "close_match" if largest <= 2 else "minor_difference" if largest <= 5 else "different"
            if paycom_source is None:
                pairs = []
            elif person is None:
                pairs = []
            if comparison_status not in {"identity_unmatched", "identity_ambiguous"}:
                summary[count_field[comparison_status]] += 1
            review_reasons: list[str] = []
            if comparison_status in {"identity_unmatched", "identity_ambiguous"}:
                review_reasons.append(comparison_status)
            elif comparison_status == "paycom_not_loaded":
                review_reasons.append("paycom_not_loaded")
            else:
                if flex["status"] == "invalid" or (flex["status"] == "warning" and flex["out"] is None):
                    review_reasons.append("invalid_flex_evidence")
                elif flex["out"] is None:
                    review_reasons.append("flex_no_meal")
                reason = {"multiple_paycom_pairs": "multiple_paycom_pairs", "incomplete_paycom_lunch": "incomplete_paycom_lunch", "unresolved_paycom_evidence": "unresolved_paycom_evidence", "flex_only": "no_paycom_lunch", "no_lunch_evidence": "no_lunch_evidence", "paycom_only": "paycom_only"}.get(comparison_status)
                if reason:
                    review_reasons.append(reason)
                if comparison_status in {"minor_difference", "different"}:
                    review_reasons.append("endpoint_difference")
                    if pairs and pairs[0]["evidenceStatus"] == "approved":
                        review_reasons.append("approved_paycom_edit")
            row: dict[str, Any] = {
                "transporterId": source_row["transporterId"], "driverName": source_row["driverName"], "routeCode": source_row["routeCode"],
                "flexOut": flex["out"], "flexIn": flex["in"], "flexMinutes": flex["minutes"], "flexEvidenceStatus": flex["status"],
                "employeeCode": person["employeeCode"] if person else None, "paycomName": person["employeeName"] if person else None,
                "identityStatus": identity_status, "identitySource": identity_source, "paycomPairs": pairs,
                "outDeltaMinutes": None, "inDeltaMinutes": None, "durationDeltaMinutes": None,
                "comparisonStatus": comparison_status, "reviewReasons": review_reasons,
            }
            if comparison_status in {"exact_match", "close_match", "minor_difference", "different"} and pairs:
                row["outDeltaMinutes"] = minute_delta(parse_clock(pairs[0]["out"]), parse_clock(flex["out"]))
                row["inDeltaMinutes"] = minute_delta(parse_clock(pairs[0]["in"]), parse_clock(flex["in"]))
                row["durationDeltaMinutes"] = pairs[0]["durationMinutes"] - flex["minutes"]
            if review_reasons and evidence is not None:
                row["reviewContext"] = self._review_context(evidence, work_date)
            results.append(row)
        reports_freshness = [_freshness(report["collectedAt"], instant) for report in reports]
        latest = sorted(reports_freshness, key=lambda value: value["collectedAt"], reverse=True)[0]
        flex_source = {
            "loaded": True, "status": "complete" if all(report["status"] == "ok" for report in reports) else "partial",
            "reportCount": len(reports), "drivers": len(sources), "totalDrivers": sum(report["totalDriverCount"] for report in reports),
            "mealBreaks": sum(report["mealBreakCount"] for report in reports), "freshness": latest,
        }
        paycom_payload: dict[str, Any] = {"loaded": False}
        if paycom_source is not None:
            paycom_payload = {"loaded": True, "periodStart": paycom_source["periodStart"], "periodEnd": paycom_source["periodEnd"], "freshness": _freshness(paycom_source["run"]["collected_at"], instant)}
        result = {
            "ok": True, "status": "found", "timezone": PAYCOM_TIMEZONE, "workDate": work_date, "flexSource": flex_source,
            "paycomSource": paycom_payload, "summary": summary, "completePopulation": True, "returnedDrivers": len(results), "truncated": False,
            "results": results,
        }
        try:
            return bound_result(result, maximum=64 * 1024)
        except Exception as exc:
            if isinstance(exc, QueryError):
                raise
            raise QueryError("result_too_large", "The bounded meal comparison response limit was exceeded.") from exc

    def audit(self, values: Mapping[str, Any]) -> dict[str, Any]:
        instant = _now(self.clock)
        roster = self._roster()
        timecards = self._timecards()
        meals = self._meals()
        identity = self._identity()
        checks = {name: False for name in ("rosterDatabase", "timecardDatabase", "mealDatabase", "identityDatabase", "rosterBinding", "periodProjection", "mealPopulation", "identityCrosswalk", "comparisonValidation")}
        counts = {"employees": 0, "days": 0, "punches": 0, "incompletePunchDays": 0, "mealReports": 0, "mealDrivers": 0, "totalMealDrivers": 0, "mealBreaks": 0, "identityMappings": 0, "identityMatched": 0, "identityUnresolved": 0}
        run: dict[str, Any] | None = None
        timecard_freshness = None
        meal_freshness = None
        if roster is not None:
            checks["rosterDatabase"] = roster.quick_ok()
        if timecards is not None:
            checks["timecardDatabase"] = timecards.quick_ok()
            if checks["timecardDatabase"]:
                run = self._run_for(timecards, period_end=values.get("period_end"), work_date=values.get("work_date"))
                if run is not None:
                    projection = self._period_projection(timecards, run)
                    counts.update(employees=projection["employees"], days=projection["days"], punches=projection["punches"], incompletePunchDays=projection["incompleteDays"])
                    checks["periodProjection"] = projection == {"employees": run["employee_count"], "days": run["day_count"], "punches": run["punch_count"], "incompleteDays": run["missing_day_count"]}
                    if checks["periodProjection"]:
                        try:
                            timecard_freshness = _freshness(run["collected_at"], instant)
                        except QueryError:
                            timecard_freshness = None
        selected_work_date = values.get("work_date")
        if selected_work_date is None and meals is not None and meals.quick_ok():
            if run is not None:
                row = meals.execute("SELECT MAX(report_date) FROM meal_break_gap_reports WHERE report_date>=? AND report_date<=?", (run["period_start"], run["period_end"])).fetchone()
            elif values.get("period_end") is not None:
                try:
                    selected_period = period_from_end(values["period_end"])
                    row = meals.execute("SELECT MAX(report_date) FROM meal_break_gap_reports WHERE report_date>=? AND report_date<=?", (selected_period.start, selected_period.end)).fetchone()
                except PeriodError:
                    row = None
            else:
                row = meals.execute("SELECT MAX(report_date) FROM meal_break_gap_reports").fetchone()
            selected_work_date = row[0] if row and row[0] else None
        if meals is not None:
            checks["mealDatabase"] = meals.quick_ok()
            if checks["mealDatabase"] and selected_work_date is not None:
                try:
                    reports, rows = self._meal_rows(meals, selected_work_date)
                    if reports:
                        counts.update(mealReports=len(reports), mealDrivers=len(rows), totalMealDrivers=sum(report["totalDriverCount"] for report in reports), mealBreaks=sum(report["mealBreakCount"] for report in reports))
                        meal_freshness = max((_freshness(report["collectedAt"], instant) for report in reports), key=lambda value: value["collectedAt"])
                except QueryError:
                    pass
        mappings = None
        if identity is not None:
            checks["identityDatabase"] = identity.quick_ok()
            if checks["identityDatabase"]:
                try:
                    mappings = self._identity_mappings(identity)
                    counts["identityMappings"] = len(mappings)
                    checks["identityCrosswalk"] = True
                except QueryError:
                    mappings = None
        if run is not None and checks["rosterDatabase"] and checks["timecardDatabase"] and roster is not None and timecards is not None:
            try:
                roster_run = self._roster_run(roster, run["roster_revision"])
                roster_rows = self._roster_rows(roster, run["roster_revision"])
                codes = sorted(row["employeeCode"] for row in roster_rows)
                card_codes = sorted(_stored_text(row[0], 4).upper() for row in timecards.execute("SELECT employee_code FROM employee_timecards WHERE run_id=? ORDER BY employee_code", (run["id"],)).fetchall())
                checks["rosterBinding"] = bool(roster_run and roster_run["sourceSha256"] == run["roster_source_sha256"] and roster_run["activeEmployees"] == run["employee_count"] and codes == card_codes)
            except QueryError:
                pass
        comparison = None
        if selected_work_date is not None and checks["mealDatabase"]:
            try:
                comparison = self.meal_comparison({"action": "meal_comparison", "work_date": selected_work_date})
            except QueryError:
                comparison = None
        if comparison and comparison.get("status") == "found":
            results = comparison["results"]
            ids = [row["transporterId"] for row in results]
            checks["mealPopulation"] = comparison.get("completePopulation") is True and comparison.get("truncated") is False and comparison.get("returnedDrivers") == comparison["flexSource"]["drivers"] == len(results) and len(set(ids)) == len(ids)
            checks["comparisonValidation"] = checks["mealPopulation"] and comparison["summary"]["drivers"] == len(results) and sum(comparison["summary"][name] for name in ("identityMatched", "identityUnmatched", "identityAmbiguous", "identityNotEvaluated")) == len(results)
            counts["identityMatched"] = comparison["summary"]["identityMatched"]
            counts["identityUnresolved"] = comparison["summary"]["identityUnmatched"] + comparison["summary"]["identityAmbiguous"]
        verified = all(checks.values())
        result = {
            "ok": True, "status": "verified" if verified else "failed", "periodEnd": run["period_end"] if run else values.get("period_end"),
            "workDate": selected_work_date, "checks": checks, "counts": counts,
            "freshness": {"timecards": timecard_freshness, "meals": meal_freshness},
        }
        return bound_result(result)

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        values = validate_request(request)
        if values["action"] == "meal_comparison":
            return self.meal_comparison(values)
        if values["action"] == "audit":
            return self.audit(values)
        raise QueryError("invalid_input", "The health action belongs to the service boundary.")


PaycomQueryService = PaycomQuery


def execute(request: Mapping[str, Any], paths: PaycomPaths | Mapping[str, Any] | Any | None = None, clock: Any | None = None) -> dict[str, Any]:
    with PaycomQuery(paths, clock) as query:
        return query.execute(request)
