"""Builders for model objects, so tests don't need the workflow loader or the store.

`make_step` builds the conditions `success()`, `failure()`, `always()`, `cancelled()`,
`true`, and `false` directly as syntax trees. Any other condition needs the loader.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from treadle.config import Limits
from treadle.expr.ast import Call, Condition, Const, Expression, Template, TemplateTree
from treadle.models import (
    AttemptRecord,
    AttemptStatus,
    CatchUp,
    Concurrency,
    CronSpec,
    Defaults,
    InputSpec,
    InputType,
    RetryPolicy,
    RunDetail,
    RunReason,
    RunRecord,
    RunStatus,
    ScheduleTrigger,
    SignatureMode,
    SkipReason,
    Step,
    StepRecord,
    StepStatus,
    Trigger,
    TriggerKind,
    WatchEvent,
    WatchTrigger,
    WebhookTrigger,
    Workflow,
    WorkflowEntry,
)
from treadle.protocols import ProcessTracker
from treadle.steps.base import StepLog, StepRuntime
from treadle.types import Issue, JSONObject, JSONScalar

FIXED_TIME = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
PROJECT_DIR = Path("/project")
NO_RETRY = RetryPolicy()
DEFAULT_CONCURRENCY = Concurrency()
DEFAULT_DEFAULTS = Defaults()
DEFAULT_LIMITS = Limits()
_STATUS_FUNCTIONS = ("success", "failure", "always", "cancelled")


def make_condition(text: str = "success()") -> Condition:
    """Build a condition for a status function call, `true`, or `false`."""
    if text.endswith("()") and text[:-2] in _STATUS_FUNCTIONS:
        root: Call | Const = Call(text[:-2], (), 0, len(text))
        return Condition(text, Expression(text, root), uses_status=True)
    if text in ("true", "false"):
        root = Const(text == "true", 0, len(text))
        return Condition(text, Expression(text, root), uses_status=False)
    raise ValueError(f"make_condition can't build {text!r}; load the workflow instead")


def static_template(text: str) -> Template:
    """Return a template without expressions."""
    return Template(text, (text,) if text else ())


def make_step(
    step_id: str = "step",
    *,
    uses: str = "fake",
    needs: Sequence[str] | None = None,
    condition: str = "success()",
    params: Mapping[str, TemplateTree] | None = None,
    env: Mapping[str, str | Template] | None = None,
    outputs: Mapping[str, str | Template] | None = None,
    retry: RetryPolicy = NO_RETRY,
    timeout: float = 1800.0,
    continue_on_error: bool = False,
    name: str | None = None,
    position: int = 0,
) -> Step:
    """Build a step.

    `needs=None` means implicit needs, which `make_workflow` resolves to the previous step,
    as the loader does.
    """
    return Step(
        id=step_id,
        name=name or step_id,
        position=position,
        uses=uses,
        params=dict(params or {}),
        needs=tuple(needs or ()),
        implicit_needs=needs is None,
        condition=make_condition(condition),
        env=_templates(env),
        retry=retry,
        timeout=timeout,
        continue_on_error=continue_on_error,
        outputs=_templates(outputs),
    )


def make_workflow(
    *steps: Step,
    workflow_id: str = "wf",
    name: str | None = None,
    description: str = "",
    inputs: Iterable[InputSpec] = (),
    env: Mapping[str, str | Template] | None = None,
    triggers: Iterable[Trigger] = (),
    concurrency: Concurrency = DEFAULT_CONCURRENCY,
    defaults: Defaults = DEFAULT_DEFAULTS,
    timeout: float = 7200.0,
    cleanup_timeout: float = 300.0,
    max_parallel: int = 4,
    source: str | None = None,
) -> Workflow:
    """Build a workflow: number the steps, resolve implicit needs, and compute ancestors."""
    resolved: list[Step] = []
    for position, step in enumerate(steps or (make_step(),)):
        needs = step.needs
        if step.implicit_needs:
            needs = (resolved[-1].id,) if resolved else ()
        resolved.append(dataclasses.replace(step, position=position, needs=needs))
    text = source if source is not None else f"# workflow {workflow_id}\n"
    return Workflow(
        id=workflow_id,
        name=name or workflow_id,
        description=description,
        path=PROJECT_DIR / "workflows" / f"{workflow_id}.yml",
        digest=hashlib.sha256(text.encode()).hexdigest(),
        source=text,
        inputs=tuple(inputs),
        env=_templates(env),
        triggers=tuple(triggers),
        concurrency=concurrency,
        defaults=defaults,
        timeout=timeout,
        cleanup_timeout=cleanup_timeout,
        max_parallel=max_parallel,
        steps=tuple(resolved),
        ancestors=_ancestors(resolved),
    )


def make_input(
    name: str,
    input_type: InputType = InputType.STRING,
    *,
    required: bool = False,
    default: JSONScalar = None,
    options: Sequence[str] = (),
    description: str = "",
) -> InputSpec:
    """Build an input declaration."""
    return InputSpec(name, input_type, description, required, default, tuple(options))


def make_entry(
    workflow: Workflow | None = None,
    *,
    workflow_id: str | None = None,
    issues: Iterable[Issue] = (),
    source: str | None = None,
    stale: bool = False,
    loaded_at: datetime = FIXED_TIME,
) -> WorkflowEntry:
    """Build a registry entry for a workflow, or for an invalid file by `workflow_id`."""
    entry_id = workflow.id if workflow is not None else workflow_id
    if entry_id is None:
        raise ValueError("make_entry needs a workflow or a workflow_id")
    return WorkflowEntry(
        workflow_id=entry_id,
        path=workflow.path if workflow else PROJECT_DIR / "workflows" / f"{entry_id}.yml",
        current=workflow,
        issues=tuple(issues),
        source=source if source is not None else (workflow.source if workflow else None),
        stale=stale,
        loaded_at=loaded_at,
    )


def make_run(
    run_id: int = 1,
    *,
    workflow_id: str = "wf",
    workflow_name: str | None = None,
    digest: str | None = None,
    status: RunStatus = RunStatus.QUEUED,
    reason: RunReason | None = None,
    error: str | None = None,
    trigger_kind: TriggerKind = TriggerKind.MANUAL,
    trigger_key: str | None = None,
    trigger: JSONObject | None = None,
    inputs: JSONObject | None = None,
    scheduled_for: datetime | None = None,
    attempt: int = 1,
    rerun_of: int | None = None,
    replaced_by: int | None = None,
    created_at: datetime = FIXED_TIME,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    cancel_requested_at: datetime | None = None,
) -> RunRecord:
    """Build a stored run. The default trigger payload is that of a CLI manual run."""
    payload: JSONObject = {"kind": trigger_kind.value}
    if trigger_kind is TriggerKind.MANUAL:
        payload["origin"] = "cli"
    return RunRecord(
        id=run_id,
        workflow_id=workflow_id,
        workflow_name=workflow_name or workflow_id,
        digest=digest or hashlib.sha256(workflow_id.encode()).hexdigest(),
        status=status,
        reason=reason,
        error=error,
        trigger_kind=trigger_kind,
        trigger_key=trigger_key,
        trigger=trigger if trigger is not None else payload,
        inputs=dict(inputs or {}),
        scheduled_for=scheduled_for,
        attempt=attempt,
        rerun_of=rerun_of,
        replaced_by=replaced_by,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        cancel_requested_at=cancel_requested_at,
    )


def make_attempt(
    attempt: int = 1,
    *,
    status: AttemptStatus = AttemptStatus.SUCCEEDED,
    started_at: datetime = FIXED_TIME,
    finished_at: datetime | None = FIXED_TIME,
    exit_code: int | None = None,
    error: str | None = None,
    delay_before_ms: int = 0,
    log_bytes: int = 0,
    log_truncated: bool = False,
) -> AttemptRecord:
    """Build a stored attempt."""
    return AttemptRecord(
        attempt,
        status,
        started_at,
        finished_at,
        exit_code,
        error,
        delay_before_ms,
        log_bytes,
        log_truncated,
    )


def make_step_record(
    step_id: str = "step",
    *,
    status: StepStatus = StepStatus.SUCCEEDED,
    name: str | None = None,
    position: int = 0,
    uses: str = "fake",
    tolerated: bool = False,
    skip_reason: SkipReason | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    outputs: JSONObject | None = None,
    error: str | None = None,
    attempts: Iterable[AttemptRecord] = (),
) -> StepRecord:
    """Build a stored step."""
    return StepRecord(
        step_id=step_id,
        name=name or step_id,
        position=position,
        uses=uses,
        status=status,
        tolerated=tolerated,
        skip_reason=skip_reason,
        started_at=started_at,
        finished_at=finished_at,
        outputs=dict(outputs or {}),
        error=error,
        attempts=tuple(attempts),
    )


def make_detail(run: RunRecord | None = None, steps: Iterable[StepRecord] = ()) -> RunDetail:
    """Build a run with its steps."""
    return RunDetail(run or make_run(), tuple(steps))


def make_schedule(
    cron: CronSpec,
    *,
    workflow_id: str = "wf",
    timezone: str = "UTC",
    catch_up: CatchUp = CatchUp.LATEST,
    key: str | None = None,
) -> ScheduleTrigger:
    """Build a schedule trigger from a parsed cron expression."""
    return ScheduleTrigger(key or f"{workflow_id}:schedule:{cron.source}", cron, timezone, catch_up)


def make_watch(
    *paths: Path,
    workflow_id: str = "wf",
    include: Sequence[str] = ("*",),
    exclude: Sequence[str] = (".*", "*~", "*.tmp", "*.swp", "*.part", "*.crdownload"),
    recursive: bool = False,
    events: Iterable[WatchEvent] = tuple(WatchEvent),
    debounce: float = 2.0,
    poll_interval: float = 2.0,
    initial_scan: bool = False,
    key: str | None = None,
) -> WatchTrigger:
    """Build a watch trigger with the design's defaults."""
    return WatchTrigger(
        key=key or f"{workflow_id}:watch:{'|'.join(str(path) for path in paths)}",
        paths=tuple(paths),
        include=tuple(include),
        exclude=tuple(exclude),
        recursive=recursive,
        events=frozenset(events),
        debounce=debounce,
        poll_interval=poll_interval,
        initial_scan=initial_scan,
    )


