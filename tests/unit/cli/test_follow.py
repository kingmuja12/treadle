"""Live progress (`follow_run`) and failure summaries (`render_summary`), in virtual time."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.support.builders import (
    FIXED_TIME,
    make_attempt,
    make_detail,
    make_run,
    make_step_record,
)
from tests.support.fakes import MemoryRunReader

from treadle.cli.follow import follow_run, render_summary
from treadle.cli.output import Output
from treadle.errors import NotFoundError
from treadle.models import (
    AttemptRecord,
    AttemptStatus,
    LogLine,
    LogStream,
    RunDetail,
    RunReason,
    RunRecord,
    RunStatus,
    SkipReason,
    StepStatus,
    TriggerKind,
)

T0 = FIXED_TIME
LONDON = ZoneInfo("Europe/London")
Stage = tuple[RunDetail, Sequence[LogLine]]


class ScriptedReader(MemoryRunReader):
    """A reader whose run moves to its next stage, and that stage's log lines, on each poll."""

    def __init__(self, stages: Sequence[Stage]) -> None:
        super().__init__()
        self._stages = list(stages)
        self._next = 0

    async def get_run(self, run_id: int) -> RunDetail | None:
        detail, lines = self._stages[min(self._next, len(self._stages) - 1)]
        self._next += 1
        self.details = {detail.run.id: detail}
        self.lines = {detail.run.id: list(lines)}
        return await super().get_run(run_id)


def _output(encoding: str = "utf-8") -> tuple[Output, io.TextIOWrapper]:
    out = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    return Output(out, io.TextIOWrapper(io.BytesIO(), encoding=encoding), color=False), out


def _printed(stream: io.TextIOWrapper) -> list[str]:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode(stream.encoding).splitlines()


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _log(
    seq: int, text: str, *, step: str | None = "build", attempt: int = 1, system: bool = False
) -> LogLine:
    stream = LogStream.SYSTEM if system or step is None else LogStream.STDOUT
    return LogLine(seq, T0, step, attempt if step else None, stream, text)


def _run(status: RunStatus, *, finished: float | None = None, **fields: Any) -> RunRecord:
    end = None if finished is None else _at(finished)
    return make_run(
        7, workflow_id="deploy", status=status, started_at=T0, finished_at=end, **fields
    )


def _failed_attempt(number: int, start: float) -> AttemptRecord:
    return make_attempt(
        number,
        status=AttemptStatus.FAILED,
        started_at=_at(start),
        finished_at=_at(start + 0.5),
        exit_code=1,
        error="exit code 1",
    )


def _retrying_stages() -> list[Stage]:
    """`build` fails, waits 1s, and succeeds on attempt 2 of 2; then `ship` is skipped.

    The retry line commits only after the first poll, as it can in a real run.
    """
    first = _failed_attempt(1, 0)
    retrying = make_step_record("build", status=StepStatus.RETRYING, attempts=[first])
    second = make_attempt(2, started_at=_at(1.5), finished_at=_at(2.0))
    done = make_step_record(
        "build",
        status=StepStatus.SUCCEEDED,
        started_at=T0,
        finished_at=_at(2.0),
        attempts=[first, second],
    )
    pending = make_step_record("ship", status=StepStatus.PENDING, position=1)
    ship = make_step_record(
        "ship", status=StepStatus.SKIPPED, position=1, skip_reason=SkipReason.CONDITION
    )
    failed_line = _log(1, "attempt 1 failed: exit code 1", system=True)
    retry_line = _log(2, "retrying in 1s (attempt 2 of 2)", system=True)
    return [
        (make_detail(_run(RunStatus.RUNNING), [retrying, pending]), [failed_line]),
        (make_detail(_run(RunStatus.RUNNING), [retrying, pending]), [failed_line, retry_line]),
        (
            make_detail(_run(RunStatus.SUCCEEDED, finished=3.0), [done, ship]),
            [failed_line, retry_line],
        ),
    ]


