"""Run logs: masking, caps, built-in step lines, sequence numbers, and logs before statuses."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Literal

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.fakes import SUCCEED, Fail, FakeStep, Hang, Succeed
from tests.unit.engine.conftest import API_TOKEN
from tests.unit.engine.helpers import ENVIRON, MANUAL, Engine, step_of

from treadle.clock import to_ms
from treadle.config import Limits
from treadle.engine.executor import RunHandle
from treadle.engine.logsink import MAX_RUNTIME_MASKS
from treadle.models import CancelReason, LogStream, RetryPolicy, StepStatus, Workflow
from treadle.schema import Obj
from treadle.steps.base import StepResult, StepRuntime
from treadle.steps.procs import TRUNCATION_MARKER
from treadle.store.repo import NewLogLine, Repository, StepUpdate
from treadle.types import Issue, JSONValue, YamlPath

GENERATED = "generated-value-42"


class MaskingStep:
    """A step type that creates a value, masks it, prints it, and returns it."""

    name = "mint"
    output_names: ClassVar[frozenset[str] | None] = None
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    @property
    def schema(self) -> Obj:
        return Obj("mint step", {})

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        rt.log.write(LogStream.STDOUT, f"before {GENERATED}")
        rt.log.add_mask(GENERATED)
        rt.log.write(LogStream.STDOUT, f"after {GENERATED}")
        return StepResult({"value": GENERATED})


async def test_log_lines_are_masked_with_secrets_and_hidden_values(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("a", Succeed(logs=(f"token {API_TOKEN}", f"hook {ENVIRON['HOOK_SECRET']}", "ok")))

    detail = await engine.execute(make_workflow(make_step("a")))

    assert await engine.texts(detail.run.id, stream=LogStream.STDOUT) == [
        "token ***",
        "hook ***",
        "ok",
    ]


async def test_add_mask_covers_later_lines_and_stored_outputs_only(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(MaskingStep())
    workflow = make_workflow(
        make_step("mint", uses="mint"),
        make_step("use", params={"value": "static"}),
    )

    detail = await engine.execute(workflow)

    assert await engine.texts(detail.run.id, step_id="mint") == [
        f"before {GENERATED}",
        "after ***",
    ]
    assert step_of(detail, "mint").outputs == {"value": "***"}


class FloodStep(MaskingStep):
    """A step type that masks and prints as many values as a run may, and then one more."""

    name = "flood"

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        values = [f"minted-{index:04d}" for index in range(MAX_RUNTIME_MASKS + 1)]
        *allowed, extra = values
        for value in allowed:
            rt.log.add_mask(value)
        for value in allowed:
            rt.log.write(LogStream.STDOUT, f"made {value}")
        rt.log.add_mask(extra)
        rt.log.write(LogStream.STDOUT, f"made {extra}")
        return StepResult({"last": extra})


async def test_a_step_that_passes_the_mask_limit_fails_without_storing_the_value(
    make_engine: Callable[..., Engine],
) -> None:
    engine = make_engine(FloodStep())
    policy = RetryPolicy(attempts=3, delay=0)

    detail = await engine.execute(make_workflow(make_step("flood", uses="flood", retry=policy)))

    flood = step_of(detail, "flood")
    error = "too many ::add-mask:: values: a run masks at most 1000"
    assert (flood.status, flood.error, flood.outputs, len(flood.attempts)) == (
        StepStatus.FAILED,
        error,
        {},
        1,
    )
    assert flood.attempts[0].log_truncated
    texts = await engine.texts(detail.run.id, stream=LogStream.STDOUT)
    assert texts == ["made ***"] * MAX_RUNTIME_MASKS
    assert "minted-1000" not in " ".join(await engine.texts(detail.run.id))


async def test_built_in_steps_write_masked_system_lines(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: say
            uses: log
            with:
              message: "hello ${{ secrets.API_TOKEN }}"
              level: warning
          - id: pause
            uses: sleep
            with:
              duration: 2s
        """
    )

    detail = await engine.execute(workflow)

    assert await engine.texts(detail.run.id, step_id="say") == ["[warning] hello ***"]
    assert step_of(detail, "pause").duration_ms == 2000


