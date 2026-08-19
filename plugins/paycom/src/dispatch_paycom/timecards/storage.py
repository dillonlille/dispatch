"""Atomic SQLite projection for complete Paycom timecard runs."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable
from uuid import uuid4

from .artifacts import TimecardArtifact, verify_artifact_run
from .period import Period, canonical_timecard_url, is_captured_timecard_url, parse_period_key, period_from_end, validate_code


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
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    details = path.parent.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise TimecardStorageError("storage_root_invalid")
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise TimecardStorageError("schema_invalid")
    if path.exists():
        os.chmod(path, 0o600)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class TimecardStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        _secure_db(self.path)
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
        verify_artifact_run(artifact.directory, [item["employeeCode"] for item in employees])
        if not isinstance(roster_revision, str) or not roster_revision or len(roster_revision) > 100 or not isinstance(roster_source_sha256, str) or len(roster_source_sha256) != 64:
            raise TimecardStorageError("timecard_storage_invalid")
        current = self.active_run(period.end)
        if current is not None and current["manifest_sha256"] == artifact.manifest_sha256:
            return TimecardPublication("already_current", str(current["run_id"]), int(current["employee_count"]), int(current["day_count"]), int(current["punch_count"]), int(current["missing_day_count"]))
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
        run = self.active_run(period_end)
        if run is None or run["roster_revision"] != roster_revision or run["roster_source_sha256"] != roster_source_sha256 or run["manifest_sha256"] != artifact.manifest_sha256 or int(run["employee_count"]) != len(employees):
            return False
        employee_count = self.db.execute("SELECT COUNT(*) AS n FROM employee_timecards WHERE run_id=?", (run["id"],)).fetchone()["n"]
        day_count = self.db.execute("SELECT COUNT(*) AS n FROM days WHERE run_id=?", (run["id"],)).fetchone()["n"]
        return int(employee_count) == len(employees) and int(day_count) == len(employees) * 14 and int(run["day_count"]) == int(day_count)

    def status(self, period_end: str) -> dict[str, Any]:
        row = self.active_run(period_end)
        if row is None:
            return {"loaded": False, "periodEnd": period_end}
        return {"loaded": True, "periodStart": row["period_start"], "periodEnd": row["period_end"], "revision": row["revision"], "employees": row["employee_count"], "days": row["day_count"], "punches": row["punch_count"], "missingDays": row["missing_day_count"], "manifestSha256": row["manifest_sha256"]}

    def close(self) -> None:
        self.db.close()
        _secure_db(self.path)


__all__ = ["TimecardPublication", "TimecardStorageError", "TimecardStore"]