async def test_progress_reports_each_retry_once_and_each_step_when_it_ends() -> None:
    output, out = _output()

    run = await follow_run(
        ScriptedReader(_retrying_stages()), 7, output, verbose=False, json_lines=False
    )

    assert run.status is RunStatus.SUCCEEDED
    assert _printed(out) == [
        "Run #7  deploy  (manual)",
        "  ✗ build   attempt 1/2: exit code 1; retrying in 1s",
        "  ✓ build   2s",
        "  ⊘ ship    skipped (condition)",
        "Run #7 succeeded in 3s.",
    ]


async def test_a_retry_without_its_log_line_still_prints_once_the_next_attempt_runs() -> None:
    output, out = _output()
    first = _failed_attempt(1, 0)
    second = make_attempt(2, status=AttemptStatus.RUNNING, started_at=_at(1), finished_at=None)
    running = make_step_record("build", status=StepStatus.RUNNING, attempts=[first, second])
    failed = make_step_record(
        "build",
        status=StepStatus.FAILED,
        started_at=T0,
        finished_at=_at(2.0),
        error="exit code 1",
        attempts=[first, _failed_attempt(2, 1)],
    )
    error = "step 'build' failed after 2 attempts: exit code 1"
    stages: list[Stage] = [
        (make_detail(_run(RunStatus.RUNNING), [running]), []),
        (
            make_detail(_run(RunStatus.FAILED, finished=2.0, error=error), [failed]),
            [_log(1, "boom", attempt=2)],
        ),
    ]

    await follow_run(ScriptedReader(stages), 7, output, verbose=False, json_lines=False)

    assert _printed(out) == [
        "Run #7  deploy  (manual)",
        "  ✗ build   attempt 1: exit code 1; retrying",
        "  ✗ build   attempt 2: exit code 1 (2s)",
        "--- build, attempt 2: last 20 lines ---",
        "boom",
        "Run #7 failed in 2s. Details: treadle show 7",
    ]


async def test_verbose_streams_log_lines_with_a_gutter() -> None:
    output, out = _output()
    done = make_step_record("build", status=StepStatus.SUCCEEDED, attempts=[make_attempt(1)])
    lines = [_log(1, "compiling"), _log(2, "cancelling the run (user)", step=None)]
    stage = (make_detail(_run(RunStatus.SUCCEEDED, finished=1.0), [done]), lines)

    await follow_run(ScriptedReader([stage]), 7, output, verbose=True, json_lines=False)

    assert _printed(out)[1:3] == [
        "  │ build#1 stdout: compiling",
        "  │ run system: cancelling the run (user)",
    ]


async def test_without_utf8_the_progress_uses_ascii_symbols() -> None:
    output, out = _output("ascii")
    done = make_step_record(
        "build",
        status=StepStatus.SUCCEEDED,
        started_at=T0,
        finished_at=_at(1.5),
        attempts=[make_attempt(1)],
    )
    stage = (make_detail(_run(RunStatus.SUCCEEDED, finished=1.0), [done]), [_log(1, "hi")])

    await follow_run(ScriptedReader([stage]), 7, output, verbose=True, json_lines=False)

    assert _printed(out)[1:3] == ["  | build#1 stdout: hi", "  [ok] build   1.5s"]


async def test_json_lines_emit_step_events_only_on_change() -> None:
    output, out = _output()

    await follow_run(ScriptedReader(_retrying_stages()), 7, output, verbose=True, json_lines=True)

    events = [json.loads(line) for line in _printed(out)]
    assert [event["event"] for event in events] == [
        "run_created",
        "log",
        "step",
        "log",
        "step",
        "step",
        "run_finished",
    ]
    steps = [
        (event["step"]["id"], event["step"]["status"])
        for event in events
        if event["event"] == "step"
    ]
    assert steps == [("build", "retrying"), ("build", "succeeded"), ("ship", "skipped")]
    assert events[-1]["exit_code"] == 0
    assert events[-1]["run"]["status"] == "succeeded"


