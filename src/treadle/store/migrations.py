"""Schema migrations. Migration 1 is the DDL in section 7."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from treadle.clock import from_ms
from treadle.errors import SchemaTooNewError
from treadle.store.db import connect


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema version. `migrate` runs the statements one at a time in one transaction."""

    version: int
    description: str
    statements: tuple[str, ...]


_SCHEMA_V1: Final = (
    """CREATE TABLE schema_version (
  version     INTEGER PRIMARY KEY,
  description TEXT    NOT NULL,
  applied_at  INTEGER NOT NULL
)""",
    """CREATE TABLE workflow_versions (
  digest      TEXT    PRIMARY KEY,
  workflow_id TEXT    NOT NULL,
  path        TEXT    NOT NULL,
  source      TEXT    NOT NULL,
  created_at  INTEGER NOT NULL
)""",
    "CREATE INDEX workflow_versions_by_workflow ON workflow_versions (workflow_id, created_at)",
    """CREATE TABLE runs (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id         TEXT    NOT NULL,
  workflow_name       TEXT    NOT NULL,
  digest              TEXT    NOT NULL REFERENCES workflow_versions (digest),
  status              TEXT    NOT NULL CHECK (status IN (
                        'queued','running','succeeded','failed',
                        'cancelled','timed_out','skipped','interrupted')),
  reason              TEXT    CHECK (reason IN (
                        'already_active','queue_full','user','replaced','timeout',
                        'shutdown','workflow_removed','executor_stopped','setup_failed')),
  error               TEXT,
  trigger_kind        TEXT    NOT NULL CHECK (
                        trigger_kind IN ('manual','schedule','watch','webhook')),
  trigger_key         TEXT,
  trigger_json        TEXT    NOT NULL,
  inputs_json         TEXT    NOT NULL,
  scheduled_for       INTEGER,
  attempt             INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  rerun_of            INTEGER REFERENCES runs (id) ON DELETE SET NULL,
  replaced_by         INTEGER REFERENCES runs (id) ON DELETE SET NULL,
  created_at          INTEGER NOT NULL,
  started_at          INTEGER,
  finished_at         INTEGER,
  cancel_requested_at INTEGER,
  executor_pid        INTEGER
)""",
    "CREATE UNIQUE INDEX runs_schedule_slot ON runs (trigger_key, scheduled_for)"
    " WHERE scheduled_for IS NOT NULL",
    "CREATE INDEX runs_by_workflow ON runs (workflow_id, id)",
    "CREATE INDEX runs_active ON runs (status, id) WHERE status IN ('queued','running')",
    "CREATE INDEX runs_by_finished ON runs (finished_at) WHERE finished_at IS NOT NULL",
    "CREATE INDEX runs_by_digest ON runs (digest)",
    "CREATE INDEX runs_by_rerun ON runs (rerun_of) WHERE rerun_of IS NOT NULL",
    "CREATE INDEX runs_by_replaced ON runs (replaced_by) WHERE replaced_by IS NOT NULL",
    """CREATE TABLE steps (
  run_id       INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  step_id      TEXT    NOT NULL,
  name         TEXT    NOT NULL,
  position     INTEGER NOT NULL,
  uses         TEXT    NOT NULL,
  status       TEXT    NOT NULL CHECK (status IN (
                 'pending','running','retrying','succeeded','failed',
                 'timed_out','cancelled','skipped','interrupted')),
  tolerated    INTEGER NOT NULL DEFAULT 0 CHECK (tolerated IN (0, 1)),
  attempts     INTEGER NOT NULL DEFAULT 0,
  skip_reason  TEXT    CHECK (skip_reason IN ('condition','upstream_failed','run_cancelled',
                                              'setup_failed','run_interrupted')),
  started_at   INTEGER,
  finished_at  INTEGER,
  outputs_json TEXT    NOT NULL DEFAULT '{}',
  error        TEXT,
  PRIMARY KEY (run_id, step_id)
) WITHOUT ROWID""",
    """CREATE TABLE step_attempts (
  run_id          INTEGER NOT NULL,
  step_id         TEXT    NOT NULL,
  attempt         INTEGER NOT NULL CHECK (attempt >= 1),
  status          TEXT    NOT NULL CHECK (status IN ('running','succeeded','failed','timed_out',
                                                     'cancelled','interrupted')),
  started_at      INTEGER NOT NULL,
  finished_at     INTEGER,
  exit_code       INTEGER,
  error           TEXT,
  delay_before_ms INTEGER NOT NULL DEFAULT 0,
  log_bytes       INTEGER NOT NULL DEFAULT 0,
  log_truncated   INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
  pgid            INTEGER,
  PRIMARY KEY (run_id, step_id, attempt),
  FOREIGN KEY (run_id, step_id) REFERENCES steps (run_id, step_id) ON DELETE CASCADE
) WITHOUT ROWID""",
    """CREATE TABLE log_lines (
  run_id  INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
  seq     INTEGER NOT NULL,
  ts      INTEGER NOT NULL,
  step_id TEXT,
  attempt INTEGER,
  stream  TEXT    NOT NULL CHECK (stream IN ('stdout','stderr','system')),
  text    TEXT    NOT NULL,
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID""",
    "CREATE INDEX log_lines_by_step ON log_lines (run_id, step_id, attempt, seq)",
    """CREATE TABLE schedule_state (
  trigger_key TEXT    PRIMARY KEY,
  workflow_id TEXT    NOT NULL,
  last_slot   INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
) WITHOUT ROWID""",
    """CREATE TABLE webhook_deliveries (
  hook        TEXT    NOT NULL,
  digest      TEXT    NOT NULL,
  received_at INTEGER NOT NULL,
  run_id      INTEGER REFERENCES runs (id) ON DELETE SET NULL,
  PRIMARY KEY (hook, digest)
) WITHOUT ROWID""",
    "CREATE INDEX webhook_deliveries_by_time ON webhook_deliveries (received_at)",
    "CREATE INDEX webhook_deliveries_by_run ON webhook_deliveries (run_id)"
    " WHERE run_id IS NOT NULL",
    """CREATE TABLE control_requests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT    NOT NULL CHECK (kind IN ('run','cancel')),
  payload     TEXT    NOT NULL,
  created_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  result      TEXT
)""",
    "CREATE INDEX control_requests_pending ON control_requests (id) WHERE consumed_at IS NULL",
)

