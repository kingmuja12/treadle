from __future__ import annotations

import os

from tests.unit.store.helpers import SECOND, T0, Runs

from treadle.clock import from_ms
from treadle.models import (
    AttemptStatus,
    LogLine,
    LogStream,
    RunReason,
    RunStatus,
    SkipReason,
    StepStatus,
)
from treadle.store.repo import NewLogLine, RecoveryReport, Repository, StepUpdate

NOW = T0 + 3600 * SECOND
GONE_PGID = 99_999_999  # far above any PID limit, so no such process group exists
INTERRUPTED_LINE = "Run interrupted: the Treadle process stopped while this run was active."


def set_step(repo: Repository, run_id: int, step_id: str, status: StepStatus) -> None:
    assert repo.update_step(run_id, step_id, StepUpdate(status, frozenset(StepStatus), T0))


def start_attempt(repo: Repository, run_id: int, step_id: str, attempt: int, pgid: int) -> None:
    repo.start_attempt(run_id, step_id, attempt, at_ms=T0, delay_before_ms=0)
    repo.set_attempt_pgid(run_id, step_id, attempt, pgid)


def test_recover_interrupts_what_a_stopped_process_left_running(
    repo: Repository, runs: Runs
) -> None:
    crashed = runs.running(steps=["done", "build", "lint", "test", "deploy"])
    set_step(repo, crashed.id, "done", StepStatus.SUCCEEDED)
    set_step(repo, crashed.id, "build", StepStatus.RUNNING)
    start_attempt(repo, crashed.id, "build", 1, pgid=os.getpgrp())
    set_step(repo, crashed.id, "lint", StepStatus.RUNNING)
    start_attempt(repo, crashed.id, "lint", 1, pgid=GONE_PGID)
    set_step(repo, crashed.id, "test", StepStatus.RETRYING)
    start_attempt(repo, crashed.id, "test", 1, pgid=GONE_PGID)
    repo.finish_attempt(
        crashed.id,
        "test",
        1,
        status=AttemptStatus.FAILED,
        at_ms=T0 + 1,
        exit_code=1,
        error="exit code 1",
        log_bytes=0,
        log_truncated=False,
    )
    repo.append_logs(crashed.id, [NewLogLine(1, T0, "build", 1, LogStream.STDOUT, "working")])
    waiting = runs.queued()
    finished = runs.finished(RunStatus.SUCCEEDED, steps=["only"])

    report = repo.recover(at_ms=NOW)

    assert report == RecoveryReport(
        interrupted_runs=(crashed.id,),
        orphan_pgids=(os.getpgrp(),),
        queued_runs=1,
        expired_controls=0,
    )
    detail = repo.get_run_detail(crashed.id)
    assert detail is not None
    assert (detail.run.status, detail.run.reason, detail.run.finished_at) == (
        RunStatus.INTERRUPTED,
        RunReason.EXECUTOR_STOPPED,
        from_ms(NOW),
    )
    steps = {step.step_id: step for step in detail.steps}
    assert {step_id: step.status for step_id, step in steps.items()} == {
        "done": StepStatus.SUCCEEDED,
        "build": StepStatus.INTERRUPTED,
        "lint": StepStatus.INTERRUPTED,
        "test": StepStatus.INTERRUPTED,
        "deploy": StepStatus.SKIPPED,
    }
    assert steps["deploy"].skip_reason is SkipReason.RUN_INTERRUPTED
    assert steps["done"].finished_at == from_ms(T0)
    assert all(steps[s].finished_at == from_ms(NOW) for s in ("build", "lint", "test", "deploy"))
    attempts = {s: [(a.status, a.finished_at) for a in steps[s].attempts] for s in steps}
    assert attempts["build"] == [(AttemptStatus.INTERRUPTED, from_ms(NOW))]
    assert attempts["lint"] == [(AttemptStatus.INTERRUPTED, from_ms(NOW))]
    assert attempts["test"] == [(AttemptStatus.FAILED, from_ms(T0 + 1))]
    assert repo.logs(crashed.id)[-1] == LogLine(
        2, from_ms(NOW), None, None, LogStream.SYSTEM, INTERRUPTED_LINE
    )
    assert runs.get(waiting.id) == waiting
    untouched = repo.get_run_detail(finished.id)
    assert untouched is not None
    assert untouched.run == finished
    assert [step.status for step in untouched.steps] == [StepStatus.PENDING]


def test_each_interrupted_run_gets_its_own_log_line(repo: Repository, runs: Runs) -> None:
    first, second = runs.running(), runs.running()
    repo.append_logs(first.id, [NewLogLine(1, T0, None, None, LogStream.SYSTEM, "a")])

    report = repo.recover(at_ms=NOW)

    assert report.interrupted_runs == (first.id, second.id)
    assert [line.seq for line in repo.logs(first.id)] == [1, 2]
    assert [(line.seq, line.text) for line in repo.logs(second.id)] == [(1, INTERRUPTED_LINE)]


def test_recover_expires_control_requests_older_than_a_minute(repo: Repository) -> None:
    stale = repo.enqueue_control("run", {"workflow": "wf"}, at_ms=NOW - 61 * SECOND)
    fresh = repo.enqueue_control("run", {"workflow": "wf"}, at_ms=NOW - 59 * SECOND)
    handled = repo.enqueue_control("cancel", {"run_id": 1}, at_ms=NOW - 3600 * SECOND)
    repo.complete_control(handled, {"run_id": 1, "accepted": True}, at_ms=NOW - 3599 * SECOND)

    report = repo.recover(at_ms=NOW)

    assert report == RecoveryReport((), (), 0, 1)
    result = repo.control_result(stale)
    assert result is not None
    assert result["error"] == {
        "code": "expired",
        "message": "no Treadle process handled the request in time",
    }
    assert repo.control_result(fresh) is None
    assert repo.control_result(handled) == {"run_id": 1, "accepted": True}
    assert [request.id for request in repo.claim_controls(at_ms=NOW)] == [fresh]


def test_recover_with_nothing_to_do(repo: Repository) -> None:
    assert repo.recover(at_ms=NOW) == RecoveryReport((), (), 0, 0)


def test_recover_reports_a_process_group_that_another_user_owns(
    repo: Repository, runs: Runs
) -> None:
    run = runs.running(steps=["build"])
    set_step(repo, run.id, "build", StepStatus.RUNNING)
    start_attempt(repo, run.id, "build", 1, pgid=1)  # init or launchd: it exists, but isn't ours

    assert repo.recover(at_ms=NOW).orphan_pgids == (1,)
