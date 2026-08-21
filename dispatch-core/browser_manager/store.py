"""Durable SQLite lease ledger for Browser Manager."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat

from .models import (
    BrowserLease,
    BrowserLeaseRequest,
    BrowserManagerError,
    BrowserMode,
    BrowserPurpose,
    LeaseState,
    TERMINAL_STATES,
    format_timestamp,
    parse_timestamp,
    require_transition,
)
from .runtime_authority import BrowserRuntimeIdentity


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    plugin_release TEXT NOT NULL,
    realm TEXT NOT NULL,
    purpose TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    profile_key TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    runtime_playwright_version TEXT NOT NULL,
    runtime_chromium_version TEXT,
    runtime_executable TEXT NOT NULL,
    runtime_control_executable TEXT NOT NULL,
    control_pid INTEGER,
    control_process_start_ticks INTEGER,
    pid INTEGER,
    process_start_ticks INTEGER,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS leases_profile_state
    ON leases(profile_key, state);
CREATE INDEX IF NOT EXISTS leases_state_expires
    ON leases(state, expires_at);
"""
_TERMINAL_VALUES = tuple(item.value for item in TERMINAL_STATES)
_SCHEMA_VERSION = "4"
_LEASE_COLUMNS = {
    "lease_id",
    "plugin_id",
    "plugin_release",
    "realm",
    "purpose",
    "account_alias",
    "profile_key",
    "mode",
    "state",
    "created_at",
    "updated_at",
    "expires_at",
    "runtime_playwright_version",
    "runtime_chromium_version",
    "runtime_executable",
    "runtime_control_executable",
    "control_pid",
    "control_process_start_ticks",
    "pid",
    "process_start_ticks",
    "error_code",
}


@dataclass(frozen=True)
class LeaseRow:
    lease_id: str
    plugin_id: str
    plugin_release: str
    realm: str
    purpose: BrowserPurpose
    account_alias: str
    profile_key: str
    mode: BrowserMode
    state: LeaseState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    runtime_playwright_version: str
    runtime_chromium_version: str | None
    runtime_executable: Path
    runtime_control_executable: Path
    control_pid: int | None
    control_process_start_ticks: int | None
    pid: int | None
    process_start_ticks: int | None
    error_code: str | None

    def lease(self) -> BrowserLease:
        return BrowserLease(
            lease_id=self.lease_id,
            plugin_id=self.plugin_id,
            plugin_release=self.plugin_release,
            realm=self.realm,
            purpose=self.purpose,
            account_alias=self.account_alias,
            mode=self.mode,
            state=self.state,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )

    @property
    def runtime_identity(self) -> BrowserRuntimeIdentity:
        return BrowserRuntimeIdentity(
            playwright_version=self.runtime_playwright_version,
            chromium_version=self.runtime_chromium_version,
            executable=self.runtime_executable,
            control_executable=self.runtime_control_executable,
        )

    def safe_data(self) -> dict[str, object]:
        data: dict[str, object] = self.lease().safe_data()
        data["updated_at"] = format_timestamp(self.updated_at)
        if self.state in TERMINAL_STATES:
            process_tracking = "none"
        elif self.pid is not None and self.control_pid is not None:
            process_tracking = "browser_and_control_tracked"
        elif self.control_pid is not None:
            process_tracking = "control_tracked"
        elif self.pid is not None:
            process_tracking = "browser_tracked"
        elif self.state == LeaseState.QUARANTINED:
            process_tracking = "possible_untracked"
        else:
            process_tracking = "identity_pending"
        data["process_tracking"] = process_tracking
        data["error_code"] = self.error_code
        data["runtime_playwright_version"] = self.runtime_playwright_version
        data["runtime_chromium_version"] = self.runtime_chromium_version
        return data


