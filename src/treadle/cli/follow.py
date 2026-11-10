"""Live run progress and failure summaries."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import datetime, tzinfo
from typing import Final

from treadle.cli.output import Output
from treadle.clock import UTC
from treadle.errors import NotFoundError
from treadle.models import (
    AttemptRecord,
    AttemptStatus,
    LogLine,
    LogStream,
    RunDetail,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
    TriggerKind,
)
from treadle.protocols import RunReader
from treadle.serialize import log_json, run_json, step_json
from treadle.types import ExitCode
from treadle.units import format_duration

# The exit code of `treadle run` for each terminal run status.
_EXIT_CODES: Final[Mapping[RunStatus, ExitCode]] = {
    RunStatus.SUCCEEDED: ExitCode.OK,
    RunStatus.FAILED: ExitCode.FAILURE,
    RunStatus.CANCELLED: ExitCode.CANCELLED,
    RunStatus.INTERRUPTED: ExitCode.CANCELLED,
    RunStatus.TIMED_OUT: ExitCode.TIMED_OUT,
    RunStatus.SKIPPED: ExitCode.SKIPPED,
}
# The system line that the executor writes when a step waits before its next attempt, such
# as "retrying in 30s (attempt 2 of 3)". It's the only record of the backoff delay.
_RETRY_LINE: Final = re.compile(r"retrying in (?P<delay>\S+) \(attempt \d+ of (?P<total>\d+)\)")
_RETRY_SEARCH: Final = 20  # system lines at the end of a failed attempt to look through
_FAILED_ATTEMPTS: Final = frozenset({AttemptStatus.FAILED, AttemptStatus.TIMED_OUT})
_FAILED_STEPS: Final = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})
_DETAILED: Final = frozenset(
    {RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCELLED, RunStatus.INTERRUPTED}
)
_PAGE: Final = 1000


async def follow_run(
    reader: RunReader,
    run_id: int,
    out: Output,
    *,
    verbose: bool,
    json_lines: bool,
    poll: float = 0.3,
) -> RunRecord:
    """Print a run's progress until it's terminal, and return the terminal run.

    With `verbose`, log lines stream too; with `json_lines`, events print as JSON Lines:
    `run_created`, then `step` on each step status or attempt change, `log` with `verbose`,
    and `run_finished` with the exit code that `treadle run` returns for the run's status.
    Raises NotFoundError when the run doesn't exist.
    """
    detail = await _detail(reader, run_id)
    progress = _Progress(reader, out, detail, verbose=verbose, json_lines=json_lines)
    while True:
        await progress.update(detail)
        if detail.run.status.terminal:
            await progress.finish(detail)
            return detail.run
        await asyncio.sleep(poll)
        detail = await _detail(reader, run_id)


async def render_summary(
    reader: RunReader,
    detail: RunDetail,
    out: Output,
    *,
    tail: int = 20,
    timezone: tzinfo = UTC,
) -> None:
    """Print a finished run's summary, leading with the failure and its last log lines.

    Times show in `timezone`. Each failed step gets the last `tail` lines of its last
    attempt, and a hint names the next command to try.
    """
    run = detail.run
    dot = " · " if out.unicode else " - "
    heading = [f"Run #{run.id}", run.workflow_id, _status_text(run), _trigger_text(run, timezone)]
    if run.rerun_of is not None:
        heading.append(f"re-run of #{run.rerun_of}")
    out.line(dot.join(heading))
    out.line(dot.join(_timing(run, timezone)))
    if run.error:
        out.line(f"Error: {run.error}")
    if run.replaced_by is not None:
        out.line(f"Replaced by run #{run.replaced_by}")
    if detail.steps:
        out.line()
        rows = [_step_row(out, step) for step in detail.steps]
        out.table(["STEP", "STATUS", "ATTEMPTS", "DURATION", "NOTE"], rows, indent=2)
    failed = [step for step in detail.steps if _failed(step)]
    for step in failed:
        await _excerpt(reader, out, run.id, step, tail, spaced=True)
    hint = _hint(run, failed)
    if hint is not None:
        out.line()
        out.line(f"Hint: {hint}")


def _exit_code(status: RunStatus) -> ExitCode:
    """Return the exit code for a terminal run status. Raises ValueError for others."""
    try:
        return _EXIT_CODES[status]
    except KeyError:
        raise ValueError(f"run status {status.value!r} isn't terminal") from None


class _Progress:
    """What `follow_run` has printed so far, so each poll prints only what's new."""

    def __init__(
        self, reader: RunReader, out: Output, detail: RunDetail, *, verbose: bool, json_lines: bool
    ) -> None:
        self._reader = reader
        self._out = out
        self._run_id = detail.run.id
        self._verbose = verbose
        self._json = json_lines
        self._after_seq = 0
        self._seen: dict[str, tuple[StepStatus, int, AttemptStatus | None]] = {}
        self._retried: dict[str, int] = {}  # attempts already reported as retried, per step
        self._totals: dict[str, int] = {}  # attempts allowed, from the executor's retry lines
        self._finished: set[str] = set()  # steps whose final line is printed
        if json_lines:
            out.json_line({"event": "run_created", "run": run_json(detail.run)})
        else:
            out.line(f"Run #{detail.run.id}  {detail.run.workflow_id}  ({detail.run.trigger_kind})")

    async def update(self, detail: RunDetail) -> None:
        """Print the log lines and step changes that are new since the last call."""
        if self._verbose:
            await self._print_logs()
        width = max((len(step.step_id) for step in detail.steps), default=0)
        for step in detail.steps:
            if self._json:
                self._step_event(step)
            elif step.step_id not in self._finished:
                await self._step_lines(step, width)

    async def finish(self, detail: RunDetail) -> None:
        """Print the end of the progress: the failure excerpt and a closing line."""
        run = detail.run
        if self._json:
            exit_code = int(_exit_code(run.status))
            self._out.json_line(
                {"event": "run_finished", "run": run_json(run), "exit_code": exit_code}
            )
            return
        failed = next((step for step in detail.steps if _failed(step)), None)
        if failed is not None:
            await _excerpt(self._reader, self._out, run.id, failed, 20, spaced=False)
        self._out.line(_closing_line(run))

    async def _print_logs(self) -> None:
        while True:
            lines = await self._reader.logs(self._run_id, after_seq=self._after_seq, limit=_PAGE)
            for line in lines:
                if self._json:
                    self._out.json_line({"event": "log", "line": log_json(line)})
                else:
                    self._out.line(self._log_text(line))
            if lines:
                self._after_seq = lines[-1].seq
            if len(lines) < _PAGE:
                return

    def _log_text(self, line: LogLine) -> str:
        """Format a streamed line, `  │ archive#1 stdout: text`."""
        gutter = "│" if self._out.unicode else "|"
        where = "run"
        if line.step_id is not None:
            where = line.step_id if line.attempt is None else f"{line.step_id}#{line.attempt}"
        return f"  {gutter} {where} {line.stream}: {line.text}"

    def _step_event(self, step: StepRecord) -> None:
        """Emit a `step` event when the step's status or attempts changed."""
        last = step.attempts[-1].status if step.attempts else None
        state = (step.status, len(step.attempts), last)
        previous = self._seen.get(step.step_id, (StepStatus.PENDING, 0, None))
        self._seen[step.step_id] = state
        if state != previous:
            self._out.json_line({"event": "step", "step": step_json(step)})

    async def _step_lines(self, step: StepRecord, width: int) -> None:
        """Print each retried attempt once, then the step's final line once it's terminal."""
        await self._report_retries(step, width)
        if step.status.terminal:
            self._finished.add(step.step_id)
            self._out.line(self._line(step.status.value, step.step_id, width, self._summary(step)))

    async def _report_retries(self, step: StepRecord, width: int) -> None:
        for attempt in step.attempts[self._retried.get(step.step_id, 0) :]:
            last = attempt is step.attempts[-1]
            if attempt.status not in _FAILED_ATTEMPTS:
                return
            if last and step.status is not StepStatus.RETRYING:
                return  # the final attempt; the step's final line reports it
            retry = await self._retry(step.step_id, attempt.attempt)
            if retry is None and last:
                return  # the retry line isn't committed yet; the next poll reports it
            detail = f"attempt {attempt.attempt}: {_attempt_error(attempt)}; retrying"
            if retry is not None:
                delay, total = retry
                self._totals[step.step_id] = total
                detail = (
                    f"attempt {attempt.attempt}/{total}: {_attempt_error(attempt)}; "
                    f"retrying in {delay}"
                )
            self._out.line(self._line(StepStatus.FAILED.value, step.step_id, width, detail))
            self._retried[step.step_id] = attempt.attempt

    async def _retry(self, step_id: str, attempt: int) -> tuple[str, int] | None:
        """Return the backoff delay and the attempts allowed from an attempt's retry line."""
        lines = await self._reader.logs(
            self._run_id, step_id=step_id, attempt=attempt, tail=True, limit=_RETRY_SEARCH
        )
        for line in reversed(lines):
            match = _RETRY_LINE.fullmatch(line.text) if line.stream is LogStream.SYSTEM else None
            if match is not None:
                return match["delay"], int(match["total"])
        return None

    def _summary(self, step: StepRecord) -> str:
        """The text after a terminal step's ID: its duration, error, or skip reason."""
        took = _duration(step.duration_ms)
        if step.status is StepStatus.SKIPPED:
            reason = step.skip_reason.value if step.skip_reason else "condition"
            return f"skipped ({reason})"
        if step.status in _FAILED_STEPS:
            if not step.attempts:
                return step.error or "failed"
            last = step.attempts[-1]
            total = self._totals.get(step.step_id)
            prefix = f"attempt {last.attempt}/{total}: " if total else ""
            if not prefix and last.attempt > 1:
                prefix = f"attempt {last.attempt}: "
            note = ", tolerated" if step.tolerated else ""
            return f"{prefix}{step.error or _attempt_error(last)} ({took}{note})"
        if step.status is StepStatus.SUCCEEDED:
            return took
        return step.status.value if took == "-" else f"{step.status.value} ({took})"

    def _line(self, status: str, step_id: str, width: int, detail: str) -> str:
        symbol = self._out.status(status, symbol_only=True)
        return f"  {symbol} {step_id:<{width}}   {detail}"


