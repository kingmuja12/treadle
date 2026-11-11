"""The run manager: submission, every concurrency policy, the global cap, cancel, and rerun."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import itertools
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from tests.support.builders import make_entry, make_input, make_step, make_workflow
from tests.support.fakes import FakeStep, Hang, Sleep
from tests.support.vtime import current_loop
from tests.unit.engine.conftest import API_TOKEN
from tests.unit.engine.helpers import MANUAL, Engine, Manager, call_times, statuses

from treadle.clock import to_ms
from treadle.config import RunSettings
from treadle.errors import (
    ConflictError,
    InputError,
    NotFoundError,
    ShuttingDownError,
    WorkflowInvalidError,
)
from treadle.models import (
    CancelOutcome,
    CancelReason,
    Concurrency,
    ConcurrencyPolicy,
    Delivery,
    InputType,
    RunReason,
    RunRequest,
    RunStatus,
    ScheduleSlot,
    SubmitOutcome,
    TriggerKind,
    Workflow,
)
from treadle.types import Issue, Severity

QUEUE, SKIP, REPLACE = ConcurrencyPolicy.QUEUE, ConcurrencyPolicy.SKIP, ConcurrencyPolicy.REPLACE


def slow(
    workflow_id: str = "wf", *, policy: ConcurrencyPolicy = QUEUE, max_queued: int = 10
) -> Workflow:
    """A workflow with one step that the fake step runs for as long as its script says."""
    return make_workflow(
        make_step("work"),
        workflow_id=workflow_id,
        concurrency=Concurrency(policy, max_queued),
    )


def manual(workflow_id: str = "wf", **kwargs: Any) -> RunRequest:
    return RunRequest(workflow_id, TriggerKind.MANUAL, MANUAL, **kwargs)


async def test_a_submitted_run_is_queued_started_and_finished(engine: Engine) -> None:
    async with Manager(engine, slow()) as harness:
        submission = await harness.manager.submit(manual())
        assert submission.outcome is SubmitOutcome.QUEUED
        assert submission.run is not None
        assert submission.run.status is RunStatus.QUEUED

        status = await harness.manager.wait(submission.run.id)

    assert status is RunStatus.SUCCEEDED
    assert (await engine.detail(submission.run.id)).run.status is RunStatus.SUCCEEDED


async def test_inputs_are_coerced_and_masked_for_storage_but_not_for_steps(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        inputs:
          token: {type: string}
          count: {type: integer}
          verbose: {type: boolean, default: false}
        steps:
          - id: a
            uses: fake
            with:
              data: ${{ inputs }}
        """
    )
    trigger = {"kind": "manual", "origin": "cli", "note": f"sent {API_TOKEN}"}
    request = RunRequest(
        "wf",
        TriggerKind.MANUAL,
        trigger,
        inputs={"token": API_TOKEN, "count": "3"},
        inputs_are_strings=True,
    )

    async with Manager(engine, workflow) as harness:
        submission = await harness.manager.submit(request)
        assert submission.run is not None
        await harness.manager.wait(submission.run.id)

    stored = (await engine.detail(submission.run.id)).run
    assert stored.inputs == {"token": "***", "count": 3, "verbose": False}
    assert stored.trigger == {"kind": "manual", "origin": "cli", "note": "sent ***"}
    assert fake.calls_for("a")[0].params == {
        "data": {"token": API_TOKEN, "count": 3, "verbose": False}
    }