class LeaseStore:
    """One-connection-per-transaction durable lease storage."""

    def __init__(self, database: Path) -> None:
        self.database = database
        if database.is_symlink() or not database.parent.is_dir() or database.parent.is_symlink():
            raise BrowserManagerError("unsafe_browser_storage", "browser database location is unsafe")
        if database.exists():
            details = database.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.getuid()
            ):
                raise BrowserManagerError("unsafe_browser_storage", "browser database is not a private regular file")
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialise(self) -> None:
        try:
            connection = self._connect()
        except sqlite3.DatabaseError as exc:
            raise BrowserManagerError("browser_state_corrupt", "browser state database is invalid") from exc
        try:
            metadata_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
            ).fetchone()
            if metadata_exists is not None:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if version is None:
                    raise BrowserManagerError("browser_state_corrupt", "browser schema version is missing")
                if version[0] != _SCHEMA_VERSION:
                    raise BrowserManagerError(
                        "unsupported_browser_schema",
                        "browser state schema version is unsupported",
                    )
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (_SCHEMA_VERSION,),
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(leases)").fetchall()
            }
            if columns != _LEASE_COLUMNS:
                raise BrowserManagerError("browser_state_corrupt", "browser lease schema is not approved")
            connection.commit()
        except BrowserManagerError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise BrowserManagerError("browser_state_corrupt", "browser state database is invalid") from exc
        finally:
            connection.close()
        details = self.database.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.getuid()
        ):
            raise BrowserManagerError("unsafe_browser_storage", "browser database is not a private regular file")
        os.chmod(self.database, 0o600)

    @staticmethod
    def _row(value: sqlite3.Row | None) -> LeaseRow:
        if value is None:
            raise BrowserManagerError("unknown_browser_lease", "browser lease does not exist")
        try:
            row = LeaseRow(
                lease_id=value["lease_id"],
                plugin_id=value["plugin_id"],
                plugin_release=value["plugin_release"],
                realm=value["realm"],
                purpose=BrowserPurpose(value["purpose"]),
                account_alias=value["account_alias"],
                profile_key=value["profile_key"],
                mode=BrowserMode(value["mode"]),
                state=LeaseState(value["state"]),
                created_at=parse_timestamp(value["created_at"]),
                updated_at=parse_timestamp(value["updated_at"]),
                expires_at=parse_timestamp(value["expires_at"]),
                runtime_playwright_version=value["runtime_playwright_version"],
                runtime_chromium_version=value["runtime_chromium_version"],
                runtime_executable=Path(value["runtime_executable"]),
                runtime_control_executable=Path(value["runtime_control_executable"]),
                control_pid=value["control_pid"],
                control_process_start_ticks=value["control_process_start_ticks"],
                pid=value["pid"],
                process_start_ticks=value["process_start_ticks"],
                error_code=value["error_code"],
            )
            row.runtime_identity
            return row
        except (BrowserManagerError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise BrowserManagerError("browser_state_corrupt", "stored browser lease is invalid") from exc

    def create(
        self,
        *,
        lease_id: str,
        request: BrowserLeaseRequest,
        mode: BrowserMode,
        created_at: datetime,
        expires_at: datetime,
        runtime_identity: BrowserRuntimeIdentity,
        maximum_browsers: int,
    ) -> LeaseRow:
        now_value = format_timestamp(created_at)
        placeholders = ",".join("?" for _ in _TERMINAL_VALUES)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT lease_id FROM leases WHERE profile_key = ? AND state NOT IN ({placeholders}) LIMIT 1",
                (request.profile_key, *_TERMINAL_VALUES),
            ).fetchone()
            if existing is not None:
                raise BrowserManagerError("browser_profile_busy", "browser profile already has an active lease")
            existing = connection.execute(
                f"SELECT lease_id FROM leases WHERE realm = ? AND state NOT IN ({placeholders}) LIMIT 1",
                (request.realm, *_TERMINAL_VALUES),
            ).fetchone()
            if existing is not None:
                raise BrowserManagerError("browser_realm_busy", "browser realm already has an active lease")
            occupied = connection.execute(
                f"SELECT COUNT(*) FROM leases WHERE state NOT IN ({placeholders})",
                _TERMINAL_VALUES,
            ).fetchone()
            if occupied is None or int(occupied[0]) >= maximum_browsers:
                raise BrowserManagerError("browser_capacity_unavailable", "all approved browser slots are occupied")
            connection.execute(
                """
                INSERT INTO leases(
                    lease_id, plugin_id, plugin_release, realm, purpose, account_alias,
                    profile_key, mode, state, created_at, updated_at, expires_at,
                    runtime_playwright_version, runtime_chromium_version,
                    runtime_executable, runtime_control_executable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    request.plugin_id,
                    request.plugin_release,
                    request.realm,
                    request.purpose.value,
                    request.account_alias,
                    request.profile_key,
                    mode.value,
                    LeaseState.REQUESTED.value,
                    now_value,
                    now_value,
                    format_timestamp(expires_at),
                    runtime_identity.playwright_version,
                    runtime_identity.chromium_version,
                    str(runtime_identity.executable),
                    str(runtime_identity.control_executable),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise BrowserManagerError("browser_lease_collision", "browser lease identity already exists") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def get(self, lease_id: str) -> LeaseRow:
        connection = self._connect()
        try:
            value = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        finally:
            connection.close()
        return self._row(value)

    def transition(
        self,
        lease_id: str,
        target: LeaseState,
        at: datetime,
        *,
        error_code: str | None = None,
    ) -> LeaseRow:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            value = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            current = self._row(value)
            require_transition(current.state, target)
            connection.execute(
                "UPDATE leases SET state = ?, updated_at = ?, error_code = ? WHERE lease_id = ? AND state = ?",
                (target.value, format_timestamp(at), error_code, lease_id, current.state.value),
            )
            if connection.total_changes != 1:
                raise BrowserManagerError("browser_state_conflict", "browser lease changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def attach_control_process(
        self,
        lease_id: str,
        *,
        pid: int,
        process_start_ticks: int,
        at: datetime,
    ) -> LeaseRow:
        if pid <= 1 or process_start_ticks <= 0:
            raise BrowserManagerError("invalid_browser_process", "browser control process identity is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            value = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            current = self._row(value)
            if current.state != LeaseState.STARTING or current.control_pid is not None:
                raise BrowserManagerError("invalid_lease_transition", "browser control process can attach only once while starting")
            connection.execute(
                "UPDATE leases SET control_pid = ?, control_process_start_ticks = ?, updated_at = ? WHERE lease_id = ? AND state = ? AND control_pid IS NULL",
                (pid, process_start_ticks, format_timestamp(at), lease_id, LeaseState.STARTING.value),
            )
            if connection.total_changes != 1:
                raise BrowserManagerError("browser_state_conflict", "browser lease changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def attach_process(
        self,
        lease_id: str,
        *,
        pid: int,
        process_start_ticks: int,
        at: datetime,
    ) -> LeaseRow:
        if pid <= 1 or process_start_ticks <= 0:
            raise BrowserManagerError("invalid_browser_process", "browser process identity is invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            value = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            current = self._row(value)
            if current.state != LeaseState.STARTING or current.pid is not None:
                raise BrowserManagerError("invalid_lease_transition", "browser process can attach only once while starting")
            connection.execute(
                "UPDATE leases SET pid = ?, process_start_ticks = ?, updated_at = ? WHERE lease_id = ? AND state = ? AND pid IS NULL",
                (pid, process_start_ticks, format_timestamp(at), lease_id, LeaseState.STARTING.value),
            )
            if connection.total_changes != 1:
                raise BrowserManagerError("browser_state_conflict", "browser lease changed concurrently")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get(lease_id)

    def nonterminal(self) -> list[LeaseRow]:
        placeholders = ",".join("?" for _ in _TERMINAL_VALUES)
        connection = self._connect()
        try:
            values = connection.execute(
                f"SELECT * FROM leases WHERE state NOT IN ({placeholders}) ORDER BY created_at, lease_id",
                _TERMINAL_VALUES,
            ).fetchall()
        finally:
            connection.close()
        return [self._row(value) for value in values]

    def recent(self, limit: int = 50) -> list[LeaseRow]:
        bounded = min(max(limit, 1), 200)
        connection = self._connect()
        try:
            values = connection.execute(
                "SELECT * FROM leases ORDER BY created_at DESC, lease_id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        finally:
            connection.close()
        return [self._row(value) for value in values]


__all__ = ["LeaseRow", "LeaseStore"]
