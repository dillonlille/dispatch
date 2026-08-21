"""Small transactional SQLite queue for Collection Manager."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from types import MappingProxyType
from typing import Mapping

from paths import DispatchPaths, PathConfigError, require_within


_ID = re.compile(r"^[0-9a-f]{32}$")
_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_PUBLICATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SCHEMA_VERSION = "1"
_TRANSIENT_BASE_SECONDS = 30
_MAX_RETRY_SECONDS = 900


class CollectionStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


_TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.UNCERTAIN}
_CLAIMABLE = {TaskState.QUEUED, TaskState.RETRY_WAIT}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    collector_id TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    not_before TEXT,
    worker_id TEXT,
    lease_expires_at TEXT,
    worker_pid INTEGER,
    worker_start_ticks INTEGER,
    worker_deadline_at TEXT,
    execution_started INTEGER NOT NULL,
    cancel_requested INTEGER NOT NULL,
    resume_requested INTEGER NOT NULL,
    last_error_code TEXT,
    receipt_json TEXT,
    publication_absence_verified_at TEXT,
    verified_absent_publication_id TEXT,
    idempotency_key TEXT UNIQUE,
    schedule_id TEXT,
    occurrence_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(schedule_id, occurrence_key)
);
CREATE INDEX IF NOT EXISTS tasks_ready
    ON tasks(state, not_before, created_at, task_id);
CREATE INDEX IF NOT EXISTS tasks_lease
    ON tasks(state, lease_expires_at);
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    collector_id TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    next_run_at TEXT NOT NULL,
    max_attempts INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due
    ON schedules(enabled, next_run_at, schedule_id);
"""
_METADATA_COLUMNS = {"key", "value"}
_TASK_COLUMNS = {
    "task_id", "collector_id", "account_alias", "parameters_json", "state",
    "attempt_count", "max_attempts", "not_before", "worker_id", "lease_expires_at",
    "worker_pid", "worker_start_ticks", "worker_deadline_at", "execution_started",
    "cancel_requested", "resume_requested", "last_error_code", "receipt_json",
    "publication_absence_verified_at", "verified_absent_publication_id",
    "idempotency_key", "schedule_id", "occurrence_key", "created_at", "updated_at",
}
_SCHEDULE_COLUMNS = {
    "schedule_id", "collector_id", "account_alias", "parameters_json", "interval_seconds",
    "next_run_at", "max_attempts", "enabled", "created_at", "updated_at",
}

_INTEGER_COLUMNS = {
    "tasks": {
        "attempt_count", "max_attempts", "worker_pid", "worker_start_ticks",
        "execution_started", "cancel_requested", "resume_requested",
    },
    "schedules": {"interval_seconds", "max_attempts", "enabled"},
}
_NULLABLE_COLUMNS = {
    "tasks": {
        "not_before", "worker_id", "lease_expires_at", "worker_pid", "worker_start_ticks",
        "worker_deadline_at", "last_error_code", "receipt_json",
        "publication_absence_verified_at", "verified_absent_publication_id",
        "idempotency_key", "schedule_id", "occurrence_key",
    },
    "schedules": set(),
    "metadata": set(),
}


