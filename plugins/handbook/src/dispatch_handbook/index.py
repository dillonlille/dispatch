"""Atomic SQLite FTS5 storage for handbook chunks and vector payloads."""
from __future__ import annotations

from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import tempfile
from typing import Any, Iterable

from .chunking import Chunk


class IndexError(RuntimeError):
    """The local handbook index is missing, corrupt, or unsafe."""


SCHEMA_VERSION = 1
_REQUIRED_METADATA = {"document_version", "source_sha256"}
_QUERY_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _valid_vector(vector: bytes, dimension: int) -> bool:
    if type(vector) is not bytes or type(dimension) is not int or dimension < 1 or len(vector) != dimension * 4:
        return False
    try:
        return all(math.isfinite(value[0]) for value in struct.iter_unpack("<f", vector))
    except (struct.error, TypeError):
        return False


def _physical_file(path: Path, *, must_exist: bool) -> None:
    if path.is_symlink():
        raise IndexError("index path cannot be a symlink")
    if must_exist and not path.is_file():
        raise IndexError("index is unavailable")
    if path.exists() and not path.is_file():
        raise IndexError("index path must be a regular file")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            citation_id TEXT NOT NULL UNIQUE,
            section_id TEXT NOT NULL,
            section_title TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            language TEXT NOT NULL,
            physical_pages_json TEXT NOT NULL,
            printed_page_labels_json TEXT NOT NULL,
            text TEXT NOT NULL,
            word_count INTEGER NOT NULL CHECK(word_count > 0 AND word_count <= 600),
            previous_chunk_id TEXT,
            next_chunk_id TEXT,
            vector BLOB,
            vector_dim INTEGER,
            CHECK((vector IS NULL AND vector_dim IS NULL) OR (vector IS NOT NULL AND vector_dim > 0))
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED,
            section_title,
            text,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def _metadata_payload(metadata: dict[str, Any], chunk_count: int) -> dict[str, Any]:
    if not isinstance(metadata, dict) or not _REQUIRED_METADATA <= set(metadata):
        raise IndexError("required index metadata is missing")
    result = dict(metadata)
    result["schema_version"] = SCHEMA_VERSION
    result["chunk_count"] = chunk_count
    try:
        json.dumps(result, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise IndexError("index metadata is not JSON serializable") from exc
    return result


def build_index(
    target: Path,
    chunks: Iterable[Chunk],
    *,
    metadata: dict[str, Any],
    vectors: dict[str, bytes] | None = None,
    vector_dim: int | None = None,
) -> dict[str, Any]:
    if not target.is_absolute() or any(part in {".", ".."} for part in target.parts):
        raise IndexError("index target must be an absolute path without traversal")
    if target.is_symlink() or target.parent.resolve(strict=False) != target.parent:
        raise IndexError("index target and parent must be physical paths")
    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise IndexError("index parent must be a physical directory")
    _physical_file(target, must_exist=False)
    values = list(chunks)
    if vectors is not None and set(vectors) != {chunk.chunk_id for chunk in values}:
        raise IndexError("vector inventory does not match chunk inventory")
    meta = _metadata_payload(metadata, len(values))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.stage-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with closing(_connect(temporary)) as connection:
            _schema(connection)
            connection.executemany(
                "INSERT INTO metadata(key,value_json) VALUES (?,?)",
                [(key, json.dumps(value, sort_keys=True, separators=(",", ":"))) for key, value in sorted(meta.items())],
            )
            for chunk in values:
                vector = vectors.get(chunk.chunk_id) if vectors else None
                dimension = vector_dim if vector is not None else None
                if vectors is not None and (vector is None or dimension is None or not _valid_vector(vector, dimension)):
                    raise IndexError("chunk vector payload is missing or invalid")
                connection.execute(
                    """INSERT INTO chunks(
                        chunk_id,citation_id,section_id,section_title,source_kind,language,
                        physical_pages_json,printed_page_labels_json,text,word_count,
                        previous_chunk_id,next_chunk_id,vector,vector_dim
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        chunk.chunk_id,
                        chunk.citation_id,
                        chunk.section_id,
                        chunk.section_title,
                        chunk.source_kind,
                        chunk.language,
                        json.dumps(chunk.physical_pages, separators=(",", ":")),
                        json.dumps(chunk.printed_page_labels, separators=(",", ":")),
                        chunk.text,
                        chunk.word_count,
                        chunk.previous_chunk_id,
                        chunk.next_chunk_id,
                        vector,
                        dimension,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(chunk_id,section_title,text) VALUES (?,?,?)",
                    (chunk.chunk_id, chunk.section_title, chunk.text),
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise IndexError("staged index failed SQLite integrity check")
            fts_count = connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            if fts_count != len(values):
                raise IndexError("staged FTS row count mismatch")
            connection.commit()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if isinstance(exc, IndexError):
            raise
        raise IndexError("index build failed; previous index was preserved") from exc
    return verify_index(target)


def _read_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        row["key"]: json.loads(row["value_json"])
        for row in connection.execute("SELECT key,value_json FROM metadata ORDER BY key")
    }


def verify_index(path: Path) -> dict[str, Any]:
    _physical_file(path, must_exist=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise IndexError("index permissions are not owner-only")
    try:
        with closing(_connect(path, readonly=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            metadata = _read_metadata(connection)
            chunk_count = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
            fts_count = connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            vector_rows = connection.execute("SELECT vector,vector_dim FROM chunks ORDER BY chunk_id").fetchall()
    except (sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
        raise IndexError("index verification failed") from exc
    if integrity != "ok" or chunk_count != fts_count:
        raise IndexError("index integrity or row counts are invalid")
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("chunk_count") != chunk_count:
        raise IndexError("index metadata does not match stored rows")
    if not _REQUIRED_METADATA <= set(metadata):
        raise IndexError("index identity metadata is incomplete")
    expected_dimension = metadata.get("embedding_dimension")
    if expected_dimension is None:
        if any(row["vector"] is not None or row["vector_dim"] is not None for row in vector_rows):
            raise IndexError("index vectors have no dimension metadata")
    elif (
        type(expected_dimension) is not int
        or expected_dimension < 1
        or len(vector_rows) != chunk_count
        or any(
            type(row["vector"]) is not bytes
            or row["vector_dim"] != expected_dimension
            or not _valid_vector(row["vector"], expected_dimension)
            for row in vector_rows
        )
    ):
        raise IndexError("index vector inventory is invalid")
    return {"integrity": integrity, "chunk_count": chunk_count, "metadata": metadata}


def _fts_expression(question: str) -> str:
    tokens = []
    for token in _QUERY_TOKEN.findall(question.casefold()):
        if len(token) >= 2 and token not in tokens:
            tokens.append(token)
    if not tokens:
        raise IndexError("question has no searchable terms")
    return " OR ".join(f'"{token}"' for token in tokens[:32])


def search_fts(path: Path, question: str, *, limit: int = 20) -> list[dict[str, Any]]:
    _physical_file(path, must_exist=True)
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise IndexError("FTS result limit is invalid")
    expression = _fts_expression(question)
    try:
        with closing(_connect(path, readonly=True)) as connection:
            rows = connection.execute(
                """SELECT c.*, bm25(chunks_fts, 0.0, 3.0, 1.0) AS rank
                   FROM chunks_fts JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
                   WHERE chunks_fts MATCH ? ORDER BY rank, c.chunk_id LIMIT ?""",
                (expression, limit),
            ).fetchall()
    except sqlite3.Error as exc:
        raise IndexError("keyword search failed") from exc
    result = []
    for row in rows:
        value = dict(row)
        value["physical_pages"] = json.loads(value["physical_pages_json"])
        value["printed_page_labels"] = json.loads(value["printed_page_labels_json"])
        value["fts_score"] = -float(value.pop("rank"))
        value.pop("vector", None)
        result.append(value)
    return result

def list_sections(path: Path) -> dict[str, Any]:
    """Return the verified section inventory without mutating the index."""
    verified = verify_index(path)
    try:
        with closing(_connect(path, readonly=True)) as connection:
            rows = connection.execute(
                """SELECT section_id, section_title, MIN(physical_pages_json) AS first_pages
                   FROM chunks GROUP BY section_id, section_title ORDER BY MIN(rowid)"""
            ).fetchall()
    except sqlite3.Error as exc:
        raise IndexError("section inventory read failed") from exc
    return {
        "metadata": verified["metadata"],
        "sections": [
            {
                "section_id": row["section_id"],
                "section_title": row["section_title"],
                "physical_pages": json.loads(row["first_pages"]),
            }
            for row in rows
        ],
    }

