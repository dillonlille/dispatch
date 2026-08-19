from __future__ import annotations

import sqlite3
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ConversationMapping:
    team_id: str
    channel_id: str
    thread_ts: str
    conversation_id: str
    last_message_id: str | None
    updated_at: int
    generation: int = 0


class ConversationStore:
    """Private Slack-thread mapping store; prompts and answers are never persisted."""

    def __init__(self, sqlite_path: str | Path) -> None:
        self.path = Path(sqlite_path).expanduser()
        _ensure_private_directory(self.path.parent)
        _ensure_private_database(self.path)
        self._init_db()

    def get(self, *, team_id: str, channel_id: str, thread_ts: str) -> ConversationMapping | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT team_id, channel_id, thread_ts, conversation_id, last_message_id, updated_at, generation "
                "FROM conversation_threads WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
        return _mapping(row) if row else None

    def upsert(self, *, team_id: str, channel_id: str, thread_ts: str, conversation_id: str,
               last_message_id: str | None = None, updated_at: int | None = None,
               generation: int | None = None) -> ConversationMapping:
        timestamp = int(time.time() if updated_at is None else updated_at)
        generation = self.get_generation(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts) if generation is None else generation
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversation_threads(team_id,channel_id,thread_ts,conversation_id,last_message_id,updated_at,generation) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(team_id,channel_id,thread_ts) DO UPDATE SET conversation_id=excluded.conversation_id, "
                "last_message_id=excluded.last_message_id,updated_at=excluded.updated_at,generation=excluded.generation",
                (team_id, channel_id, thread_ts, conversation_id, last_message_id, timestamp, generation),
            )
        return ConversationMapping(team_id, channel_id, thread_ts, conversation_id, last_message_id, timestamp, generation)

    def upsert_if_generation(self, *, team_id: str, channel_id: str, thread_ts: str,
                             conversation_id: str, expected_generation: int,
                             last_message_id: str | None = None, updated_at: int | None = None) -> ConversationMapping | None:
        timestamp = int(time.time() if updated_at is None else updated_at)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT generation FROM thread_generations WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
            current_generation = int(row["generation"]) if row else 0
            if current_generation != expected_generation:
                return None
            db.execute(
                "INSERT INTO conversation_threads(team_id,channel_id,thread_ts,conversation_id,last_message_id,updated_at,generation) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(team_id,channel_id,thread_ts) DO UPDATE SET conversation_id=excluded.conversation_id, "
                "last_message_id=excluded.last_message_id,updated_at=excluded.updated_at,generation=excluded.generation",
                (team_id, channel_id, thread_ts, conversation_id, last_message_id, timestamp, expected_generation),
            )
        return ConversationMapping(
            team_id,
            channel_id,
            thread_ts,
            conversation_id,
            last_message_id,
            timestamp,
            expected_generation,
        )

    def reset_thread(self, *, team_id: str, channel_id: str, thread_ts: str) -> int:
        timestamp = int(time.time())
        with self._connect() as db:
            row = db.execute("SELECT generation FROM thread_generations WHERE team_id=? AND channel_id=? AND thread_ts=?", (team_id, channel_id, thread_ts)).fetchone()
            generation = int(row["generation"]) + 1 if row else 1
            db.execute("INSERT INTO thread_generations VALUES(?,?,?,?,?) ON CONFLICT(team_id,channel_id,thread_ts) DO UPDATE SET generation=excluded.generation,updated_at=excluded.updated_at", (team_id, channel_id, thread_ts, generation, timestamp))
            db.execute("DELETE FROM conversation_threads WHERE team_id=? AND channel_id=? AND thread_ts=?", (team_id, channel_id, thread_ts))
        return generation

    def get_generation(self, *, team_id: str, channel_id: str, thread_ts: str) -> int:
        with self._connect() as db:
            row = db.execute("SELECT generation FROM thread_generations WHERE team_id=? AND channel_id=? AND thread_ts=?", (team_id, channel_id, thread_ts)).fetchone()
        return int(row["generation"]) if row else 0

    def mark_event_processed(self, *, event_key: str, ttl_seconds: int = 7 * 86400, now: int | None = None) -> bool:
        timestamp = int(time.time() if now is None else now)
        with self._connect() as db:
            db.execute("DELETE FROM processed_events WHERE processed_at < ?", (timestamp - ttl_seconds,))
            try:
                db.execute("INSERT INTO processed_events VALUES(?,?)", (event_key, timestamp))
            except sqlite3.IntegrityError:
                return False
        return True

    def forget_event(self, *, event_key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM processed_events WHERE event_key=?", (event_key,))

    def cleanup_stale(self, *, ttl_seconds: int, now: int | None = None) -> int:
        cutoff = int(time.time() if now is None else now) - ttl_seconds
        with self._connect() as db:
            cursor = db.execute("DELETE FROM conversation_threads WHERE updated_at < ?", (cutoff,))
            db.execute("DELETE FROM thread_generations WHERE updated_at < ?", (cutoff,))
            db.execute("DELETE FROM processed_events WHERE processed_at < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def list_mappings(self, *, limit: int = 100) -> list[ConversationMapping]:
        with self._connect() as db:
            rows = db.execute("SELECT team_id,channel_id,thread_ts,conversation_id,last_message_id,updated_at,generation FROM conversation_threads ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(limit), 1000)),)).fetchall()
        return [_mapping(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        _assert_private_database(self.path)
        db = sqlite3.connect(self.path, timeout=30.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        finally:
            db.close()
            _assert_private_database(self.path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    _assert_private_file(sidecar)

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_threads (
              team_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL,
              conversation_id TEXT NOT NULL, last_message_id TEXT, updated_at INTEGER NOT NULL,
              generation INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(team_id,channel_id,thread_ts)
            );
            CREATE TABLE IF NOT EXISTS thread_generations (
              team_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL,
              generation INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(team_id,channel_id,thread_ts)
            );
            CREATE TABLE IF NOT EXISTS processed_events (event_key TEXT PRIMARY KEY, processed_at INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_threads_updated ON conversation_threads(updated_at);
            CREATE INDEX IF NOT EXISTS idx_events_processed ON processed_events(processed_at);
            """)


def _mapping(row: sqlite3.Row) -> ConversationMapping:
    return ConversationMapping(str(row["team_id"]), str(row["channel_id"]), str(row["thread_ts"]), str(row["conversation_id"]), str(row["last_message_id"]) if row["last_message_id"] is not None else None, int(row["updated_at"]), int(row["generation"]))


def validate_conversation_database(path: str | Path) -> None:
    database = Path(path).expanduser()
    if not database.exists() and not database.is_symlink():
        return
    _assert_private_database(database)


def _ensure_private_directory(path: Path) -> None:
    pending: list[Path] = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        pending.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise ValueError("conversation database directory is unsafe")
    details = cursor.stat(follow_symlinks=False)
    if details.st_uid != os.geteuid() or details.st_mode & 0o022:
        raise ValueError("conversation database ancestor is unsafe")
    for directory in reversed(pending):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    details = path.stat(follow_symlinks=False)
    if path.is_symlink() or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("conversation database directory is unsafe")


def _ensure_private_database(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.close(descriptor)
    _assert_private_database(path)


def _assert_private_database(path: Path) -> None:
    _assert_private_file(path)
    parent = path.parent
    details = parent.stat(follow_symlinks=False)
    if parent.is_symlink() or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("conversation database directory is unsafe")


def _assert_private_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError("conversation database file is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise ValueError("conversation database file is unsafe")
