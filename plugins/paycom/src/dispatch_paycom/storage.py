"""Immutable, private, fail-closed SQLite reads for Paycom projections."""
from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import stat
from typing import Any, Iterable


class StorageError(RuntimeError):
    def __init__(self, code: str = "unavailable", message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def _metadata(path: Path) -> tuple[str, ...] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StorageError("unavailable", "The local Paycom database is unavailable.") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise StorageError("unavailable", "The local Paycom database is not a private regular file.")
    return tuple(str(value) for value in (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def database_family_state(path: str | Path) -> tuple[Any, ...]:
    target = Path(os.path.abspath(path))
    parent = target.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise StorageError("unavailable", "The Paycom database directory is unavailable.") from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o077
        or parent.resolve(strict=True) != parent
    ):
        raise StorageError("unavailable", "The Paycom database directory is not private.")
    main = _metadata(target)
    if main is None:
        raise StorageError("not_loaded", "The local Paycom database is not loaded.")
    wal = _metadata(Path(f"{target}-wal"))
    shm = _metadata(Path(f"{target}-shm"))
    if wal is not None and int(wal[4]) > 0:
        raise StorageError("unavailable", "The Paycom database has a live WAL and cannot be read immutably.")
    return (main, wal, shm)


class ReadOnlyDatabase:
    """A connection that admits only immutable URI reads and verifies no drift."""

    def __init__(self, path: str | Path, *, required_tables: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self._state = database_family_state(self.path)
        self.connection: sqlite3.Connection | None = None
        try:
            uri = self.path.as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            self.connection = connection
            self.require_tables(required_tables)
        except StorageError:
            self.close(best_effort=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            self.close(best_effort=True)
            raise StorageError("unavailable", "The local Paycom database could not be opened read-only.") from exc

    def require_tables(self, tables: Iterable[str]) -> None:
        if self.connection is None:
            raise StorageError("unavailable", "The database is closed.")
        wanted = tuple(tables)
        if not wanted:
            return
        placeholders = ",".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})", wanted
        ).fetchall()
        if {row[0] for row in rows} != set(wanted):
            raise StorageError("schema_invalid", "The Paycom database schema is incomplete.")

    def columns(self, table: str) -> set[str]:
        if self.connection is None:
            raise StorageError("unavailable", "The database is closed.")
        if not table.replace("_", "").isalnum():
            raise StorageError("schema_invalid", "The Paycom schema identifier is invalid.")
        return {str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()}

    def require_columns(self, requirements: dict[str, Iterable[str]]) -> None:
        for table, columns in requirements.items():
            actual = self.columns(table)
            missing = set(columns) - actual
            if missing:
                raise StorageError("schema_invalid", f"The Paycom {table} schema is incomplete.")

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        if self.connection is None:
            raise StorageError("unavailable", "The database is closed.")
        try:
            return self.connection.execute(sql, tuple(parameters))
        except sqlite3.Error as exc:
            raise StorageError("schema_invalid", "The Paycom database query failed.") from exc

    def quick_ok(self) -> bool:
        try:
            row = self.execute("PRAGMA quick_check").fetchone()
            foreign = self.execute("PRAGMA foreign_key_check").fetchone()
            return row is not None and row[0] == "ok" and foreign is None
        except StorageError:
            return False

    def close(self, *, best_effort: bool = False) -> None:
        failure: StorageError | None = None
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error as exc:
                failure = StorageError("unavailable", "The Paycom database could not be closed.")
                if not best_effort:
                    raise failure from exc
            finally:
                self.connection = None
        if failure is None and not best_effort:
            try:
                after = database_family_state(self.path)
            except StorageError as exc:
                failure = exc
            else:
                if after != self._state:
                    failure = StorageError("unavailable", "The Paycom database changed during the read.")
        if failure is not None and not best_effort:
            raise failure

    def __enter__(self) -> "ReadOnlyDatabase":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def open_read_only(path: str | Path, *, required_tables: Iterable[str] = ()) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(path, required_tables=required_tables)