async def test_a_number_that_holds_a_secret_is_stored_masked_and_a_rerun_says_why_it_fails(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    engine.secrets.snapshot = dataclasses.replace(
        engine.secrets.snapshot, secrets={"PIN": "834512907"}
    )
    workflow = load_workflow(
        """
        inputs:
          pin: {type: integer}
        steps:
          - id: a
            uses: fake
        """
    )
    trigger = {"kind": "webhook", "body": {"chat": 834512907}}
    request = RunRequest("wf", TriggerKind.WEBHOOK, trigger, inputs={"pin": 834512907})

    async with Manager(engine, workflow) as harness:
        submission = await harness.manager.submit(request)
        assert submission.run is not None
        await harness.manager.wait(submission.run.id)
        with pytest.raises(InputError, match="input 'pin'"):
            await harness.manager.rerun(submission.run.id)

    stored = (await engine.detail(submission.run.id)).run
    assert (stored.inputs, stored.trigger) == (
        {"pin": "***"},
        {"kind": "webhook", "body": {"chat": "***"}},
    )


async def test_unknown_invalid_and_badly_called_workflows_are_rejected(engine: Engine) -> None:
    broken = make_entry(
        workflow_id="broken", issues=[Issue(Severity.ERROR, "E101", "unknown key 'on'")]
    )
    typed = make_workflow(workflow_id="typed", inputs=[make_input("n", InputType.INTEGER)])

    async with Manager(engine, broken, typed) as harness:
        with pytest.raises(NotFoundError, match="workflow 'nope' doesn't exist"):
            await harness.manager.submit(manual("nope"))
        with pytest.raises(WorkflowInvalidError) as invalid:
            await harness.manager.submit(manual("broken"))
        with pytest.raises(InputError) as bad_inputs:
            await harness.manager.submit(manual("typed", inputs={"n": "x", "m": 1}))

    assert invalid.value.issues == broken.issues
    assert bad_inputs.value.errors == {"m": "isn't declared", "n": "must be an integer"}
    assert await engine.store.latest_run_id() is None


async def test_the_queue_policy_runs_one_at_a_time_in_order(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Sleep(10))
    start = current_loop().time()

    async with Manager(engine, slow()) as harness:
        runs = [await harness.submit() for _ in range(3)]
        for run in runs:
            await harness.manager.wait(run.id)

    assert [time - start for time in call_times(fake, "work")] == [0, 10, 20]
    details = [await engine.detail(run.id) for run in runs]
    for earlier, later in itertools.pairwise(details):
        assert earlier.run.finished_at is not None
        assert later.run.started_at is not None
        assert later.run.started_at >= earlier.run.finished_at


async def test_a_full_queue_skips_new_runs(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang())

    async with Manager(engine, slow(max_queued=1)) as harness:
        active = await harness.submit()
        queued = await harness.submit()
        submission = await harness.manager.submit(manual())
        await harness.manager.cancel(active.id)
        await harness.manager.cancel(queued.id)

    assert submission.outcome is SubmitOutcome.SKIPPED
    assert submission.run is not None
    skipped = submission.run
    assert (skipped.status, skipped.reason) == (RunStatus.SKIPPED, RunReason.QUEUE_FULL)
    assert skipped.finished_at == skipped.created_at


async def test_the_skip_policy_skips_while_a_run_is_active_or_queued(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(runs=RunSettings(max_concurrent=1))
    fake.script("work", Hang())

    async with Manager(engine, slow(policy=SKIP), slow("other")) as harness:
        other = await harness.submit("other")
        first = await harness.submit()  # queued behind the global cap
        while_queued = await harness.manager.submit(manual())
        await harness.manager.cancel(other.id)
        await asyncio.sleep(1)
        while_active = await harness.manager.submit(manual())
        await harness.manager.cancel(first.id)
        status = await harness.manager.wait(first.id)

    for submission in (while_queued, while_active):
        assert submission.outcome is SubmitOutcome.SKIPPED
        assert submission.run is not None
        assert submission.run.reason is RunReason.ALREADY_ACTIVE
    assert status is RunStatus.CANCELLED


async def test_replace_cancels_the_old_run_and_waits_until_it_ends(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("cleanup", Sleep(5))
    workflow = make_workflow(
        make_step("work"),
        make_step("cleanup", condition="always()"),
        concurrency=Concurrency(REPLACE),
    )
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        old = await harness.submit()
        await asyncio.sleep(10)
        replaced_at = loop.time()
        new = await harness.submit()
        assert harness.manager.stats().active == 1
        await harness.manager.wait(old.id)
        await asyncio.sleep(0)
        await harness.manager.cancel(new.id)
        await harness.manager.wait(new.id)

    old_run, new_run = (await engine.detail(old.id)).run, (await engine.detail(new.id)).run
    assert (old_run.status, old_run.reason, old_run.replaced_by) == (
        RunStatus.CANCELLED,
        RunReason.REPLACED,
        new.id,
    )
    assert old_run.cancel_requested_at is not None
    assert old_run.finished_at is not None
    assert new_run.started_at is not None
    assert new_run.started_at >= old_run.finished_at
    assert call_times(fake, "work")[1] == replaced_at + 5


async def test_replace_cancels_queued_runs_and_links_them_to_the_new_one(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("cleanup", Sleep(30))
    workflow = make_workflow(
        make_step("work"),
        make_step("cleanup", condition="always()"),
        concurrency=Concurrency(REPLACE),
    )

    async with Manager(engine, workflow) as harness:
        first = await harness.submit()
        await asyncio.sleep(1)
        second = await harness.submit()  # first is still cleaning up, so second waits
        third = await harness.submit()
        second_status = await harness.manager.wait(second.id)
        await harness.manager.wait(first.id)
        await asyncio.sleep(0)
        await harness.manager.cancel(third.id)
        await harness.manager.wait(third.id)

    first_detail, second_run = (
        await engine.detail(first.id),
        (await engine.detail(second.id)).run,
    )
    assert second_status is RunStatus.CANCELLED
    assert (second_run.reason, second_run.replaced_by, second_run.started_at) == (
        RunReason.REPLACED,
        third.id,
        None,
    )
    assert first_detail.run.replaced_by == third.id
    assert statuses(first_detail) == {"work": "cancelled", "cleanup": "succeeded"}


async def test_another_replacement_leaves_the_cancelling_run_its_cleanup(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("cleanup", Sleep(60))
    workflow = make_workflow(
        make_step("work"),
        make_step("cleanup", condition="always()"),
        concurrency=Concurrency(REPLACE),
    )
    loop = current_loop()

    async with Manager(engine, workflow) as harness:
        first = await harness.submit()
        await asyncio.sleep(1)
        replaced_at = loop.time()
        await harness.submit()
        await asyncio.sleep(1)
        third = await harness.submit()
        await harness.manager.wait(first.id)
        await asyncio.sleep(0)
        await harness.manager.cancel(third.id)
        await harness.manager.wait(third.id)

    first_detail = await engine.detail(first.id)
    assert (first_detail.run.status, first_detail.run.reason) == (
        RunStatus.CANCELLED,
        RunReason.REPLACED,
    )
    assert statuses(first_detail) == {"work": "cancelled", "cleanup": "succeeded"}
    assert call_times(fake, "work")[1] == replaced_at + 60


async def test_the_global_cap_holds_runs_in_run_id_order(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(runs=RunSettings(max_concurrent=2))
    fake.script("work", Sleep(10))
    start = current_loop().time()
    workflows = [slow(name) for name in ("w1", "w2", "w3", "w4")]

    async with Manager(engine, *workflows) as harness:
        runs = [await harness.submit(name) for name in ("w1", "w2", "w3", "w4")]
        assert harness.manager.stats().active == 2
        assert harness.manager.stats().queued == 2
        for run in runs:
            await harness.manager.wait(run.id)

    started = [(await engine.detail(run.id)).run.started_at for run in runs]
    assert [time - start for time in call_times(fake, "work")] == [0, 0, 10, 10]
    assert started[0] == started[1]
    assert started[2] == started[3]


async def test_a_blocked_workflow_doesnt_block_other_workflows(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(runs=RunSettings(max_concurrent=2))
    fake.script("work", Sleep(10))
    start = current_loop().time()

    async with Manager(engine, slow("a"), slow("b")) as harness:
        a1, a2 = await harness.submit("a"), await harness.submit("a")
        b1 = await harness.submit("b")
        for run in (a1, a2, b1):
            await harness.manager.wait(run.id)

    times = {run.id: (await engine.detail(run.id)).run.started_at for run in (a1, a2, b1)}
    assert times[b1.id] == times[a1.id]
    assert [time - start for time in call_times(fake, "work")] == [0, 0, 10]


async def test_cancelling_a_queued_run_cancels_it_at_once(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang())

    async with Manager(engine, slow()) as harness:
        active = await harness.submit()
        queued = await harness.submit()
        waiter = asyncio.create_task(harness.manager.wait(queued.id))
        outcome = await harness.manager.cancel(queued.id)
        await harness.manager.cancel(active.id)
        await harness.manager.wait(active.id)

    assert outcome == CancelOutcome(queued.id, RunStatus.CANCELLED, accepted=True, forced=False)
    assert await waiter is RunStatus.CANCELLED
    stored = (await engine.detail(queued.id)).run
    assert (stored.status, stored.reason, stored.started_at) == (
        RunStatus.CANCELLED,
        RunReason.USER,
        None,
    )


async def test_cancelling_an_active_run_records_the_request_and_a_second_cancel_forces(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Hang())
    workflow = make_workflow(make_step("cleanup", condition="always()"))

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await asyncio.sleep(5)
        first = await harness.manager.cancel(run.id)
        during = (await engine.detail(run.id)).run
        await asyncio.sleep(5)
        second = await harness.manager.cancel(run.id)
        status = await harness.manager.wait(run.id)

    assert first == CancelOutcome(run.id, RunStatus.RUNNING, accepted=True, forced=False)
    assert second == CancelOutcome(run.id, RunStatus.RUNNING, accepted=True, forced=True)
    assert during.cancel_requested_at is not None
    assert status is RunStatus.CANCELLED
    detail = await engine.detail(run.id)
    assert (detail.run.reason, statuses(detail)) == (RunReason.USER, {"cleanup": "cancelled"})


@contextlib.asynccontextmanager
async def timed_out(harness: Manager) -> AsyncIterator[None]:
    """Let the workflow's 60-second timeout start cancelling the active run."""
    await asyncio.sleep(60)
    yield


@contextlib.asynccontextmanager
async def replaced(harness: Manager) -> AsyncIterator[None]:
    """Submit a run that replaces the active one."""
    await harness.submit()
    yield


@contextlib.asynccontextmanager
async def shutting_down(harness: Manager) -> AsyncIterator[None]:
    """Shut down while the block runs, with a grace that outlasts it."""
    shutdown = asyncio.create_task(harness.manager.shutdown(grace=3600))
    yield
    await shutdown


@pytest.mark.parametrize(
    ("cancelling", "reason"),
    [
        pytest.param(timed_out, RunReason.TIMEOUT, id="timeout"),
        pytest.param(replaced, RunReason.REPLACED, id="replacement"),
        pytest.param(shutting_down, RunReason.SHUTDOWN, id="shutdown"),
    ],
)
async def test_a_cancel_that_treadle_started_doesnt_make_the_users_first_cancel_force(
    engine: Engine,
    fake: FakeStep,
    cancelling: Callable[[Manager], contextlib.AbstractAsyncContextManager[None]],
    reason: RunReason,
) -> None:
    fake.script("work", Hang()).script("cleanup", Hang())
    workflow = make_workflow(
        make_step("work"),
        make_step("cleanup", condition="always()"),
        concurrency=Concurrency(REPLACE),
        timeout=60,
    )

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await asyncio.sleep(1)
        async with cancelling(harness):
            await asyncio.sleep(1)
            first = await harness.manager.cancel(run.id)
            await asyncio.sleep(1)
            second = await harness.manager.cancel(run.id)
            await harness.manager.wait(run.id)

    # The first cancel leaves the cleanup step running, and only the second forces it.
    assert first == CancelOutcome(run.id, RunStatus.RUNNING, accepted=True, forced=False)
    assert second == CancelOutcome(run.id, RunStatus.RUNNING, accepted=True, forced=True)
    detail = await engine.detail(run.id)
    assert (detail.run.reason, statuses(detail)) == (
        reason,
        {"work": "cancelled", "cleanup": "cancelled"},
    )


async def test_cancelling_a_finished_or_missing_run(engine: Engine) -> None:
    async with Manager(engine, slow()) as harness:
        detail = await harness.run_to_end()
        outcome = await harness.manager.cancel(detail.run.id)
        with pytest.raises(NotFoundError, match="run 99 doesn't exist"):
            await harness.manager.cancel(99)

    assert outcome == CancelOutcome(
        detail.run.id, RunStatus.SUCCEEDED, accepted=False, forced=False
    )


async def test_cancelling_a_queued_run_this_process_never_admitted(engine: Engine) -> None:
    workflow = slow()
    stored = await engine.create(workflow)

    async with Manager(engine, workflow) as harness:
        outcome = await harness.manager.cancel(stored.id, CancelReason.WORKFLOW_REMOVED)
        again = await harness.manager.cancel(stored.id)

    assert outcome == CancelOutcome(stored.id, RunStatus.CANCELLED, accepted=True, forced=False)
    assert again.accepted is False
    run = (await engine.detail(stored.id)).run
    assert (run.status, run.reason) == (RunStatus.CANCELLED, RunReason.WORKFLOW_REMOVED)


async def test_rerun_submits_the_stored_trigger_and_inputs_with_the_current_version(
    engine: Engine, fake: FakeStep
) -> None:
    first_version = make_workflow(make_step("work"), inputs=[make_input("token")])
    trigger = {"kind": "webhook", "hook": "deploy", "body": {"token": API_TOKEN}}
    request = RunRequest(
        "wf",
        TriggerKind.WEBHOOK,
        trigger,
        inputs={"token": API_TOKEN},
        trigger_key="wf:webhook:deploy",
    )

    async with Manager(engine, first_version) as harness:
        submission = await harness.manager.submit(request)
        assert submission.run is not None
        original = submission.run
        await harness.manager.wait(original.id)
        second_version = make_workflow(
            make_step("work"), make_step("new-step"), inputs=[make_input("token")], source="# v2\n"
        )
        harness.source.add(second_version)
        rerun = await harness.manager.rerun(original.id)
        assert rerun.run is not None
        await harness.manager.wait(rerun.run.id)
        again = await harness.manager.rerun(rerun.run.id)
        assert again.run is not None
        await harness.manager.wait(again.run.id)

    assert rerun.outcome is SubmitOutcome.QUEUED
    run = (await engine.detail(rerun.run.id)).run
    assert (run.rerun_of, run.attempt, run.trigger_kind) == (original.id, 2, TriggerKind.WEBHOOK)
    assert (run.trigger_key, run.scheduled_for) == (None, None)
    assert run.trigger == {"kind": "webhook", "hook": "deploy", "body": {"token": "***"}}
    assert run.inputs == {"token": "***"}
    assert run.digest == second_version.digest
    assert [call.params for call in fake.calls_for("new-step")] == [{}, {}]
    assert ((await engine.detail(again.run.id)).run.attempt, again.run.rerun_of) == (
        3,
        rerun.run.id,
    )


async def test_only_a_finished_run_can_be_rerun(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang())

    async with Manager(engine, slow()) as harness:
        with pytest.raises(NotFoundError, match="run 5 doesn't exist"):
            await harness.manager.rerun(5)
        active = await harness.submit()
        queued = await harness.submit()
        with pytest.raises(ConflictError, match=f"run {active.id} is running"):
            await harness.manager.rerun(active.id)
        with pytest.raises(ConflictError, match=f"run {queued.id} is queued"):
            await harness.manager.rerun(queued.id)
        await harness.manager.cancel(queued.id)
        await harness.manager.cancel(active.id)


async def test_a_rerun_of_a_workflow_that_became_invalid_is_rejected(engine: Engine) -> None:
    async with Manager(engine, slow()) as harness:
        detail = await harness.run_to_end()
        harness.source.add(
            make_entry(workflow_id="wf", issues=[Issue(Severity.ERROR, "E101", "x")])
        )
        with pytest.raises(WorkflowInvalidError):
            await harness.manager.rerun(detail.run.id)


async def test_wait_answers_finished_and_unknown_runs(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang())

    async with Manager(engine, slow(policy=SKIP)) as harness:
        active = await harness.submit()
        skipped = await harness.manager.submit(manual())
        assert skipped.run is not None
        assert await harness.manager.wait(skipped.run.id) is RunStatus.SKIPPED
        with pytest.raises(NotFoundError):
            await harness.manager.wait(1234)
        await harness.manager.cancel(active.id)
        assert await harness.manager.wait(active.id) is RunStatus.CANCELLED


async def test_stats_count_active_and_queued_runs(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang())

    async with Manager(engine, slow()) as harness:
        await harness.submit()
        await harness.submit()
        await harness.submit()
        stats = harness.manager.stats()
        draining_before = harness.manager.draining
        await harness.manager.shutdown(grace=0)

    assert (stats.active, stats.queued, stats.draining, draining_before) == (1, 2, False, False)
    assert harness.manager.stats().draining
    assert harness.manager.draining


async def test_a_schedule_slot_is_claimed_once(engine: Engine) -> None:
    workflow = slow()
    key = "wf:schedule:0123456789abcdef"
    now = to_ms(engine.clock.now())
    await engine.store.write(lambda repo: repo.init_schedule(key, "wf", now, at_ms=now))

    async with Manager(engine, workflow) as harness:
        slot = ScheduleSlot(key, now + 60_000, now)
        first = await harness.manager.submit(manual(trigger_key=key, slot=slot))
        taken = await harness.manager.submit(manual(trigger_key=key, slot=slot))
        assert first.run is not None
        await harness.manager.wait(first.run.id)

    assert taken == dataclasses.replace(taken, outcome=SubmitOutcome.SLOT_TAKEN, run=None)
    assert first.outcome is SubmitOutcome.QUEUED
    assert first.run.trigger_key == key
    assert first.run.scheduled_for is not None
    assert to_ms(first.run.scheduled_for) == now + 60_000


async def test_a_duplicate_delivery_returns_the_earlier_run(engine: Engine) -> None:
    delivery = Delivery("deploy", "ab" * 32, window=3600.0)

    async with Manager(engine, slow()) as harness:
        first = await harness.manager.submit(manual(delivery=delivery))
        duplicate = await harness.manager.submit(manual(delivery=delivery))
        assert first.run is not None
        await harness.manager.wait(first.run.id)

    assert duplicate.outcome is SubmitOutcome.DUPLICATE
    assert duplicate.run is not None
    assert duplicate.run.id == first.run.id
    assert await engine.store.latest_run_id() == first.run.id


async def test_requests_after_shutdown_are_refused(engine: Engine) -> None:
    async with Manager(engine, slow()) as harness:
        await harness.manager.shutdown(grace=0)
        await harness.task
        with pytest.raises(ShuttingDownError):
            await harness.manager.submit(manual())
        with pytest.raises(ShuttingDownError):
            await harness.manager.wait(1)
        await harness.manager.shutdown(grace=0)  # a later call does nothing
