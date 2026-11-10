"""Attempts: retries, the exact backoff sequence, and how each attempt outcome is classified."""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Callable, Mapping
from typing import ClassVar

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.fakes import SUCCEED, Fail, FakeStep, Hang, Raise, Sleep, Succeed
from tests.support.vtime import current_loop
from tests.unit.engine.conftest import API_TOKEN
from tests.unit.engine.helpers import Engine, call_times, step_of

from treadle.engine.executor import backoff_delay
from treadle.errors import StepConfigError
from treadle.models import AttemptStatus, LogStream, RetryPolicy, RunStatus, StepStatus
from treadle.steps.base import StepResult, StepRuntime
from treadle.types import JSONValue

A = AttemptStatus


def gaps(times: list[float]) -> list[float]:
    return [later - earlier for earlier, later in itertools.pairwise(times)]


def test_backoff_grows_by_the_factor_up_to_max_delay() -> None:
    policy = RetryPolicy(attempts=8, delay=30.0, backoff=2.0, max_delay=600.0)
    rng = random.Random(1)

    waits = [backoff_delay(policy, attempt, rng) for attempt in range(1, 8)]

    assert waits == [30, 60, 120, 240, 480, 600, 600]


def test_jitter_subtracts_a_random_fraction() -> None:
    policy = RetryPolicy(attempts=3, delay=10.0, backoff=1.0, max_delay=10.0, jitter=0.5)
    rng, expected = random.Random(1234), random.Random(1234)

    waits = [backoff_delay(policy, 1, rng) for _ in range(50)]

    assert waits == [10 * (1 - 0.5 * expected.random()) for _ in range(50)]
    assert all(5.0 < wait <= 10.0 for wait in waits)


async def test_failed_attempts_wait_the_exact_backoff(engine: Engine, fake: FakeStep) -> None:
    fake.script("archive", Fail("exit code 23", exit_code=23))
    policy = RetryPolicy(attempts=8, delay=30.0, backoff=2.0, max_delay=600.0)

    detail = await engine.execute(make_workflow(make_step("archive", retry=policy)))

    assert gaps(call_times(fake, "archive")) == [30, 60, 120, 240, 480, 600, 600]
    archive = step_of(detail, "archive")
    assert [attempt.delay_before_ms for attempt in archive.attempts] == [
        0,
        30_000,
        60_000,
        120_000,
        240_000,
        480_000,
        600_000,
        600_000,
    ]
    assert {attempt.status for attempt in archive.attempts} == {A.FAILED}
    assert {attempt.exit_code for attempt in archive.attempts} == {23}
    assert (archive.status, archive.error) == (StepStatus.FAILED, "exit code 23")