def make_webhook(
    name: str,
    *,
    workflow_id: str = "wf",
    signature: SignatureMode = SignatureMode.HMAC_SHA256,
    secret_env: str = "HOOK_SECRET",  # noqa: S107 - a variable name, not a secret
    signature_header: str | None = None,
    timestamp_header: str | None = None,
    tolerance: float = 300.0,
    dedupe_window: float = 86400.0,
    max_body: int = 1 << 20,
    rate_limit: int = 60,
) -> WebhookTrigger:
    """Build a webhook trigger with the design's defaults."""
    default_header = (
        "x-hub-signature-256" if signature is SignatureMode.HMAC_SHA256 else "x-gitlab-token"
    )
    return WebhookTrigger(
        key=f"{workflow_id}:webhook:{name}",
        name=name,
        signature=signature,
        secret_env=secret_env,
        signature_header=(signature_header or default_header).lower(),
        timestamp_header=timestamp_header.lower() if timestamp_header else None,
        tolerance=tolerance,
        dedupe_window=dedupe_window,
        max_body=max_body,
        rate_limit=rate_limit,
    )


def make_runtime(
    tmp_path: Path,
    *,
    step_id: str = "step",
    attempt: int = 1,
    run_id: int = 1,
    workflow_id: str = "wf",
    env: Mapping[str, str] | None = None,
    log: StepLog | None = None,
    processes: ProcessTracker | None = None,
    http: httpx.AsyncClient | None = None,
    limits: Limits = DEFAULT_LIMITS,
    kill_grace: float = 5.0,
) -> StepRuntime:
    """Build a step runtime under `tmp_path`.

    The attempt directory and output file exist, as the executor creates them. The default
    HTTP client rejects every request.
    """
    # Imported here because tests.support.fakes imports this module.
    from tests.support.fakes import RecordingLog, RecordingProcesses

    project_dir = tmp_path / "project"
    state_dir = project_dir / ".treadle"
    attempt_dir = state_dir / "tmp" / f"run-{run_id}" / f"{step_id}-{attempt}"
    attempt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path = attempt_dir / "output"
    output_path.touch(mode=0o600)
    return StepRuntime(
        run_id=run_id,
        workflow_id=workflow_id,
        step_id=step_id,
        attempt=attempt,
        env=dict(env if env is not None else {"PATH": os.defpath}),
        project_dir=project_dir,
        attempt_dir=attempt_dir,
        output_path=output_path,
        default_shell="bash",
        default_cwd=project_dir,
        file_roots=(project_dir,),
        protected_paths=(state_dir, project_dir),
        state_dir=state_dir,
        kill_grace=kill_grace,
        limits=limits,
        log=log or RecordingLog(),
        processes=processes or RecordingProcesses(),
        http=http or httpx.AsyncClient(transport=httpx.MockTransport(_refuse_request)),
    )


def _refuse_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")


def _templates(values: Mapping[str, str | Template] | None) -> tuple[tuple[str, Template], ...]:
    items = (values or {}).items()
    return tuple(
        (key, value if isinstance(value, Template) else static_template(value))
        for key, value in items
    )


def _ancestors(steps: Sequence[Step]) -> dict[str, frozenset[str]]:
    ancestors: dict[str, frozenset[str]] = {}
    for step in steps:
        found = set(step.needs)
        for need in step.needs:
            found |= ancestors.get(need, frozenset())
        ancestors[step.id] = frozenset(found)
    return ancestors
