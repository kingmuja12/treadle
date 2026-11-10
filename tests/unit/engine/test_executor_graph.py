"""The step graph: ordering, parallelism, status functions, skip reasons, and conclusions."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.support.builders import make_run, make_step, make_workflow
from tests.support.fakes import Fail, FakeStep, Hang, Sleep
from tests.unit.engine.helpers import MANUAL, Engine, call_times, statuses, step_of

from treadle.engine.context import RunState
from treadle.engine.executor import RunHandle, conclude
from treadle.models import (
    CancelReason,
    RetryPolicy,
    RunReason,
    RunStatus,
    SkipReason,
    StepStatus,
    Workflow,
)
from treadle.types import Issue, Severity

RETRY_3 = RetryPolicy(attempts=3, delay=1.0)


async def test_steps_without_needs_run_one_after_another(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Sleep(5)).script("b", Sleep(7))
    workflow = make_workflow(make_step("a"), make_step("b"), make_step("c"))

    detail = await engine.execute(workflow)

    assert detail.run.status is RunStatus.SUCCEEDED
    assert [call.step_id for call in fake.calls] == ["a", "b", "c"]
    start = call_times(fake, "a")[0]
    assert [call_times(fake, step)[0] - start for step in "bc"] == [5, 12]


async def test_roots_run_together_up_to_max_parallel(engine: Engine, fake: FakeStep) -> None:
    for step in "abc":
        fake.script(step, Sleep(10))
    roots = [make_step(step, needs=[]) for step in "abc"]

    await engine.execute(make_workflow(*roots, max_parallel=2))

    start = call_times(fake, "a")[0]
    assert [call_times(fake, step)[0] - start for step in "abc"] == [0, 0, 10]


async def test_a_step_waits_for_every_need(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Sleep(5)).script("b", Sleep(10)).script("c", Sleep(3))
    workflow = make_workflow(
        make_step("a"),
        make_step("b", needs=["a"]),
        make_step("c", needs=["a"]),
        make_step("d", needs=["b", "c"]),
    )

    detail = await engine.execute(workflow)

    start = call_times(fake, "a")[0]
    assert {step: call_times(fake, step)[0] - start for step in "bcd"} == {
        "b": 5,
        "c": 5,
        "d": 15,
    }
    assert [step.status for step in detail.steps] == [StepStatus.SUCCEEDED] * 4


async def test_a_blocked_step_doesnt_hold_back_later_ready_steps(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("slow", Sleep(20))
    workflow = make_workflow(
        make_step("slow", needs=[]),
        make_step("after-slow", needs=["slow"]),
        make_step("quick", needs=[]),
    )

    await engine.execute(workflow)

    assert call_times(fake, "quick")[0] == call_times(fake, "slow")[0]


async def test_a_failure_skips_dependents_and_runs_failure_handlers(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("check-drive", Fail("drive missing"))
    workflow = make_workflow(
        make_step("check-drive"),
        make_step("archive"),
        make_step("record"),
        make_step("alert", needs=["archive"], condition="failure()"),
        make_step("unrelated", needs=[]),
    )

    detail = await engine.execute(workflow)

    assert statuses(detail) == {
        "check-drive": "failed",
        "archive": "skipped:upstream_failed",
        "record": "skipped:upstream_failed",
        "alert": "succeeded",
        "unrelated": "succeeded",
    }
    assert (detail.run.status, detail.run.reason) == (RunStatus.FAILED, None)
    assert detail.run.error == "step 'check-drive' failed: drive missing"


async def test_status_functions_see_only_ancestors(engine: Engine, fake: FakeStep) -> None:
    fake.script("broken", Fail())
    workflow = make_workflow(
        make_step("broken", needs=[]),
        make_step("fine", needs=[]),
        make_step("after-fine", needs=["fine"]),
        make_step("handler", needs=["fine"], condition="failure()"),
    )

    detail = await engine.execute(workflow)

    assert statuses(detail)["after-fine"] == "succeeded"
    assert statuses(detail)["handler"] == "skipped:condition"


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("always()", "succeeded"),
        ("failure()", "succeeded"),
        ("success()", "skipped:upstream_failed"),
        ("cancelled()", "skipped:condition"),
        ("true", "skipped:upstream_failed"),
        ("false", "skipped:upstream_failed"),
    ],
)
async def test_skip_reasons_after_a_failure(
    engine: Engine, fake: FakeStep, condition: str, expected: str
) -> None:
    fake.script("a", Fail())
    workflow = make_workflow(make_step("a"), make_step("b", condition=condition))

    detail = await engine.execute(workflow)

    assert statuses(detail)["b"] == expected


async def test_a_false_condition_skips_with_reason_condition(engine: Engine) -> None:
    workflow = make_workflow(make_step("a", condition="false"), make_step("b"))

    detail = await engine.execute(workflow)

    assert statuses(detail) == {"a": "skipped:condition", "b": "succeeded"}
    assert detail.run.status is RunStatus.SUCCEEDED


async def test_a_run_whose_steps_all_skip_succeeds(engine: Engine) -> None:
    workflow = make_workflow(make_step("a", condition="false"), make_step("b", condition="false"))

    detail = await engine.execute(workflow)

    assert (detail.run.status, detail.run.error) == (RunStatus.SUCCEEDED, None)


async def test_a_tolerated_failure_lets_dependents_run(engine: Engine, fake: FakeStep) -> None:
    fake.script("flaky", Fail("flaked"))
    workflow = make_workflow(
        make_step("flaky", continue_on_error=True),
        make_step("next"),
        make_step("handler", needs=["flaky"], condition="failure()"),
    )

    detail = await engine.execute(workflow)

    flaky = step_of(detail, "flaky")
    assert (flaky.status, flaky.tolerated, flaky.error) == (StepStatus.FAILED, True, "flaked")
    assert statuses(detail)["next"] == "succeeded"
    assert statuses(detail)["handler"] == "skipped:condition"
    assert (detail.run.status, detail.run.error) == (RunStatus.SUCCEEDED, None)


async def test_a_tolerated_timeout_lets_the_run_succeed(engine: Engine, fake: FakeStep) -> None:
    fake.script("slow", Hang())
    workflow = make_workflow(make_step("slow", timeout=5, continue_on_error=True))

    detail = await engine.execute(workflow)

    slow = step_of(detail, "slow")
    assert (slow.status, slow.tolerated) == (StepStatus.TIMED_OUT, True)
    assert detail.run.status is RunStatus.SUCCEEDED


async def test_expressions_see_the_raw_status_of_a_tolerated_step(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("flaky", Fail())
    workflow = load_workflow(
        """
        steps:
          - id: flaky
            uses: fake
            continue_on_error: true
          - id: report
            uses: fake
            if: steps.flaky.status == 'failed'
            with:
              value: ${{ steps.flaky.status }}
        """
    )

    detail = await engine.execute(workflow)

    assert statuses(detail)["report"] == "succeeded"
    assert fake.calls_for("report")[0].params == {"value": "failed"}


async def test_the_run_error_names_the_first_failed_step_by_position(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("first", Sleep(10, then=Fail("late failure")))
    fake.script("second", Fail("early failure"))
    workflow = make_workflow(make_step("first", needs=[]), make_step("second", needs=[]))

    detail = await engine.execute(workflow)

    assert detail.run.error == "step 'first' failed: late failure"


async def test_the_run_error_counts_attempts(engine: Engine, fake: FakeStep) -> None:
    fake.script("archive", Fail("exit code 23", exit_code=23))
    workflow = make_workflow(make_step("archive", retry=RETRY_3))

    detail = await engine.execute(workflow)

    assert detail.run.error == "step 'archive' failed after 3 attempts: exit code 23"


@pytest.mark.parametrize(
    ("retry", "error"),
    [
        (RetryPolicy(), "step 'build' timed out after 10m"),
        (RetryPolicy(attempts=2, delay=0), "step 'build' timed out after 10m (2 attempts)"),
    ],
)
async def test_the_run_error_of_a_timed_out_step(
    engine: Engine, fake: FakeStep, retry: RetryPolicy, error: str
) -> None:
    fake.script("build", Hang())

    detail = await engine.execute(make_workflow(make_step("build", timeout=600, retry=retry)))

    assert (detail.run.status, detail.run.error) == (RunStatus.FAILED, error)
    assert step_of(detail, "build").status is StepStatus.TIMED_OUT


async def test_a_condition_error_fails_the_step_without_attempts(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        inputs:
          n:
            type: integer
        steps:
          - id: guarded
            uses: fake
            if: inputs.n > 1
          - id: after
            uses: fake
        """
    )

    detail = await engine.execute(workflow, inputs={"n": None})

    guarded = step_of(detail, "guarded")
    assert (guarded.status, guarded.attempts) == (StepStatus.FAILED, ())
    assert guarded.error == "in steps.guarded.if: cannot compare null with number"
    assert statuses(detail)["after"] == "skipped:upstream_failed"
    assert fake.calls == []
    assert "step failed: in steps.guarded.if: cannot compare null with number" in (
        await engine.texts(detail.run.id, step_id="guarded")
    )


