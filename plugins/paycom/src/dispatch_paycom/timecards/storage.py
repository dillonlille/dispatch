"""Atomic SQLite projection for complete Paycom timecard runs."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3

from typing import Any, Iterable
from uuid import uuid4

from .artifacts import TimecardArtifact, verify_artifact_run
from .period import Period, canonical_timecard_url, is_captured_timecard_url, parse_period_key, period_from_end, validate_code
from ..filesystem import FilesystemError, ensure_private_directory, validate_private_regular_file


class TimecardStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TimecardPublication:
    disposition: str
    run_id: str
    employee_count: int
    day_count: int
    punch_count: int
    missing_day_count: int


def _secure_db(path: Path) -> None:
    try:
        ensure_private_directory(path.parent)
    except FilesystemError as exc:
        raise TimecardStorageError("storage_root_invalid") from exc
    if path.exists() or path.is_symlink():
        try:
            validate_private_regular_file(path)
        except FilesystemError as exc:
            raise TimecardStorageError("schema_invalid") from exc


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("timecard_projection_invalid")
    return float(value)


def _nullable_number(value: Any) -> float | None:
    return None if value is None else _number(value)


def _flag(value: Any) -> int:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("timecard_projection_invalid")
    return value


def _record_flag(value: Any) -> int:
    if type(value) is not bool:
        raise ValueError("timecard_projection_invalid")
    return int(value)


def _stored_json(value: Any) -> str:
    if type(value) is not str:
        raise ValueError("timecard_projection_invalid")
    try:
        return _json(json.loads(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("timecard_projection_invalid") from exc


class TimecardStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        _secure_db(self.path)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
        self.db = sqlite3.connect(self.path, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, run_id TEXT UNIQUE NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, period_key TEXT NOT NULL, revision INTEGER NOT NULL, roster_revision TEXT NOT NULL, roster_source_sha256 TEXT NOT NULL, artifact_directory TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, employee_count INTEGER NOT NULL, day_count INTEGER NOT NULL, punch_count INTEGER NOT NULL, missing_day_count INTEGER NOT NULL, collected_at TEXT NOT NULL, UNIQUE(period_end, revision));
            CREATE TABLE IF NOT EXISTS active_periods(period_end TEXT PRIMARY KEY, run_id INTEGER UNIQUE NOT NULL REFERENCES runs(id) ON DELETE RESTRICT);
            CREATE TABLE IF NOT EXISTS employee_timecards(run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE, employee_code TEXT NOT NULL, employee_name TEXT NOT NULL, timecard_url TEXT NOT NULL, period_total_hours REAL NOT NULL, approvals_json TEXT NOT NULL, attestations_json TEXT NOT NULL, meal_waivers_json TEXT NOT NULL, additional_rows_json TEXT NOT NULL, html_path TEXT NOT NULL, html_sha256 TEXT NOT NULL, json_path TEXT NOT NULL, json_sha256 TEXT NOT NULL, PRIMARY KEY(run_id, employee_code));
            CREATE TABLE IF NOT EXISTS days(run_id INTEGER NOT NULL, employee_code TEXT NOT NULL, work_date TEXT NOT NULL, day_label TEXT NOT NULL, pay_code TEXT NOT NULL, allocation1 TEXT NOT NULL, allocation2 TEXT NOT NULL, hours REAL, total_hours REAL, dollars REAL, exception_text TEXT NOT NULL, waiver_checked INTEGER, comments_json TEXT NOT NULL, missing_punch INTEGER NOT NULL, unresolved_slots_json TEXT NOT NULL, PRIMARY KEY(run_id, employee_code, work_date), FOREIGN KEY(run_id, employee_code) REFERENCES employee_timecards(run_id, employee_code) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS punches(run_id INTEGER NOT NULL, employee_code TEXT NOT NULL, work_date TEXT NOT NULL, ordinal INTEGER NOT NULL, row_index INTEGER NOT NULL, slot TEXT NOT NULL, kind TEXT NOT NULL, display_time TEXT NOT NULL, actual_time TEXT NOT NULL, rounded_time TEXT NOT NULL, clock_name TEXT NOT NULL, clock_code TEXT NOT NULL, comment TEXT NOT NULL, provenance_available INTEGER NOT NULL, change_request_status TEXT, approved INTEGER NOT NULL, PRIMARY KEY(run_id, employee_code, work_date, ordinal), FOREIGN KEY(run_id, employee_code, work_date) REFERENCES days(run_id, employee_code, work_date) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS weekly_totals(run_id INTEGER NOT NULL, employee_code TEXT NOT NULL, week_number INTEGER NOT NULL, hours REAL NOT NULL, PRIMARY KEY(run_id, employee_code, week_number), FOREIGN KEY(run_id, employee_code) REFERENCES employee_timecards(run_id, employee_code) ON DELETE CASCADE);
            PRAGMA user_version=2;
            """
        )
        self.db.commit()

    def active_run(self, period_end: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT r.* FROM active_periods a JOIN runs r ON r.id=a.run_id WHERE a.period_end=?", (period_end,)).fetchone()

    def publish(self, *, period: Period, roster_revision: str, roster_source_sha256: str, artifact: TimecardArtifact, employees: list[dict[str, Any]], replace: bool = False, collected_at: str) -> TimecardPublication:
        period = parse_period_key(period.key)
        if type(replace) is not bool:
            raise TimecardStorageError("replace_invalid")
        verify_artifact_run(artifact.directory, [item["employeeCode"] for item in employees])
        if not isinstance(roster_revision, str) or not roster_revision or len(roster_revision) > 100 or not isinstance(roster_source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", roster_source_sha256) is None:
            raise TimecardStorageError("timecard_storage_invalid")
        current = self.active_run(period.end)
        if current is not None and current["manifest_sha256"] == artifact.manifest_sha256:
            publication = TimecardPublication("already_current", str(current["run_id"]), int(current["employee_count"]), int(current["day_count"]), int(current["punch_count"]), int(current["missing_day_count"]))
            if not self.verify_projection(period.end, roster_revision, roster_source_sha256, artifact, employees):
                raise TimecardStorageError("post_verification_failed")
            return publication
        if current is not None and not replace:
            raise TimecardStorageError("replacement_required")
        expected_codes = sorted(item["employeeCode"] for item in employees)
        if not employees or expected_codes != sorted(set(expected_codes)):
            raise TimecardStorageError("employee_membership_mismatch")
        by_code = {entry["employeeCode"]: entry for entry in artifact.entries}
        for item in employees:
            code = item.get("employeeCode")
            record = item.get("record")
            if code not in by_code or not isinstance(item.get("employeeName"), str) or not isinstance(record, dict) or record.get("employeeCode") != code or not is_captured_timecard_url(record.get("sourceUrl", ""), employee_code=code, period=period):
                raise TimecardStorageError("timecard_storage_invalid")
        revision_row = self.db.execute("SELECT COALESCE(MAX(revision),0)+1 AS value FROM runs WHERE period_end=?", (period.end,)).fetchone()
        revision = int(revision_row["value"])
        run_id = uuid4().hex
        day_count = sum(len(item["record"]["days"]) for item in employees)
        punch_count = sum(sum(len(day["punches"]) for day in item["record"]["days"]) for item in employees)
        missing_count = sum(sum(bool(day["missingPunch"]) for day in item["record"]["days"]) for item in employees)
        manifest = artifact.manifest
        if (
            manifest.get("employeeCount") != len(employees)
            or manifest.get("expectedEmployeeCount") != len(employees)
            or manifest.get("timecardUrlCount") != len(employees)
            or manifest.get("dayCount") != day_count
            or manifest.get("punchCount") != punch_count
            or manifest.get("missingDayCount") != missing_count
        ):
            raise TimecardStorageError("timecard_projection_invalid")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            run_cursor = self.db.execute("INSERT INTO runs(run_id,period_start,period_end,period_key,revision,roster_revision,roster_source_sha256,artifact_directory,manifest_sha256,employee_count,day_count,punch_count,missing_day_count,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, period.start, period.end, period.key, revision, roster_revision, roster_source_sha256, str(artifact.directory), artifact.manifest_sha256, len(employees), day_count, punch_count, missing_count, collected_at))
            run_db_id = int(run_cursor.lastrowid)
            for item in employees:
                code, record, entry = item["employeeCode"], item["record"], by_code[item["employeeCode"]]
                self.db.execute("INSERT INTO employee_timecards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_db_id, code, item["employeeName"], canonical_timecard_url(code, period), float(record["periodTotalHours"]), _json(record["approvals"]), _json(record["attestations"]), _json(record["mealWaivers"]), _json(record.get("additionalRows", [])), str(artifact.directory / entry["html"]["path"]), entry["html"]["sha256"], str(artifact.directory / entry["json"]["path"]), entry["json"]["sha256"]))
                for day in record["days"]:
                    self.db.execute("INSERT INTO days VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_db_id, code, day["date"], day["label"], day["payCode"], day["allocation1"], day["allocation2"], day["hours"], day["totalHours"], day["dollars"], day["exceptionText"], None if day["waiverChecked"] is None else int(day["waiverChecked"]), _json(day["comments"]), int(day["missingPunch"]), _json(day["unresolvedSlots"])))
                    for punch in day["punches"]:
                        self.db.execute("INSERT INTO punches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_db_id, code, day["date"], punch["ordinal"], punch["rowIndex"], punch["slot"], punch["kind"], punch["displayTime"], punch["actualTime"], punch["roundedTime"], punch["clockName"], punch["clockCode"], punch["comment"], int(punch["provenanceAvailable"]), punch["changeRequestStatus"], int(punch["approved"])))
                for index, hours in enumerate(record["weeklyTotals"], 1):
                    self.db.execute("INSERT INTO weekly_totals VALUES(?,?,?,?)", (run_db_id, code, index, hours))
            self.db.execute("INSERT INTO active_periods(period_end,run_id) VALUES(?,?) ON CONFLICT(period_end) DO UPDATE SET run_id=excluded.run_id", (period.end, run_db_id))
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            raise TimecardStorageError("publication_failed") from exc
        publication = TimecardPublication("replaced" if current is not None else "loaded", run_id, len(employees), day_count, punch_count, missing_count)
        if not self.verify_projection(period.end, roster_revision, roster_source_sha256, artifact, employees):
            raise TimecardStorageError("post_verification_failed")
        return publication

    def verify_projection(self, period_end: str, roster_revision: str, roster_source_sha256: str, artifact: TimecardArtifact, employees: list[dict[str, Any]]) -> bool:
        """Verify every normalized persisted field and its source binding."""
        try:
            period = parse_period_key(artifact.manifest["periodKey"])
            if period.end != period_end or not isinstance(roster_revision, str) or not roster_revision or re.fullmatch(r"[0-9a-f]{64}", roster_source_sha256) is None:
                return False
            expected_codes = sorted(validate_code(item["employeeCode"]) for item in employees)
            if not expected_codes or expected_codes != sorted(set(expected_codes)):
                return False
            verified_artifact = verify_artifact_run(artifact.directory, expected_codes)
            if verified_artifact.manifest_sha256 != artifact.manifest_sha256 or verified_artifact.manifest != artifact.manifest or verified_artifact.entries != artifact.entries:
                return False
            entries = {entry["employeeCode"]: entry for entry in verified_artifact.entries}
            if set(entries) != set(expected_codes):
                return False
            expected_employee_rows: list[tuple[Any, ...]] = []
            expected_days: list[tuple[Any, ...]] = []
            expected_punches: list[tuple[Any, ...]] = []
            expected_weekly: list[tuple[Any, ...]] = []
            for item in employees:
                code = item["employeeCode"]
                record = item["record"]
                entry = entries.get(code)
                if (
                    not isinstance(item.get("employeeName"), str)
                    or not isinstance(record, dict)
                    or record.get("employeeCode") != code
                    or record.get("periodStart") != period.start
                    or record.get("periodEnd") != period.end
                    or record.get("periodKey") != period.key
                    or not is_captured_timecard_url(record.get("sourceUrl", ""), employee_code=code, period=period)
                    or entry is None
                    or entry.get("timecardUrl") != canonical_timecard_url(code, period)
                ):
                    return False
                expected_employee_rows.append(
                    (
                        code,
                        item["employeeName"],
                        canonical_timecard_url(code, period),
                        _number(record["periodTotalHours"]),
                        _json(record["approvals"]),
                        _json(record["attestations"]),
                        _json(record["mealWaivers"]),
                        _json(record.get("additionalRows", [])),
                        str(artifact.directory / entry["html"]["path"]),
                        entry["html"]["sha256"],
                        str(artifact.directory / entry["json"]["path"]),
                        entry["json"]["sha256"],
                    )
                )
                for day in record["days"]:
                    expected_days.append(
                        (
                            code,
                            day["date"],
                            day["label"],
                            day["payCode"],
                            day["allocation1"],
                            day["allocation2"],
                            _nullable_number(day["hours"]),
                            _nullable_number(day["totalHours"]),
                            _nullable_number(day["dollars"]),
                            day["exceptionText"],
                            None if day["waiverChecked"] is None else _record_flag(day["waiverChecked"]),
                            _json(day["comments"]),
                            _record_flag(day["missingPunch"]),
                            _json(day["unresolvedSlots"]),
                        )
                    )
                    for punch in day["punches"]:
                        expected_punches.append(
                            (
                                code,
                                day["date"],
                                punch["ordinal"],
                                punch["rowIndex"],
                                punch["slot"],
                                punch["kind"],
                                punch["displayTime"],
                                punch["actualTime"],
                                punch["roundedTime"],
                                punch["clockName"],
                                punch["clockCode"],
                                punch["comment"],
                                _record_flag(punch["provenanceAvailable"]),
                                punch["changeRequestStatus"],
                                _record_flag(punch["approved"]),
                            )
                        )
                expected_weekly.extend((code, index, _number(hours)) for index, hours in enumerate(record["weeklyTotals"], 1))
            expected_employee_rows.sort(key=lambda row: row[0])
            expected_days.sort(key=lambda row: (row[0], row[1]))
            expected_punches.sort(key=lambda row: (row[0], row[1], row[2]))
            expected_weekly.sort(key=lambda row: (row[0], row[1]))
            run = self.active_run(period.end)
            if run is None or (
                run["period_start"],
                run["period_end"],
                run["period_key"],
                run["roster_revision"],
                run["roster_source_sha256"],
                run["artifact_directory"],
                run["manifest_sha256"],
            ) != (
                period.start,
                period.end,
                period.key,
                roster_revision,
                roster_source_sha256,
                str(artifact.directory),
                artifact.manifest_sha256,
            ):
                return False
            expected_counts = {
                "employee_count": len(expected_employee_rows),
                "day_count": len(expected_days),
                "punch_count": len(expected_punches),
                "missing_day_count": sum(row[12] for row in expected_days),
            }
            if any(type(run[key]) is not int or run[key] != value for key, value in expected_counts.items()):
                return False
            if any(verified_artifact.manifest.get(key) != value for key, value in {
                "employeeCount": expected_counts["employee_count"],
                "dayCount": expected_counts["day_count"],
                "punchCount": expected_counts["punch_count"],
                "missingDayCount": expected_counts["missing_day_count"],
                "timecardUrlCount": expected_counts["employee_count"],
            }.items()):
                return False
            employee_rows = self.db.execute(
                """SELECT employee_code, employee_name, timecard_url, period_total_hours,
                          approvals_json, attestations_json, meal_waivers_json, additional_rows_json,
                          html_path, html_sha256, json_path, json_sha256
                     FROM employee_timecards WHERE run_id=? ORDER BY employee_code""",
                (run["id"],),
            ).fetchall()
            actual_employee_rows = [
                (
                    row[0], row[1], row[2], _number(row[3]),
                    _stored_json(row[4]), _stored_json(row[5]), _stored_json(row[6]), _stored_json(row[7]),
                    row[8], row[9], row[10], row[11],
                )
                for row in employee_rows
            ]
            day_rows = self.db.execute(
                """SELECT employee_code, work_date, day_label, pay_code, allocation1, allocation2,
                          hours, total_hours, dollars, exception_text, waiver_checked, comments_json,
                          missing_punch, unresolved_slots_json
                     FROM days WHERE run_id=? ORDER BY employee_code, work_date""",
                (run["id"],),
            ).fetchall()
            actual_days = [
                (
                    row[0], row[1], row[2], row[3], row[4], row[5],
                    _nullable_number(row[6]),
                    _nullable_number(row[7]),
                    _nullable_number(row[8]),
                    row[9], _flag(row[10]) if row[10] is not None else None, _stored_json(row[11]), _flag(row[12]), _stored_json(row[13]),
                )
                for row in day_rows
            ]
            punch_rows = self.db.execute(
                """SELECT employee_code, work_date, ordinal, row_index, slot, kind, display_time,
                          actual_time, rounded_time, clock_name, clock_code, comment,
                          provenance_available, change_request_status, approved
                     FROM punches WHERE run_id=? ORDER BY employee_code, work_date, ordinal""",
                (run["id"],),
            ).fetchall()
            actual_punches = [
                (row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10], row[11], row[12], row[13], row[14])
                for row in punch_rows
            ]
            weekly_rows = self.db.execute(
                "SELECT employee_code, week_number, hours FROM weekly_totals WHERE run_id=? ORDER BY employee_code, week_number",
                (run["id"],),
            ).fetchall()
            actual_weekly = [(row[0], row[1], _number(row[2])) for row in weekly_rows]
            return actual_employee_rows == expected_employee_rows and actual_days == expected_days and actual_punches == expected_punches and actual_weekly == expected_weekly
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error):
            return False

    def status(self, period_end: str) -> dict[str, Any]:
        row = self.active_run(period_end)
        if row is None:
            return {"loaded": False, "periodEnd": period_end}
        return {"loaded": True, "periodStart": row["period_start"], "periodEnd": row["period_end"], "revision": row["revision"], "employees": row["employee_count"], "days": row["day_count"], "punches": row["punch_count"], "missingDays": row["missing_day_count"], "manifestSha256": row["manifest_sha256"]}

    def close(self) -> None:
        self.db.close()
        _secure_db(self.path)


__all__ = ["TimecardPublication", "TimecardStorageError", "TimecardStore"]
