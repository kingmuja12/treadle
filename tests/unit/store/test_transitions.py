from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.unit.store.helpers import SECOND, T0, Runs

from treadle.clock import from_ms
from treadle.models import (
    AttemptRecord,
    AttemptStatus,
    LogLine,
    LogStream,
    RunReason,
    RunStatus,
    SkipReason,
    StepRecord,
    StepStatus,
)
from treadle.store.db import connect
from treadle.store.repo import NewLogLine, Repository, StepSeed, StepUpdate
from treadle.types import JSONObject

ANY_STATUS = frozenset(StepStatus)


def step(repo: Repository, run_id: int, step_id: str) -> StepRecord:
    detail = repo.get_run_detail(run_id)
    assert detail is not None
    return next(record for record in detail.steps if record.step_id == step_id)


def stored_attempt_count(conn: sqlite3.Connection, run_id: int, step_id: str) -> int:
    sql = "SELECT attempts FROM steps WHERE run_id = ? AND step_id = ?"
    return int(conn.execute(sql, (run_id, step_id)).fetchone()[0])


def test_start_run_moves_a_queued_run_to_running_with_pending_steps(
    repo: Repository, runs: Runs
) -> None:
    run = runs.queued()
    seeds = [StepSeed("build", "Build it", 0, "shell"), StepSeed("notify", "Notify", 1, "log")]

    assert repo.start_run(run.id, seeds, at_ms=T0 + 10, pid=777)

    detail = repo.get_run_detail(run.id)
    assert detail is not None
    assert (detail.run.status, detail.run.started_at) == (RunStatus.RUNNING, from_ms(T0 + 10))
    assert [
        (s.step_id, s.name, s.position, s.uses, s.status, s.tolerated, s.outputs, s.attempts)
        for s in detail.steps
    ] == [
        ("build", "Build it", 0, "shell", StepStatus.PENDING, False, {}, ()),
        ("notify", "Notify", 1, "log", StepStatus.PENDING, False, {}, ()),
    ]


def test_start_run_applies_once(repo: Repository, runs: Runs) -> None:
    run = runs.running(steps=["build"])

    assert not repo.start_run(run.id, [StepSeed("other", "other", 0, "fake")], at_ms=T0, pid=1)

    detail = repo.get_run_detail(run.id)
    assert detail is not None
    assert [s.step_id for s in detail.steps] == ["build"]


def test_finish_run_moves_a_running_run_to_a_terminal_status(repo: Repository, runs: Runs) -> None:
    run = runs.running()

    assert repo.finish_run(
        run.id,
        RunStatus.CANCELLED,
        at_ms=T0 + 90 * SECOND,
        reason=RunReason.USER,
        error="run cancelled (user)",
    )

    stored = runs.get(run.id)
    assert (stored.status, stored.reason, stored.error) == (
        RunStatus.CANCELLED,
        RunReason.USER,
        "run cancelled (user)",
    )
    assert stored.finished_at == from_ms(T0 + 90 * SECOND)
    assert stored.duration_ms == 90_000


def test_finish_run_needs_a_running_run(repo: Repository, runs: Runs) -> None:
    queued = runs.queued()

    assert not repo.finish_run(queued.id, RunStatus.SUCCEEDED, at_ms=T0, reason=None, error=None)
    assert runs.get(queued.id).status is RunStatus.QUEUED


def test_terminal_states_never_change(repo: Repository, runs: Runs) -> None:
    run = runs.finished(RunStatus.FAILED, error="step 'build' failed")

    assert not repo.finish_run(run.id, RunStatus.SUCCEEDED, at_ms=T0 + 1, reason=None, error=None)
    assert not repo.fail_queued(run.id, at_ms=T0 + 1, error="setup")
    assert repo.skip_queued([run.id], reason=RunReason.ALREADY_ACTIVE, at_ms=T0 + 1) == []
    assert repo.cancel_queued([run.id], reason=RunReason.USER, at_ms=T0 + 1) == []
    assert not repo.start_run(run.id, [], at_ms=T0 + 1, pid=1)
    repo.mark_cancel_requested(run.id, at_ms=T0 + 1)

    assert runs.get(run.id) == run


def test_fail_queued_records_a_setup_failure(repo: Repository, runs: Runs) -> None:
    run = runs.queued()

    assert repo.fail_queued(run.id, at_ms=T0 + 1, error="stored version no longer loads")

    stored = runs.get(run.id)
    assert (stored.status, stored.reason, stored.error, stored.finished_at) == (
        RunStatus.FAILED,
        RunReason.SETUP_FAILED,
        "stored version no longer loads",
        from_ms(T0 + 1),
    )
    assert not repo.fail_queued(runs.running().id, at_ms=T0 + 1, error="setup")


