"""SQLite connections with Treadle's pragmas and explicit transaction control."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from pathlib import Path
from typing import Final

from treadle.errors import StoreError

MIN_SQLITE_VERSION: Final = (3, 31, 0)
_EVERY_CONNECTION: Final = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
)
# synchronous=FULL makes a committed schedule-slot advance survive power loss.
_WRITER: Final = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA synchronous = FULL",
)
_READER: Final = ("PRAGMA query_only = ON",)


def sqlite_version() -> tuple[int, int, int]:
    """Return the version of the SQLite library that `sqlite3` links."""
    return sqlite3.sqlite_version_info


def connect(path: Path, *, query_only: bool = False) -> sqlite3.Connection:
    """Open a connection with explicit transaction control and `check_same_thread=False`.

    A writer connection creates a missing file with mode 0600 and uses WAL with
    `synchronous=FULL`. A `query_only` reader never creates the file. Raises StoreError when
    SQLite is older than `MIN_SQLITE_VERSION`.
    """
    version = sqlite_version()
    if version < MIN_SQLITE_VERSION:
        found = ".".join(str(part) for part in version)
        raise StoreError(f"Treadle needs SQLite 3.31.0 or later, but Python uses SQLite {found}")
    if query_only:
        # mode=rw opens an existing file and never creates one.
        conn = _open(f"{path.absolute().as_uri()}?mode=rw", uri=True)
    else:
        # O_EXCL never opens an existing file: closing a second descriptor of a database that
        # this process has open would drop SQLite's POSIX locks on it.
        with contextlib.suppress(FileExistsError):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        conn = _open(str(path), uri=False)
    try:
        for pragma in (*_EVERY_CONNECTION, *(_READER if query_only else _WRITER)):
            conn.execute(pragma)
    except BaseException:
        conn.close()
        raise
    return conn


def _open(target: str, *, uri: bool) -> sqlite3.Connection:
    """Connect without implicit transactions; Treadle issues BEGIN and COMMIT itself."""
    # An if/else lets mypy skip the branch that the target version rules out. A `return`
    # after an `if` that always returns would be reported as unreachable on Python 3.12+.
    if sys.version_info >= (3, 12):
        conn = sqlite3.connect(target, uri=uri, check_same_thread=False, autocommit=True)
    else:
        conn = sqlite3.connect(target, uri=uri, check_same_thread=False, isolation_level=None)
    return conn
