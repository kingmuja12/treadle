from __future__ import annotations

import sqlite3
import stat
import threading
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from tests.unit.store.helpers import T0, Runs, project_workflow

from treadle.config import Settings
from treadle.errors import NoDatabaseError, SchemaTooNewError, StoreError
from treadle.models import LogStream, RunFilter, RunRecord, Workflow
from treadle.store.async_store import AsyncStore
from treadle.store.db import connect
from treadle.store.migrations import CURRENT_VERSION, MIGRATIONS, Migration, migrate, schema_version
from treadle.store.repo import NewLogLine, Repository
from treadle.types import ExitCode


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def workflow(settings: Settings) -> Workflow:
    return project_workflow(settings.project_dir)


def migrate_to_v2(settings: Settings) -> None:
    """Upgrade the database to a schema version this Treadle doesn't know."""
    extra = Migration(2, "from the future", ("CREATE TABLE future (x)",))
    with closing(connect(settings.db_path)) as conn:
        migrate(
            conn,
            db_path=settings.db_path,
            backup_dir=settings.backups_dir,
            now_ms=T0,
            migrations=(*MIGRATIONS, extra),
        )


async def test_owner_mode_creates_the_state_directory_and_migrates(settings: Settings) -> None:
    store = await AsyncStore.open(settings, inline=True)
    try:
        assert store.schema_version == CURRENT_VERSION
        for directory in (settings.state_dir, settings.tmp_dir, settings.backups_dir):
            assert mode(directory) == 0o700
        assert mode(settings.db_path) == 0o600
    finally:
        await store.close()


async def test_owner_mode_reopens_an_existing_database(
    settings: Settings, workflow: Workflow
) -> None:
    first = await AsyncStore.open(settings, inline=True)
    run = await first.write(lambda repo: Runs(repo, workflow).queued())
    await first.close()

    second = await AsyncStore.open(settings, inline=True)
    try:
        assert await second.latest_run_id() == run.id
        assert list(settings.backups_dir.iterdir()) == []
    finally:
        await second.close()


async def test_client_mode_without_a_database_raises_no_database(settings: Settings) -> None:
    with pytest.raises(NoDatabaseError, match="doesn't exist yet"):
        await AsyncStore.open(settings, mode="client", inline=True)

    assert not settings.state_dir.exists()


async def test_client_mode_reads_and_writes_without_migrating(
    settings: Settings, store: AsyncStore, workflow: Workflow
) -> None:
    run = await store.write(lambda repo: Runs(repo, workflow).queued())

    client = await AsyncStore.open(settings, mode="client", inline=True)
    try:
        assert client.schema_version == CURRENT_VERSION
        assert await client.latest_run_id() == run.id
        request_id = await client.write(
            lambda repo: repo.enqueue_control("cancel", {"run_id": run.id}, at_ms=T0)
        )
    finally:
        await client.close()

    claimed = await store.write(lambda repo: repo.claim_controls(at_ms=T0))
    assert [(request.id, request.payload) for request in claimed] == [
        (request_id, {"run_id": run.id})
    ]


@pytest.mark.parametrize("open_mode", ["owner", "client"])
async def test_a_newer_schema_is_refused(
    settings: Settings, open_mode: Literal["owner", "client"]
) -> None:
    await (await AsyncStore.open(settings, inline=True)).close()
    migrate_to_v2(settings)

    with pytest.raises(SchemaTooNewError, match=r"schema 2 > 1\); upgrade Treadle") as raised:
        await AsyncStore.open(settings, mode=open_mode, inline=True)

    assert raised.value.exit_code == ExitCode.CONFIG


async def test_client_mode_refuses_an_older_schema_and_leaves_it(settings: Settings) -> None:
    settings.state_dir.mkdir(mode=0o700)
    with closing(connect(settings.db_path)):
        pass  # a database file that no owner has migrated yet

    with pytest.raises(StoreError, match="start `treadle serve` or `treadle run` once"):
        await AsyncStore.open(settings, mode="client", inline=True)

    with closing(connect(settings.db_path)) as conn:
        assert schema_version(conn) == 0


async def test_inline_mode_runs_every_call_on_the_loop_thread(store: AsyncStore) -> None:
    loop_thread = threading.get_ident()

    assert await store.write(lambda repo: threading.get_ident()) == loop_thread
    assert await store.read(lambda repo: threading.get_ident()) == loop_thread


async def test_a_write_is_one_transaction(store: AsyncStore, workflow: Workflow) -> None:
    def create_then_fail(repo: Repository) -> None:
        Runs(repo, workflow).queued()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await store.write(create_then_fail)

    assert await store.latest_run_id() is None


async def test_a_write_without_a_transaction_runs_outside_one(
    store: AsyncStore, settings: Settings
) -> None:
    def holds_the_write_lock(repo: Repository) -> bool:
        with closing(connect(settings.db_path)) as other:
            other.execute("PRAGMA busy_timeout = 0")
            try:
                other.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return True
            other.execute("ROLLBACK")
            return False

    assert await store.write(holds_the_write_lock) is True
    assert await store.write(holds_the_write_lock, transaction=False) is False


async def test_reads_see_one_snapshot(
    store: AsyncStore, settings: Settings, workflow: Workflow
) -> None:
    def read_around_a_commit(repo: Repository) -> tuple[int | None, int | None]:
        before = repo.latest_run_id()
        with closing(connect(settings.db_path)) as other:
            Runs(Repository(other, project_dir=settings.project_dir), workflow).queued()
        return before, repo.latest_run_id()

    assert await store.read(read_around_a_commit) == (None, None)
    assert await store.latest_run_id() == 1


async def test_reads_never_write(store: AsyncStore) -> None:
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await store.read(lambda repo: repo.enqueue_control("run", {}, at_ms=T0))


async def test_run_reader_methods(store: AsyncStore, workflow: Workflow) -> None:
    def seed(repo: Repository) -> RunRecord:
        run = Runs(repo, workflow).running(steps=["build"])
        repo.append_logs(
            run.id,
            [
                NewLogLine(1, T0, "build", 1, LogStream.STDOUT, "one"),
                NewLogLine(2, T0, "build", 1, LogStream.STDOUT, "two"),
            ],
        )
        return run

    run = await store.write(seed)

    detail = await store.get_run(run.id)
    assert detail is not None
    assert (detail.run, [s.step_id for s in detail.steps]) == (run, ["build"])
    assert (await store.list_runs(RunFilter(workflow_id="wf"))).runs == (run,)
    assert [line.text for line in await store.logs(run.id, step_id="build", attempt=1)] == [
        "one",
        "two",
    ]
    assert [line.text for line in await store.logs(run.id, tail=True, limit=1)] == ["two"]
    assert [line.seq for line in await store.logs(run.id, after_seq=1)] == [2]
    assert await store.last_runs() == {"wf": run}
    assert await store.latest_run_id("wf") == run.id
    assert await store.get_run(999) is None


async def test_close_checkpoints_the_wal_and_is_idempotent(
    settings: Settings, workflow: Workflow
) -> None:
    store = await AsyncStore.open(settings, inline=True)
    await store.write(lambda repo: Runs(repo, workflow).queued())
    wal = settings.db_path.with_name("treadle.db-wal")
    assert wal.stat().st_size > 0

    await store.close()
    await store.close()

    assert not wal.exists() or wal.stat().st_size == 0
    with pytest.raises(StoreError, match="the store is closed"):
        await store.read(lambda repo: repo.latest_run_id())
    with pytest.raises(StoreError, match="the store is closed"):
        await store.write(lambda repo: repo.latest_run_id())