MIGRATIONS: Final[tuple[Migration, ...]] = (Migration(1, "initial schema", _SCHEMA_V1),)
CURRENT_VERSION: Final[int] = MIGRATIONS[-1].version


def schema_version(conn: sqlite3.Connection) -> int:
    """Return `MAX(version)` from `schema_version`, or 0 when that table doesn't exist."""
    table = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    if conn.execute(table).fetchone() is None:
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0])


def migrate(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    backup_dir: Path,
    now_ms: int,
    keep_backups: int = 3,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> tuple[int, int]:
    """Apply pending migrations and return the (from, to) versions.

    A database that already holds data is first copied to
    `backups/treadle-v<old>-<YYYYmmddTHHMMSSZ>.db`, keeping the newest `keep_backups` copies.
    A new database gets `PRAGMA auto_vacuum = INCREMENTAL` first. Every pending migration
    runs in one `BEGIN IMMEDIATE` transaction, so a failure leaves the old schema intact.
    Raises SchemaTooNewError for a database newer than the last of `migrations` (in version
    order), which tests replace to add a migration.
    """
    found, newest = schema_version(conn), migrations[-1].version
    if found > newest:
        raise _too_new(found, newest)
    pending = [migration for migration in migrations if migration.version > found]
    if not pending:
        return found, found
    if conn.execute("SELECT 1 FROM sqlite_master").fetchone() is not None:
        _backup(conn, backup_dir, db_path.stem, found, now_ms=now_ms, keep=keep_backups)
    else:
        # Design note: connect() already switched the new file to WAL, which writes its header,
        # so the auto_vacuum setting takes effect only through a VACUUM (instant while empty).
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for migration in pending:
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.description, now_ms),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return found, newest


def _too_new(found: int, newest: int) -> SchemaTooNewError:
    """Return the error for a database whose schema is newer than this Treadle knows."""
    return SchemaTooNewError(
        f"this database was created by a newer Treadle (schema {found} > {newest}); upgrade Treadle"
    )


def _backup(
    conn: sqlite3.Connection, backup_dir: Path, stem: str, version: int, *, now_ms: int, keep: int
) -> None:
    """Copy the database with the backup API, then delete all but the newest `keep` copies.

    The copy is `<stem>-v<version>-<YYYYmmddTHHMMSSZ>.db`, created with mode 0600.
    """
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = from_ms(now_ms).strftime("%Y%m%dT%H%M%SZ")
    with closing(connect(backup_dir / f"{stem}-v{version}-{stamp}.db")) as copy:
        conn.backup(copy)
    copies = sorted(
        backup_dir.glob(f"{stem}-v*-*.db"), key=lambda path: path.name.rsplit("-", 1)[-1]
    )
    for old in copies[: max(len(copies) - keep, 0)]:
        old.unlink()
