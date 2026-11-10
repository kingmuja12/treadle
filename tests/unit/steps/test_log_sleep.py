from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from tests.support.builders import make_runtime
from tests.support.fakes import RecordingLog
from tests.support.vtime import current_loop

from treadle.models import LogStream
from treadle.schema import Obj, validate
from treadle.steps.base import StepResult
from treadle.steps.log import LogStep
from treadle.steps.sleep import SleepStep
from treadle.types import JSONValue


def params(schema: Obj, **raw: object) -> dict[str, JSONValue]:
    value, issues = validate(schema, raw)
    assert issues == []
    return cast(dict[str, JSONValue], value)


def codes(schema: Obj, **raw: object) -> list[tuple[str, str]]:
    _, issues = validate(schema, raw)
    return [(issue.code, issue.message) for issue in issues]


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ({"message": "hello"}, "[info] hello"),
        ({"message": "disk at 91%", "level": "warning"}, "[warning] disk at 91%"),
        ({"message": "", "level": "error"}, "[error] "),
    ],
)
async def test_log_writes_one_system_line(
    tmp_path: Path, raw: dict[str, object], line: str
) -> None:
    log = RecordingLog()

    result = await LogStep().run(params(LogStep().schema, **raw), make_runtime(tmp_path, log=log))

    assert result == StepResult()
    assert log.lines == [(LogStream.SYSTEM, line)]


def test_log_parameters_are_validated() -> None:
    schema = LogStep().schema

    assert codes(schema) == [("E102", "missing required key 'message'")]
    assert codes(schema, message="x", level="debug") == [
        ("E104", "must be one of: info, warning, error")
    ]
    assert codes(schema, message=3) == [("E103", "expected a string, got a number")]


async def test_sleep_waits_for_the_duration(tmp_path: Path) -> None:
    loop = current_loop()
    started = loop.time()

    result = await SleepStep().run(
        params(SleepStep().schema, duration="1m30s"), make_runtime(tmp_path)
    )

    assert result == StepResult()
    assert loop.time() - started == 90.0


async def test_a_zero_sleep_returns_at_once(tmp_path: Path) -> None:
    loop = current_loop()
    started = loop.time()

    await SleepStep().run(params(SleepStep().schema, duration=0), make_runtime(tmp_path))

    assert loop.time() == started


async def test_cancellation_interrupts_a_sleep(tmp_path: Path) -> None:
    loop = current_loop()
    started = loop.time()
    task = asyncio.create_task(
        SleepStep().run(params(SleepStep().schema, duration="2h"), make_runtime(tmp_path))
    )

    await asyncio.sleep(10)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert loop.time() - started == 10.0


def test_sleep_parameters_are_validated() -> None:
    schema = SleepStep().schema

    assert codes(schema) == [("E102", "missing required key 'duration'")]
    assert codes(schema, duration="25h") == [("E104", "must be between 0s and 1d")]
    assert codes(schema, duration=True) == [
        ("E103", "expected a duration such as 30s, got a boolean")
    ]


@pytest.mark.parametrize("step", [LogStep(), SleepStep()], ids=["log", "sleep"])
def test_log_and_sleep_have_no_outputs_and_no_checks(step: LogStep | SleepStep) -> None:
    assert step.output_names == frozenset()
    assert step.reserved_outputs == frozenset()
    assert step.taint_sinks == {}
    assert step.check({"anything": 1}, ("with",)) == []
