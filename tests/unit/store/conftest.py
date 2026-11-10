"""Fixtures for repository tests: a migrated database in `tmp_path` and its repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.store.helpers import T0, Runs, project_workflow

from treadle.models import Workflow
from treadle.store.db import connect
from treadle.store.migrations import migrate
from treadle.store.repo import Repository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """The database file. `tmp_path` doubles as the project directory."""
    return tmp_path / "treadle.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A writer connection to a database at the current schema version."""
    connection = connect(db_path)
    migrate(connection, db_path=db_path, backup_dir=db_path.parent / "backups", now_ms=T0)
    yield connection
    connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection, tmp_path: Path) -> Repository:
    """The repository over `conn`."""
    return Repository(conn, project_dir=tmp_path)


@pytest.fixture
def workflow(tmp_path: Path) -> Workflow:
    """The workflow `wf`, stored at `workflows/wf.yml` inside the project."""
    return project_workflow(tmp_path)


@pytest.fixture
def other_workflow(tmp_path: Path) -> Workflow:
    """A second workflow, `other`."""
    return project_workflow(tmp_path, "other")


@pytest.fixture
def runs(repo: Repository, workflow: Workflow) -> Runs:
    """Stores runs of `wf` in a given state."""
    return Runs(repo, workflow)