def _verify_schema_constraints(connection: sqlite3.Connection) -> None:
    expected_columns = {
        "metadata": _METADATA_COLUMNS,
        "tasks": _TASK_COLUMNS,
        "schedules": _SCHEDULE_COLUMNS,
    }
    expected_primary = {"metadata": ("key",), "tasks": ("task_id",), "schedules": ("schedule_id",)}
    expected_unique = {
        "metadata": {("key",)},
        "tasks": {("task_id",), ("idempotency_key",), ("schedule_id", "occurrence_key")},
        "schedules": {("schedule_id",)},
    }
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_SCHEMA)
        expected_objects = reference.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    finally:
        reference.close()
    actual_objects = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    if [tuple(row) for row in actual_objects] != [tuple(row) for row in expected_objects]:
        raise CollectionStoreError("collection_state_corrupt", "collection schema objects are not approved")
    for table in expected_columns:
        info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if {row["name"] for row in info} != expected_columns[table]:
            raise CollectionStoreError("collection_state_corrupt", "collection schema columns are not approved")
        primary = tuple(row["name"] for row in sorted(info, key=lambda row: row["pk"]) if row["pk"])
        if primary != expected_primary[table]:
            raise CollectionStoreError("collection_state_corrupt", "collection schema primary key is invalid")
        for row in info:
            expected_type = "INTEGER" if row["name"] in _INTEGER_COLUMNS.get(table, set()) else "TEXT"
            if row["type"].upper() != expected_type:
                raise CollectionStoreError("collection_state_corrupt", "collection schema column type is invalid")
            if row["dflt_value"] is not None:
                raise CollectionStoreError("collection_state_corrupt", "collection schema default is invalid")
            if row["name"] not in _NULLABLE_COLUMNS[table] and not row["pk"] and row["notnull"] != 1:
                raise CollectionStoreError("collection_state_corrupt", "collection schema nullability is invalid")
        unique: set[tuple[str, ...]] = set()
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if not index["unique"]:
                continue
            name = index["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
                raise CollectionStoreError("collection_state_corrupt", "collection schema index is invalid")
            unique.add(tuple(row["name"] for row in connection.execute(f"PRAGMA index_info({name})")))
        if unique != expected_unique[table]:
            raise CollectionStoreError("collection_state_corrupt", "collection schema uniqueness is invalid")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectionStoreError("invalid_collection_time", f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format(value: datetime) -> str:
    return _aware(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CollectionStoreError("collection_state_corrupt", "stored timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CollectionStoreError("collection_state_corrupt", "stored timestamp is invalid") from exc
    return _aware(parsed, "stored timestamp")


def _json_mapping(value: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CollectionStoreError("collection_state_corrupt", "stored JSON is invalid") from exc
    if type(decoded) is not dict:
        raise CollectionStoreError("collection_state_corrupt", "stored parameters are invalid")
    for key, item in decoded.items():
        if not isinstance(key, str) or not _SLUG.fullmatch(key):
            raise CollectionStoreError("collection_state_corrupt", "stored parameter name is invalid")
        if isinstance(item, str):
            if not item or len(item) > 256 or "\x00" in item:
                raise CollectionStoreError("collection_state_corrupt", "stored parameter text is invalid")
        elif type(item) is int:
            if abs(item) > 1_000_000_000:
                raise CollectionStoreError("collection_state_corrupt", "stored parameter number is invalid")
        elif not isinstance(item, bool) and item is not None:
            raise CollectionStoreError("collection_state_corrupt", "stored parameter type is invalid")
    return MappingProxyType(decoded)


def _receipt_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    expected = {"disposition", "publication_id", "artifact_count", "domain_complete"}
    if type(value) is not dict or set(value) != expected:
        raise CollectionStoreError("collection_state_corrupt", "stored collection receipt is invalid")
    if value["disposition"] not in {"published", "skipped_existing", "no_data"}:
        raise CollectionStoreError("collection_state_corrupt", "stored receipt disposition is invalid")
    publication_id = value["publication_id"]
    if publication_id is not None and (
        not isinstance(publication_id, str) or not _PUBLICATION_ID.fullmatch(publication_id)
    ):
        raise CollectionStoreError("collection_state_corrupt", "stored publication identity is invalid")
    if value["disposition"] in {"published", "skipped_existing"} and publication_id is None:
        raise CollectionStoreError("collection_state_corrupt", "stored publication identity is missing")
    if type(value["artifact_count"]) is not int or not 0 <= value["artifact_count"] <= 1000:
        raise CollectionStoreError("collection_state_corrupt", "stored artifact count is invalid")
    if value["disposition"] == "published" and value["artifact_count"] == 0:
        raise CollectionStoreError("collection_state_corrupt", "stored published receipt lacks an artifact")
    if value["disposition"] == "skipped_existing" and value["artifact_count"] != 0:
        raise CollectionStoreError("collection_state_corrupt", "stored retained receipt reports new artifacts")
    if value["disposition"] == "no_data" and (publication_id is not None or value["artifact_count"] != 0):
        raise CollectionStoreError("collection_state_corrupt", "stored no-data receipt reports publication artifacts")
    if type(value["domain_complete"]) is not bool:
        raise CollectionStoreError("collection_state_corrupt", "stored completeness is invalid")
    return MappingProxyType(dict(value))


def _private_directory(path: Path, boundary: Path) -> Path:
    path = require_within(path, boundary, "collection database directory")
    if not boundary.exists():
        boundary.mkdir(parents=True, mode=0o700)
    boundary_details = boundary.stat(follow_symlinks=False)
    if (
        boundary.is_symlink()
        or not stat.S_ISDIR(boundary_details.st_mode)
        or boundary_details.st_uid != os.getuid()
        or boundary_details.st_mode & 0o077
    ):
        raise CollectionStoreError("unsafe_collection_storage", "collection data root is not private")
    current = boundary
    for part in path.relative_to(boundary).parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise CollectionStoreError("unsafe_collection_storage", "collection database path is unsafe")
        if not current.exists():
            current.mkdir(mode=0o700)
        details = current.stat(follow_symlinks=False)
        if details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise CollectionStoreError("unsafe_collection_storage", "collection database directory is not private")
    return path


def retry_delay(attempt_count: int) -> timedelta:
    bounded = min(max(attempt_count, 1), 10)
    seconds = min(_TRANSIENT_BASE_SECONDS * (2 ** (bounded - 1)), _MAX_RETRY_SECONDS)
    return timedelta(seconds=seconds)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    collector_id: str
    account_alias: str
    parameters: Mapping[str, object]
    state: TaskState
    attempt_count: int
    max_attempts: int
    not_before: datetime | None
    worker_id: str | None
    lease_expires_at: datetime | None
    worker_pid: int | None
    worker_start_ticks: int | None
    worker_deadline_at: datetime | None
    execution_started: bool
    cancel_requested: bool
    resume_requested: bool
    last_error_code: str | None
    receipt: Mapping[str, object] | None
    publication_absence_verified_at: datetime | None
    verified_absent_publication_id: str | None
    idempotency_key: str | None
    schedule_id: str | None
    occurrence_key: str | None
    created_at: datetime
    updated_at: datetime

    def safe_data(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "collector_id": self.collector_id,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "not_before": None if self.not_before is None else _format(self.not_before),
            "cancel_requested": self.cancel_requested,
            "resume_requested": self.resume_requested,
            "last_error_code": self.last_error_code,
            "receipt": None if self.receipt is None else dict(self.receipt),
            "publication_absence_verified_at": (
                None
                if self.publication_absence_verified_at is None
                else _format(self.publication_absence_verified_at)
            ),
            "verified_absent_publication_id": self.verified_absent_publication_id,
            "schedule_id": self.schedule_id,
            "created_at": _format(self.created_at),
            "updated_at": _format(self.updated_at),
        }


_VERIFICATION_AUTHORITY = object()


@dataclass(frozen=True)
class _PublicationAbsenceVerification:
    task_id: str
    collector_id: str
    publication_id: str | None
    updated_at: datetime
    verified_at: datetime
    authority: object


def _confirmed_publication_absence(task: TaskRecord, at: datetime) -> _PublicationAbsenceVerification:
    at = _aware(at, "publication verification time")
    return _PublicationAbsenceVerification(
        task.task_id,
        task.collector_id,
        None if task.receipt is None else task.receipt.get("publication_id"),
        task.updated_at,
        at,
        _VERIFICATION_AUTHORITY,
    )


@dataclass(frozen=True)
class ScheduleRecord:
    schedule_id: str
    collector_id: str
    account_alias: str
    parameters: Mapping[str, object]
    interval_seconds: int
    next_run_at: datetime
    max_attempts: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def safe_data(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "collector_id": self.collector_id,
            "interval_seconds": self.interval_seconds,
            "next_run_at": _format(self.next_run_at),
            "max_attempts": self.max_attempts,
            "enabled": self.enabled,
        }


class CollectionTaskStore:
    """One-connection-per-transaction durable task and schedule storage."""

    def __init__(self, database: Path, *, sqlite_timeout_seconds: float = 5.0) -> None:
        if not 0 <= sqlite_timeout_seconds <= 5:
            raise ValueError("SQLite timeout must be between zero and five seconds")
        self.database = database
        self._sqlite_timeout_seconds = sqlite_timeout_seconds
        self._validate_location()
        self._initialise()

    @classmethod
    def from_paths(cls, paths: DispatchPaths) -> "CollectionTaskStore":
        database = cls.database_path(paths)
        _private_directory(database.parent, paths.data)
        return cls(database)

    @staticmethod
    def database_path(paths: DispatchPaths) -> Path:
        try:
            return require_within(
                paths.data / "db" / "collection-manager" / "collection-manager.sqlite3",
                paths.data,
                "collection manager database",
            )
        except PathConfigError as exc:
            raise CollectionStoreError("unsafe_collection_storage", "collection database path is unsafe") from exc

    @classmethod
    def inspect_paths(cls, paths: DispatchPaths) -> dict[str, object]:
        """Inspect durable storage without creating or changing private paths."""
        database = cls.database_path(paths)
        directories = [paths.data]
        current = paths.data
        for part in database.parent.relative_to(paths.data).parts:
            current /= part
            directories.append(current)
        for current in directories:
            if current.is_symlink():
                raise CollectionStoreError("unsafe_collection_storage", "collection database path is not private")
            if not current.exists():
                return {
                    "ready": True,
                    "status": "empty",
                    "tasks": {},
                    "schedules": 0,
                    "workers": 0,
                    "overdue_workers": 0,
                }
            details = current.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid()
                or details.st_mode & 0o077
            ):
                raise CollectionStoreError("unsafe_collection_storage", "collection database path is not private")
        if database.is_symlink():
            raise CollectionStoreError("unsafe_collection_storage", "collection database is not a private regular file")
        if not database.exists():
            return {
                "ready": True,
                "status": "empty",
                "tasks": {},
                "schedules": 0,
                "workers": 0,
                "overdue_workers": 0,
            }
        details = database.stat(follow_symlinks=False)
        if (
            database.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
        ):
            raise CollectionStoreError("unsafe_collection_storage", "collection database is not a private regular file")
        try:
            connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
            connection.row_factory = sqlite3.Row
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version is None:
                raise CollectionStoreError("collection_state_corrupt", "collection schema version is missing")
            if version[0] != _SCHEMA_VERSION:
                raise CollectionStoreError("unsupported_collection_schema", "collection schema version is unsupported")
            task_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")}
            schedule_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(schedules)")}
            if task_columns != _TASK_COLUMNS or schedule_columns != _SCHEDULE_COLUMNS:
                raise CollectionStoreError("collection_state_corrupt", "collection schema is not approved")
            _verify_schema_constraints(connection)
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise CollectionStoreError("collection_state_corrupt", "collection database integrity check failed")
            counts = {state.value: 0 for state in TaskState}
            workers = 0
            overdue_workers = 0
            inspected_at = utc_now()
            for row in connection.execute("SELECT * FROM tasks"):
                task = cls._task(row)
                counts[task.state.value] += 1
                workers += int(task.worker_pid is not None)
                if task.worker_pid is not None:
                    active = task.state in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}
                    deadline_overdue = (
                        task.worker_deadline_at is not None
                        and task.worker_deadline_at <= inspected_at
                    )
                    lease_overdue = (
                        active
                        and task.lease_expires_at is not None
                        and task.lease_expires_at <= inspected_at
                    )
                    overdue_workers += int(deadline_overdue or lease_overdue)
            schedule_rows = connection.execute("SELECT * FROM schedules").fetchall()
            for row in schedule_rows:
                cls._schedule(row)
            schedules = len(schedule_rows)
        except CollectionStoreError:
            raise
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            raise CollectionStoreError("collection_state_corrupt", "collection database is invalid") from exc
        finally:
            if "connection" in locals():
                connection.close()
        return {
            "ready": overdue_workers == 0,
            "status": "ready" if overdue_workers == 0 else "reconciliation_required",
            "tasks": counts,
            "schedules": schedules,
            "workers": workers,
            "overdue_workers": overdue_workers,
        }

    def _validate_location(self) -> None:
        parent = self.database.parent
        if self.database.is_symlink() or parent.is_symlink() or not parent.is_dir():
            raise CollectionStoreError("unsafe_collection_storage", "collection database location is unsafe")
        details = parent.stat(follow_symlinks=False)
        if details.st_uid != os.getuid() or not stat.S_ISDIR(details.st_mode) or details.st_mode & 0o077:
            raise CollectionStoreError("unsafe_collection_storage", "collection database directory is not private")
        if self.database.exists():
            details = self.database.stat(follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.getuid():
                raise CollectionStoreError("unsafe_collection_storage", "collection database is not a private regular file")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=self._sqlite_timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._sqlite_timeout_seconds * 1000)}")
        return connection

    def _initialise(self) -> None:
        try:
            connection = self._connect()
        except sqlite3.DatabaseError as exc:
            raise CollectionStoreError("collection_state_corrupt", "collection database is invalid") from exc
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if exists is not None:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if version is None:
                    raise CollectionStoreError("collection_state_corrupt", "collection schema version is missing")
                if version[0] != _SCHEMA_VERSION:
                    raise CollectionStoreError("unsupported_collection_schema", "collection schema version is unsupported")
            else:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO metadata(key,value) VALUES('schema_version',?)",
                    (_SCHEMA_VERSION,),
                )
            _verify_schema_constraints(connection)
            connection.commit()
        except CollectionStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CollectionStoreError("collection_state_corrupt", "collection database is invalid") from exc
        finally:
            connection.close()
        self._validate_location()
        os.chmod(self.database, 0o600)

    @staticmethod
    def _task(value: sqlite3.Row | None) -> TaskRecord:
        if value is None:
            raise CollectionStoreError("unknown_collection_task", "collection task does not exist")
        try:
            task_id = str(value["task_id"])
            collector_id = str(value["collector_id"])
            account_alias = str(value["account_alias"])
            if not _ID.fullmatch(task_id) or not _SLUG.fullmatch(collector_id) or not _SLUG.fullmatch(account_alias):
                raise ValueError("invalid identity")
            receipt_value = value["receipt_json"]
            receipt = None if receipt_value is None else json.loads(receipt_value)
            if receipt is not None:
                receipt = _receipt_mapping(receipt)
            created_at = _parse(value["created_at"])
            updated_at = _parse(value["updated_at"])
            if created_at is None or updated_at is None:
                raise ValueError("missing timestamp")
            row = TaskRecord(
                task_id=task_id,
                collector_id=collector_id,
                account_alias=account_alias,
                parameters=_json_mapping(value["parameters_json"]),
                state=TaskState(value["state"]),
                attempt_count=int(value["attempt_count"]),
                max_attempts=int(value["max_attempts"]),
                not_before=_parse(value["not_before"]),
                worker_id=value["worker_id"],
                lease_expires_at=_parse(value["lease_expires_at"]),
                worker_pid=None if value["worker_pid"] is None else int(value["worker_pid"]),
                worker_start_ticks=(
                    None if value["worker_start_ticks"] is None else int(value["worker_start_ticks"])
                ),
                worker_deadline_at=_parse(value["worker_deadline_at"]),
                execution_started=bool(value["execution_started"]),
                cancel_requested=bool(value["cancel_requested"]),
                resume_requested=bool(value["resume_requested"]),
                last_error_code=value["last_error_code"],
                receipt=receipt,
                publication_absence_verified_at=_parse(value["publication_absence_verified_at"]),
                verified_absent_publication_id=value["verified_absent_publication_id"],
                idempotency_key=value["idempotency_key"],
                schedule_id=value["schedule_id"],
                occurrence_key=value["occurrence_key"],
                created_at=created_at,
                updated_at=updated_at,
            )
            if not 0 <= row.attempt_count <= row.max_attempts <= 10:
                raise ValueError("invalid attempts")
            if (
                value["execution_started"] not in {0, 1}
                or value["cancel_requested"] not in {0, 1}
                or value["resume_requested"] not in {0, 1}
            ):
                raise ValueError("invalid boolean")
            if row.worker_id is not None and not _SLUG.fullmatch(row.worker_id):
                raise ValueError("invalid worker")
            if row.last_error_code is not None and not _KEY.fullmatch(row.last_error_code):
                raise ValueError("invalid error")
            if row.verified_absent_publication_id is not None and not _PUBLICATION_ID.fullmatch(
                row.verified_absent_publication_id
            ):
                raise ValueError("invalid verified publication identity")
            if (
                row.verified_absent_publication_id is not None
                and row.publication_absence_verified_at is None
            ):
                raise ValueError("verified publication identity lacks timestamp")
            if (
                row.publication_absence_verified_at is not None
                and row.publication_absence_verified_at > row.updated_at
            ):
                raise ValueError("publication verification is newer than task")
            if row.idempotency_key is not None and not _KEY.fullmatch(row.idempotency_key):
                raise ValueError("invalid idempotency key")
            if row.schedule_id is not None and not _ID.fullmatch(row.schedule_id):
                raise ValueError("invalid schedule identity")
            if row.occurrence_key is not None and not _KEY.fullmatch(row.occurrence_key):
                raise ValueError("invalid occurrence identity")
            if (row.schedule_id is None) != (row.occurrence_key is None):
                raise ValueError("incomplete schedule occurrence")
            active_state = row.state in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}
            if active_state:
                if row.worker_id is None or row.lease_expires_at is None:
                    raise ValueError("active task lacks lease")
            elif row.lease_expires_at is not None:
                raise ValueError("inactive task has lease")
            process_identity = (row.worker_pid, row.worker_start_ticks, row.worker_deadline_at)
            if any(item is not None for item in process_identity):
                if any(item is None for item in process_identity):
                    raise ValueError("worker process identity is incomplete")
                if row.worker_id is None:
                    raise ValueError("worker process lacks worker identity")
                if row.worker_pid <= 1 or row.worker_start_ticks <= 0:
                    raise ValueError("worker process identity is invalid")
            elif not active_state and row.worker_id is not None:
                raise ValueError("inactive task has worker identity")
            if row.state == TaskState.RETRY_WAIT and row.not_before is None:
                raise ValueError("retry lacks readiness time")
            if row.state not in {TaskState.QUEUED, TaskState.RETRY_WAIT} and row.not_before is not None:
                raise ValueError("unexpected readiness time")
            if row.state == TaskState.SUCCEEDED and row.receipt is None:
                raise ValueError("success lacks receipt")
            if row.receipt is not None and row.state not in {TaskState.SUCCEEDED, TaskState.FAILED}:
                raise ValueError("unexpected receipt")
            if row.state == TaskState.SUCCEEDED and row.last_error_code is not None:
                raise ValueError("success has error")
            if row.state in {TaskState.RETRY_WAIT, TaskState.FAILED, TaskState.CANCELLED, TaskState.UNCERTAIN} and row.last_error_code is None:
                raise ValueError("state lacks error")
            if row.state == TaskState.QUEUED and row.last_error_code is not None:
                raise ValueError("queued task has error")
            if row.state == TaskState.WAITING_FOR_USER and row.execution_started:
                raise ValueError("manual task already started")
            if row.cancel_requested and row.state not in {TaskState.RUNNING, TaskState.WAITING_FOR_USER}:
                raise ValueError("inactive task has cancellation request")
            if row.resume_requested and (
                row.state != TaskState.WAITING_FOR_USER or row.cancel_requested
            ):
                raise ValueError("task has invalid resume request")
            return row
        except (CollectionStoreError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CollectionStoreError("collection_state_corrupt", "stored collection task is invalid") from exc

    @staticmethod
    def _schedule(value: sqlite3.Row | None) -> ScheduleRecord:
        if value is None:
            raise CollectionStoreError("unknown_collection_schedule", "collection schedule does not exist")
        try:
            schedule_id = str(value["schedule_id"])
            collector_id = str(value["collector_id"])
            account_alias = str(value["account_alias"])
            if not _ID.fullmatch(schedule_id) or not _SLUG.fullmatch(collector_id) or not _SLUG.fullmatch(account_alias):
                raise ValueError("invalid identity")
            next_run_at = _parse(value["next_run_at"])
            created_at = _parse(value["created_at"])
            updated_at = _parse(value["updated_at"])
            if next_run_at is None or created_at is None or updated_at is None:
                raise ValueError("missing timestamp")
            row = ScheduleRecord(
                schedule_id=schedule_id,
                collector_id=collector_id,
                account_alias=account_alias,
                parameters=_json_mapping(value["parameters_json"]),
                interval_seconds=int(value["interval_seconds"]),
                next_run_at=next_run_at,
                max_attempts=int(value["max_attempts"]),
                enabled=bool(value["enabled"]),
                created_at=created_at,
                updated_at=updated_at,
            )
            if not 60 <= row.interval_seconds <= 31_536_000 or not 1 <= row.max_attempts <= 10:
                raise ValueError("invalid schedule")
            if value["enabled"] not in {0, 1}:
                raise ValueError("invalid schedule boolean")
            return row
        except (CollectionStoreError, KeyError, TypeError, ValueError) as exc:
            raise CollectionStoreError("collection_state_corrupt", "stored collection schedule is invalid") from exc

    def get(self, task_id: str) -> TaskRecord:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        finally:
            connection.close()
        return self._task(row)

    def counts(self) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT state,COUNT(*) AS amount FROM tasks GROUP BY state").fetchall()
        finally:
            connection.close()
        counts = {state.value: 0 for state in TaskState}
        for row in rows:
            counts[TaskState(row["state"]).value] = int(row["amount"])
        return counts

    def recent(self, limit: int = 50) -> list[TaskRecord]:
        bounded = min(max(limit, 1), 200)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC,task_id DESC LIMIT ?", (bounded,)
            ).fetchall()
        finally:
            connection.close()
        return [self._task(row) for row in rows]

    def enqueue(
        self,
        *,
        collector_id: str,
        account_alias: str,
        parameters: Mapping[str, object],
        max_attempts: int,
        at: datetime,
        not_before: datetime | None = None,
        idempotency_key: str | None = None,
        schedule_id: str | None = None,
        occurrence_key: str | None = None,
    ) -> TaskRecord:
        at = _aware(at, "enqueue time")
        if not _SLUG.fullmatch(collector_id) or not _SLUG.fullmatch(account_alias):
            raise CollectionStoreError("invalid_collection_request", "collection identity is invalid")
        if not 1 <= max_attempts <= 10:
            raise CollectionStoreError("invalid_collection_request", "max_attempts is invalid")
        if idempotency_key is not None and not _KEY.fullmatch(idempotency_key):
            raise CollectionStoreError("invalid_collection_request", "idempotency key is invalid")
        if (schedule_id is None) != (occurrence_key is None):
            raise CollectionStoreError("invalid_collection_request", "schedule occurrence is incomplete")
        if schedule_id is not None and (
            not _ID.fullmatch(schedule_id) or not _KEY.fullmatch(occurrence_key)
        ):
            raise CollectionStoreError("invalid_collection_request", "schedule occurrence is invalid")
        task_id = secrets.token_hex(16)
        parameters_json = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"))
        _json_mapping(parameters_json)
        eligible_at = None if not_before is None else _aware(not_before, "not_before")
        if eligible_at is not None and eligible_at <= at:
            eligible_at = None
        state = TaskState.QUEUED
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id,collector_id,account_alias,parameters_json,state,attempt_count,max_attempts,
                    not_before,worker_id,lease_expires_at,worker_pid,worker_start_ticks,worker_deadline_at,
                    execution_started,cancel_requested,resume_requested,
                    last_error_code,receipt_json,idempotency_key,schedule_id,occurrence_key,created_at,updated_at
                ) VALUES(?,?,?,?,?,0,?,?,NULL,NULL,NULL,NULL,NULL,0,0,0,NULL,NULL,?,?,?,?,?)
                """,
                (
                    task_id, collector_id, account_alias, parameters_json, state.value, max_attempts,
                    None if eligible_at is None else _format(eligible_at), idempotency_key, schedule_id,
                    occurrence_key, _format(at), _format(at),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            if idempotency_key is not None:
                row = connection.execute("SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            elif schedule_id is not None and occurrence_key is not None:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE schedule_id=? AND occurrence_key=?",
                    (schedule_id, occurrence_key),
                ).fetchone()
            else:
                row = None
            if row is None:
                raise CollectionStoreError("collection_task_collision", "collection task identity collided")
            existing = self._task(row)
            if idempotency_key is not None and (
                existing.collector_id != collector_id
                or existing.account_alias != account_alias
                or dict(existing.parameters) != dict(parameters)
                or existing.max_attempts != max_attempts
            ):
                raise CollectionStoreError("collection_idempotency_conflict", "idempotency key has different work")
            return existing
        finally:
            connection.close()
        return self.get(task_id)

    def claim(
        self,
        worker_id: str,
        at: datetime,
        lease_seconds: int = 900,
        collector_ids: tuple[str, ...] | None = None,
    ) -> TaskRecord | None:
        at = _aware(at, "claim time")
        if not _SLUG.fullmatch(worker_id) or not 30 <= lease_seconds <= 3600:
            raise CollectionStoreError("invalid_collection_worker", "worker lease is invalid")
        if collector_ids is not None:
            if len(collector_ids) > 256 or any(not _SLUG.fullmatch(value) for value in collector_ids):
                raise CollectionStoreError("invalid_collection_worker", "worker collector set is invalid")
            if not collector_ids:
                return None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            collector_clause = "" if collector_ids is None else (
                f" AND collector_id IN ({','.join('?' for _ in collector_ids)})"
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE state IN (?,?) AND worker_pid IS NULL AND "
                f"(not_before IS NULL OR not_before<=?){collector_clause} "
                "ORDER BY COALESCE(not_before,created_at),created_at,task_id LIMIT 1",
                (TaskState.QUEUED.value, TaskState.RETRY_WAIT.value, _format(at), *(collector_ids or ())),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            current = self._task(row)
            connection.execute(
                """
                UPDATE tasks SET state=?,attempt_count=attempt_count+1,not_before=NULL,worker_id=?,
                    lease_expires_at=?,worker_pid=NULL,worker_start_ticks=NULL,worker_deadline_at=NULL,
                    execution_started=0,cancel_requested=0,resume_requested=0,last_error_code=NULL,
                    receipt_json=NULL,updated_at=? WHERE task_id=? AND state=?
                """,
                (
                    TaskState.RUNNING.value, worker_id, _format(at + timedelta(seconds=lease_seconds)),
                    _format(at), current.task_id, current.state.value,
                ),
            )
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(current.task_id)

    def mark_execution_started(self, task_id: str, worker_id: str, at: datetime) -> TaskRecord:
        return self._active_update(
            task_id, worker_id, at,
            "UPDATE tasks SET execution_started=1,updated_at=? WHERE task_id=? AND state=? AND worker_id=? AND execution_started=0 AND cancel_requested=0",
        )

    def attach_worker_process(
        self,
        task_id: str,
        worker_id: str,
        pid: int,
        process_start_ticks: int,
        deadline_at: datetime,
        at: datetime,
    ) -> TaskRecord:
        at = _aware(at, "worker attachment time")
        deadline_at = _aware(deadline_at, "worker deadline")
        if type(pid) is not int or pid <= 1 or type(process_start_ticks) is not int or process_start_ticks <= 0:
            raise CollectionStoreError("invalid_collection_worker", "worker process identity is invalid")
        if deadline_at <= at:
            raise CollectionStoreError("invalid_collection_worker", "worker deadline is invalid")
        return self._active_update(
            task_id,
            worker_id,
            at,
            "UPDATE tasks SET worker_pid=?,worker_start_ticks=?,worker_deadline_at=?,updated_at=? "
            "WHERE task_id=? AND state=? AND worker_id=? AND execution_started=0 "
            "AND worker_pid IS NULL AND worker_start_ticks IS NULL AND worker_deadline_at IS NULL",
            (
                pid,
                process_start_ticks,
                _format(deadline_at),
                _format(at),
                task_id,
                TaskState.RUNNING.value,
                worker_id,
            ),
        )

    def heartbeat(self, task_id: str, worker_id: str, at: datetime, lease_seconds: int = 900) -> TaskRecord:
        at = _aware(at, "heartbeat time")
        if not 30 <= lease_seconds <= 3600:
            raise CollectionStoreError("invalid_collection_worker", "worker lease is invalid")
        return self._active_update(
            task_id, worker_id, at,
            "UPDATE tasks SET lease_expires_at=?,updated_at=? WHERE task_id=? AND state IN (?,?) AND worker_id=?",
            (_format(at + timedelta(seconds=lease_seconds)), _format(at), task_id,
             TaskState.RUNNING.value, TaskState.WAITING_FOR_USER.value, worker_id),
            select_task=task_id,
        )

    def update_worker_deadline(
        self,
        task_id: str,
        worker_id: str,
        pid: int,
        process_start_ticks: int,
        deadline_at: datetime,
        at: datetime,
    ) -> TaskRecord:
        at = _aware(at, "worker deadline update time")
        deadline_at = _aware(deadline_at, "worker deadline")
        if deadline_at <= at:
            raise CollectionStoreError("invalid_collection_worker", "worker deadline is invalid")
        return self._active_update(
            task_id,
            worker_id,
            at,
            "UPDATE tasks SET worker_deadline_at=?,updated_at=? WHERE task_id=? AND state IN (?,?) "
            "AND worker_id=? AND worker_pid=? AND worker_start_ticks=?",
            (
                _format(deadline_at),
                _format(at),
                task_id,
                TaskState.RUNNING.value,
                TaskState.WAITING_FOR_USER.value,
                worker_id,
                pid,
                process_start_ticks,
            ),
            select_task=task_id,
        )

    def clear_worker_process(
        self,
        task_id: str,
        worker_id: str,
        pid: int,
        process_start_ticks: int,
        at: datetime,
    ) -> TaskRecord:
        at = _aware(at, "worker cleanup time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tasks SET worker_id=NULL,worker_pid=NULL,worker_start_ticks=NULL,"
                "worker_deadline_at=NULL,updated_at=? WHERE task_id=? AND state NOT IN (?,?) "
                "AND worker_id=? AND worker_pid=? AND worker_start_ticks=?",
                (
                    _format(at),
                    task_id,
                    TaskState.RUNNING.value,
                    TaskState.WAITING_FOR_USER.value,
                    worker_id,
                    pid,
                    process_start_ticks,
                ),
            )
            if cursor.rowcount != 1:
                raise CollectionStoreError("collection_state_conflict", "worker process cleanup did not match")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def _active_update(
        self,
        task_id: str,
        worker_id: str,
        at: datetime,
        statement: str,
        arguments: tuple[object, ...] | None = None,
        *,
        select_task: str | None = None,
    ) -> TaskRecord:
        at = _aware(at, "task update time")
        if not _SLUG.fullmatch(worker_id):
            raise CollectionStoreError("invalid_collection_worker", "worker identity is invalid")
        values = arguments or (_format(at), task_id, TaskState.RUNNING.value, worker_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(statement, values)
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(select_task or task_id)

    def wait_for_user(self, task_id: str, worker_id: str, status: str, at: datetime) -> TaskRecord:
        if status not in {"mfa_required", "captcha_required"}:
            raise CollectionStoreError("invalid_collection_transition", "manual status is invalid")
        return self._active_update(
            task_id, worker_id, at,
            "UPDATE tasks SET state=?,resume_requested=0,last_error_code=?,updated_at=? WHERE task_id=? AND state=? AND worker_id=? AND execution_started=0 AND cancel_requested=0",
            (TaskState.WAITING_FOR_USER.value, status, _format(at), task_id, TaskState.RUNNING.value, worker_id),
        )

    def request_resume(self, task_id: str, at: datetime) -> TaskRecord:
        at = _aware(at, "resume request time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._task(connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
            if row.state != TaskState.WAITING_FOR_USER or row.cancel_requested:
                raise CollectionStoreError("invalid_collection_transition", "collection task is not resumable")
            if row.resume_requested:
                connection.commit()
                return row
            connection.execute(
                "UPDATE tasks SET resume_requested=1,updated_at=? WHERE task_id=? AND state=? "
                "AND cancel_requested=0 AND resume_requested=0",
                (_format(at), task_id, TaskState.WAITING_FOR_USER.value),
            )
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def resume_waiting(self, task_id: str, worker_id: str, at: datetime, lease_seconds: int = 900) -> TaskRecord:
        at = _aware(at, "resume time")
        return self._active_update(
            task_id, worker_id, at,
            "UPDATE tasks SET state=?,lease_expires_at=?,resume_requested=0,last_error_code=NULL,updated_at=? WHERE task_id=? AND state=? AND worker_id=? AND cancel_requested=0 AND resume_requested=1",
            (TaskState.RUNNING.value, _format(at + timedelta(seconds=lease_seconds)), _format(at),
             task_id, TaskState.WAITING_FOR_USER.value, worker_id),
        )

    def finish(
        self,
        task_id: str,
        worker_id: str,
        state: TaskState,
        at: datetime,
        *,
        error_code: str | None = None,
        receipt: Mapping[str, object] | None = None,
        not_before: datetime | None = None,
    ) -> TaskRecord:
        at = _aware(at, "finish time")
        if state not in _TERMINAL | {TaskState.RETRY_WAIT}:
            raise CollectionStoreError("invalid_collection_transition", "finish state is invalid")
        if state == TaskState.RETRY_WAIT:
            if not_before is None or error_code is None:
                raise CollectionStoreError("invalid_collection_transition", "retry requires time and error code")
        elif not_before is not None:
            raise CollectionStoreError("invalid_collection_transition", "terminal collection cannot have retry time")
        if error_code is not None and (not isinstance(error_code, str) or not _KEY.fullmatch(error_code)):
            raise CollectionStoreError("invalid_collection_transition", "collection error code is invalid")
        if state == TaskState.SUCCEEDED:
            if receipt is None or error_code is not None:
                raise CollectionStoreError("invalid_collection_transition", "successful collection requires only a receipt")
        elif error_code is None:
            raise CollectionStoreError("invalid_collection_transition", "unsuccessful collection requires an error code")
        if receipt is not None and state not in {TaskState.SUCCEEDED, TaskState.FAILED}:
            raise CollectionStoreError("invalid_collection_transition", "collection state cannot retain a receipt")
        try:
            receipt_data = None if receipt is None else _receipt_mapping(dict(receipt))
        except CollectionStoreError as exc:
            raise CollectionStoreError("invalid_collection_transition", "collection receipt is invalid") from exc
        receipt_json = None if receipt_data is None else json.dumps(dict(receipt_data), sort_keys=True, separators=(",", ":"))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._task(connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
            if row.state not in {TaskState.RUNNING, TaskState.WAITING_FOR_USER} or row.worker_id != worker_id:
                raise CollectionStoreError("collection_state_conflict", "worker does not own collection task")
            if state == TaskState.SUCCEEDED and (not row.execution_started or receipt_data is None):
                raise CollectionStoreError("invalid_collection_transition", "successful collection requires a receipt")
            if row.execution_started and state in {TaskState.RETRY_WAIT, TaskState.CANCELLED}:
                raise CollectionStoreError("invalid_collection_transition", "started collection cannot be retried or cancelled")
            if row.execution_started and state == TaskState.FAILED and receipt_data is None:
                raise CollectionStoreError("invalid_collection_transition", "started collection failure is uncertain")
            connection.execute(
                """
                UPDATE tasks SET state=?,not_before=?,
                    worker_id=CASE WHEN worker_pid IS NULL THEN NULL ELSE worker_id END,
                    lease_expires_at=NULL,
                    execution_started=0,cancel_requested=0,resume_requested=0,
                    last_error_code=?,receipt_json=?,updated_at=?
                WHERE task_id=? AND state=? AND worker_id=?
                """,
                (
                    state.value, None if not_before is None else _format(not_before), error_code,
                    receipt_json, _format(at), task_id, row.state.value, worker_id,
                ),
            )
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def request_cancel(self, task_id: str, at: datetime) -> TaskRecord:
        at = _aware(at, "cancel time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._task(connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
            if row.state in _TERMINAL:
                connection.commit()
                return row
            if row.state in _CLAIMABLE:
                connection.execute(
                    "UPDATE tasks SET state=?,not_before=NULL,resume_requested=0,last_error_code='cancelled',updated_at=? WHERE task_id=? AND state=?",
                    (TaskState.CANCELLED.value, _format(at), task_id, row.state.value),
                )
            else:
                connection.execute(
                    "UPDATE tasks SET cancel_requested=1,resume_requested=0,updated_at=? WHERE task_id=? AND state=?",
                    (_format(at), task_id, row.state.value),
                )
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def retry(
        self,
        task_id: str,
        at: datetime,
        *,
        verification: _PublicationAbsenceVerification | None = None,
    ) -> TaskRecord:
        at = _aware(at, "retry time")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._task(connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())
            if row.state not in {TaskState.FAILED, TaskState.CANCELLED, TaskState.UNCERTAIN}:
                raise CollectionStoreError("invalid_collection_transition", "collection task cannot be retried")
            if row.worker_pid is not None:
                raise CollectionStoreError("collection_worker_cleanup_pending", "worker cleanup is not complete")
            publication_id = None if row.receipt is None else row.receipt.get("publication_id")
            if row.state == TaskState.UNCERTAIN or publication_id is not None:
                if (
                    verification is None
                    or verification.authority is not _VERIFICATION_AUTHORITY
                    or verification.task_id != row.task_id
                    or verification.collector_id != row.collector_id
                    or verification.publication_id != publication_id
                    or verification.updated_at != row.updated_at
                    or verification.verified_at != at
                ):
                    raise CollectionStoreError(
                        "publication_verification_required",
                        "collection retry requires verified absent publication",
                    )
            connection.execute(
                """
                UPDATE tasks SET state=?,attempt_count=0,not_before=NULL,worker_id=NULL,
                    lease_expires_at=NULL,worker_pid=NULL,worker_start_ticks=NULL,worker_deadline_at=NULL,
                    execution_started=0,cancel_requested=0,resume_requested=0,last_error_code=NULL,
                    receipt_json=NULL,
                    publication_absence_verified_at=CASE WHEN ? THEN ? ELSE publication_absence_verified_at END,
                    verified_absent_publication_id=CASE WHEN ? THEN ? ELSE verified_absent_publication_id END,
                    updated_at=? WHERE task_id=? AND state=?
                """,
                (
                    TaskState.QUEUED.value,
                    verification is not None,
                    None if verification is None else _format(verification.verified_at),
                    verification is not None,
                    None if verification is None else verification.publication_id,
                    _format(at),
                    task_id,
                    row.state.value,
                ),
            )
            if connection.total_changes != 1:
                raise CollectionStoreError("collection_state_conflict", "collection task changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(task_id)

    def active_process_tasks(self) -> list[TaskRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE worker_pid IS NOT NULL ORDER BY created_at,task_id LIMIT 256"
            ).fetchall()
        finally:
            connection.close()
        return [self._task(row) for row in rows]

    def process_worker_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE worker_pid IS NOT NULL"
            ).fetchone()
            return int(row[0])
        finally:
            connection.close()

    def reconcile(self, at: datetime) -> list[TaskRecord]:
        at = _aware(at, "reconciliation time")
        return self._reconcile_rows(
            at,
            "state IN (?,?) AND lease_expires_at<=? AND worker_pid IS NULL",
            (TaskState.RUNNING.value, TaskState.WAITING_FOR_USER.value, _format(at)),
        )

    def reconcile_worker(
        self,
        worker_id: str,
        pid: int,
        process_start_ticks: int,
        at: datetime,
    ) -> list[TaskRecord]:
        at = _aware(at, "worker reconciliation time")
        if (
            not _SLUG.fullmatch(worker_id)
            or type(pid) is not int
            or pid <= 1
            or type(process_start_ticks) is not int
            or process_start_ticks <= 0
        ):
            raise CollectionStoreError("invalid_collection_worker", "worker process identity is invalid")
        return self._reconcile_rows(
            at,
            "state IN (?,?) AND worker_id=? AND worker_pid=? AND worker_start_ticks=?",
            (
                TaskState.RUNNING.value,
                TaskState.WAITING_FOR_USER.value,
                worker_id,
                pid,
                process_start_ticks,
            ),
        )

    def _reconcile_rows(
        self,
        at: datetime,
        predicate: str,
        arguments: tuple[object, ...],
    ) -> list[TaskRecord]:
        changed: list[str] = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE {predicate} ORDER BY lease_expires_at,task_id",
                arguments,
            ).fetchall()
            for value in rows:
                row = self._task(value)
                if row.execution_started:
                    target = TaskState.UNCERTAIN
                    error = "worker_interrupted_after_execution"
                    not_before = None
                elif row.cancel_requested:
                    target = TaskState.CANCELLED
                    error = "cancelled"
                    not_before = None
                elif row.attempt_count < row.max_attempts:
                    target = TaskState.RETRY_WAIT
                    error = "worker_interrupted"
                    not_before = at + retry_delay(row.attempt_count)
                else:
                    target = TaskState.FAILED
                    error = "attempt_limit"
                    not_before = None
                cursor = connection.execute(
                    """
                    UPDATE tasks SET state=?,not_before=?,worker_id=NULL,lease_expires_at=NULL,
                        worker_pid=NULL,worker_start_ticks=NULL,worker_deadline_at=NULL,
                        execution_started=0,cancel_requested=0,resume_requested=0,
                        last_error_code=?,updated_at=?
                    WHERE task_id=? AND state=? AND worker_id=?
                    """,
                    (
                        target.value,
                        None if not_before is None else _format(not_before),
                        error,
                        _format(at),
                        row.task_id,
                        row.state.value,
                        row.worker_id,
                    ),
                )
                if cursor.rowcount != 1:
                    # A row that changed concurrently is skipped so one racing
                    # worker cannot block reconciliation of the remaining rows;
                    # it is picked up again on the next pass.
                    continue
                changed.append(row.task_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [self.get(task_id) for task_id in changed]

    def create_schedule(
        self,
        *,
        schedule_id: str | None = None,
        collector_id: str,
        account_alias: str,
        parameters: Mapping[str, object],
        interval_seconds: int,
        next_run_at: datetime,
        max_attempts: int,
        at: datetime,
    ) -> ScheduleRecord:
        if not _SLUG.fullmatch(collector_id) or not _SLUG.fullmatch(account_alias):
            raise CollectionStoreError("invalid_collection_schedule", "collection schedule identity is invalid")
        if not 60 <= interval_seconds <= 31_536_000 or not 1 <= max_attempts <= 10:
            raise CollectionStoreError("invalid_collection_schedule", "collection schedule is invalid")
        at = _aware(at, "schedule creation time")
        next_run_at = _aware(next_run_at, "next run time")
        if schedule_id is not None and not _ID.fullmatch(schedule_id):
            raise CollectionStoreError("invalid_collection_schedule", "collection schedule identity is invalid")
        schedule_id = schedule_id or secrets.token_hex(16)
        parameters_json = json.dumps(dict(parameters), sort_keys=True, separators=(",", ":"))
        _json_mapping(parameters_json)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO schedules(schedule_id,collector_id,account_alias,parameters_json,
                    interval_seconds,next_run_at,max_attempts,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,1,?,?)
                """,
                (schedule_id, collector_id, account_alias, parameters_json, interval_seconds,
                 _format(next_run_at), max_attempts, _format(at), _format(at)),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            row = connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
            if row is None:
                raise CollectionStoreError("collection_schedule_collision", "collection schedule identity collided") from exc
            existing = self._schedule(row)
            if (
                existing.collector_id != collector_id
                or existing.account_alias != account_alias
                or dict(existing.parameters) != dict(parameters)
                or existing.interval_seconds != interval_seconds
                or existing.max_attempts != max_attempts
            ):
                raise CollectionStoreError("collection_schedule_conflict", "schedule identity has different work") from exc
            return existing
        finally:
            connection.close()
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> ScheduleRecord:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
        finally:
            connection.close()
        return self._schedule(row)

    def schedules(self) -> list[ScheduleRecord]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM schedules ORDER BY created_at,schedule_id"
            ).fetchall()
        finally:
            connection.close()
        return [self._schedule(row) for row in rows]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool, at: datetime) -> ScheduleRecord:
        if type(enabled) is not bool:
            raise CollectionStoreError("invalid_collection_schedule", "schedule enabled value is invalid")
        at = _aware(at, "schedule update time")
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE schedules SET enabled=?,updated_at=? WHERE schedule_id=?",
                (int(enabled), _format(at), schedule_id),
            )
            if connection.total_changes != 1:
                raise CollectionStoreError("unknown_collection_schedule", "collection schedule does not exist")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_schedule(schedule_id)

    def enqueue_due(self, at: datetime) -> list[TaskRecord]:
        at = _aware(at, "schedule time")
        created: list[str] = []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            schedules = [self._schedule(row) for row in connection.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at,schedule_id",
                (_format(at),),
            ).fetchall()]
            for schedule in schedules:
                occurrence_key = _format(schedule.next_run_at)
                task_id = secrets.token_hex(16)
                try:
                    connection.execute(
                        """
                        INSERT INTO tasks(task_id,collector_id,account_alias,parameters_json,state,
                            attempt_count,max_attempts,not_before,worker_id,lease_expires_at,
                            worker_pid,worker_start_ticks,worker_deadline_at,
                            execution_started,cancel_requested,resume_requested,last_error_code,receipt_json,
                            idempotency_key,schedule_id,occurrence_key,created_at,updated_at)
                        VALUES(?,?,?,?,?,0,?,NULL,NULL,NULL,NULL,NULL,NULL,0,0,0,NULL,NULL,NULL,?,?,?,?)
                        """,
                        (task_id, schedule.collector_id, schedule.account_alias,
                         json.dumps(dict(schedule.parameters), sort_keys=True, separators=(",", ":")),
                         TaskState.QUEUED.value, schedule.max_attempts, schedule.schedule_id,
                         occurrence_key, _format(at), _format(at)),
                    )
                    created.append(task_id)
                except sqlite3.IntegrityError as exc:
                    existing = connection.execute(
                        "SELECT task_id FROM tasks WHERE schedule_id=? AND occurrence_key=?",
                        (schedule.schedule_id, occurrence_key),
                    ).fetchone()
                    if existing is None:
                        raise CollectionStoreError(
                            "collection_schedule_enqueue_failed",
                            "scheduled occurrence could not be created",
                        ) from exc
                elapsed = max(0, int((at - schedule.next_run_at).total_seconds()))
                steps = elapsed // schedule.interval_seconds + 1
                next_run = schedule.next_run_at + timedelta(seconds=steps * schedule.interval_seconds)
                connection.execute(
                    "UPDATE schedules SET next_run_at=?,updated_at=? WHERE schedule_id=? AND next_run_at=?",
                    (_format(next_run), _format(at), schedule.schedule_id, occurrence_key),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [self.get(task_id) for task_id in created]


__all__ = [
    "CollectionStoreError",
    "CollectionTaskStore",
    "ScheduleRecord",
    "TaskRecord",
    "TaskState",
    "retry_delay",
    "utc_now",
]
