"""Failure paths: store errors, crashed step tasks, stale writes, and requests the actor drops."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Any, ClassVar, Literal

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeStep, FakeWorkflowSource, Hang, Sleep
from tests.unit.engine.helpers import MANUAL, Engine, Manager, ScriptedExecutor, statuses

from treadle.clock import to_ms
from treadle.engine.logsink import LogSink
from treadle.errors import ShuttingDownError
from treadle.models import (
    AttemptStatus,
    CancelOutcome,
    RunReason,
    RunRequest,
    RunStatus,
    StepStatus,
    TriggerKind,
    Workflow,
)
from treadle.schema import Obj
from treadle.steps.base import StepResult, StepRuntime
from treadle.steps.procs import ProcessRegistry
from treadle.store.repo import Repository, StepUpdate
from treadle.types import Issue, JSONValue, YamlPath

PGID = 987_654  # a process group number that the tests only record, never signal


class GroupStep:
    """A step type that registers a process group, sleeps, and releases it."""

    name = "group"
    output_names: ClassVar[frozenset[str] | None] = None
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    def __init__(self) -> None:
        self.registry: ProcessRegistry | None = None
        self.tracked_during: int | None = None

    @property
    def schema(self) -> Obj:
        return Obj("group step", {})

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        assert self.registry is not None
        rt.processes.add(PGID)
        await asyncio.sleep(1)
        self.tracked_during = len(self.registry)
        rt.processes.discard(PGID)
        return StepResult()


class FinishThenRaise(ScriptedExecutor):
    """An executor double that commits the run's success, then fails anyway."""

    async def execute(self, *args: Any, **kwargs: Any) -> RunStatus:
        await super().execute(*args, **kwargs)
        raise RuntimeError("a bug after the commit")


def failing(method: Callable[..., Any], when: Callable[..., bool] | None = None) -> Any:
    """Wrap a Repository method so it raises RuntimeError("disk full") when `when` holds."""

    def wrapper(self: Repository, *args: Any, **kwargs: Any) -> Any:
        if when is None or when(*args, **kwargs):
            raise RuntimeError("disk full")
        return method(self, *args, **kwargs)

    return wrapper


def manual(workflow_id: str = "wf") -> RunRequest:
    return RunRequest(workflow_id, TriggerKind.MANUAL, MANUAL)


# The executor


async def test_an_attempt_records_its_process_groups(make_engine: Callable[..., Engine]) -> None:
    step = GroupStep()
    engine = make_engine(step)
    step.registry = engine.processes

    detail = await engine.execute(make_workflow(make_step("g", uses="group")))

    assert step.tracked_during == 1
    assert len(engine.processes) == 0
    with closing(sqlite3.connect(engine.settings.db_path)) as conn:
        row = conn.execute(
            "SELECT pgid FROM step_attempts WHERE run_id = ? AND step_id = 'g'", (detail.run.id,)
        ).fetchone()
    assert row == (PGID,)


