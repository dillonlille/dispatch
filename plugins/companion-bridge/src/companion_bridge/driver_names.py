from __future__ import annotations

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DriverNameResolver:
    id_to_name: dict[str, str]
    id_pattern: re.Pattern[str]
    fallback_to_id: bool = True

    @property
    def entry_count(self) -> int:
        return len(self.id_to_name)

    @classmethod
    def disabled(cls, *, id_regex: str = r"\bA[A-Z0-9]{10,24}\b") -> "DriverNameResolver":
        return cls({}, re.compile(id_regex), True)

    @classmethod
    def from_sqlite(cls, path: str, *, id_regex: str, fallback_to_id: bool = True) -> "DriverNameResolver":
        database = Path(path).expanduser()
        pattern = re.compile(id_regex)
        if not database.exists() and not database.is_symlink():
            return cls({}, pattern, fallback_to_id)
        try:
            parent = database.parent.lstat()
            details = database.lstat()
        except OSError as exc:
            raise ValueError("driver-name database is unavailable") from exc
        if (
            database.parent.is_symlink()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or database.is_symlink()
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size > 64 * 1024 * 1024
        ):
            raise ValueError("driver-name database is unsafe")
        rows: dict[str, str] = {}
        with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as connection:
            result = connection.execute(
                "SELECT transporter_id, driver_name FROM driver_names LIMIT 10001"
            ).fetchall()
            if len(result) > 10000:
                raise ValueError("driver-name database exceeds its row bound")
            for identifier, name in result:
                identifier = str(identifier or "").strip().upper()
                name = str(name or "").strip()
                if identifier and name and len(identifier) <= 64 and len(name) <= 256:
                    rows[identifier] = name
        return cls(rows, pattern, fallback_to_id)

    def replace_ids(self, text: str) -> str:
        return self.id_pattern.sub(
            lambda match: self.id_to_name.get(match.group(0).upper(), match.group(0) if self.fallback_to_id else ""),
            text,
        )


class StreamingDriverIdRewriter:
    def __init__(self, resolver: DriverNameResolver, *, max_candidate_chars: int = 32) -> None:
        self.resolver = resolver
        self.max_candidate_chars = max_candidate_chars
        self._pending = ""

    def add(self, text: str) -> str:
        self._pending += text or ""
        safe, self._pending = self._split_safe_prefix(self._pending)
        return self.resolver.replace_ids(safe)

    def flush(self) -> str:
        safe = self.resolver.replace_ids(self._pending)
        self._pending = ""
        return safe

    def _split_safe_prefix(self, text: str) -> tuple[str, str]:
        match = re.search(rf"[A-Za-z0-9]{{1,{self.max_candidate_chars}}}$", text)
        return (text[: match.start()], text[match.start() :]) if match else (text, "")
