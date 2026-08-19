import sqlite3

import pytest

from companion_bridge.driver_names import DriverNameResolver, StreamingDriverIdRewriter


def test_streaming_driver_id_rewrite_handles_chunk_boundaries(tmp_path) -> None:
    database = tmp_path / "drivers.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE driver_names (transporter_id TEXT PRIMARY KEY, driver_name TEXT)")
        connection.execute("INSERT INTO driver_names VALUES (?, ?)", ("A2TD8VG6QDKT4N", "Synthetic Driver"))
    database.chmod(0o600)
    resolver = DriverNameResolver.from_sqlite(str(database), id_regex=r"\bA[A-Z0-9]{10,24}\b")
    rewriter = StreamingDriverIdRewriter(resolver)
    assert rewriter.add("Transporter A2TD8VG6Q") == "Transporter "
    assert rewriter.add("DKT4N reported") == "Synthetic Driver "
    assert rewriter.flush() == "reported"


def test_unknown_id_is_preserved_by_default() -> None:
    resolver = DriverNameResolver.disabled()
    assert resolver.replace_ids("A11111111111") == "A11111111111"


def test_driver_name_database_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target.sqlite3"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE driver_names (transporter_id TEXT, driver_name TEXT)")
    target.chmod(0o600)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)

    with pytest.raises(ValueError, match="unsafe"):
        DriverNameResolver.from_sqlite(
            str(linked),
            id_regex=r"\bA[A-Z0-9]{10,24}\b",
        )