async def _detail(reader: RunReader, run_id: int) -> RunDetail:
    detail = await reader.get_run(run_id)
    if detail is None:
        raise NotFoundError(f"run {run_id} doesn't exist")
    return detail


async def _excerpt(
    reader: RunReader, out: Output, run_id: int, step: StepRecord, tail: int, *, spaced: bool
) -> None:
    """Print the last `tail` log lines of a step's last attempt, under a heading.

    Nothing prints when the attempt logged nothing; otherwise `spaced` adds a blank line first.
    """
    attempt = step.attempts[-1].attempt
    lines = await reader.logs(run_id, step_id=step.step_id, attempt=attempt, tail=True, limit=tail)
    if not lines:
        return
    if spaced:
        out.line()
    out.line(f"--- {step.step_id}, attempt {attempt}: last {tail} lines ---")
    for line in lines:
        out.line(line.text)


def _failed(step: StepRecord) -> bool:
    """True for a step that failed the run: failed or timed out, not tolerated, with attempts."""
    return step.status in _FAILED_STEPS and not step.tolerated and bool(step.attempts)


def _attempt_error(attempt: AttemptRecord) -> str:
    return attempt.error or attempt.status.value


def _closing_line(run: RunRecord) -> str:
    """`Run #142 failed in 1m08s. Details: treadle show 142`."""
    took = f" in {_duration(run.duration_ms)}" if run.duration_ms is not None else ""
    text = f"Run #{run.id} {_status_text(run)}{took}."
    if run.status in _DETAILED:
        text += f" Details: treadle show {run.id}"
    return text


