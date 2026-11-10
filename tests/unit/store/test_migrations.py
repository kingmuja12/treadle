from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path

import pytest
from tests.unit.store.helpers import DAY, T0, Runs, project_workflow

from treadle.errors import SchemaTooNewError
from treadle.models import AttemptStatus, LogStream
from treadle.store.db import connect
from treadle.store.migrations import (
    CURRENT_VERSION,
    MIGRATIONS,
    Migration,
    migrate,
    schema_version,
)
from treadle.store.repo import NewLogLine, Repository
from treadle.types import ExitCode

TABLES = {
    "schema_version",
    "workflow_versions",
    "runs",
    "steps",
    "step_attempts",
    "log_lines",
    "schedule_state",
    "webhook_deliveries",
    "control_requests",
}
WITH_V2 = (*MIGRATIONS, Migration(2, "add a note", ("ALTER TABLE runs ADD COLUMN note TEXT",)))


@pytest.fixture
def db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection to a new, empty database."""
    connection = connect(db_path)
    yield connection
    connection.close()


def run_migrate(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    now_ms: int = T0,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> tuple[int, int]:
    return migrate(
        conn,
        db_path=db_path,
        backup_dir=db_path.parent / "backups",
        now_ms=now_ms,
        migrations=migrations,
    )


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {name for (name,) in rows} - {"sqlite_sequence"}


def backups(db_path: Path) -> list[str]:
    return sorted(path.name for path in (db_path.parent / "backups").glob("*"))


def test_current_version_is_the_last_migration() -> None:
    assert CURRENT_VERSION == MIGRATIONS[-1].version == 1
    assert MIGRATIONS[0].description == "initial schema"


def test_a_new_database_has_version_0(db: sqlite3.Connection) -> None:
    assert schema_version(db) == 0


def test_a_new_database_migrates_to_the_current_version(
    db: sqlite3.Connection, db_path: Path
) -> None:
    assert run_migrate(db, db_path) == (0, 1)

    assert schema_version(db) == 1
    assert tables(db) == TABLES
    assert db.execute("SELECT * FROM schema_version").fetchall() == [(1, "initial schema", T0)]
    assert db.execute("PRAGMA auto_vacuum").fetchone() == (2,)  # INCREMENTAL
    assert not db.in_transaction
    assert backups(db_path) == []


def test_migrating_again_changes_nothing(db: sqlite3.Connection, db_path: Path) -> None:
    run_migrate(db, db_path)

    assert run_migrate(db, db_path, now_ms=T0 + DAY) == (1, 1)
    assert run_migrate(db, db_path, now_ms=T0 + 2 * DAY) == (1, 1)

    assert db.execute("SELECT * FROM schema_version").fetchall() == [(1, "initial schema", T0)]
    assert backups(db_path) == []


def test_a_pending_migration_backs_up_a_database_with_data(
    db: sqlite3.Connection, db_path: Path
) -> None:
    run_migrate(db, db_path)
    db.execute("INSERT INTO control_requests (kind, payload, created_at) VALUES ('run', '{}', 1)")

    assert run_migrate(db, db_path, now_ms=T0 + 1500, migrations=WITH_V2) == (1, 2)

    assert schema_version(db) == 2
    assert db.execute("SELECT note FROM runs").fetchall() == []
    assert backups(db_path) == ["treadle-v1-20261109T120001Z.db"]
    copy_path = db_path.parent / "backups" / "treadle-v1-20261109T120001Z.db"
    assert stat.S_IMODE(copy_path.stat().st_mode) == 0o600
    with closing(sqlite3.connect(copy_path)) as copy:
        assert schema_version(copy) == 1
        assert copy.execute("SELECT kind FROM control_requests").fetchall() == [("run",)]


def test_backups_keep_the_newest_three(db: sqlite3.Connection, db_path: Path) -> None:
    run_migrate(db, db_path)
    migrations = list(MIGRATIONS)
    for day in range(1, 5):
        migrations.append(Migration(day + 1, f"day {day}", (f"CREATE TABLE day{day} (x)",)))
        run_migrate(db, db_path, now_ms=T0 + day * DAY, migrations=migrations)

    assert schema_version(db) == 5
    assert backups(db_path) == [
        "treadle-v2-20261111T120000Z.db",
        "treadle-v3-20261112T120000Z.db",
        "treadle-v4-20261113T120000Z.db",
    ]


def test_a_newer_schema_is_refused(db: sqlite3.Connection, db_path: Path) -> None:
    run_migrate(db, db_path, migrations=WITH_V2)

    with pytest.raises(SchemaTooNewError) as raised:
        run_migrate(db, db_path)

    assert str(raised.value) == (
        "this database was created by a newer Treadle (schema 2 > 1); upgrade Treadle"
    )
    assert raised.value.exit_code == ExitCode.CONFIG
    assert schema_version(db) == 2


def test_a_failed_migration_leaves_the_previous_schema(
    db: sqlite3.Connection, db_path: Path
) -> None:
    run_migrate(db, db_path)
    statements = ("CREATE TABLE half_done (x)", "INSERT INTO missing VALUES (1)")
    broken = Migration(2, "broken", statements)

    with pytest.raises(sqlite3.OperationalError, match="no such table: missing"):
        run_migrate(db, db_path, migrations=(*MIGRATIONS, broken))

    assert not db.in_transaction
    assert schema_version(db) == 1
    assert tables(db) == TABLES


@pytest.fixture
def seeded(conn: sqlite3.Connection, repo: Repository, tmp_path: Path) -> sqlite3.Connection:
    """A database with one valid row in every table that has CHECK constraints."""
    run = Runs(repo, project_workflow(tmp_path)).running(steps=["build"])
    repo.start_attempt(run.id, "build", 1, at_ms=T0, delay_before_ms=0)
    repo.append_logs(run.id, [NewLogLine(1, T0, "build", 1, LogStream.STDOUT, "hello")])
    repo.enqueue_control("run", {}, at_ms=T0)
    repo.finish_attempt(
        run.id,
        "build",
        1,
        status=AttemptStatus.SUCCEEDED,
        at_ms=T0,
        exit_code=0,
        error=None,
        log_bytes=6,
        log_truncated=False,
    )
    return conn


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE runs SET status = 'paused'",
        "UPDATE runs SET reason = 'bored'",
        "UPDATE runs SET trigger_kind = 'email'",
        "UPDATE runs SET attempt = 0",
        "UPDATE steps SET status = 'waiting'",
        "UPDATE steps SET tolerated = 2",
        "UPDATE steps SET skip_reason = 'tired'",
        "UPDATE step_attempts SET status = 'pending'",
        "UPDATE step_attempts SET attempt = 0",
        "UPDATE step_attempts SET log_truncated = 2",
        "UPDATE log_lines SET stream = 'stdin'",
        "UPDATE control_requests SET kind = 'pause'",
    ],
)
def test_check_constraints_reject_unknown_values(
    seeded: sqlite3.Connection, statement: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        seeded.execute(statement)


def test_runs_must_reference_a_stored_workflow_version(seeded: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        seeded.execute("UPDATE runs SET digest = 'unknown'")


def test_a_migration_that_sqlite_rolls_back_keeps_the_original_error(
    db: sqlite3.Connection, db_path: Path
) -> None:
    run_migrate(db, db_path)
    statements = (
        "CREATE TABLE refused (x)",
        "CREATE TRIGGER refuse BEFORE INSERT ON refused"
        " BEGIN SELECT RAISE(ROLLBACK, 'refused by a trigger'); END",
        "INSERT INTO refused VALUES (1)",
    )

    with pytest.raises(sqlite3.IntegrityError, match="refused by a trigger"):
        run_migrate(db, db_path, migrations=(*MIGRATIONS, Migration(2, "refused", statements)))

    assert not db.in_transaction
    assert schema_version(db) == 1
    assert tables(db) == TABLES
