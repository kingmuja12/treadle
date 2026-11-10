"""Step outputs: declared outputs over `result`, the size cap, and masking in storage only."""

from __future__ import annotations

from collections.abc import Callable

from tests.support.builders import make_step, make_workflow
from tests.support.fakes import Fail, FakeStep, Succeed
from tests.unit.engine.conftest import API_TOKEN
from tests.unit.engine.helpers import Engine, statuses, step_of

from treadle.config import Limits
from treadle.models import RunStatus, StepStatus, Workflow


async def test_declared_outputs_see_result_and_merge_over_the_raw_outputs(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("count", Succeed({"n": 3, "label": "raw", "keep": True}))
    workflow = load_workflow(
        """
        steps:
          - id: count
            uses: fake
            outputs:
              doubled: ${{ result.n }}
              text: "n=${{ result.n }} ${{ result.label }}"
              label: declared
          - id: use
            uses: fake
            with:
              value: ${{ steps.count.outputs.doubled }}
              data: ${{ steps.count.outputs }}
        """
    )

    detail = await engine.execute(workflow)

    expected = {"n": 3, "label": "declared", "keep": True, "doubled": 3, "text": "n=3 raw"}
    assert step_of(detail, "count").outputs == expected
    assert fake.calls_for("use")[0].params == {"value": 3, "data": expected}


async def test_an_output_error_fails_the_step_and_keeps_the_raw_outputs(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("a", Succeed({"n": None}))
    workflow = load_workflow(
        """
        steps:
          - id: a
            uses: fake
            outputs:
              big: ${{ result.n > 1 }}
          - id: handler
            uses: fake
            if: failure()
            with:
              value: ${{ steps.a.outputs }}
        """
    )

    detail = await engine.execute(workflow)

    a = step_of(detail, "a")
    assert (a.status, a.error) == (
        StepStatus.FAILED,
        "in steps.a.outputs.big: cannot compare null with number",
    )
    assert a.outputs == {"n": None}
    assert len(a.attempts) == 1
    assert fake.calls_for("handler")[0].params == {"value": {"n": None}}
    assert (
        detail.run.error
        == "step 'a' failed: in steps.a.outputs.big: cannot compare null with number"
    )


async def test_declared_outputs_of_a_failed_step_arent_evaluated(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("a", Fail("broke", outputs={"exit_code": 2}))
    workflow = load_workflow(
        """
        steps:
          - id: a
            uses: fake
            outputs:
              never: ${{ result.missing > 1 }}
        """
    )

    detail = await engine.execute(workflow)

    assert (step_of(detail, "a").error, step_of(detail, "a").outputs) == ("broke", {"exit_code": 2})


async def test_a_failed_step_keeps_its_partial_outputs_for_downstream_steps(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("probe", Fail("status 500", outputs={"status": 500, "body": "oops"}))
    workflow = load_workflow(
        """
        steps:
          - id: probe
            uses: fake
          - id: alert
            uses: fake
            if: failure()
            with:
              message: "${{ steps.probe.outputs.status }}: ${{ steps.probe.outputs.body }}"
        """
    )

    detail = await engine.execute(workflow)

    assert step_of(detail, "probe").outputs == {"status": 500, "body": "oops"}
    assert fake.calls_for("alert")[0].params == {"message": "500: oops"}


async def test_outputs_over_the_cap_fail_the_step(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(limits=Limits(outputs_bytes=64))
    fake.script("big", Succeed({"blob": "x" * 64})).script("small", Succeed({"blob": "x" * 40}))
    workflow = make_workflow(make_step("big", needs=[]), make_step("small", needs=[]))

    detail = await engine.execute(workflow)

    big = step_of(detail, "big")
    assert (big.status, big.error, big.outputs) == (StepStatus.FAILED, "outputs exceed 64 B", {})
    assert step_of(detail, "small").status is StepStatus.SUCCEEDED
    assert "step failed: outputs exceed 64 B" in await engine.texts(detail.run.id, step_id="big")


async def test_partial_outputs_over_the_cap_are_dropped(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(limits=Limits(outputs_bytes=64))
    fake.script("big", Fail("exit code 1", outputs={"stdout": "y" * 100}))

    detail = await engine.execute(make_workflow(make_step("big")))

    big = step_of(detail, "big")
    assert (big.status, big.error, big.outputs) == (StepStatus.FAILED, "exit code 1", {})
    assert "dropped the outputs: outputs exceed 64 B" in await engine.texts(detail.run.id)


async def test_outputs_are_masked_in_storage_but_not_downstream(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    fake.script("login", Succeed({"token": API_TOKEN, "user": "ada"}))
    workflow = load_workflow(
        """
        steps:
          - id: login
            uses: fake
            outputs:
              header: "Bearer ${{ result.token }}"
          - id: call
            uses: fake
            with:
              message: ${{ steps.login.outputs.header }}
        """
    )

    detail = await engine.execute(workflow)

    assert step_of(detail, "login").outputs == {
        "token": "***",
        "user": "ada",
        "header": "Bearer ***",
    }
    assert fake.calls_for("call")[0].params == {"message": f"Bearer {API_TOKEN}"}


async def test_errors_are_masked_in_storage(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Fail(f"rejected {API_TOKEN}"))

    detail = await engine.execute(make_workflow(make_step("a")))

    a = step_of(detail, "a")
    assert (a.error, a.attempts[0].error) == ("rejected ***", "rejected ***")
    assert detail.run.error == "step 'a' failed: rejected ***"
    assert detail.run.status is RunStatus.FAILED


async def test_skipped_steps_have_no_outputs(engine: Engine) -> None:
    workflow = make_workflow(make_step("a", condition="false"), make_step("b"))

    detail = await engine.execute(workflow)

    assert statuses(detail)["a"] == "skipped:condition"
    assert step_of(detail, "a").outputs == {}