async def test_a_process_group_that_cant_be_recorded_is_only_a_warning(
    make_engine: Callable[..., Engine],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    step = GroupStep()
    engine = make_engine(step)
    step.registry = engine.processes
    monkeypatch.setattr(Repository, "set_attempt_pgid", failing(Repository.set_attempt_pgid))

    with caplog.at_level(logging.WARNING, logger="treadle.engine"):
        detail = await engine.execute(make_workflow(make_step("g", uses="group")))

    assert detail.run.status is RunStatus.SUCCEEDED
    assert f"couldn't record process group {PGID}" in caplog.text


async def test_a_crashed_step_task_stops_the_execution_and_its_other_steps(
    engine: Engine, fake: FakeStep, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script("other", Hang())
    workflow = make_workflow(make_step("doomed", needs=[]), make_step("other", needs=[]))
    only_doomed = failing(
        Repository.finish_attempt, lambda run_id, step_id, *args, **kwargs: step_id == "doomed"
    )
    monkeypatch.setattr(Repository, "finish_attempt", only_doomed)
    launched = await engine.launch(workflow)

    with pytest.raises(RuntimeError, match="disk full"):
        await launched.task

    detail = await engine.detail(launched.run_id)
    assert detail.run.status is RunStatus.RUNNING  # the run manager records the failure
    assert statuses(detail)["other"] == "cancelled"
    assert launched.handle.finalizing
    assert not (engine.settings.tmp_dir / f"run-{launched.run_id}").exists()


async def test_a_crash_whose_last_log_lines_cant_be_written_raises_the_first_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def unwritable(self: LogSink) -> None:
        raise OSError("the disk is gone")

    monkeypatch.setattr(Repository, "finish_attempt", failing(Repository.finish_attempt))
    monkeypatch.setattr(LogSink, "aclose", unwritable)

    with (
        caplog.at_level(logging.ERROR, logger="treadle.engine"),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await engine.execute(make_workflow(make_step("a")))

    assert "couldn't write the last log lines" in caplog.text


async def test_steps_that_crash_together_are_all_collected(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(Repository, "finish_attempt", failing(Repository.finish_attempt))
    workflow = make_workflow(make_step("a", needs=[]), make_step("b", needs=[]))

    with (
        caplog.at_level(logging.DEBUG, logger="treadle.engine"),
        pytest.raises(RuntimeError, match="disk full"),
    ):
        await engine.execute(workflow)

    assert "the step task failed" in caplog.text


async def test_a_setup_write_failure_raises_after_cleaning_up(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        env:
          TOKEN: ${{ secrets.MISSING }}
        steps:
          - id: a
            uses: fake
        """
    )
    monkeypatch.setattr(Repository, "update_step", failing(Repository.update_step))
    launched = await engine.launch(workflow)

    with pytest.raises(RuntimeError, match="disk full"):
        await launched.task

    assert not (engine.settings.tmp_dir / f"run-{launched.run_id}").exists()


async def test_a_run_finished_by_someone_else_returns_the_stored_status(
    engine: Engine, fake: FakeStep, clock: FakeClock
) -> None:
    fake.script("a", Sleep(5))
    launched = await engine.launch(make_workflow(make_step("a")))
    await asyncio.sleep(1)
    now = to_ms(clock.now())
    await engine.store.write(
        lambda repo: repo.finish_run(
            launched.run_id, RunStatus.FAILED, at_ms=now, reason=None, error="elsewhere"
        )
    )

    status = await launched.task

    assert status is RunStatus.FAILED
    assert (await engine.detail(launched.run_id)).run.error == "elsewhere"


async def test_a_stale_step_write_is_ignored(
    engine: Engine, fake: FakeStep, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    fake.script("a", Sleep(5))
    launched = await engine.launch(make_workflow(make_step("a")))
    await asyncio.sleep(1)
    update = StepUpdate(StepStatus.INTERRUPTED, frozenset({StepStatus.RUNNING}), to_ms(clock.now()))
    await engine.store.write(lambda repo: repo.update_step(launched.run_id, "a", update))

    with caplog.at_level(logging.DEBUG, logger="treadle.engine"):
        await engine.finish(launched)

    assert "step=a: stale move to succeeded" in caplog.text
    assert statuses(await engine.detail(launched.run_id)) == {"a": "interrupted"}


# The run manager


async def test_a_failing_command_without_a_caller_is_logged(
    engine: Engine,
    fake: FakeStep,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.script("work", Sleep(5))

    async with Manager(engine, make_workflow(make_step("work"))) as harness:
        first = await harness.submit()
        second = await harness.submit()
        monkeypatch.setattr(Repository, "start_run", failing(Repository.start_run))
        with caplog.at_level(logging.ERROR, logger="treadle.engine"):
            await harness.manager.wait(first.id)
            await asyncio.sleep(1)
        monkeypatch.undo()
        assert harness.manager.stats().queued == 1
        await harness.manager.cancel(second.id)

    assert "the run manager failed to handle a command" in caplog.text


async def test_requests_whose_callers_left_are_still_handled(engine: Engine) -> None:
    async with Manager(engine, make_workflow(make_step("work"))) as harness:
        left = asyncio.create_task(harness.manager.submit(manual()))
        left_failing = asyncio.create_task(harness.manager.submit(manual("missing")))
        await asyncio.sleep(0)
        left.cancel()
        left_failing.cancel()
        await asyncio.sleep(1)

    assert await engine.store.latest_run_id() == 1


async def test_a_waiter_on_a_run_the_actor_never_admitted_fails_at_stop(engine: Engine) -> None:
    workflow = make_workflow(make_step("work"))
    never_admitted = await engine.create(workflow)

    async with Manager(engine, workflow) as harness:
        waiter = asyncio.create_task(harness.manager.wait(never_admitted.id))
        await asyncio.sleep(0)

    with pytest.raises(ShuttingDownError, match=f"before run {never_admitted.id} finished"):
        await waiter


async def test_cancelling_the_actor_fails_its_queued_and_in_flight_requests(
    engine: Engine,
) -> None:
    class SlowSource(FakeWorkflowSource):
        async def version(self, digest: str) -> Workflow | None:
            await asyncio.sleep(60)
            return await super().version(digest)

    workflow = make_workflow(make_step("work"))
    await engine.create(workflow)
    harness = Manager(engine, workflow, source=SlowSource())
    actor = asyncio.create_task(harness.manager.run())
    in_flight = asyncio.create_task(harness.manager.readmit())
    queued = asyncio.create_task(harness.manager.submit(manual()))
    await asyncio.sleep(1)

    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor

    for request in (in_flight, queued):
        with pytest.raises(ShuttingDownError):
            await request


async def test_a_queued_run_that_left_the_queue_elsewhere_is_dropped(
    engine: Engine, fake: FakeStep, clock: FakeClock
) -> None:
    fake.script("work", Sleep(10))

    async with Manager(engine, make_workflow(make_step("work"))) as harness:
        first = await harness.submit()
        second = await harness.submit()
        third = await harness.submit()
        waiter = asyncio.create_task(harness.manager.wait(second.id))
        now = to_ms(clock.now())
        await engine.store.write(
            lambda repo: repo.cancel_queued([second.id, third.id], reason=RunReason.USER, at_ms=now)
        )
        outcome = await harness.manager.cancel(third.id)
        await harness.manager.wait(first.id)
        assert await waiter is RunStatus.CANCELLED
        assert harness.manager.stats().queued == 0

    assert outcome == CancelOutcome(third.id, RunStatus.CANCELLED, accepted=False, forced=False)
    assert (await engine.detail(second.id)).run.started_at is None


async def test_readmit_leaves_a_run_that_changed_while_its_version_loaded(
    engine: Engine, clock: FakeClock
) -> None:
    queued = await engine.create(make_workflow(make_step("work"), source="# gone\n"))

    class CancellingSource(FakeWorkflowSource):
        async def version(self, digest: str) -> Workflow | None:
            now = to_ms(clock.now())
            await engine.store.write(
                lambda repo: repo.cancel_queued([queued.id], reason=RunReason.USER, at_ms=now)
            )
            return None

    source = CancellingSource(make_workflow(make_step("work")))  # the current version differs

    async with Manager(engine, source=source) as harness:
        assert await harness.manager.readmit() == 0

    stored = (await engine.detail(queued.id)).run
    assert (stored.status, stored.reason) == (RunStatus.CANCELLED, RunReason.USER)


async def test_an_executor_error_after_the_run_finished_keeps_the_stored_status(
    engine: Engine, clock: FakeClock
) -> None:
    executor = FinishThenRaise(engine.store, clock)
    executor.release.set()

    async with Manager(engine, make_workflow(make_step("work")), executor=executor) as harness:
        detail = await harness.run_to_end()

    assert detail.run.status is RunStatus.SUCCEEDED


async def test_an_executor_crash_leaves_no_step_or_attempt_unfinished(
    engine: Engine, fake: FakeStep, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script("slow", Sleep(5))
    workflow = make_workflow(
        make_step("first", needs=[]), make_step("slow", needs=[]), make_step("after")
    )
    update_step = Repository.update_step

    def full_disk_once(self: Repository, run_id: int, step_id: str, update: StepUpdate) -> bool:
        if step_id == "first" and update.status is StepStatus.SUCCEEDED:
            raise sqlite3.OperationalError("database or disk is full")
        return update_step(self, run_id, step_id, update)

    monkeypatch.setattr(Repository, "update_step", full_disk_once)

    async with Manager(engine, workflow) as harness:
        detail = await harness.run_to_end()

    assert (detail.run.status, detail.run.error) == (
        RunStatus.FAILED,
        "internal error: OperationalError",
    )
    assert statuses(detail) == {
        "first": "interrupted",
        "slow": "cancelled",
        "after": "skipped:run_interrupted",
    }
    attempts = {step.step_id: [a.status for a in step.attempts] for step in detail.steps}
    assert attempts == {
        "first": [AttemptStatus.SUCCEEDED],
        "slow": [AttemptStatus.CANCELLED],
        "after": [],
    }
    assert all(step.finished_at is not None for step in detail.steps)


async def test_an_internal_error_that_cant_be_recorded_is_logged(
    engine: Engine,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = ScriptedExecutor(engine.store, clock, behavior="raise")

    async with Manager(engine, make_workflow(make_step("work")), executor=executor) as harness:
        monkeypatch.setattr(Repository, "finish_run", failing(Repository.finish_run))
        with caplog.at_level(logging.ERROR, logger="treadle.engine"):
            run = await harness.submit()
            await asyncio.sleep(1)
        monkeypatch.undo()
        waiter = asyncio.create_task(harness.manager.wait(run.id))
        await asyncio.sleep(0)

    assert "couldn't record the internal error" in caplog.text
    assert (await engine.detail(run.id)).run.status is RunStatus.RUNNING
    with pytest.raises(ShuttingDownError):
        await waiter


@pytest.mark.parametrize("patience", [0, 1], ids=["before-registering", "while-waiting"])
async def test_waiters_that_gave_up_are_skipped(
    engine: Engine, fake: FakeStep, patience: float
) -> None:
    fake.script("work", Hang())

    async with Manager(engine, make_workflow(make_step("work"))) as harness:
        active = await harness.submit()
        queued = await harness.submit()
        impatient = [asyncio.create_task(harness.manager.wait(run.id)) for run in (active, queued)]
        await asyncio.sleep(patience)
        for task in impatient:
            task.cancel()
        await harness.manager.shutdown(grace=0)

    assert all(task.cancelled() for task in impatient)
    assert (await engine.detail(active.id)).run.status is RunStatus.CANCELLED
    assert (await engine.detail(queued.id)).run.status is RunStatus.QUEUED


async def test_a_submission_during_the_grace_is_refused(engine: Engine, fake: FakeStep) -> None:
    fake.script("cleanup", Sleep(10))

    async with Manager(
        engine, make_workflow(make_step("cleanup", condition="always()"))
    ) as harness:
        await harness.submit()
        await asyncio.sleep(1)
        draining = asyncio.create_task(harness.manager.shutdown(grace=60))
        await asyncio.sleep(1)
        with pytest.raises(ShuttingDownError, match="accepts no new runs"):
            await harness.manager.submit(manual())
        await draining


async def test_concurrent_shutdowns_both_return(engine: Engine) -> None:
    async with Manager(engine, make_workflow(make_step("work"))) as harness:
        await asyncio.gather(harness.manager.shutdown(grace=0), harness.manager.shutdown(grace=0))
        await harness.task
