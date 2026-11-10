"""The domain model: workflows, triggers, runs, steps, and the records that packages exchange.

Everything here is immutable data. The loader builds `Workflow` values, the store returns the
records, and the run manager, triggers, web layer, and CLI pass them between each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias

from treadle.clock import to_ms
from treadle.expr.ast import Condition, Template, TemplateTree
from treadle.types import Issue, JSONObject, JSONScalar, JSONValue, Position, Severity


class InputType(StrEnum):
    """The type of a workflow input."""

    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    CHOICE = "choice"


class ConcurrencyPolicy(StrEnum):
    """What happens to a new run while the workflow already has one."""

    QUEUE = "queue"
    SKIP = "skip"
    REPLACE = "replace"


class CatchUp(StrEnum):
    """Which missed schedule slots fire after downtime."""

    NONE = "none"
    LATEST = "latest"
    ALL = "all"


class WatchEvent(StrEnum):
    """A kind of file change that a watch trigger reports."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class SignatureMode(StrEnum):
    """How a webhook authenticates its sender."""

    HMAC_SHA256 = "hmac-sha256"
    TOKEN = "token"  # noqa: S105 - a signature mode, not a secret


class TriggerKind(StrEnum):
    """What started a run."""

    MANUAL = "manual"
    SCHEDULE = "schedule"
    WATCH = "watch"
    WEBHOOK = "webhook"


class Origin(StrEnum):
    """Where a manual run came from."""

    CLI = "cli"
    API = "api"
    DASHBOARD = "dashboard"


class RunStatus(StrEnum):
    """The state of a run. Every status except `queued` and `running` is final."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        """True for every status except `queued` and `running`."""
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


class StepStatus(StrEnum):
    """The state of one step in a run."""

    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        """True for every status except `pending`, `running`, and `retrying`."""
        return self not in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.RETRYING)


class AttemptStatus(StrEnum):
    """The state of one attempt of a step."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class CancelReason(StrEnum):
    """Why a run is being cancelled."""

    USER = "user"
    REPLACED = "replaced"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"
    WORKFLOW_REMOVED = "workflow_removed"


class RunReason(StrEnum):
    """Why a run ended the way it did, stored in `runs.reason`."""

    ALREADY_ACTIVE = "already_active"
    QUEUE_FULL = "queue_full"
    USER = "user"
    REPLACED = "replaced"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"
    WORKFLOW_REMOVED = "workflow_removed"
    EXECUTOR_STOPPED = "executor_stopped"
    SETUP_FAILED = "setup_failed"


class SkipReason(StrEnum):
    """Why a step was skipped."""

    CONDITION = "condition"
    UPSTREAM_FAILED = "upstream_failed"
    RUN_CANCELLED = "run_cancelled"
    SETUP_FAILED = "setup_failed"
    RUN_INTERRUPTED = "run_interrupted"


class LogStream(StrEnum):
    """Where a log line came from. `system` lines come from Treadle itself."""

    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"


class SubmitOutcome(StrEnum):
    """The result of submitting a run request."""

    QUEUED = "queued"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    SLOT_TAKEN = "slot_taken"


# A shell: "bash", "sh", "python", or an argv template in which "{0}" is the script path.
ShellSpec: TypeAlias = str | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A parsed cron expression, as the sets of values that each field matches."""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool

    @property
    def interval_like(self) -> bool:
        """True when the schedule fires in every hour, which changes the DST rules."""
        return self.hours == frozenset(range(24))


@dataclass(frozen=True, slots=True)
class InputSpec:
    """A declared workflow input. `default` is None when the input has no default."""

    name: str
    type: InputType
    description: str
    required: bool
    default: JSONScalar
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often a step runs and how long it waits between attempts. Delays are seconds."""

    attempts: int = 1
    delay: float = 1.0
    backoff: float = 2.0
    max_delay: float = 300.0
    jitter: float = 0.0


@dataclass(frozen=True, slots=True)
class Concurrency:
    """The concurrency policy of a workflow."""

    policy: ConcurrencyPolicy = ConcurrencyPolicy.QUEUE
    max_queued: int = 10


@dataclass(frozen=True, slots=True)
class Defaults:
    """Step settings that apply when a step doesn't set its own."""

    timeout: float = 1800.0
    retry: RetryPolicy = RetryPolicy()
    shell: ShellSpec = "bash"
    cwd: str = "."