def _status_text(run: RunRecord) -> str:
    """The run status with its reason, such as "cancelled (user)"."""
    return run.status.value if run.reason is None else f"{run.status} ({run.reason})"


def _trigger_text(run: RunRecord, timezone: tzinfo) -> str:
    """How the run started, such as "schedule 2026-11-09 02:30 GMT" or "manual (cli)"."""
    trigger, kind = run.trigger, run.trigger_kind
    if kind is TriggerKind.SCHEDULE and run.scheduled_for is not None:
        late = " (catch-up)" if trigger.get("catch_up") is True else ""
        return f"schedule {_local(run.scheduled_for, timezone, seconds=False)}{late}"
    if kind is TriggerKind.WEBHOOK and isinstance(hook := trigger.get("hook"), str):
        return f"webhook /hooks/{hook}"
    if kind is TriggerKind.MANUAL and isinstance(origin := trigger.get("origin"), str):
        return f"manual ({origin})"
    if kind is TriggerKind.WATCH and isinstance(total := trigger.get("total"), int):
        return f"watch ({total} change{'' if total == 1 else 's'})"
    return kind.value


def _timing(run: RunRecord, timezone: tzinfo) -> list[str]:
    """The second summary line's parts: when the run started, how long it took, its attempt."""
    attempt = f"attempt {run.attempt}"
    if run.started_at is None:
        return [f"Created {_local(run.created_at, timezone)}", attempt]
    took = "running" if run.duration_ms is None else f"took {_duration(run.duration_ms)}"
    return [f"Started {_local(run.started_at, timezone)}", took, attempt]


def _step_row(out: Output, step: StepRecord) -> list[str]:
    label = "ok" if step.status is StepStatus.SUCCEEDED else step.status.value
    return [
        step.step_id,
        out.status(label),
        str(len(step.attempts)),
        _duration(step.duration_ms),
        _note(step),
    ]


def _note(step: StepRecord) -> str:
    """The NOTE column: the error of a failed step, or why a step was skipped."""
    if step.status is StepStatus.SKIPPED and step.skip_reason is not None:
        return step.skip_reason.value
    error = step.error or ""
    if step.tolerated:
        return f"tolerated: {error}" if error else "tolerated"
    return error


def _hint(run: RunRecord, failed: list[StepRecord]) -> str | None:
    """The next command to try, for a failed, unfinished, or interrupted run."""
    if failed:
        return f"treadle logs {run.id} --step {failed[0].step_id}"
    if not run.status.terminal:
        return f"treadle logs {run.id} -f"
    if run.status is RunStatus.INTERRUPTED:
        return f"treadle run --rerun {run.id}"
    return None


def _duration(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else format_duration(milliseconds / 1000)


def _local(moment: datetime, timezone: tzinfo, *, seconds: bool = True) -> str:
    """Format a time in `timezone`, such as "2026-11-09 02:30:00 GMT"."""
    pattern = "%Y-%m-%d %H:%M:%S %Z" if seconds else "%Y-%m-%d %H:%M %Z"
    return moment.astimezone(timezone).strftime(pattern)
