"""Test doubles for the Protocols in `treadle.protocols` and `treadle.steps.base`.

Each double records what it receives, so tests can assert on calls instead of internals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias

from tests.support.builders import make_entry, make_run
from treadle.errors import StepError
from treadle.models import (
    CancelOutcome,
    CancelReason,
    HookRequest,
    HookResponse,
    LogLine,
    LogStream,
    ManagerStats,
    RegistryChange,
    RunDetail,
    RunFilter,
    RunPage,
    RunRecord,
    RunRequest,
    RunStatus,
    Submission,
    SubmitOutcome,
    TriggerState,
    Workflow,
    WorkflowEntry,
)
from treadle.schema import AnyJSON, Field, Obj, Str
from treadle.steps.base import StepRegistry, StepResult, StepRuntime
from treadle.types import Issue, JSONValue, YamlPath


@dataclass(frozen=True)
class Succeed:
    """The attempt succeeds with `outputs`, after writing `logs` to stdout."""

    outputs: Mapping[str, JSONValue] = field(default_factory=dict)
    logs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fail:
    """The attempt raises StepError, after writing `logs` to stdout."""

    message: str = "fake failure"
    retryable: bool = True
    outputs: Mapping[str, JSONValue] | None = None
    exit_code: int | None = None
    logs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Raise:
    """The attempt raises `exc`, for example TimeoutError or an unexpected error."""

    exc: BaseException


@dataclass(frozen=True)
class Sleep:
    """The attempt sleeps `seconds` (virtual time in unit tests), then behaves as `then`."""

    seconds: float
    then: Behavior = Succeed()


@dataclass(frozen=True)
class Hang:
    """The attempt waits until it's cancelled."""


Behavior: TypeAlias = Succeed | Fail | Raise | Sleep | Hang
SUCCEED = Succeed()


@dataclass(frozen=True)
class StepCall:
    """One recorded `FakeStep.run` call. `time` is the loop time at the call."""

    step_id: str
    attempt: int
    params: dict[str, JSONValue]
    env: dict[str, str]
    time: float


FAKE_SCHEMA = Obj(
    "fake step",
    {"value": Field(AnyJSON()), "message": Field(Str()), "data": Field(AnyJSON())},
)


class FakeStep:
    """A StepType whose behavior is scripted per step ID and attempt.

    Unscripted steps behave as `default`. A script's last behavior repeats for later attempts.
    """

    def __init__(
        self,
        name: str = "fake",
        *,
        default: Behavior = SUCCEED,
        schema: Obj = FAKE_SCHEMA,
        output_names: frozenset[str] | None = None,
        reserved_outputs: frozenset[str] = frozenset(),
        taint_sinks: Mapping[str, str] | None = None,
        issues: Iterable[Issue] = (),
    ) -> None:
        self.name = name
        self.schema = schema
        self.output_names = output_names
        self.reserved_outputs = reserved_outputs
        self.taint_sinks = dict(taint_sinks or {})
        self.default = default
        self.issues = list(issues)
        self.calls: list[StepCall] = []
        self._scripts: dict[str, tuple[Behavior, ...]] = {}

    def script(self, step_id: str, *behaviors: Behavior) -> FakeStep:
        """Set the behaviors of attempts 1, 2, and so on of one step."""
        self._scripts[step_id] = behaviors
        return self

    def behavior(self, step_id: str, attempt: int) -> Behavior:
        """Return the behavior of one attempt."""
        script = self._scripts.get(step_id)
        if not script:
            return self.default
        return script[min(attempt, len(script)) - 1]

    def calls_for(self, step_id: str) -> list[StepCall]:
        """Return the recorded calls of one step, in call order."""
        return [call for call in self.calls if call.step_id == step_id]

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return the scripted static-check issues."""
        return list(self.issues)

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Record the call, then perform the scripted behavior."""
        now = asyncio.get_running_loop().time()
        self.calls.append(StepCall(rt.step_id, rt.attempt, dict(params), dict(rt.env), now))
        return await _perform(self.behavior(rt.step_id, rt.attempt), rt)


@dataclass(frozen=True)
class FakeSteps:
    """What the `fake_steps` fixture provides: a registry and the FakeStep it holds."""

    registry: StepRegistry
    step: FakeStep


async def _perform(behavior: Behavior, rt: StepRuntime) -> StepResult:
    if isinstance(behavior, Sleep):
        await asyncio.sleep(behavior.seconds)
        return await _perform(behavior.then, rt)
    if isinstance(behavior, Hang):
        await asyncio.get_running_loop().create_future()
        raise AssertionError("a hung step resumed without being cancelled")
    if isinstance(behavior, Raise):
        raise behavior.exc
    for line in behavior.logs:
        rt.log.write(LogStream.STDOUT, line)
    if isinstance(behavior, Fail):
        raise StepError(
            behavior.message,
            retryable=behavior.retryable,
            outputs=behavior.outputs,
            exit_code=behavior.exit_code,
        )
    return StepResult(outputs=dict(behavior.outputs))