@dataclass(frozen=True, slots=True)
class ScheduleTrigger:
    """A cron schedule. `key` identifies the trigger's stored schedule state."""

    key: str
    cron: CronSpec
    timezone: str
    catch_up: CatchUp


@dataclass(frozen=True, slots=True)
class WatchTrigger:
    """A polling file watch. `paths` are absolute, with "~" expanded and symlinks kept."""

    key: str
    paths: tuple[Path, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    recursive: bool
    events: frozenset[WatchEvent]
    debounce: float
    poll_interval: float
    initial_scan: bool


@dataclass(frozen=True, slots=True)
class WebhookTrigger:
    """A signed webhook at `POST /hooks/<name>`. Header names are lowercase."""

    key: str
    name: str
    signature: SignatureMode
    secret_env: str
    signature_header: str
    timestamp_header: str | None
    tolerance: float
    dedupe_window: float
    max_body: int
    rate_limit: int


Trigger: TypeAlias = ScheduleTrigger | WatchTrigger | WebhookTrigger


def trigger_kind(trigger: Trigger) -> TriggerKind:
    """Return the kind of a trigger."""
    if isinstance(trigger, ScheduleTrigger):
        return TriggerKind.SCHEDULE
    if isinstance(trigger, WatchTrigger):
        return TriggerKind.WATCH
    return TriggerKind.WEBHOOK


@dataclass(frozen=True, slots=True)
class Step:
    """One step of a workflow, as the loader normalized it.

    `needs` holds only earlier steps; `implicit_needs` is True when the file omitted `needs`
    and the step depends on the step before it. `env` and `outputs` keep declaration order.
    """

    id: str
    name: str
    position: int
    uses: str
    params: dict[str, TemplateTree]
    needs: tuple[str, ...]
    implicit_needs: bool
    condition: Condition
    env: tuple[tuple[str, Template], ...]
    retry: RetryPolicy
    timeout: float
    continue_on_error: bool
    outputs: tuple[tuple[str, Template], ...]
    location: Position | None = None


@dataclass(frozen=True, slots=True)
class Workflow:
    """A valid, loaded workflow file.

    `path` is absolute, `digest` is the SHA-256 hex of the file bytes, and `ancestors` maps
    each step ID to every step it depends on, directly or transitively.
    """

    id: str
    name: str
    description: str
    path: Path
    digest: str
    source: str
    inputs: tuple[InputSpec, ...]
    env: tuple[tuple[str, Template], ...]
    triggers: tuple[Trigger, ...]
    concurrency: Concurrency
    defaults: Defaults
    timeout: float
    cleanup_timeout: float
    max_parallel: int
    steps: tuple[Step, ...]
    ancestors: Mapping[str, frozenset[str]]

    def step(self, step_id: str) -> Step:
        """Return the step with this ID, or raise KeyError."""
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def input(self, name: str) -> InputSpec | None:
        """Return the declared input with this name, or None."""
        return next((spec for spec in self.inputs if spec.name == name), None)

    def webhooks(self) -> tuple[WebhookTrigger, ...]:
        """Return the webhook triggers, in declaration order."""
        return tuple(trigger for trigger in self.triggers if isinstance(trigger, WebhookTrigger))

    @property
    def short_digest(self) -> str:
        """The first 12 hex characters of the digest."""
        return self.digest[:12]


@dataclass(frozen=True, slots=True)
class WorkflowEntry:
    """What the registry knows about one workflow file.

    `current` is the last good version. `issues` and `source` describe the newest file
    version, and `stale` is True when that version is invalid while an older one runs.
    """

    workflow_id: str
    path: Path
    current: Workflow | None
    issues: tuple[Issue, ...]
    source: str | None
    stale: bool
    loaded_at: datetime

    @property
    def valid(self) -> bool:
        """True when the newest file version has no errors."""
        return not any(issue.severity is Severity.ERROR for issue in self.issues)


@dataclass(frozen=True, slots=True)
class RegistryChange:
    """A workflow version change: `old` or `new` is None when the workflow appears or goes."""

    workflow_id: str
    old: Workflow | None
    new: Workflow | None


@dataclass(frozen=True, slots=True)
class ScheduleSlot:
    """A schedule slot to claim with a compare-and-set from `previous_ms` to `slot_ms`."""

    key: str
    slot_ms: int
    previous_ms: int


@dataclass(frozen=True, slots=True)
class Delivery:
    """A webhook delivery digest to deduplicate within `window` seconds."""

    hook: str
    digest: str
    window: float


@dataclass(frozen=True, slots=True)
class RunRequest:
    """A request to start a run.

    `inputs_are_strings` selects the string coercion rules (CLI and dashboard) instead of the
    JSON rules (API). Only `RunManager.rerun` sets `attempt`.
    """

    workflow_id: str
    kind: TriggerKind
    trigger: Mapping[str, JSONValue]
    inputs: Mapping[str, JSONValue] = field(default_factory=dict)
    inputs_are_strings: bool = False
    trigger_key: str | None = None
    slot: ScheduleSlot | None = None
    delivery: Delivery | None = None
    rerun_of: int | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A stored run. `trigger` and `inputs` are masked, and `error` is a one-line summary."""

    id: int
    workflow_id: str
    workflow_name: str
    digest: str
    status: RunStatus
    reason: RunReason | None
    error: str | None
    trigger_kind: TriggerKind
    trigger_key: str | None
    trigger: JSONObject
    inputs: JSONObject
    scheduled_for: datetime | None
    attempt: int
    rerun_of: int | None
    replaced_by: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None

    @property
    def duration_ms(self) -> int | None:
        """Milliseconds from start to finish, or None unless the run started and finished."""
        return _duration_ms(self.started_at, self.finished_at)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """A stored step attempt."""

    attempt: int
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    error: str | None
    delay_before_ms: int
    log_bytes: int
    log_truncated: bool

    @property
    def duration_ms(self) -> int | None:
        """Milliseconds from start to finish, or None while the attempt runs."""
        return _duration_ms(self.started_at, self.finished_at)


@dataclass(frozen=True, slots=True)
class StepRecord:
    """A stored step. `tolerated` marks a `continue_on_error` failure; outputs are masked."""

    step_id: str
    name: str
    position: int
    uses: str
    status: StepStatus
    tolerated: bool
    skip_reason: SkipReason | None
    started_at: datetime | None
    finished_at: datetime | None
    outputs: JSONObject
    error: str | None
    attempts: tuple[AttemptRecord, ...]

    @property
    def duration_ms(self) -> int | None:
        """Milliseconds from start to finish, or None unless the step started and finished."""
        return _duration_ms(self.started_at, self.finished_at)


@dataclass(frozen=True, slots=True)
class RunDetail:
    """A run with its steps, in position order."""

    run: RunRecord
    steps: tuple[StepRecord, ...]


@dataclass(frozen=True, slots=True)
class LogLine:
    """A stored log line. `seq` increases by one per run, starting at 1; text is masked."""

    seq: int
    ts: datetime
    step_id: str | None
    attempt: int | None
    stream: LogStream
    text: str


@dataclass(frozen=True, slots=True)
class RunFilter:
    """Filters for run history. `text` matches the run error or any log line, ignoring case."""

    workflow_id: str | None = None
    statuses: frozenset[RunStatus] = frozenset()
    trigger_kind: TriggerKind | None = None
    text: str | None = None
    before_id: int | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class RunPage:
    """One page of run history, newest first. `next_before_id` is None on the last page."""

    runs: tuple[RunRecord, ...]
    next_before_id: int | None


@dataclass(frozen=True, slots=True)
class Submission:
    """The result of a submit.

    `run` is None for `slot_taken`, and for a `duplicate` delivery whose earlier run was pruned.
    """

    outcome: SubmitOutcome
    run: RunRecord | None


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """The result of a cancel request. `status` is the run status when it was handled."""

    run_id: int
    status: RunStatus
    accepted: bool
    forced: bool


@dataclass(frozen=True, slots=True)
class ManagerStats:
    """Counts from the run manager, for the status endpoint."""

    active: int
    queued: int
    draining: bool


@dataclass(frozen=True, slots=True)
class TriggerState:
    """A trigger as people see it: a summary, the next fire, and any health problem."""

    workflow_id: str
    key: str
    kind: TriggerKind
    summary: str
    next_fire: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class HookRequest:
    """A webhook request. Header names are lowercase; repeated headers are joined with ", "."""

    method: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes
    content_type: str | None


@dataclass(frozen=True, slots=True)
class HookResponse:
    """A webhook response with a JSON body."""

    status: int
    body: JSONObject
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """A request from a CLI process to the lock holder, through the `control_requests` table."""

    id: int
    kind: Literal["run", "cancel"]
    payload: JSONObject
    created_at: datetime


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return to_ms(end) - to_ms(start)