async def test_log_caps_cut_lines_and_drop_output_past_the_attempt_cap(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(limits=Limits(step_log_bytes=48, line_bytes=20))
    fake.script("chatty", Succeed(logs=("y" * 30, *(["x" * 9] * 10))))

    detail = await engine.execute(make_workflow(make_step("chatty")))

    attempt = step_of(detail, "chatty").attempts[0]
    assert (attempt.log_bytes, attempt.log_truncated) == (38 + 10, True)
    assert await engine.texts(detail.run.id) == [
        "y" * 20 + TRUNCATION_MARKER,
        "x" * 9,
        "output truncated: further output from this step is dropped (limit 48 B)",
        "dropped 90 bytes of output",
    ]


async def test_each_attempt_has_its_own_cap(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine(limits=Limits(step_log_bytes=20))
    fake.script("a", Fail(logs=("x" * 9,) * 3), Succeed(logs=("z" * 9,)))
    policy = RetryPolicy(attempts=2, delay=0)

    detail = await engine.execute(make_workflow(make_step("a", retry=policy)))

    first, second = step_of(detail, "a").attempts
    assert (first.log_bytes, first.log_truncated) == (20, True)
    assert (second.log_bytes, second.log_truncated) == (10, False)


async def test_new_lines_continue_after_the_runs_existing_lines(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("a", Succeed(logs=("first new line",)))
    workflow = make_workflow(make_step("a"))
    run = await engine.start(workflow)
    earlier = NewLogLine(1, to_ms(engine.clock.now()), None, None, LogStream.SYSTEM, "earlier")
    await engine.store.write(lambda repo: repo.append_logs(run.id, [earlier]))

    await engine.executor.execute(run, workflow, RunHandle(run.id), inputs={}, trigger=MANUAL)

    lines = await engine.logs(run.id)
    assert [(line.seq, line.text) for line in lines] == [(1, "earlier"), (2, "first new line")]


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Record, in commit order, log lines, attempt results, and step status changes."""
    events: list[tuple[Any, ...]] = []
    append_logs, finish_attempt = Repository.append_logs, Repository.finish_attempt
    update_step = Repository.update_step

    def logged(self: Repository, run_id: int, lines: list[NewLogLine]) -> None:
        events.extend(("log", line.step_id, line.attempt, line.text) for line in lines)
        append_logs(self, run_id, lines)

    def finished(self: Repository, run_id: int, step_id: str, attempt: int, **kwargs: Any) -> bool:
        events.append(("attempt", step_id, attempt))
        return finish_attempt(self, run_id, step_id, attempt, **kwargs)

    def updated(self: Repository, run_id: int, step_id: str, update: StepUpdate) -> bool:
        events.append(("step", step_id, update.status))
        return update_step(self, run_id, step_id, update)

    monkeypatch.setattr(Repository, "append_logs", logged)
    monkeypatch.setattr(Repository, "finish_attempt", finished)
    monkeypatch.setattr(Repository, "update_step", updated)
    return events


async def test_a_steps_logs_are_committed_before_its_attempts_and_status(
    engine: Engine, fake: FakeStep, journal: list[tuple[Any, ...]]
) -> None:
    fake.script("a", Fail("first", logs=("a1-out",)), Succeed(logs=("a2-out",)))
    fake.script("b", SUCCEED)
    policy = RetryPolicy(attempts=2, delay=5.0)

    await engine.execute(make_workflow(make_step("a", retry=policy), make_step("b")))

    def index(event: tuple[Any, ...]) -> int:
        return journal.index(event)

    assert index(("log", "a", 1, "a1-out")) < index(("attempt", "a", 1))
    assert index(("log", "a", 1, "attempt 1 failed: first")) < index(("attempt", "a", 1))
    assert index(("log", "a", 2, "a2-out")) < index(("attempt", "a", 2))
    final = index(("step", "a", StepStatus.SUCCEEDED))
    step_lines = [i for i, event in enumerate(journal) if event[:2] == ("log", "a")]
    assert max(step_lines) < final


class LoudStep(FakeStep):
    """A fake step that writes one line before its scripted behavior."""

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        rt.log.write(LogStream.STDOUT, "working")
        return await super().run(params, rt)


async def test_a_cancelled_steps_logs_are_committed_before_its_status(
    make_engine: Callable[..., Engine], journal: list[tuple[Any, ...]]
) -> None:
    engine = make_engine(LoudStep("loud", default=Hang()))
    launched = await engine.launch(make_workflow(make_step("w", uses="loud")))

    await asyncio.sleep(1)
    launched.handle.request_cancel(CancelReason.USER)
    await engine.finish(launched)

    assert journal.index(("log", "w", 1, "working")) < journal.index(("attempt", "w", 1))
    assert journal.index(("attempt", "w", 1)) < journal.index(("step", "w", StepStatus.CANCELLED))