class FakeSubmitter:
    """A RunSubmitter that records requests and answers with `outcome`.

    Set `error` to raise it instead, or `handler` to compute each submission.
    """

    def __init__(
        self,
        *,
        outcome: SubmitOutcome = SubmitOutcome.QUEUED,
        error: Exception | None = None,
        handler: Callable[[RunRequest], Submission] | None = None,
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.handler = handler
        self.requests: list[RunRequest] = []
        self._next_id = 1

    async def submit(self, request: RunRequest) -> Submission:
        """Record the request and return a submission."""
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return self.handler(request)
        return self._submission(request.workflow_id, rerun_of=request.rerun_of)

    def _submission(self, workflow_id: str, *, rerun_of: int | None) -> Submission:
        if self.outcome is SubmitOutcome.SLOT_TAKEN:
            return Submission(self.outcome, None)
        status = RunStatus.SKIPPED if self.outcome is SubmitOutcome.SKIPPED else RunStatus.QUEUED
        run = make_run(self._next_id, workflow_id=workflow_id, status=status, rerun_of=rerun_of)
        self._next_id += 1
        return Submission(self.outcome, run)


class FakeRunControl(FakeSubmitter):
    """A RunControl that records cancels and re-runs as well as submissions."""

    def __init__(
        self,
        *,
        stats: ManagerStats | None = None,
        cancel_outcome: CancelOutcome | None = None,
        outcome: SubmitOutcome = SubmitOutcome.QUEUED,
        error: Exception | None = None,
        handler: Callable[[RunRequest], Submission] | None = None,
    ) -> None:
        super().__init__(outcome=outcome, error=error, handler=handler)
        self.manager_stats = stats or ManagerStats(active=0, queued=0, draining=False)
        self.cancel_outcome = cancel_outcome
        self.cancels: list[tuple[int, CancelReason, bool]] = []
        self.reruns: list[int] = []

    async def cancel(
        self, run_id: int, reason: CancelReason = CancelReason.USER, *, force: bool = False
    ) -> CancelOutcome:
        """Record the cancel and return `cancel_outcome`, or an accepted outcome."""
        self.cancels.append((run_id, reason, force))
        if self.error is not None:
            raise self.error
        return self.cancel_outcome or CancelOutcome(run_id, RunStatus.RUNNING, True, force)

    async def rerun(self, run_id: int) -> Submission:
        """Record the re-run and return a submission whose run has `rerun_of` set."""
        self.reruns.append(run_id)
        if self.error is not None:
            raise self.error
        return self._submission("wf", rerun_of=run_id)

    def stats(self) -> ManagerStats:
        """Return `manager_stats`."""
        return self.manager_stats


class FakeWorkflowSource:
    """A WorkflowSource over entries that tests add."""

    def __init__(self, *items: Workflow | WorkflowEntry) -> None:
        self._entries: dict[str, WorkflowEntry] = {}
        self.versions: dict[str, Workflow] = {}
        for item in items:
            self.add(item)

    def add(self, item: Workflow | WorkflowEntry) -> WorkflowEntry:
        """Add or replace an entry. A workflow's version becomes available by digest."""
        entry = item if isinstance(item, WorkflowEntry) else make_entry(item)
        self._entries[entry.workflow_id] = entry
        if entry.current is not None:
            self.versions[entry.current.digest] = entry.current
        return entry

    def remove(self, workflow_id: str) -> None:
        """Remove an entry, keeping its stored versions."""
        self._entries.pop(workflow_id, None)

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        """Return the entry, or None."""
        return self._entries.get(workflow_id)

    def entries(self) -> list[WorkflowEntry]:
        """Return the entries sorted by workflow ID."""
        return [self._entries[key] for key in sorted(self._entries)]

    async def version(self, digest: str, *, workflow_id: str | None = None) -> Workflow | None:
        """Return a stored version, or None. With `workflow_id`, only that workflow's."""
        version = self.versions.get(digest)
        if version is None or workflow_id not in (None, version.id):
            return None
        return version


class MemoryRunReader:
    """A RunReader over run details and log lines held in memory."""

    def __init__(self, *details: RunDetail | RunRecord) -> None:
        self.details: dict[int, RunDetail] = {}
        self.lines: dict[int, list[LogLine]] = {}
        for detail in details:
            self.add(detail)

    def add(self, detail: RunDetail | RunRecord, lines: Sequence[LogLine] = ()) -> None:
        """Add or replace a run, and append its log lines."""
        if isinstance(detail, RunRecord):
            detail = RunDetail(detail, ())
        self.details[detail.run.id] = detail
        self.lines.setdefault(detail.run.id, []).extend(lines)

    async def get_run(self, run_id: int) -> RunDetail | None:
        """Return a run, or None."""
        return self.details.get(run_id)

    async def list_runs(self, flt: RunFilter) -> RunPage:
        """Return one page of matching runs, newest first."""
        runs = sorted((detail.run for detail in self.details.values()), key=lambda r: -r.id)
        matching = [run for run in runs if self._matches(run, flt)]
        page = matching[: flt.limit]
        more = len(matching) > flt.limit
        return RunPage(tuple(page), page[-1].id if more else None)

    async def logs(
        self,
        run_id: int,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        after_seq: int = 0,
        limit: int = 1000,
        tail: bool = False,
    ) -> list[LogLine]:
        """Return matching lines in `seq` order; with `tail`, the last `limit` of them."""
        lines = sorted(self.lines.get(run_id, []), key=lambda line: line.seq)
        matching = [
            line
            for line in lines
            if line.seq > after_seq
            and (step_id is None or line.step_id == step_id)
            and (attempt is None or line.attempt == attempt)
        ]
        return matching[-limit:] if tail else matching[:limit]

    async def last_runs(self) -> dict[str, RunRecord]:
        """Return the newest run of each workflow."""
        newest: dict[str, RunRecord] = {}
        for run in sorted((detail.run for detail in self.details.values()), key=lambda r: r.id):
            newest[run.workflow_id] = run
        return newest

    async def latest_run_id(self, workflow_id: str | None = None) -> int | None:
        """Return the newest run ID, optionally for one workflow."""
        ids = [
            run_id
            for run_id, detail in self.details.items()
            if workflow_id is None or detail.run.workflow_id == workflow_id
        ]
        return max(ids, default=None)

    def _matches(self, run: RunRecord, flt: RunFilter) -> bool:
        if flt.workflow_id is not None and run.workflow_id != flt.workflow_id:
            return False
        if flt.statuses and run.status not in flt.statuses:
            return False
        if flt.trigger_kind is not None and run.trigger_kind is not flt.trigger_kind:
            return False
        if flt.before_id is not None and run.id >= flt.before_id:
            return False
        if flt.text is None:
            return True
        needle = flt.text.lower()
        texts = [run.error or "", *(line.text for line in self.lines.get(run.id, []))]
        return any(needle in text.lower() for text in texts)


class FakeWebhookHandler:
    """A WebhookHandler that records prechecks and requests and returns scripted results."""

    def __init__(
        self, *, precheck: HookResponse | int = 1 << 20, response: HookResponse | None = None
    ) -> None:
        self.precheck_result = precheck
        self.response = response or HookResponse(202, {"status": "queued", "run_id": 1})
        self.prechecks: list[str] = []
        self.requests: list[tuple[str, HookRequest]] = []

    def precheck(self, name: str) -> HookResponse | int:
        """Record the hook name and return `precheck_result`."""
        self.prechecks.append(name)
        return self.precheck_result

    async def handle(self, name: str, request: HookRequest) -> HookResponse:
        """Record the request and return `response`."""
        self.requests.append((name, request))
        return self.response


class FakeTriggerInfo:
    """A TriggerInfo over fixed trigger states."""

    def __init__(self, *states: TriggerState) -> None:
        self.states = list(states)

    def trigger_states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return the states, optionally for one workflow."""
        return [s for s in self.states if workflow_id is None or s.workflow_id == workflow_id]


class FakeSecrets:
    """A SecretLookup over two dictionaries: `secrets.NAME` values and environment values."""

    def __init__(
        self, secrets: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None
    ) -> None:
        self.secrets = dict(secrets or {})
        self.env = dict(env or {})

    def get(self, name: str) -> str | None:
        """Return a secret, or None."""
        return self.secrets.get(name)

    def lookup_env(self, name: str) -> str | None:
        """Return an environment value, or None."""
        return self.env.get(name)


class RecordingSink:
    """A TriggerSink that records every change."""

    def __init__(self) -> None:
        self.changes: list[RegistryChange] = []

    def apply(self, change: RegistryChange) -> None:
        """Record the change."""
        self.changes.append(change)


class RecordingProcesses:
    """A ProcessTracker that records additions and removals."""

    def __init__(self) -> None:
        self.live: set[int] = set()
        self.history: list[tuple[str, int]] = []

    def add(self, pgid: int) -> None:
        """Track a process group."""
        self.live.add(pgid)
        self.history.append(("add", pgid))

    def discard(self, pgid: int) -> None:
        """Stop tracking a process group."""
        self.live.discard(pgid)
        self.history.append(("discard", pgid))


class RecordingLog:
    """A StepLog that records every line and mask."""

    def __init__(self) -> None:
        self.lines: list[tuple[LogStream, str]] = []
        self.masks: list[str] = []

    def write(self, stream: LogStream, text: str) -> None:
        """Record a line."""
        self.lines.append((stream, text))

    def system(self, text: str) -> None:
        """Record a system line."""
        self.lines.append((LogStream.SYSTEM, text))

    def add_mask(self, value: str) -> None:
        """Record a mask."""
        self.masks.append(value)

    def texts(self, stream: LogStream | None = None) -> list[str]:
        """Return the recorded text, optionally for one stream."""
        return [text for line_stream, text in self.lines if stream in (None, line_stream)]