async def test_a_run_that_was_skipped_at_creation_ends_at_once() -> None:
    output, out = _output()
    skipped = make_run(
        3, workflow_id="deploy", status=RunStatus.SKIPPED, reason=RunReason.ALREADY_ACTIVE
    )

    run = await follow_run(
        ScriptedReader([(make_detail(skipped), [])]), 3, output, verbose=False, json_lines=False
    )

    assert run.status is RunStatus.SKIPPED
    assert _printed(out) == ["Run #3  deploy  (manual)", "Run #3 skipped (already_active)."]


async def test_following_a_missing_run_raises() -> None:
    output, _ = _output()

    with pytest.raises(NotFoundError, match="run 9 doesn't exist"):
        await follow_run(MemoryRunReader(), 9, output, verbose=False, json_lines=False)


async def test_the_summary_of_a_scheduled_run_uses_the_project_zone() -> None:
    output, out = _output()
    run = make_run(
        142,
        workflow_id="nightly-backup",
        status=RunStatus.FAILED,
        error="step 'archive' failed after 3 attempts: exit code 23",
        trigger_kind=TriggerKind.SCHEDULE,
        trigger={"kind": "schedule", "catch_up": True},
        scheduled_for=T0,
        started_at=T0,
        finished_at=_at(68),
    )
    archive = make_step_record(
        "archive",
        status=StepStatus.FAILED,
        error="exit code 23",
        started_at=T0,
        finished_at=_at(6.6),
        attempts=[_failed_attempt(1, 0), _failed_attempt(2, 1), _failed_attempt(3, 2)],
    )
    record = make_step_record(
        "record", status=StepStatus.SKIPPED, position=1, skip_reason=SkipReason.UPSTREAM_FAILED
    )
    reader = MemoryRunReader()
    reader.add(
        make_detail(run, [archive, record]), [_log(1, "rsync: refused", step="archive", attempt=3)]
    )

    await render_summary(reader, make_detail(run, [archive, record]), output, timezone=LONDON)

    assert _printed(out) == [
        "Run #142 · nightly-backup · failed · schedule 2026-11-09 12:00 GMT (catch-up)",
        "Started 2026-11-09 12:00:00 GMT · took 1m08s · attempt 1",
        "Error: step 'archive' failed after 3 attempts: exit code 23",
        "",
        "  STEP     STATUS     ATTEMPTS  DURATION  NOTE",
        "  archive  ✗ failed   3         6.6s      exit code 23",
        "  record   ⊘ skipped  0         -         upstream_failed",
        "",
        "--- archive, attempt 3: last 20 lines ---",
        "rsync: refused",
        "",
        "Hint: treadle logs 142 --step archive",
    ]


@pytest.mark.parametrize(
    ("kind", "trigger", "expected"),
    [
        (TriggerKind.WEBHOOK, {"kind": "webhook", "hook": "blog-push"}, "webhook /hooks/blog-push"),
        (TriggerKind.WATCH, {"kind": "watch", "total": 2}, "watch (2 changes)"),
        (TriggerKind.WATCH, {"kind": "watch", "total": 1}, "watch (1 change)"),
        (TriggerKind.MANUAL, {"kind": "manual", "origin": "dashboard"}, "manual (dashboard)"),
        (TriggerKind.SCHEDULE, {"kind": "schedule"}, "schedule"),
    ],
)
async def test_the_summary_says_how_the_run_started(
    kind: TriggerKind, trigger: dict[str, Any], expected: str
) -> None:
    output, out = _output()
    run = make_run(1, status=RunStatus.SUCCEEDED, trigger_kind=kind, trigger=trigger)

    await render_summary(MemoryRunReader(), make_detail(run), output)

    assert _printed(out)[0] == f"Run #1 · wf · succeeded · {expected}"


