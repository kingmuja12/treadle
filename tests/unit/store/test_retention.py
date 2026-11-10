from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from tests.support.clock import FakeClock
from tests.unit.store.helpers import DAY, HOUR, T0, Runs, project_workflow

from treadle.clock import to_ms
from treadle.config import RetentionSettings, Settings
from treadle.models import LogStream, RunFilter, RunRecord, RunStatus, Workflow
from treadle.retention import retention_loop, run_retention
from treadle.store.async_store import AsyncStore
from treadle.store.repo import NewLogLine, PruneStats, Repository

ONE_DAY = RetentionSettings(max_age=DAY / 1000, max_runs_per_workflow=500, interval=3600.0)
ALL_RUNS = RunFilter(limit=200)
CURRENT = "c" * 64
RETIRED = "r" * 64


def count_rows(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """Count a run's rows in the tables that hang off `runs`."""
    queries = {
        "steps": "SELECT COUNT(*) FROM steps WHERE run_id = ?",
        "step_attempts": "SELECT COUNT(*) FROM step_attempts WHERE run_id = ?",
        "log_lines": "SELECT COUNT(*) FROM log_lines WHERE run_id = ?",
    }
    return {table: conn.execute(sql, (run_id,)).fetchone()[0] for table, sql in queries.items()}


def free_pages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA freelist_count").fetchone()[0])


def fill_and_prune(repo: Repository, runs: Runs) -> None:
    """Store a run with about 600 KB of logs, then prune it, which frees pages."""
    run = runs.finished(at_ms=T0)
    lines = [NewLogLine(seq, T0, None, None, LogStream.STDOUT, "x" * 2000) for seq in range(1, 300)]
    repo.append_logs(run.id, lines)
    assert repo.prune_batch(ONE_DAY, now_ms=T0 + 2 * DAY) == 1


def test_prune_batch_deletes_runs_older_than_max_age_with_their_rows(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    old = runs.finished(RunStatus.FAILED, at_ms=T0 - 1, steps=["build"])
    repo.start_attempt(old.id, "build", 1, at_ms=T0 - 1, delay_before_ms=0)
    repo.append_logs(old.id, [NewLogLine(1, T0 - 1, "build", 1, LogStream.STDOUT, "old output")])
    survivors = [
        runs.finished(at_ms=T0),  # exactly max_age old at `now`, so not older
        runs.queued(created_at_ms=T0 - 1),
        runs.running(at_ms=T0 - 1),
    ]

    assert repo.prune_batch(ONE_DAY, now_ms=T0 + DAY) == 1

    assert repo.get_run(old.id) is None
    assert count_rows(conn, old.id) == {"steps": 0, "step_attempts": 0, "log_lines": 0}
    assert [runs.get(run.id) for run in survivors] == survivors


def test_prune_batch_keeps_the_newest_runs_of_each_workflow(
    repo: Repository, runs: Runs, other_workflow: Workflow
) -> None:
    oldest, older = runs.finished(), runs.finished()
    others = [runs.finished(workflow=other_workflow) for _ in range(2)]
    newer, newest = runs.finished(), runs.finished(RunStatus.CANCELLED)
    keep_two = RetentionSettings(max_age=3650 * DAY / 1000, max_runs_per_workflow=2)

    assert repo.prune_batch(keep_two, now_ms=T0) == 2

    assert (repo.get_run(oldest.id), repo.get_run(older.id)) == (None, None)
    assert [runs.get(run.id) for run in (*others, newer, newest)] == [*others, newer, newest]


def test_prune_batch_never_deletes_queued_or_running_runs(repo: Repository, runs: Runs) -> None:
    waiting, active = runs.queued(), runs.running()
    runs.finished()

    assert repo.prune_batch(RetentionSettings(max_runs_per_workflow=1), now_ms=T0 + 400 * DAY) == 1

    assert runs.get(waiting.id).status is RunStatus.QUEUED
    assert runs.get(active.id).status is RunStatus.RUNNING


def test_prune_batch_deletes_at_most_one_batch_oldest_first(repo: Repository, runs: Runs) -> None:
    ids = [runs.finished(at_ms=T0).id for _ in range(5)]

    assert repo.prune_batch(ONE_DAY, now_ms=T0 + 2 * DAY, batch=2) == 2

    assert [repo.get_run(run_id) is None for run_id in ids] == [True, True, False, False, False]


def test_pruning_a_run_unlinks_its_rerun(repo: Repository, runs: Runs) -> None:
    original = runs.finished(RunStatus.FAILED, at_ms=T0)
    rerun = runs.queued(rerun_of=original.id, attempt=2, created_at_ms=T0 + 2 * DAY)

    assert repo.prune_batch(ONE_DAY, now_ms=T0 + 2 * DAY) == 1

    assert runs.get(rerun.id).rerun_of is None


def test_prune_misc_deletes_old_deliveries_controls_and_unused_versions(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    now = T0 + 30 * DAY
    used = runs.finished(at_ms=now).digest
    conn.executemany(
        "INSERT INTO workflow_versions VALUES (?, 'wf', 'workflows/wf.yml', '', ?)",
        [(CURRENT, T0), (RETIRED, T0)],
    )
    conn.executemany(
        "INSERT INTO webhook_deliveries (hook, digest, received_at) VALUES ('h', ?, ?)",
        [("too-old", now - 7 * DAY - 1), ("week-old", now - 7 * DAY)],
    )
    consumed_old = repo.enqueue_control("run", {}, at_ms=now - DAY - 1)
    repo.claim_controls(at_ms=now - DAY)
    pending_old = repo.enqueue_control("run", {}, at_ms=now - 2 * DAY)
    consumed_recent = repo.enqueue_control("run", {}, at_ms=now - HOUR)
    repo.complete_control(consumed_recent, {"run_id": 1}, at_ms=now - HOUR)

    stats = repo.prune_misc(now_ms=now, keep_digests={CURRENT})

    assert stats == PruneStats(runs=0, versions=1, deliveries=1, controls=1)
    versions = {row[0] for row in conn.execute("SELECT digest FROM workflow_versions")}
    assert versions == {used, CURRENT}
    digests = [row[0] for row in conn.execute("SELECT digest FROM webhook_deliveries")]
    assert digests == ["week-old"]
    controls = {row[0] for row in conn.execute("SELECT id FROM control_requests")}
    assert controls == {pending_old, consumed_recent}
    assert consumed_old not in controls


def test_incremental_vacuum_returns_free_pages(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    fill_and_prune(repo, runs)
    assert free_pages(conn) > 100

    repo.incremental_vacuum()

    assert free_pages(conn) == 0


def test_incremental_vacuum_stops_at_its_page_limit(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    fill_and_prune(repo, runs)
    before = free_pages(conn)

    repo.incremental_vacuum(pages=10)

    assert free_pages(conn) == before - 10


@pytest.fixture
def project_wf(settings: Settings) -> Workflow:
    """The workflow `wf` inside the store fixture's project."""
    return project_workflow(settings.project_dir)


async def store_finished(
    store: AsyncStore, workflow: Workflow, *, at_ms: int, count: int = 1
) -> list[RunRecord]:
    """Store `count` finished runs of `workflow` in one transaction."""

    def write(repo: Repository) -> list[RunRecord]:
        return [Runs(repo, workflow).finished(at_ms=at_ms) for _ in range(count)]

    return await store.write(write)


async def stored_ids(store: AsyncStore) -> list[int]:
    page = await store.list_runs(ALL_RUNS)
    return sorted(run.id for run in page.runs)


async def test_run_retention_prunes_in_batches_then_the_other_tables(
    store: AsyncStore, project_wf: Workflow
) -> None:
    await store_finished(store, project_wf, at_ms=T0, count=250)
    fresh = await store_finished(store, project_wf, at_ms=T0 + 2 * DAY)

    stats = await run_retention(store, ONE_DAY, now_ms=T0 + 2 * DAY, keep_digests=set())

    assert stats == PruneStats(runs=250)
    assert await stored_ids(store) == [fresh[0].id]


async def test_run_retention_stops_after_max_batches(
    store: AsyncStore, project_wf: Workflow
) -> None:
    await store_finished(store, project_wf, at_ms=T0, count=250)

    stats = await run_retention(
        store, ONE_DAY, now_ms=T0 + 2 * DAY, keep_digests=set(), max_batches=2
    )

    assert stats.runs == 200
    assert len(await stored_ids(store)) == 50


async def test_run_retention_yields_to_the_event_loop_between_batches(
    store: AsyncStore, project_wf: Workflow
) -> None:
    await store_finished(store, project_wf, at_ms=T0, count=250)
    seen: list[int] = []

    async def observe() -> None:
        seen.append(len(await stored_ids(store)))

    observer = asyncio.create_task(observe())
    await run_retention(store, ONE_DAY, now_ms=T0 + 2 * DAY, keep_digests=set())
    await observer

    assert seen == [150]  # the observer ran right after the first batch of 100


async def test_run_retention_prunes_versions_outside_keep_digests(
    store: AsyncStore, project_wf: Workflow, settings: Settings
) -> None:
    retired = project_workflow(settings.project_dir, "retired")
    await store_finished(store, retired, at_ms=T0)
    await store_finished(store, project_wf, at_ms=T0)

    stats = await run_retention(
        store, ONE_DAY, now_ms=T0 + 2 * DAY, keep_digests={project_wf.digest}
    )

    assert stats == PruneStats(runs=2, versions=1)
    assert await store.read(lambda repo: repo.workflow_version(retired.digest)) is None
    assert await store.read(lambda repo: repo.workflow_version(project_wf.digest)) is not None


async def test_retention_loop_runs_now_and_then_every_interval(
    store: AsyncStore, project_wf: Workflow, clock: FakeClock
) -> None:
    policy = RetentionSettings(max_age=HOUR / 1000, max_runs_per_workflow=500, interval=600.0)
    now = to_ms(clock.now())
    await store_finished(store, project_wf, at_ms=now - 2 * HOUR)
    [fresh] = await store_finished(store, project_wf, at_ms=now)
    passes: list[float] = []

    def keep_digests() -> frozenset[str]:
        passes.append(asyncio.get_running_loop().time())
        return frozenset({project_wf.digest})

    task = asyncio.create_task(retention_loop(store, policy, clock, keep_digests))
    try:
        await asyncio.sleep(0)
        assert passes == [0.0]
        assert await stored_ids(store) == [fresh.id]

        await asyncio.sleep(3600.5)  # passes from 600 s to 3600 s; `fresh` isn't older yet
        assert passes == [600.0 * n for n in range(7)]
        assert await stored_ids(store) == [fresh.id]

        await asyncio.sleep(600)  # the pass at 4200 s finds `fresh` more than an hour old
        assert await stored_ids(store) == []
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_retention_loop_logs_a_failed_pass_and_keeps_going(
    store: AsyncStore, project_wf: Workflow, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="treadle.retention")
    policy = RetentionSettings(max_age=HOUR / 1000, interval=600.0)
    await store_finished(store, project_wf, at_ms=to_ms(clock.now()) - 2 * HOUR)
    calls: list[float] = []

    def keep_digests() -> frozenset[str]:
        calls.append(asyncio.get_running_loop().time())
        if len(calls) == 1:
            raise RuntimeError("the registry isn't ready")
        return frozenset()

    task = asyncio.create_task(retention_loop(store, policy, clock, keep_digests))
    try:
        await asyncio.sleep(0)
        assert "retention failed; trying again in 10m" in caplog.text
        assert len(await stored_ids(store)) == 1

        await asyncio.sleep(600)
        assert calls == [0.0, 600.0]
        assert await stored_ids(store) == []
        assert "retention pruned runs=1 versions=1 deliveries=0 controls=0" in caplog.text
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