async def test_seeded_jitter_gives_the_same_waits_as_the_formula(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("ping", Fail())
    policy = RetryPolicy(attempts=5, delay=2.0, backoff=2.0, max_delay=10.0, jitter=0.2)
    expected = random.Random(1234)  # the same seed as the engine's random source

    await engine.execute(make_workflow(make_step("ping", retry=policy)))

    bases = [2.0, 4.0, 8.0, 10.0]
    assert gaps(call_times(fake, "ping")) == pytest.approx(
        [base * (1 - 0.2 * expected.random()) for base in bases]
    )


async def test_retry_lines_and_attempt_lines_are_logged(engine: Engine, fake: FakeStep) -> None:
    fake.script("archive", Fail("exit code 23"), Fail("exit code 24"), SUCCEED)
    policy = RetryPolicy(attempts=3, delay=30.0)

    detail = await engine.execute(make_workflow(make_step("archive", retry=policy)))

    run_id = detail.run.id
    assert await engine.texts(run_id, step_id="archive", attempt=1) == [
        "attempt 1 failed: exit code 23",
        "retrying in 30s (attempt 2 of 3)",
    ]
    assert await engine.texts(run_id, step_id="archive", attempt=2) == [
        "attempt 2 failed: exit code 24",
        "retrying in 1m (attempt 3 of 3)",
    ]
    assert await engine.texts(run_id, step_id="archive", attempt=3) == []
    archive = step_of(detail, "archive")
    assert [attempt.status for attempt in archive.attempts] == [A.FAILED, A.FAILED, A.SUCCEEDED]
    assert (archive.status, archive.error) == (StepStatus.SUCCEEDED, None)
    assert detail.run.status is RunStatus.SUCCEEDED


async def test_the_retry_line_is_stored_before_the_next_attempt_starts(
    engine: Engine, fake: FakeStep, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script("ping", Fail("exit code 1"), SUCCEED)
    stored: list[list[str]] = []
    perform = fake.run

    async def run(params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        if rt.attempt == 2:
            stored.append(await engine.texts(rt.run_id, step_id="ping", attempt=1))
        return await perform(params, rt)

    monkeypatch.setattr(fake, "run", run)

    policy = RetryPolicy(attempts=2, delay=0.0)

    await engine.execute(make_workflow(make_step("ping", retry=policy)))

    # treadle run reads the delay and the attempt count of a retried attempt from this line,
    # so the line must be stored by the time a later attempt can appear. A zero delay leaves
    # the log no time to flush on its own schedule.
    assert stored == [["attempt 1 failed: exit code 1", "retrying in 0ms (attempt 2 of 2)"]]


async def test_a_step_is_retrying_during_the_backoff(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Fail(), SUCCEED)
    launched = await engine.launch(
        make_workflow(make_step("a", retry=RetryPolicy(attempts=2, delay=60.0)))
    )

    await asyncio.sleep(30)
    during = step_of(await engine.detail(launched.run_id), "a")
    detail = await engine.finish(launched)

    assert (during.status, len(during.attempts)) == (StepStatus.RETRYING, 1)
    assert step_of(detail, "a").status is StepStatus.SUCCEEDED


@pytest.mark.parametrize(
    "behavior",
    [
        pytest.param(Fail("bad input", retryable=False), id="non-retryable"),
        pytest.param(Raise(StepConfigError("malformed output file")), id="config-error"),
    ],
)
async def test_a_non_retryable_failure_ends_the_step(
    engine: Engine, fake: FakeStep, behavior: Fail | Raise
) -> None:
    fake.script("a", behavior)

    detail = await engine.execute(make_workflow(make_step("a", retry=RetryPolicy(attempts=5))))

    assert len(fake.calls_for("a")) == 1
    assert step_of(detail, "a").status is StepStatus.FAILED


async def test_an_unexpected_error_fails_without_retrying_and_logs_the_traceback(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("a", Raise(RuntimeError(f"boom with {API_TOKEN}")))

    detail = await engine.execute(make_workflow(make_step("a", retry=RetryPolicy(attempts=3))))

    a = step_of(detail, "a")
    assert len(a.attempts) == 1
    assert a.error == "internal error: RuntimeError: boom with ***"
    assert a.attempts[0].error == "internal error: RuntimeError: boom with ***"
    lines = await engine.texts(detail.run.id, step_id="a", stream=LogStream.SYSTEM)
    assert lines[0] == "Traceback (most recent call last):"
    assert "RuntimeError: boom with ***" in lines
    assert not any(API_TOKEN in line for line in lines)


async def test_an_error_without_a_message_names_its_type(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Raise(KeyError()))

    detail = await engine.execute(make_workflow(make_step("a")))

    assert step_of(detail, "a").error == "internal error: KeyError"


async def test_a_timeout_error_from_the_step_is_a_retryable_failure(
    engine: Engine, fake: FakeStep
) -> None:
    fake.script("a", Raise(TimeoutError("read timed out")), Raise(TimeoutError()))

    detail = await engine.execute(make_workflow(make_step("a", retry=RetryPolicy(attempts=2))))

    a = step_of(detail, "a")
    assert [attempt.status for attempt in a.attempts] == [A.FAILED, A.FAILED]
    assert [attempt.error for attempt in a.attempts] == [
        "TimeoutError: read timed out",
        "TimeoutError",
    ]
    assert a.status is StepStatus.FAILED


async def test_the_step_timeout_bounds_each_attempt(engine: Engine, fake: FakeStep) -> None:
    fake.script("build", Hang(), Hang(), Sleep(5))
    policy = RetryPolicy(attempts=3, delay=1.0, backoff=1.0)

    detail = await engine.execute(make_workflow(make_step("build", timeout=10, retry=policy)))

    assert gaps(call_times(fake, "build")) == [11, 11]
    build = step_of(detail, "build")
    assert [attempt.status for attempt in build.attempts] == [A.TIMED_OUT, A.TIMED_OUT, A.SUCCEEDED]
    assert build.attempts[0].error == "timed out after 10s"
    assert [attempt.duration_ms for attempt in build.attempts] == [10_000, 10_000, 5_000]
    assert build.status is StepStatus.SUCCEEDED


class StubbornStep(FakeStep):
    """A fake step type whose attempts, like a file step's thread, can't be interrupted."""

    interruptible: ClassVar[bool] = False


async def test_a_timeout_of_a_step_type_that_cant_be_interrupted_is_final(
    make_engine: Callable[..., Engine],
) -> None:
    engine = make_engine(StubbornStep("stubborn", default=Hang()))
    policy = RetryPolicy(attempts=3, delay=1.0)

    detail = await engine.execute(
        make_workflow(make_step("copy", uses="stubborn", timeout=10, retry=policy))
    )

    copy = step_of(detail, "copy")
    assert (copy.status, copy.error) == (StepStatus.TIMED_OUT, "timed out after 10s")
    assert [attempt.status for attempt in copy.attempts] == [A.TIMED_OUT]


async def test_a_final_timeout_makes_the_step_timed_out(engine: Engine, fake: FakeStep) -> None:
    fake.script("build", Hang())

    detail = await engine.execute(make_workflow(make_step("build", timeout=600)))

    build = step_of(detail, "build")
    assert (build.status, build.error) == (StepStatus.TIMED_OUT, "timed out after 10m")
    assert await engine.texts(detail.run.id, step_id="build", attempt=1) == [
        "attempt 1 timed out after 10m"
    ]


async def test_the_step_timeout_excludes_backoff_waits(engine: Engine, fake: FakeStep) -> None:
    fake.script("a", Sleep(8, then=Fail()), Sleep(8))
    policy = RetryPolicy(attempts=2, delay=100.0)

    detail = await engine.execute(make_workflow(make_step("a", timeout=10, retry=policy)))

    assert step_of(detail, "a").status is StepStatus.SUCCEEDED


async def test_a_success_records_its_exit_code_and_attempt_times(
    engine: Engine, fake: FakeStep
) -> None:
    loop = current_loop()
    fake.script("a", Sleep(3, then=Succeed({"x": 1})))
    start = loop.time()

    detail = await engine.execute(make_workflow(make_step("a")))

    attempt = step_of(detail, "a").attempts[0]
    assert (attempt.attempt, attempt.status, attempt.exit_code) == (1, A.SUCCEEDED, None)
    assert attempt.duration_ms == 3_000
    assert step_of(detail, "a").duration_ms == 3_000
    assert loop.time() - start == 3
