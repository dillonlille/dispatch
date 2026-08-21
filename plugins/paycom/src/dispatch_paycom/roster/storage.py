"""Atomic SQLite publication for complete Paycom roster snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3

from typing import Any
from uuid import uuid4

from .artifacts import RosterArtifact, verify_roster_artifact
from ..filesystem import FilesystemError, create_private_file, ensure_private_directory, validate_private_regular_file
from ..storage import StorageError, open_read_only


class RosterStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RosterPublication:
    disposition: str
    target: str
    run_id: str
    employee_count: int
    active_driver_count: int


@dataclass(frozen=True, slots=True)
class RosterBinding:
    """The immutable roster identity handed to a timecard collection."""

    revision: str
    source_sha256: str
    employees: tuple[tuple[str, str], ...]


_ROSTER_TABLES = ("collection_runs", "active_snapshots", "employees")
_ROSTER_COLUMNS = {
    "collection_runs": (
        "run_id",
        "target",
        "source_sha256",
        "employee_count",
        "active_employee_count",
    ),
    "active_snapshots": ("target", "run_id"),
    "employees": ("run_id", "employee_code", "employee_name", "is_active"),
}
_EMPLOYEE_FIELDS = (
    "employeeCode",
    "employeeName",
    "status",
    "departmentCode",
    "departmentDesc",
    "deliveryStationCode",
    "deliveryStationDesc",
    "positionTitle",
    "payClass",
    "terminalGroup",
    "payType",
    "primarySupervisor",
    "missingPunches",
    "totalHours",
    "totalOvertimeHours",
    "employeeApprovalPercentage",
    "supervisorApprovalPercentage",
    "isActive",
    "isDriverDepartment",
    "isDriverPosition",
    "isActiveDriver",
)
_EMPLOYEE_TEXT_FIELDS = _EMPLOYEE_FIELDS[:17]
_EMPLOYEE_BOOL_FIELDS = _EMPLOYEE_FIELDS[17:]


def _private_parent(path: Path) -> None:
    try:
        ensure_private_directory(path.parent)
    except FilesystemError as exc:
        raise RosterStorageError("storage_root_invalid") from exc


def _secure_db(path: Path) -> None:
    _private_parent(path)
    if path.exists() or path.is_symlink():
        try:
            validate_private_regular_file(path)
        except FilesystemError as exc:
            raise RosterStorageError("schema_invalid") from exc



def _normalized_employee(value: Any) -> tuple[Any, ...]:
    if not isinstance(value, dict):
        raise ValueError("roster_projection_invalid")
    try:
        code = value["employeeCode"]
        if not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9]{4}", code) is None:
            raise ValueError
        text = tuple(value[field] for field in _EMPLOYEE_TEXT_FIELDS[1:])
        if any(type(item) is not str for item in text):
            raise ValueError
        flags = tuple(value[field] for field in _EMPLOYEE_BOOL_FIELDS)
        if any(type(item) is not bool for item in flags):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("roster_projection_invalid") from exc
    return (code.upper(), *text, *flags)


def read_active_roster(path: Path | str) -> RosterBinding:
    """Read the active roster through an immutable, integrity-checked handle."""

    try:
        database = open_read_only(path, required_tables=_ROSTER_TABLES)
    except StorageError as exc:
        raise RosterStorageError(exc.code) from exc
    try:
        database.require_columns(_ROSTER_COLUMNS)
        if not database.quick_ok():
            raise RosterStorageError("roster_integrity_failed")
        active = database.execute(
            """SELECT a.target AS active_target, r.run_id, r.target, r.source_sha256,
                      r.employee_count, r.active_employee_count
                 FROM active_snapshots a JOIN collection_runs r ON r.run_id=a.run_id
                ORDER BY a.target DESC LIMIT 1"""
        ).fetchone()
        if active is None or active["active_target"] != active["target"]:
            raise RosterStorageError("roster_missing")
        revision = active["run_id"]
        source_sha256 = active["source_sha256"]
        if (
            type(revision) is not str
            or not revision
            or type(source_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
            or type(active["employee_count"]) is not int
            or type(active["active_employee_count"]) is not int
            or active["employee_count"] < 1
            or active["active_employee_count"] < 1
        ):
            raise RosterStorageError("roster_invalid")
        rows = database.execute(
            "SELECT employee_code, employee_name, is_active FROM employees WHERE run_id=? ORDER BY employee_code",
            (revision,),
        ).fetchall()
        if len(rows) != active["employee_count"]:
            raise RosterStorageError("roster_count_mismatch")
        codes: set[str] = set()
        active_employees: list[tuple[str, str]] = []
        for row in rows:
            code = row["employee_code"]
            name = row["employee_name"]
            if (
                type(code) is not str
                or re.fullmatch(r"[A-Za-z0-9]{4}", code) is None
                or code.upper() in codes
                or type(name) is not str
                or not name.strip()
                or type(row["is_active"]) is not int
                or row["is_active"] not in (0, 1)
            ):
                raise RosterStorageError("roster_invalid")
            normalized_code = code.upper()
            codes.add(normalized_code)
            if row["is_active"] == 1:
                active_employees.append((normalized_code, name))
        if len(active_employees) != active["active_employee_count"] or not active_employees:
            raise RosterStorageError("roster_count_mismatch")
        return RosterBinding(revision, source_sha256, tuple(active_employees))
    except StorageError as exc:
        raise RosterStorageError(exc.code) from exc
    finally:
        try:
            database.close()
        except StorageError as exc:
            raise RosterStorageError(exc.code) from exc


class RosterStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        _secure_db(self.path)
        if not self.path.exists():
            try:
                create_private_file(self.path)
            except FilesystemError as exc:
                raise RosterStorageError("schema_invalid") from exc
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
            CREATE TABLE IF NOT EXISTS collection_runs (
                run_id TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                employee_count INTEGER NOT NULL,
                active_employee_count INTEGER NOT NULL,
                active_driver_count INTEGER NOT NULL,
                UNIQUE(target, source_sha256)
            );
            CREATE TABLE IF NOT EXISTS employees (
                run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE RESTRICT,
                employee_code TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                status TEXT NOT NULL,
                department_code TEXT NOT NULL,
                department_desc TEXT NOT NULL,
                delivery_station_code TEXT NOT NULL,
                delivery_station_desc TEXT NOT NULL,
                position_title TEXT NOT NULL,
                pay_class TEXT NOT NULL,
                terminal_group TEXT NOT NULL,
                pay_type TEXT NOT NULL,
                primary_supervisor TEXT NOT NULL,
                missing_punches TEXT NOT NULL,
                total_hours TEXT NOT NULL,
                total_overtime_hours TEXT NOT NULL,
                employee_approval_percentage TEXT NOT NULL,
                supervisor_approval_percentage TEXT NOT NULL,
                is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
                is_driver_department INTEGER NOT NULL CHECK(is_driver_department IN (0, 1)),
                is_driver_position INTEGER NOT NULL CHECK(is_driver_position IN (0, 1)),
                is_active_driver INTEGER NOT NULL CHECK(is_active_driver IN (0, 1)),
                PRIMARY KEY(run_id, employee_code)
            );
            CREATE TABLE IF NOT EXISTS active_snapshots (
                target TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE REFERENCES collection_runs(run_id) ON DELETE RESTRICT,
                activated_at TEXT NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
        self.db.commit()

    def active_run(self, target: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT r.* FROM active_snapshots a JOIN collection_runs r ON r.run_id=a.run_id WHERE a.target=?",
            (target,),
        ).fetchone()

    def publish(self, *, target: str, collected_at: str, artifact: RosterArtifact, parsed: dict[str, Any], replace: bool = False) -> RosterPublication:
        verify_roster_artifact(artifact)
        if type(replace) is not bool:
            raise RosterStorageError("replace_invalid")
        employees = list(parsed.get("employees") or [])
        if not employees:
            raise RosterStorageError("roster_projection_invalid")
        try:
            normalized = [_normalized_employee(employee) for employee in employees]
            row_count = parsed["rowCount"]
            employee_count = parsed["employeeCount"]
            active_employee_count = parsed["activeEmployeeCount"]
            active_driver_count = parsed["activeDriverCount"]
            if (
                any(type(value) is not int or value < 0 for value in (row_count, employee_count, active_employee_count, active_driver_count))
                or employee_count != len(employees)
                or row_count < employee_count
                or active_employee_count != sum(item[17] for item in normalized)
                or active_driver_count != sum(item[20] for item in normalized)
                or len({item[0] for item in normalized}) != len(normalized)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise RosterStorageError("roster_projection_invalid") from exc
        current = self.active_run(target)
        if current is not None and current["source_sha256"] == artifact.source_sha256:
            publication = RosterPublication("already_current", target, str(current["run_id"]), int(current["employee_count"]), int(current["active_driver_count"]))
            if not self.verify_projection(target, parsed, artifact.source_sha256):
                raise RosterStorageError("post_verification_failed")
            return publication
        if current is not None and not replace:
            raise RosterStorageError("replacement_required")
        run_id = uuid4().hex
        try:
            self.db.execute("BEGIN IMMEDIATE")
            # Duplicate lookup must happen inside the write transaction so two
            # concurrent publishers cannot both see existing=None and collide
            # on the collection_runs primary key.
            existing = self.db.execute("SELECT run_id FROM collection_runs WHERE target=? AND source_sha256=?", (target, artifact.source_sha256)).fetchone()
            run_id = str(existing["run_id"]) if existing is not None else run_id
            if existing is None:
                self.db.execute(
                    "INSERT INTO collection_runs(run_id,target,collected_at,source_sha256,artifact_path,manifest_sha256,row_count,employee_count,active_employee_count,active_driver_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (run_id, target, collected_at, artifact.source_sha256, str(artifact.source_path), artifact.manifest_sha256, int(parsed["rowCount"]), len(employees), int(parsed["activeEmployeeCount"]), int(parsed["activeDriverCount"])),
                )
                for employee in employees:
                    self.db.execute(
                        "INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, employee["employeeCode"], employee["employeeName"], employee["status"], employee["departmentCode"], employee["departmentDesc"], employee["deliveryStationCode"], employee["deliveryStationDesc"], employee["positionTitle"], employee["payClass"], employee["terminalGroup"], employee["payType"], employee["primarySupervisor"], employee["missingPunches"], employee["totalHours"], employee["totalOvertimeHours"], employee["employeeApprovalPercentage"], employee["supervisorApprovalPercentage"], int(employee["isActive"]), int(employee["isDriverDepartment"]), int(employee["isDriverPosition"]), int(employee["isActiveDriver"])),
                    )
            self.db.execute(
                "INSERT INTO active_snapshots(target,run_id,activated_at) VALUES(?,?,?) ON CONFLICT(target) DO UPDATE SET run_id=excluded.run_id, activated_at=excluded.activated_at",
                (target, run_id, collected_at),
            )
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            raise RosterStorageError("publication_failed") from exc
        disposition = "replaced" if current is not None else "loaded"
        publication = RosterPublication(disposition, target, run_id, len(employees), int(parsed["activeDriverCount"]))
        if not self.verify_projection(target, parsed, artifact.source_sha256):
            raise RosterStorageError("post_verification_failed")
        return publication

    def verify_projection(self, target: str, parsed: dict[str, Any], source_sha256: str) -> bool:
        """Verify the active run against the complete normalized source projection."""
        try:
            active = self.active_run(target)
            if active is None or active["target"] != target or not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None or active["source_sha256"] != source_sha256:
                return False
            employees = parsed.get("employees")
            if not isinstance(employees, list) or not employees:
                return False
            expected = sorted(_normalized_employee(employee) for employee in employees)
            if len(expected) != int(parsed["employeeCount"]):
                return False
            if len({row[0] for row in expected}) != len(expected):
                return False
            expected_counts = {
                "row_count": parsed["rowCount"],
                "employee_count": parsed["employeeCount"],
                "active_employee_count": parsed["activeEmployeeCount"],
                "active_driver_count": parsed["activeDriverCount"],
            }
            if (
                type(expected_counts["row_count"]) is not int
                or type(expected_counts["employee_count"]) is not int
                or type(expected_counts["active_employee_count"]) is not int
                or type(expected_counts["active_driver_count"]) is not int
                or expected_counts["row_count"] < expected_counts["employee_count"]
                or expected_counts["employee_count"] < 1
                or expected_counts["active_employee_count"] < 0
                or expected_counts["active_driver_count"] < 0
            ):
                return False
            if any(active[key] != value for key, value in expected_counts.items()):
                return False
            rows = self.db.execute(
                """SELECT employee_code AS employeeCode, employee_name AS employeeName,
                          status, department_code AS departmentCode, department_desc AS departmentDesc,
                          delivery_station_code AS deliveryStationCode,
                          delivery_station_desc AS deliveryStationDesc,
                          position_title AS positionTitle, pay_class AS payClass,
                          terminal_group AS terminalGroup, pay_type AS payType,
                          primary_supervisor AS primarySupervisor, missing_punches AS missingPunches,
                          total_hours AS totalHours, total_overtime_hours AS totalOvertimeHours,
                          employee_approval_percentage AS employeeApprovalPercentage,
                          supervisor_approval_percentage AS supervisorApprovalPercentage,
                          is_active AS isActive, is_driver_department AS isDriverDepartment,
                          is_driver_position AS isDriverPosition, is_active_driver AS isActiveDriver
                     FROM employees WHERE run_id=? ORDER BY employee_code""",
                (active["run_id"],),
            ).fetchall()
            actual: list[tuple[Any, ...]] = []
            for row in rows:
                value = {field: row[field] for field in _EMPLOYEE_FIELDS}
                for field in _EMPLOYEE_BOOL_FIELDS:
                    if type(value[field]) is not int or value[field] not in (0, 1):
                        return False
                    value[field] = bool(value[field])
                actual.append(_normalized_employee(value))
            return actual == expected
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return False

    def status(self, target: str) -> dict[str, Any]:
        row = self.active_run(target)
        if row is None:
            return {"disposition": "missing", "target": target, "employeeCount": 0, "activeDriverCount": 0}
        return {"disposition": "loaded", "target": target, "runId": row["run_id"], "sourceSha256": row["source_sha256"], "employeeCount": row["employee_count"], "activeDriverCount": row["active_driver_count"]}

    def latest_active_employees(self, target: str | None = None) -> tuple[str, str, list[tuple[str, str]]]:
        if target is None:
            row = self.db.execute("SELECT target FROM active_snapshots ORDER BY target DESC LIMIT 1").fetchone()
            target = str(row["target"]) if row else ""
        active = self.active_run(target)
        if active is None:
            raise RosterStorageError("roster_missing")
        rows = self.db.execute("SELECT employee_code, employee_name FROM employees WHERE run_id=? AND is_active=1 ORDER BY employee_code", (active["run_id"],)).fetchall()
        if not rows:
            raise RosterStorageError("roster_invalid")
        return str(active["run_id"]), str(active["source_sha256"]), [(str(row["employee_code"]), str(row["employee_name"])) for row in rows]

    def close(self) -> None:
        self.db.close()
        _secure_db(self.path)


__all__ = ["RosterBinding", "RosterPublication", "RosterStorageError", "RosterStore", "read_active_roster"]