@pytest.mark.parametrize(
    ("with_text", "error"),
    [
        (
            "value: ${{ inputs.n > 1 }}",
            "in steps.a.with.value: cannot compare null with number",
        ),
        (
            "data: {list: [ok, '${{ inputs.n > 1 }}']}",
            "in steps.a.with.data.list[1]: cannot compare null with number",
        ),
        (
            "message: ${{ inputs.count }}",
            "in steps.a.with.message: expected a string, got a number",
        ),
    ],
)
async def test_a_render_or_validation_error_fails_the_step_without_attempts(
    engine: Engine,
    fake: FakeStep,
    load_workflow: Callable[..., Workflow],
    with_text: str,
    error: str,
) -> None:
    workflow = load_workflow(
        f"""
        inputs:
          n: {{type: integer}}
          count: {{type: integer, default: 3}}
        steps:
          - id: a
            uses: fake
            retry: 3
            with:
              {with_text}
        """
    )

    detail = await engine.execute(workflow, inputs={"n": None, "count": 3})

    step = step_of(detail, "a")
    assert (step.status, step.attempts, step.error) == (StepStatus.FAILED, (), error)
    assert fake.calls == []
    assert detail.run.error == f"step 'a' failed: {error}"


async def test_an_env_error_names_the_variable(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: a
            uses: fake
            env:
              TOKEN: ${{ secrets.MISSING }}
        """
    )

    detail = await engine.execute(workflow)

    assert step_of(detail, "a").error == "in steps.a.env.TOKEN: secret MISSING is not defined"


async def test_a_step_type_check_failure_fails_the_step(engine: Engine, fake: FakeStep) -> None:
    fake.issues = [
        Issue(Severity.WARNING, "W999", "only a warning"),
        Issue(Severity.ERROR, "E407", "value and data clash", ("steps", "a", "with", "data")),
        Issue(Severity.ERROR, "E407", "something is off"),
    ]

    detail = await engine.execute(make_workflow(make_step("a")))

    assert step_of(detail, "a").error == (
        "in steps.a.with.data: value and data clash; in steps.a.with: something is off"
    )
    assert fake.calls == []


async def test_an_unknown_step_type_fails_the_step(engine: Engine) -> None:
    detail = await engine.execute(make_workflow(make_step("a", uses="teleport")))

    assert step_of(detail, "a").error == "in steps.a.uses: unknown step type 'teleport'"


async def test_a_workflow_env_error_fails_the_run_during_setup(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        env:
          FINE: ok
          TOKEN: ${{ secrets.MISSING }}
        steps:
          - id: a
            uses: fake
          - id: cleanup
            uses: fake
            if: always()
        """
    )

    detail = await engine.execute(workflow)

    assert (detail.run.status, detail.run.reason) == (RunStatus.FAILED, RunReason.SETUP_FAILED)
    assert detail.run.error == "in env.TOKEN: secret MISSING is not defined"
    assert statuses(detail) == {"a": "skipped:setup_failed", "cleanup": "skipped:setup_failed"}
    assert fake.calls == []
    assert await engine.texts(detail.run.id) == [
        "setup failed: in env.TOKEN: secret MISSING is not defined"
    ]