async def test_the_summary_of_an_interrupted_rerun_hints_at_a_new_rerun() -> None:
    output, out = _output()
    run = make_run(
        5,
        status=RunStatus.INTERRUPTED,
        reason=RunReason.EXECUTOR_STOPPED,
        rerun_of=4,
        replaced_by=6,
        attempt=2,
        started_at=T0,
        finished_at=_at(1),
    )

    await render_summary(MemoryRunReader(), make_detail(run), output)

    assert _printed(out) == [
        "Run #5 · wf · interrupted (executor_stopped) · manual (cli) · re-run of #4",
        "Started 2026-11-09 12:00:00 UTC · took 1s · attempt 2",
        "Replaced by run #6",
        "",
        "Hint: treadle run --rerun 5",
    ]


async def test_the_summary_of_an_active_run_hints_at_following_it() -> None:
    output, out = _output("ascii")
    started = make_run(5, status=RunStatus.RUNNING, started_at=T0)
    queued = make_run(6, status=RunStatus.QUEUED)
    tolerated = make_step_record(
        "lint",
        status=StepStatus.FAILED,
        tolerated=True,
        error="exit code 1",
        started_at=T0,
        finished_at=_at(0.5),
        attempts=[_failed_attempt(1, 0)],
    )
    unrendered = make_step_record(
        "deploy", status=StepStatus.FAILED, position=1, error="in steps.deploy.with.url: bad"
    )

    await render_summary(MemoryRunReader(), make_detail(started, [tolerated, unrendered]), output)
    await render_summary(MemoryRunReader(), make_detail(queued), output)

    printed = _printed(out)
    assert printed[:2] == [
        "Run #5 - wf - running - manual (cli)",
        "Started 2026-11-09 12:00:00 UTC - running - attempt 1",
    ]
    assert printed[4].split() == [
        "lint",
        "[fail]",
        "failed",
        "1",
        "500ms",
        "tolerated:",
        "exit",
        "code",
        "1",
    ]
    assert printed[5].split()[:4] == ["deploy", "[fail]", "failed", "0"]
    assert printed[6:8] == ["", "Hint: treadle logs 5 -f"]
    assert printed[8:] == [
        "Run #6 - wf - queued - manual (cli)",
        "Created 2026-11-09 12:00:00 UTC - attempt 1",
        "",
        "Hint: treadle logs 6 -f",
    ]


async def test_a_step_that_fails_before_any_attempt_reports_its_error_once() -> None:
    output, out = _output()
    lint = make_step_record(
        "lint", status=StepStatus.FAILED, error="in steps.lint.with.path: bad", attempts=[]
    )
    building = make_step_record("build", status=StepStatus.RUNNING, position=1)
    built = make_step_record(
        "build",
        status=StepStatus.SUCCEEDED,
        position=1,
        started_at=T0,
        finished_at=_at(1),
        attempts=[make_attempt(1)],
    )
    stages: list[Stage] = [
        (make_detail(_run(RunStatus.RUNNING), [lint, building]), []),
        (make_detail(_run(RunStatus.FAILED, finished=1.0), [lint, built]), []),
    ]

    await follow_run(ScriptedReader(stages), 7, output, verbose=True, json_lines=False)

    assert _printed(out) == [
        "Run #7  deploy  (manual)",
        "  ✗ lint    in steps.lint.with.path: bad",
        "  ✓ build   1s",
        "Run #7 failed in 1s. Details: treadle show 7",
    ]


async def test_a_failed_attempt_without_log_lines_gets_no_excerpt() -> None:
    output, out = _output()
    run = make_run(
        3, status=RunStatus.FAILED, error="step 'x' failed", started_at=T0, finished_at=T0
    )
    step = make_step_record(
        "x", status=StepStatus.FAILED, error="exit code 1", attempts=[_failed_attempt(1, 0)]
    )

    await render_summary(MemoryRunReader(), make_detail(run, [step]), output)

    printed = _printed(out)
    assert not any(line.startswith("---") for line in printed)
    assert printed[-1] == "Hint: treadle logs 3 --step x"
