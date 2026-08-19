"""Atomic SQLite publication for complete Paycom roster snapshots."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any
from uuid import uuid4

from .artifacts import RosterArtifact, verify_roster_artifact


class RosterStorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RosterPublication:
    disposition: str
    target: str
    run_id: str
    employee_count: int
    active_driver_count: int


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    details = path.parent.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise RosterStorageError("storage_root_invalid")


def _secure_db(path: Path) -> None:
    _private_parent(path)
    if path.exists() or path.is_symlink():
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_nlink != 1:
            raise RosterStorageError("schema_invalid")
    if path.exists():
        os.chmod(path, 0o600)


class RosterStore:
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
        current = self.active_run(target)
        if current is not None and current["source_sha256"] == artifact.source_sha256:
            return RosterPublication("already_current", target, str(current["run_id"]), int(current["employee_count"]), int(current["active_driver_count"]))
        if current is not None and not replace:
            raise RosterStorageError("replacement_required")
        existing = self.db.execute("SELECT run_id FROM collection_runs WHERE target=? AND source_sha256=?", (target, artifact.source_sha256)).fetchone()
        run_id = str(existing["run_id"]) if existing is not None else uuid4().hex
        employees = list(parsed.get("employees") or [])
        if not employees or int(parsed.get("employeeCount", -1)) != len(employees):
            raise RosterStorageError("roster_projection_invalid")
        try:
            self.db.execute("BEGIN IMMEDIATE")
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
        active = self.active_run(target)
        if active is None or active["source_sha256"] != source_sha256:
            return False
        count = self.db.execute("SELECT COUNT(*) AS n, COALESCE(SUM(is_active_driver),0) AS drivers FROM employees WHERE run_id=?", (active["run_id"],)).fetchone()
        return bool(count and int(count["n"]) == int(parsed["employeeCount"]) and int(count["drivers"]) == int(parsed["activeDriverCount"]))

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


__all__ = ["RosterPublication", "RosterStorageError", "RosterStore"]
