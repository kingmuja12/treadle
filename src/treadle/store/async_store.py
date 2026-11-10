"""The async facade over the repository: one writer thread and a pool of reader threads.

The event loop never calls `sqlite3` directly. Virtual-time tests open the store with
`inline=True`, which runs every repository call on the loop thread instead.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, TypeVar

from treadle.clock import SystemClock, inline_runner, thread_runner, to_ms
from treadle.config import Settings
from treadle.errors import NoDatabaseError, StoreError
from treadle.models import LogLine, RunDetail, RunFilter, RunPage, RunRecord
from treadle.store.db import connect
from treadle.store.migrations import CURRENT_VERSION, _too_new, migrate, schema_version
from treadle.store.repo import Repository

_T = TypeVar("_T")


class AsyncStore:
    """The database of one state directory; implements `RunReader`.

    Writes run one at a time on the writer thread, in submission order. Reads run on a pool
    of reader threads, each with its own `query_only` connection. If an awaiting coroutine
    is cancelled, its job still finishes in its thread; conditional updates make that late
    completion harmless.
    """

    def __init__(
        self,
        settings: Settings,
        writer: sqlite3.Connection,
        *,
        version: int,
        owner: bool,
        inline: bool,
        readers: int,
    ) -> None:
        """Wrap an open, checked writer connection. Use `AsyncStore.open` instead."""
        self._settings = settings
        self._version = version
        self._owner = owner
        self._writer = Repository(writer, project_dir=settings.project_dir)
        self._connections = [writer]
        self._local = threading.local()
        self._closed = False
        self._write_pool: ThreadPoolExecutor | None = None
        self._read_pool: ThreadPoolExecutor | None = None
        if inline:
            self._open_reader()
        else:
            self._write_pool = ThreadPoolExecutor(1, "treadle-db-write")
            self._read_pool = ThreadPoolExecutor(readers, "treadle-db-read", self._open_reader)

    @classmethod
    async def open(
        cls,
        settings: Settings,
        *,
        mode: Literal["owner", "client"] = "owner",
        inline: bool = False,
        readers: int = 2,
    ) -> AsyncStore:
        """Open the store of `settings.state_dir`.

        An `owner` (the lock holder) creates the state directory (mode 0700) with `tmp/` and
        `backups/`, checks the SQLite version, and migrates, with a backup when the database
        holds data. A `client` never migrates: it raises NoDatabaseError when the database
        doesn't exist, SchemaTooNewError when its schema is newer, and StoreError when it's
        older. A client may still write, for control requests.
        """
        runner = inline_runner if inline else thread_runner
        return await runner(cls._open_blocking, settings, mode == "owner", inline, readers)

    @property
    def schema_version(self) -> int:
        """The schema version of the open database."""
        return self._version

    async def write(self, fn: Callable[[Repository], _T], *, transaction: bool = True) -> _T:
        """Run `fn` on the writer thread inside `Repository.transaction()`, and return its result.

        Writes run in submission order. With `transaction=False`, `fn` runs outside any
        transaction, which `Repository.incremental_vacuum` needs.
        """
        if transaction:
            return await self._call(self._write_pool, lambda: self._in_transaction(fn))
        return await self._call(self._write_pool, lambda: fn(self._writer))

    async def read(self, fn: Callable[[Repository], _T]) -> _T:
        """Run `fn` on a reader thread inside `Repository.snapshot()`, and return its result."""
        return await self._call(self._read_pool, lambda: self._in_snapshot(fn))

    async def close(self) -> None:
        """Checkpoint and optimize an owner's database, then close every connection and pool.

        Jobs that are already submitted finish first. Closing twice does nothing.
        """
        if self._closed:
            return
        self._closed = True
        runner = inline_runner if self._write_pool is None else thread_runner
        await runner(self._shutdown)

    async def get_run(self, run_id: int) -> RunDetail | None:
        """Return a run with its steps and attempts, or None."""
        return await self.read(lambda repo: repo.get_run_detail(run_id))

    async def list_runs(self, flt: RunFilter) -> RunPage:
        """Return one page of matching runs, newest first."""
        return await self.read(lambda repo: repo.list_runs(flt))

    async def logs(
        self,
        run_id: int,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        after_seq: int = 0,
        limit: int = 1000,
        tail: bool = False,
    ) -> list[LogLine]:
        """Return log lines in ascending `seq` order; see `Repository.logs`."""
        return await self.read(
            lambda repo: repo.logs(
                run_id,
                step_id=step_id,
                attempt=attempt,
                after_seq=after_seq,
                limit=limit,
                tail=tail,
            )
        )

    async def last_runs(self) -> dict[str, RunRecord]:
        """Return the newest run of each workflow, by workflow ID."""
        return await self.read(lambda repo: repo.last_runs())

    async def latest_run_id(self, workflow_id: str | None = None) -> int | None:
        """Return the newest run ID, of one workflow when `workflow_id` is set, or None."""
        return await self.read(lambda repo: repo.latest_run_id(workflow_id))

    @classmethod
    def _open_blocking(
        cls, settings: Settings, owner: bool, inline: bool, readers: int
    ) -> AsyncStore:
        if owner:
            for directory in (settings.state_dir, settings.tmp_dir, settings.backups_dir):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not settings.db_path.exists():
            raise NoDatabaseError(f"{settings.db_path} doesn't exist yet; no workflow has run")
        writer = connect(settings.db_path)
        try:
            version = _migrate(writer, settings) if owner else _client_version(writer)
            return cls(
                settings, writer, version=version, owner=owner, inline=inline, readers=readers
            )
        except BaseException:
            writer.close()
            raise

    def _open_reader(self) -> None:
        """Open the calling thread's reader connection; the reader pool's initializer."""
        conn = connect(self._settings.db_path, query_only=True)
        self._connections.append(conn)
        self._local.reader = Repository(conn, project_dir=self._settings.project_dir)

    def _in_transaction(self, fn: Callable[[Repository], _T]) -> _T:
        with self._writer.transaction():
            return fn(self._writer)

    def _in_snapshot(self, fn: Callable[[Repository], _T]) -> _T:
        reader: Repository = self._local.reader
        with reader.snapshot():
            return fn(reader)

    async def _call(self, pool: ThreadPoolExecutor | None, job: Callable[[], _T]) -> _T:
        """Run `job` in `pool`, or inline without a pool. A cancelled caller lets it finish."""
        if self._closed:
            raise StoreError("the store is closed")
        if pool is None:
            return job()
        return await asyncio.shield(asyncio.wrap_future(pool.submit(job)))

    def _shutdown(self) -> None:
        for pool in (self._read_pool, self._write_pool):
            if pool is not None:
                pool.shutdown()
        writer, *readers = self._connections
        for conn in readers:
            conn.close()
        try:
            if self._owner:
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("PRAGMA optimize")
        finally:
            writer.close()


def _migrate(conn: sqlite3.Connection, settings: Settings) -> int:
    """Migrate an owner's database, with a backup when it holds data; return the version."""
    now_ms = to_ms(SystemClock().now())
    _, version = migrate(
        conn, db_path=settings.db_path, backup_dir=settings.backups_dir, now_ms=now_ms
    )
    return version


def _client_version(conn: sqlite3.Connection) -> int:
    """Return the schema version, which a client needs to match `CURRENT_VERSION` exactly."""
    version = schema_version(conn)
    if version > CURRENT_VERSION:
        raise _too_new(version, CURRENT_VERSION)
    if version < CURRENT_VERSION:
        raise StoreError("start `treadle serve` or `treadle run` once to upgrade the database")
    return version