async def test_a_run_directory_that_cant_be_made_fails_the_run_during_setup(
    engine: Engine, fake: FakeStep
) -> None:
    workflow = make_workflow(make_step("a"))
    run = await engine.start(workflow)
    (engine.settings.tmp_dir / f"run-{run.id}").write_text("in the way")

    status = await engine.executor.execute(
        run, workflow, RunHandle(run.id), inputs={}, trigger=MANUAL
    )

    detail = await engine.detail(run.id)
    assert (status, detail.run.reason) == (RunStatus.FAILED, RunReason.SETUP_FAILED)
    assert detail.run.error is not None
    assert detail.run.error.startswith("cannot prepare the run directory")
    assert statuses(detail) == {"a": "skipped:setup_failed"}
    assert fake.calls == []


def conclusion_state(workflow: Workflow, **step_statuses: StepStatus) -> RunState:
    return RunState(
        run=make_run(1, status=RunStatus.RUNNING),
        workflow=workflow,
        inputs={},
        trigger={},
        secrets={},
        process_env={},
        statuses={step.id: StepStatus.SUCCEEDED for step in workflow.steps} | step_statuses,
    )


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (CancelReason.TIMEOUT, (RunStatus.TIMED_OUT, "run timed out after 2h")),
        (CancelReason.USER, (RunStatus.CANCELLED, "run cancelled (user)")),
        (CancelReason.REPLACED, (RunStatus.CANCELLED, "run cancelled (replaced)")),
        (CancelReason.SHUTDOWN, (RunStatus.CANCELLED, "run cancelled (shutdown)")),
        (
            CancelReason.WORKFLOW_REMOVED,
            (RunStatus.CANCELLED, "run cancelled (workflow_removed)"),
        ),
    ],
)
def test_conclude_puts_a_cancel_reason_first(
    reason: CancelReason, expected: tuple[RunStatus, str]
) -> None:
    state = conclusion_state(make_workflow(make_step("a")), a=StepStatus.FAILED)

    assert conclude(state, reason) == expected


