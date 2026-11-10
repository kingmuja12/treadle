from __future__ import annotations

import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path

import pytest

from treadle.errors import StoreError
from treadle.store.db import MIN_SQLITE_VERSION, connect, sqlite_version


def pragma(conn: sqlite3.Connection, name: str) -> object:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_sqlite_version_is_the_linked_library_version() -> None:
    assert sqlite_version() == tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    assert sqlite_version() >= MIN_SQLITE_VERSION == (3, 31, 0)


def test_writer_creates_a_missing_database_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "treadle.db"

    with closing(connect(path)):
        pass

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writer_keeps_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "treadle.db"
    with closing(connect(path)) as conn:
        conn.execute("CREATE TABLE kept (x)")

    with closing(connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("kept",)]


def test_writer_pragmas(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "treadle.db")) as conn:
        assert pragma(conn, "foreign_keys") == 1
        assert pragma(conn, "busy_timeout") == 5000
        assert pragma(conn, "temp_store") == 2  # MEMORY
        assert pragma(conn, "journal_mode") == "wal"
        assert pragma(conn, "journal_size_limit") == 67_108_864
        assert pragma(conn, "synchronous") == 2  # FULL
        assert pragma(conn, "query_only") == 0


def test_reader_is_query_only(tmp_path: Path) -> None:
    path = tmp_path / "treadle.db"
    with closing(connect(path)):
        pass

    with closing(connect(path, query_only=True)) as reader:
        assert pragma(reader, "query_only") == 1
        assert pragma(reader, "foreign_keys") == 1
        assert pragma(reader, "busy_timeout") == 5000
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("CREATE TABLE refused (x)")


def test_reader_never_creates_the_database(tmp_path: Path) -> None:
    path = tmp_path / "treadle.db"

    with pytest.raises(sqlite3.OperationalError):
        connect(path, query_only=True)

    assert not path.exists()


def test_transactions_are_explicit(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "treadle.db")) as conn:
        if sys.version_info >= (3, 12):
            assert conn.autocommit is True
        else:
            assert conn.isolation_level is None
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert not conn.in_transaction

        conn.execute("BEGIN")
        assert conn.in_transaction
        conn.execute("ROLLBACK")


def test_old_sqlite_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 30, 1))

    with pytest.raises(StoreError, match=r"needs SQLite 3\.31\.0 or later, .* SQLite 3\.30\.1$"):
        connect(tmp_path / "treadle.db")

    assert not (tmp_path / "treadle.db").exists()


def test_a_file_that_isnt_a_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "treadle.db"
    path.write_bytes(b"this is not an SQLite database, just some text" * 100)

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        connect(path)


def test_python_3_11_turns_off_implicit_transactions_with_isolation_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 11, 9, "final", 0))
    conn = connect(tmp_path / "treadle.db")
    monkeypatch.undo()

    with closing(conn):
        assert conn.isolation_level is None
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        assert not conn.in_transaction