def test_skip_and_cancel_queued_return_only_the_runs_that_moved(
    repo: Repository, runs: Runs
) -> None:
    first, second, active = runs.queued(), runs.queued(), runs.running()

    skipped = repo.skip_queued(
        [first.id, active.id, 999], reason=RunReason.ALREADY_ACTIVE, at_ms=T0 + 1
    )
    cancelled = repo.cancel_queued(
        [first.id, second.id], reason=RunReason.WORKFLOW_REMOVED, at_ms=T0 + 2
    )

    assert (skipped, cancelled) == ([first.id], [second.id])
    assert (runs.get(first.id).status, runs.get(first.id).reason) == (
        RunStatus.SKIPPED,
        RunReason.ALREADY_ACTIVE,
    )
    assert runs.get(second.id).status is RunStatus.CANCELLED
    assert runs.get(second.id).reason is RunReason.WORKFLOW_REMOVED
    assert runs.get(second.id).replaced_by is None
    assert runs.get(active.id).status is RunStatus.RUNNING


def test_mark_cancel_requested_keeps_the_first_request(repo: Repository, runs: Runs) -> None:
    run = runs.running()

    repo.mark_cancel_requested(run.id, at_ms=T0 + 1)
    repo.mark_cancel_requested(run.id, at_ms=T0 + 2)

    assert runs.get(run.id).cancel_requested_at == from_ms(T0 + 1)


