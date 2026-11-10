"""The run manager's lifecycle: readmit, removed workflows, shutdown, and executor failures."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeStep, FakeWorkflowSource, Hang, Sleep
from tests.support.vtime import current_loop
from tests.unit.engine.helpers import (
    MANUAL,
    Engine,
    Manager,
    ScriptedExecutor,
    call_times,
    statuses,
)

from treadle.engine.manager import RunManager
from treadle.errors import ShuttingDownError
from treadle.models import (
    CancelOutcome,
    Concurrency,
    ConcurrencyPolicy,
    RunReason,
    RunRequest,
    RunStatus,
    TriggerKind,
    Workflow,
)

QUEUE, SKIP, REPLACE = ConcurrencyPolicy.QUEUE, ConcurrencyPolicy.SKIP, ConcurrencyPolicy.REPLACE


def policy_workflow(policy: ConcurrencyPolicy, *, max_queued: int = 10) -> Workflow:
    return make_workflow(make_step("work"), concurrency=Concurrency(policy, max_queued))


async def stored_status(engine: Engine, run_id: int) -> tuple[RunStatus, RunReason | None]:
    run = (await engine.detail(run_id)).run
    return run.status, run.reason


async def test_readmit_replays_queued_runs_in_id_order(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Sleep(10))
    workflow = policy_workflow(QUEUE)
    runs = [await engine.create(workflow) for _ in range(3)]
    start = current_loop().time()

    async with Manager(engine, workflow) as harness:
        admitted = await harness.manager.readmit()
        for run in runs:
            await harness.manager.wait(run.id)

    assert admitted == 3
    assert [time - start for time in call_times(fake, "work")] == [0, 10, 20]
    assert [(await stored_status(engine, run.id))[0] for run in runs] == [RunStatus.SUCCEEDED] * 3


async def test_readmit_under_skip_keeps_only_the_oldest_run(engine: Engine) -> None:
    workflow = policy_workflow(SKIP)
    runs = [await engine.create(workflow) for _ in range(3)]

    async with Manager(engine, workflow) as harness:
        admitted = await harness.manager.readmit()
        await harness.manager.wait(runs[0].id)

    assert admitted == 1
    assert [await stored_status(engine, run.id) for run in runs] == [
        (RunStatus.SUCCEEDED, None),
        (RunStatus.SKIPPED, RunReason.ALREADY_ACTIVE),
        (RunStatus.SKIPPED, RunReason.ALREADY_ACTIVE),
    ]


async def test_readmit_under_replace_keeps_only_the_newest_run(engine: Engine) -> None:
    workflow = policy_workflow(REPLACE)
    runs = [await engine.create(workflow) for _ in range(3)]

    async with Manager(engine, workflow) as harness:
        admitted = await harness.manager.readmit()
        await harness.manager.wait(runs[2].id)

    assert admitted == 1
    for run, newer in itertools.pairwise(runs):  # each replaced by the next one
        stored = (await engine.detail(run.id)).run
        assert (stored.status, stored.reason, stored.replaced_by) == (
            RunStatus.CANCELLED,
            RunReason.REPLACED,
            newer.id,
        )
    assert (await stored_status(engine, runs[2].id))[0] is RunStatus.SUCCEEDED


async def test_readmit_under_queue_skips_runs_past_max_queued(engine: Engine) -> None:
    workflow = policy_workflow(QUEUE, max_queued=2)
    runs = [await engine.create(workflow) for _ in range(4)]

    async with Manager(engine, workflow) as harness:
        admitted = await harness.manager.readmit()
        for run in runs[:2]:
            await harness.manager.wait(run.id)

    assert admitted == 2
    assert [await stored_status(engine, run.id) for run in runs[2:]] == [
        (RunStatus.SKIPPED, RunReason.QUEUE_FULL),
        (RunStatus.SKIPPED, RunReason.QUEUE_FULL),
    ]


async def test_readmit_fails_a_run_whose_version_no_longer_loads(engine: Engine) -> None:
    gone = make_workflow(make_step("work"), source="# a version that no longer loads\n")
    current = make_workflow(make_step("work"))
    orphan = await engine.create(gone)

    async with Manager(engine, current) as harness:
        admitted = await harness.manager.readmit()
        status = await harness.manager.wait(orphan.id)

    stored = (await engine.detail(orphan.id)).run
    assert (admitted, status) == (0, RunStatus.FAILED)
    assert (stored.reason, stored.error) == (
        RunReason.SETUP_FAILED,
        "the workflow version this run was created with no longer loads",
    )


async def test_readmit_cancels_the_queued_runs_of_a_workflow_deleted_while_stopped(
    engine: Engine, fake: FakeStep
) -> None:
    deleted = policy_workflow(QUEUE)
    kept = make_workflow(make_step("other"), workflow_id="kept")
    queued = await engine.create(deleted)
    source = FakeWorkflowSource(deleted, kept)
    source.remove("wf")  # its stored version still loads, as the registry's does

    async with Manager(engine, source=source) as harness:
        waiter = asyncio.create_task(harness.manager.wait(queued.id))
        admitted = await harness.manager.readmit()
        status = await waiter

    assert (admitted, status) == (0, RunStatus.CANCELLED)
    assert await stored_status(engine, queued.id) == (
        RunStatus.CANCELLED,
        RunReason.WORKFLOW_REMOVED,
    )
    assert fake.calls == []


async def test_readmit_leaves_queued_runs_alone_when_no_workflow_loaded(
    engine: Engine, fake: FakeStep, caplog: pytest.LogCaptureFixture
) -> None:
    workflow = policy_workflow(QUEUE)
    queued = await engine.create(workflow)
    source = FakeWorkflowSource(workflow)
    source.remove("wf")  # as when the workflows directory couldn't be read

    with caplog.at_level(logging.WARNING, logger="treadle.engine"):
        async with Manager(engine, source=source) as harness:
            admitted = await harness.manager.readmit()
            stats = harness.manager.stats()

    assert (admitted, stats.queued) == (0, 0)
    assert await stored_status(engine, queued.id) == (RunStatus.QUEUED, None)
    assert fake.calls == []
    assert f"run={queued.id} stays queued: workflow 'wf' isn't loaded" in caplog.text


async def test_a_readmitted_run_executes_its_stored_version_and_inputs(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    old = load_workflow(
        """
        inputs:
          token: {type: string}
        steps:
          - id: old-step
            uses: fake
            with:
              data: ${{ inputs }}
        """
    )
    new = make_workflow(make_step("new-step"), source="# the edited file\n")
    queued = await engine.create(old, inputs={"token": "***"})

    async with Manager(engine, old) as harness:
        harness.source.add(new)
        assert await harness.manager.readmit() == 1
        await harness.manager.wait(queued.id)

    assert [call.step_id for call in fake.calls] == ["old-step"]
    assert fake.calls[0].params == {"data": {"token": "***"}}


async def test_a_removed_workflow_loses_its_queued_runs_but_its_active_run_finishes(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Sleep(30))
    workflow = policy_workflow(QUEUE)

    async with Manager(engine, workflow) as harness:
        active = await harness.submit()
        queued = await harness.submit()
        waiter = asyncio.create_task(harness.manager.wait(queued.id))
        await harness.manager.workflow_removed("wf")
        await harness.manager.workflow_removed("unknown")
        active_status = await harness.manager.wait(active.id)

    assert await waiter is RunStatus.CANCELLED
    assert await stored_status(engine, queued.id) == (
        RunStatus.CANCELLED,
        RunReason.WORKFLOW_REMOVED,
    )
    assert active_status is RunStatus.SUCCEEDED


async def test_shutdown_lets_cleanup_finish_within_the_grace_and_leaves_queued_runs(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("cleanup", Sleep(5))
    workflow = make_workflow(make_step("work"), make_step("cleanup", condition="always()"))
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        active = await harness.submit()
        queued = await harness.submit()
        waiter = asyncio.create_task(harness.manager.wait(queued.id))
        await asyncio.sleep(1)
        start = loop.time()
        await harness.manager.shutdown(grace=15)
        took = loop.time() - start

    assert took == 5
    detail = await engine.detail(active.id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    assert statuses(detail) == {"work": "cancelled", "cleanup": "succeeded"}
    assert (await stored_status(engine, queued.id))[0] is RunStatus.QUEUED
    with pytest.raises(ShuttingDownError, match=f"run {queued.id} stays queued"):
        await waiter


async def test_shutdown_lets_a_cancelled_runs_cleanup_finish_within_the_grace(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("cleanup", Sleep(5))
    workflow = make_workflow(make_step("work"), make_step("cleanup", condition="always()"))
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        await harness.manager.cancel(run.id)
        await asyncio.sleep(1)
        start = loop.time()
        await harness.manager.shutdown(grace=15)
        took = loop.time() - start

    assert took == 4
    detail = await engine.detail(run.id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.USER)
    assert statuses(detail) == {"work": "cancelled", "cleanup": "succeeded"}


async def test_shutdown_forces_runs_that_outlast_the_grace(
    engine: Engine, fake: FakeStep, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script("cleanup", Hang())
    kills: list[int] = []
    monkeypatch.setattr(engine.executor, "kill_processes", lambda: kills.append(1) or 0)
    workflow = make_workflow(make_step("cleanup", condition="always()"))
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        start = loop.time()
        await harness.manager.shutdown(grace=15)
        took = loop.time() - start

    assert (took, kills) == (15, [1])
    detail = await engine.detail(run.id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    assert statuses(detail) == {"cleanup": "cancelled"}


async def test_a_second_shutdown_skips_the_rest_of_the_grace(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Hang())
    workflow = make_workflow(make_step("cleanup", condition="always()"))
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        start = loop.time()
        first = asyncio.create_task(harness.manager.shutdown(grace=600))
        await asyncio.sleep(2)
        await harness.manager.shutdown(grace=0)
        await first
        took = loop.time() - start

    assert took == 2
    assert await stored_status(engine, run.id) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)


async def test_shutdown_records_a_run_whose_executor_never_stops(
    engine: Engine, clock: FakeClock
) -> None:
    deaf = ScriptedExecutor(engine.store, clock, behavior="deaf")
    loop = current_loop()

    async with Manager(engine, make_workflow(make_step("work")), executor=deaf) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        start = loop.time()
        await harness.manager.shutdown(grace=3)
        took = loop.time() - start

    assert (took, deaf.kills) == (3 + 2, 1)
    assert deaf.handles[run.id].forced
    detail = await engine.detail(run.id)
    assert (detail.run.status, detail.run.reason, detail.run.error) == (
        RunStatus.CANCELLED,
        RunReason.SHUTDOWN,
        "run cancelled (shutdown)",
    )
    assert statuses(detail) == {"work": "skipped:run_interrupted"}


async def test_an_executor_error_fails_the_run_and_the_queue_moves_on(
    engine: Engine, clock: FakeClock
) -> None:
    executor = ScriptedExecutor(engine.store, clock, behavior="raise")

    async with Manager(engine, make_workflow(make_step("work")), executor=executor) as harness:
        broken = await harness.submit()
        executor.behavior = "finish"
        executor.release.set()
        following = await harness.submit()
        broken_status = await harness.manager.wait(broken.id)
        following_status = await harness.manager.wait(following.id)

    assert (broken_status, following_status) == (RunStatus.FAILED, RunStatus.SUCCEEDED)
    stored = (await engine.detail(broken.id)).run
    assert (stored.status, stored.error) == (RunStatus.FAILED, "internal error: RuntimeError")


async def test_a_finalizing_run_no_longer_accepts_a_cancel(
    engine: Engine, clock: FakeClock
) -> None:
    executor = ScriptedExecutor(engine.store, clock, behavior="finalizing")

    async with Manager(engine, make_workflow(make_step("work")), executor=executor) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        outcome = await harness.manager.cancel(run.id)
        executor.release.set()
        status = await harness.manager.wait(run.id)

    assert outcome == CancelOutcome(run.id, RunStatus.RUNNING, accepted=False, forced=False)
    assert status is RunStatus.SUCCEEDED
    assert executor.handles[run.id].reason is None


async def test_cancelling_the_actor_cancels_the_runs_it_owns(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang())
    harness = Manager(engine, make_workflow(make_step("work")))
    manager: RunManager = harness.manager
    actor = asyncio.create_task(manager.run())
    run = await harness.submit()
    await asyncio.sleep(1)

    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor

    assert await stored_status(engine, run.id) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    with pytest.raises(ShuttingDownError):
        await manager.submit(RunRequest("wf", TriggerKind.MANUAL, MANUAL))