def test_conclude_reports_the_first_untolerated_failure() -> None:
    workflow = make_workflow(
        make_step("a", needs=[]),
        make_step("b", needs=[]),
        make_step("c", needs=[], timeout=90),
    )
    state = conclusion_state(
        workflow, a=StepStatus.FAILED, b=StepStatus.CANCELLED, c=StepStatus.TIMED_OUT
    )
    state.tolerated.add("a")

    assert conclude(state, None) == (RunStatus.FAILED, "step 'c' timed out after 1m30s")


def test_conclude_without_an_error_text_names_the_step() -> None:
    state = conclusion_state(make_workflow(make_step("a")), a=StepStatus.FAILED)

    assert conclude(state, None) == (RunStatus.FAILED, "step 'a' failed")


def test_conclude_succeeds_when_nothing_failed() -> None:
    workflow = make_workflow(make_step("a"), make_step("b"))
    state = conclusion_state(workflow, b=StepStatus.SKIPPED)

    assert conclude(state, None) == (RunStatus.SUCCEEDED, None)


async def test_skip_reasons_are_stored(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Fail())
    workflow = make_workflow(make_step("a"), make_step("b"))

    detail = await engine.execute(workflow)

    assert step_of(detail, "b").skip_reason is SkipReason.UPSTREAM_FAILED
    assert step_of(detail, "b").attempts == ()