def test_a_step_moves_only_from_its_allowed_statuses(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    run = runs.running(steps=["build"])
    start = StepUpdate(StepStatus.RUNNING, frozenset({StepStatus.PENDING}), T0 + 1)

    assert repo.update_step(run.id, "build", start)
    assert not repo.update_step(run.id, "build", start)
    assert repo.update_step(
        run.id,
        "build",
        StepUpdate(StepStatus.RETRYING, frozenset({StepStatus.RUNNING}), T0 + 2, attempts=1),
    )
    assert repo.update_step(
        run.id, "build", StepUpdate(StepStatus.RUNNING, frozenset({StepStatus.RETRYING}), T0 + 3)
    )
    assert repo.update_step(
        run.id,
        "build",
        StepUpdate(
            StepStatus.FAILED,
            frozenset({StepStatus.RUNNING}),
            T0 + 4,
            attempts=2,
            tolerated=True,
            outputs={"artifact": "***"},
            error="exit code 23",
        ),
    )

    record = step(repo, run.id, "build")
    assert (record.status, record.tolerated, record.outputs, record.error) == (
        StepStatus.FAILED,
        True,
        {"artifact": "***"},
        "exit code 23",
    )
    assert (record.started_at, record.finished_at) == (from_ms(T0 + 1), from_ms(T0 + 4))
    assert stored_attempt_count(conn, run.id, "build") == 2
    assert not repo.update_step(run.id, "build", start)


def test_a_step_update_keeps_fields_it_leaves_unset(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    run = runs.running(steps=["build"])
    repo.update_step(
        run.id,
        "build",
        StepUpdate(
            StepStatus.FAILED,
            ANY_STATUS,
            T0 + 1,
            attempts=3,
            tolerated=True,
            outputs={"a": 1},
            error="boom",
        ),
    )

    assert repo.update_step(run.id, "build", StepUpdate(StepStatus.FAILED, ANY_STATUS, T0 + 2))

    record = step(repo, run.id, "build")
    assert (record.tolerated, record.outputs, record.error) == (True, {"a": 1}, "boom")
    assert stored_attempt_count(conn, run.id, "build") == 3


def test_a_skipped_step_records_its_reason_and_never_started(repo: Repository, runs: Runs) -> None:
    run = runs.running(steps=["deploy"])

    assert repo.update_step(
        run.id,
        "deploy",
        StepUpdate(
            StepStatus.SKIPPED,
            frozenset({StepStatus.PENDING}),
            T0 + 5,
            skip_reason=SkipReason.UPSTREAM_FAILED,
        ),
    )

    record = step(repo, run.id, "deploy")
    assert (record.status, record.skip_reason) == (StepStatus.SKIPPED, SkipReason.UPSTREAM_FAILED)
    assert (record.started_at, record.finished_at) == (None, from_ms(T0 + 5))
    assert record.duration_ms is None


def test_an_unknown_step_is_never_updated(repo: Repository, runs: Runs) -> None:
    run = runs.running(steps=["build"])

    assert not repo.update_step(run.id, "missing", StepUpdate(StepStatus.RUNNING, ANY_STATUS, T0))


def test_attempts_start_once_and_finish_once(
    repo: Repository, conn: sqlite3.Connection, runs: Runs
) -> None:
    run = runs.running(steps=["build"])
    repo.start_attempt(run.id, "build", 1, at_ms=T0 + 1, delay_before_ms=0)
    repo.set_attempt_pgid(run.id, "build", 1, 4242)

    assert repo.finish_attempt(
        run.id,
        "build",
        1,
        status=AttemptStatus.FAILED,
        at_ms=T0 + 500,
        exit_code=23,
        error="exit code 23",
        log_bytes=120,
        log_truncated=True,
    )
    assert not repo.finish_attempt(
        run.id,
        "build",
        1,
        status=AttemptStatus.SUCCEEDED,
        at_ms=T0 + 600,
        exit_code=0,
        error=None,
        log_bytes=0,
        log_truncated=False,
    )
    repo.start_attempt(run.id, "build", 2, at_ms=T0 + 30_500, delay_before_ms=30_000)

    assert step(repo, run.id, "build").attempts == (
        AttemptRecord(
            1,
            AttemptStatus.FAILED,
            from_ms(T0 + 1),
            from_ms(T0 + 500),
            23,
            "exit code 23",
            0,
            120,
            True,
        ),
        AttemptRecord(
            2, AttemptStatus.RUNNING, from_ms(T0 + 30_500), None, None, None, 30_000, 0, False
        ),
    )
    pgid = conn.execute("SELECT pgid FROM step_attempts WHERE attempt = 1").fetchone()[0]
    assert pgid == 4242
    with pytest.raises(sqlite3.IntegrityError):
        repo.start_attempt(run.id, "build", 2, at_ms=T0, delay_before_ms=0)


def test_append_logs_and_last_log_seq(repo: Repository, runs: Runs) -> None:
    run, other = runs.running(), runs.running()
    assert repo.last_log_seq(run.id) == 0

    repo.append_logs(
        run.id,
        [
            NewLogLine(1, T0, "build", 1, LogStream.STDOUT, "compiling ✓"),
            NewLogLine(2, T0 + 1, None, None, LogStream.SYSTEM, "step 'build' succeeded"),
        ],
    )
    repo.append_logs(other.id, [NewLogLine(1, T0, None, None, LogStream.STDERR, "other")])

    assert repo.last_log_seq(run.id) == 2
    assert repo.logs(run.id) == [
        LogLine(1, from_ms(T0), "build", 1, LogStream.STDOUT, "compiling ✓"),
        LogLine(2, from_ms(T0 + 1), None, None, LogStream.SYSTEM, "step 'build' succeeded"),
    ]


def enqueue_then_fail(repo: Repository, payload: JSONObject) -> None:
    """Write a control request inside a transaction, then raise."""
    with repo.transaction():
        repo.enqueue_control("run", payload, at_ms=T0)
        raise RuntimeError("boom")


def test_a_transaction_rolls_back_when_its_block_raises(repo: Repository) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        enqueue_then_fail(repo, {})

    assert repo.claim_controls(at_ms=T0) == []


def test_a_failed_nested_transaction_undoes_only_its_own_writes(repo: Repository) -> None:
    with repo.transaction():
        repo.enqueue_control("run", {"n": 1}, at_ms=T0)
        with pytest.raises(RuntimeError, match="boom"):
            enqueue_then_fail(repo, {"n": 2})
        repo.enqueue_control("run", {"n": 3}, at_ms=T0)

    assert [request.payload for request in repo.claim_controls(at_ms=T0)] == [{"n": 1}, {"n": 3}]


def test_a_transaction_takes_the_write_lock_at_once(repo: Repository, db_path: Path) -> None:
    with closing(connect(db_path)) as other:
        other.execute("PRAGMA busy_timeout = 0")
        with repo.transaction(), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")


def test_a_snapshot_keeps_one_view_while_others_commit(
    runs: Runs, db_path: Path, tmp_path: Path
) -> None:
    with closing(connect(db_path, query_only=True)) as reader_conn:
        reader = Repository(reader_conn, project_dir=tmp_path)
        with reader.snapshot():
            assert reader.latest_run_id() is None
            with reader.snapshot():  # a nested snapshot joins the open one
                committed = runs.queued()
                assert reader.latest_run_id() is None
        assert reader.latest_run_id() == committed.id
        assert not reader_conn.in_transaction


def nested_enqueue(repo: Repository) -> None:
    with repo.transaction(), repo.transaction():
        repo.enqueue_control("run", {}, at_ms=T0)


def test_a_transaction_that_sqlite_rolled_back_keeps_the_original_error(
    repo: Repository, conn: sqlite3.Connection
) -> None:
    # RAISE(ROLLBACK) makes SQLite end the whole transaction itself, as a full disk would.
    conn.execute(
        "CREATE TEMP TRIGGER refuse BEFORE INSERT ON control_requests"
        " BEGIN SELECT RAISE(ROLLBACK, 'refused by a trigger'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="refused by a trigger"):
        nested_enqueue(repo)

    assert not conn.in_transaction
