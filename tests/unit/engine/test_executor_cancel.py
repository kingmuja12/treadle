"""Cancellation: the run handle, user cancels, the run timeout, cleanup steps, and force."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.fakes import Fail, FakeStep, Hang, Sleep
from tests.support.vtime import current_loop
from tests.unit.engine.helpers import Engine, call_times, statuses, step_of

from treadle.engine.executor import RunHandle
from treadle.models import (
    AttemptStatus,
    CancelReason,
    RetryPolicy,
    RunReason,
    RunStatus,
    StepStatus,
    Workflow,
)


def test_the_first_cancel_reason_wins_and_a_second_request_forces() -> None:
    handle = RunHandle(7)

    assert (handle.run_id, handle.reason, handle.forced, handle.finalizing) == (
        7,
        None,
        False,
        False,
    )
    assert handle.request_cancel(CancelReason.USER)
    assert (handle.reason, handle.forced) == (CancelReason.USER, False)
    assert handle.request_cancel(CancelReason.TIMEOUT)
    assert (handle.reason, handle.forced) == (CancelReason.USER, True)
    assert not handle.request_cancel(CancelReason.SHUTDOWN)
    assert not handle.force()


def test_a_forced_first_request_sets_both() -> None:
    handle = RunHandle(1)

    assert handle.request_cancel(CancelReason.SHUTDOWN, force=True)
    assert (handle.reason, handle.forced) == (CancelReason.SHUTDOWN, True)


def test_forcing_a_run_that_isnt_cancelling_gives_it_the_user_reason() -> None:
    handle = RunHandle(1)

    assert handle.force()
    assert (handle.reason, handle.forced) == (CancelReason.USER, True)


async def test_wait_changed_returns_at_the_next_change_only() -> None:
    handle = RunHandle(1)
    waiter = asyncio.create_task(handle.wait_changed())
    await asyncio.sleep(1)
    assert not waiter.done()

    handle.request_cancel(CancelReason.USER)
    await asyncio.sleep(0)
    assert waiter.done()

    later = asyncio.create_task(handle.wait_changed())
    await asyncio.sleep(0)
    handle.request_cancel(CancelReason.USER, force=True)
    await asyncio.sleep(0)
    assert later.done()

    unchanged = asyncio.create_task(handle.wait_changed())
    await asyncio.sleep(0)
    handle.force()
    await asyncio.sleep(1)
    assert not unchanged.done()
    unchanged.cancel()


def call_soon_nested(depth: int, callback: Callable[[], object]) -> None:
    """Run `callback` `depth` event-loop iterations from now."""
    loop = asyncio.get_running_loop()
    if depth == 1:
        loop.call_soon(callback)
    else:
        loop.call_soon(call_soon_nested, depth - 1, callback)


@pytest.mark.parametrize("depth", range(1, 9))
async def test_a_force_right_after_the_executor_looks_at_the_handle_is_seen(
    engine: Engine, fake: FakeStep, depth: int
) -> None:
    # A regression test: a force that lands between the executor reading the handle and its
    # new waiter's first step was once missed, so a hung cleanup step was never stopped.
    fake.script("cleanup", Hang())
    workflow = make_workflow(make_step("cleanup", condition="always()"), cleanup_timeout=3600)
    launched = await engine.launch(workflow)
    await asyncio.sleep(5)
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER)
    call_soon_nested(depth, launched.handle.force)
    detail = await engine.finish(launched)

    assert statuses(detail) == {"cleanup": "cancelled"}
    assert current_loop().time() == start


async def cancel_at(seconds: float, handle: RunHandle, reason: CancelReason) -> None:
    await asyncio.sleep(seconds)
    handle.request_cancel(reason)


async def test_a_user_cancel_stops_the_running_step_and_skips_the_rest(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang())
    workflow = make_workflow(
        make_step("work"),
        make_step("next"),
        make_step("cleanup", condition="always()"),
        make_step("notify", condition="cancelled()"),
    )
    launched = await engine.launch(workflow)
    start = current_loop().time()

    await cancel_at(10, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.USER)
    assert detail.run.error == "run cancelled (user)"
    assert statuses(detail) == {
        "work": "cancelled",
        "next": "skipped:run_cancelled",
        "cleanup": "succeeded",
        "notify": "succeeded",
    }
    work = step_of(detail, "work")
    assert [(a.status, a.error) for a in work.attempts] == [(AttemptStatus.CANCELLED, None)]
    assert work.error is None
    assert call_times(fake, "cleanup") == [start + 10]
    assert "cancelling the run (user)" in await engine.texts(detail.run.id, step_id=None)


async def test_the_run_timeout_cancels_with_reason_timeout(engine: Engine, fake: FakeStep) -> None:
    fake.script("slow", Hang())
    workflow = make_workflow(
        make_step("slow"), make_step("cleanup", condition="always()"), timeout=60
    )
    launched = await engine.launch(workflow)
    start = current_loop().time()

    detail = await engine.finish(launched)

    assert (detail.run.status, detail.run.reason) == (RunStatus.TIMED_OUT, RunReason.TIMEOUT)
    assert detail.run.error == "run timed out after 1m"
    assert statuses(detail) == {"slow": "cancelled", "cleanup": "succeeded"}
    assert call_times(fake, "cleanup") == [start + 60]
    lines = await engine.texts(detail.run.id)
    assert "the run timed out after 1m; cancelling" in lines


async def test_a_running_cleanup_step_keeps_running_while_others_stop(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("work", Hang()).script("watcher", Sleep(100))
    workflow = make_workflow(
        make_step("work", needs=[]),
        make_step("watcher", needs=[], condition="always()"),
    )
    launched = await engine.launch(workflow)
    start = current_loop().time()

    await cancel_at(10, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert statuses(detail) == {"work": "cancelled", "watcher": "succeeded"}
    assert current_loop().time() - start == 100
    assert detail.run.status is RunStatus.CANCELLED


async def test_a_condition_that_raises_when_reevaluated_cancels_the_step(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("guarded", Hang())
    workflow = load_workflow(
        """
        inputs:
          n: {type: integer}
        steps:
          - id: guarded
            uses: fake
            if: "!cancelled() || inputs.n > 1"
        """
    )
    launched = await engine.launch(workflow, inputs={"n": None})

    await cancel_at(5, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert statuses(detail) == {"guarded": "cancelled"}


async def test_cleanup_timeout_forces_a_slow_cleanup(engine: Engine, fake: FakeStep) -> None:
    fake.script("work", Hang()).script("cleanup", Hang())
    workflow = make_workflow(
        make_step("work"),
        make_step("cleanup", condition="always()"),
        make_step("final", condition="always()"),
        cleanup_timeout=30,
    )
    launched = await engine.launch(workflow)
    start = current_loop().time()

    await cancel_at(10, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert statuses(detail) == {
        "work": "cancelled",
        "cleanup": "cancelled",
        "final": "skipped:run_cancelled",
    }
    assert current_loop().time() - start == 40
    assert detail.run.error == "run cancelled (user)"
    assert "forcing cancellation: stopping every running step" in await engine.texts(detail.run.id)


async def test_a_second_cancel_forces_at_once(engine: Engine, fake: FakeStep) -> None:
    fake.script("cleanup", Hang())
    workflow = make_workflow(make_step("cleanup", condition="always()"))
    launched = await engine.launch(workflow)
    start = current_loop().time()

    await cancel_at(10, launched.handle, CancelReason.USER)
    await cancel_at(5, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert statuses(detail) == {"cleanup": "cancelled"}
    assert current_loop().time() - start == 15


async def test_the_run_timer_stops_once_a_user_cancel_starts(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Sleep(50))
    workflow = make_workflow(
        make_step("cleanup", condition="always()"), timeout=20, cleanup_timeout=300
    )
    launched = await engine.launch(workflow)

    await cancel_at(10, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert statuses(detail) == {"cleanup": "succeeded"}
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.USER)


async def test_a_cancel_during_a_backoff_wait_has_no_open_attempt(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("flaky", Fail("nope"))
    workflow = make_workflow(make_step("flaky", retry=RetryPolicy(attempts=3, delay=60.0)))
    launched = await engine.launch(workflow)

    await cancel_at(20, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    flaky = step_of(detail, "flaky")
    assert flaky.status is StepStatus.CANCELLED
    assert [attempt.status for attempt in flaky.attempts] == [AttemptStatus.FAILED]
    assert len(fake.calls_for("flaky")) == 1


async def test_a_cleanup_step_stops_retrying_once_the_run_is_cancelling(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Fail("still broken"))
    policy = RetryPolicy(attempts=5, delay=60.0)
    workflow = make_workflow(make_step("cleanup", condition="always()", retry=policy))
    launched = await engine.launch(workflow)

    await cancel_at(20, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    cleanup = step_of(detail, "cleanup")
    assert (cleanup.status, cleanup.error, len(cleanup.attempts)) == (
        StepStatus.FAILED,
        "still broken",
        1,
    )


async def test_an_attempt_that_fails_while_cancelling_isnt_retried(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Sleep(30, then=Fail("broke")))
    policy = RetryPolicy(attempts=5, delay=1.0)
    workflow = make_workflow(make_step("cleanup", condition="always()", retry=policy))
    launched = await engine.launch(workflow)

    await cancel_at(10, launched.handle, CancelReason.USER)
    detail = await engine.finish(launched)

    assert len(step_of(detail, "cleanup").attempts) == 1


async def test_a_cancelled_attempt_isnt_a_timeout(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Hang())
    workflow = make_workflow(make_step("a", timeout=20))
    launched = await engine.launch(workflow)

    await cancel_at(20, launched.handle, CancelReason.USER)  # the same instant as the timeout
    detail = await engine.finish(launched)

    attempt = step_of(detail, "a").attempts[0]
    assert attempt.status in (AttemptStatus.CANCELLED, AttemptStatus.TIMED_OUT)
    assert detail.run.status is RunStatus.CANCELLED


async def test_a_run_cancelled_before_it_starts_runs_only_cleanup_steps(
    engine: Engine, fake: FakeStep
) -> None:
    workflow = make_workflow(make_step("a"), make_step("cleanup", needs=[], condition="always()"))
    run = await engine.start(workflow)
    handle = RunHandle(run.id)
    handle.request_cancel(CancelReason.REPLACED)

    status = await engine.executor.execute(run, workflow, handle, inputs={}, trigger={})

    detail = await engine.detail(run.id)
    assert (status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.REPLACED)
    assert statuses(detail) == {"a": "skipped:run_cancelled", "cleanup": "succeeded"}


async def test_cancelling_the_execution_records_the_run_as_cancelled(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("a", Hang())
    workflow = make_workflow(make_step("a"), make_step("b"))
    launched = await engine.launch(workflow)
    await asyncio.sleep(5)

    launched.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launched.task

    detail = await engine.detail(launched.run_id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    assert detail.run.error == "run cancelled (shutdown)"
    assert statuses(detail) == {"a": "cancelled", "b": "skipped:run_cancelled"}
    assert launched.handle.finalizing
    assert not (engine.settings.tmp_dir / f"run-{launched.run_id}").exists()


async def test_cancelling_the_execution_keeps_an_existing_reason(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("cleanup", Hang())
    workflow = make_workflow(make_step("cleanup", condition="always()"))
    launched = await engine.launch(workflow)
    await cancel_at(5, launched.handle, CancelReason.USER)
    await asyncio.sleep(1)

    launched.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launched.task

    detail = await engine.detail(launched.run_id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.USER)
    assert statuses(detail) == {"cleanup": "cancelled"}
