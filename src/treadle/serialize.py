"""The JSON shapes that the API, the CLI `--json` output, and the dashboard script share.

Section 8 ("JSON shapes") defines each shape. Times are `iso_utc` strings, and missing values
are null.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from treadle.clock import iso_utc
from treadle.models import (
    AttemptRecord,
    InputSpec,
    LogLine,
    RunDetail,
    RunRecord,
    Step,
    StepRecord,
    TriggerState,
    WorkflowEntry,
)
from treadle.types import JSONObject, JSONValue


def run_json(run: RunRecord) -> JSONObject:
    """Return the Run shape."""
    return {
        "id": run.id,
        "workflow": run.workflow_id,
        "workflow_name": run.workflow_name,
        "digest": run.digest,
        "status": run.status.value,
        "reason": _enum(run.reason),
        "error": run.error,
        "trigger_kind": run.trigger_kind.value,
        "trigger": run.trigger,
        "inputs": run.inputs,
        "attempt": run.attempt,
        "rerun_of": run.rerun_of,
        "replaced_by": run.replaced_by,
        "scheduled_for": _time(run.scheduled_for),
        "created_at": iso_utc(run.created_at),
        "started_at": _time(run.started_at),
        "finished_at": _time(run.finished_at),
        "duration_ms": run.duration_ms,
    }


def step_json(step: StepRecord) -> JSONObject:
    """Return the Step shape, with its attempts."""
    attempts: list[JSONValue] = [_attempt_json(attempt) for attempt in step.attempts]
    return {
        "id": step.step_id,
        "name": step.name,
        "position": step.position,
        "uses": step.uses,
        "status": step.status.value,
        "tolerated": step.tolerated,
        "skip_reason": _enum(step.skip_reason),
        "started_at": _time(step.started_at),
        "finished_at": _time(step.finished_at),
        "duration_ms": step.duration_ms,
        "outputs": step.outputs,
        "error": step.error,
        "attempts": attempts,
    }


def run_detail_json(detail: RunDetail) -> JSONObject:
    """Return the RunDetail shape: a Run plus its steps."""
    steps: list[JSONValue] = [step_json(step) for step in detail.steps]
    return {**run_json(detail.run), "steps": steps}


def log_json(line: LogLine) -> JSONObject:
    """Return the Log shape."""
    return {
        "seq": line.seq,
        "ts": iso_utc(line.ts),
        "step": line.step_id,
        "attempt": line.attempt,
        "stream": line.stream.value,
        "text": line.text,
    }


def trigger_json(state: TriggerState) -> JSONObject:
    """Return the Trigger shape."""
    return {
        "kind": state.kind.value,
        "key": state.key,
        "summary": state.summary,
        "next_fire": _time(state.next_fire),
        "error": state.error,
    }


def workflow_json(
    entry: WorkflowEntry,
    *,
    project_dir: Path,
    triggers: Sequence[TriggerState],
    last_run: RunRecord | None,
) -> JSONObject:
    """Return the Workflow shape. `path` is relative to `project_dir`."""
    # Design note: an entry without a current version (its file never loaded) has no name,
    # description, digest, or concurrency, so those values are null.
    workflow = entry.current
    path = _display_path(entry.path, project_dir)
    issues: list[JSONValue] = [issue.to_json(path) for issue in entry.issues]
    states: list[JSONValue] = [trigger_json(state) for state in triggers]
    concurrency: JSONValue = None
    if workflow is not None:
        concurrency = {
            "policy": workflow.concurrency.policy.value,
            "max_queued": workflow.concurrency.max_queued,
        }
    return {
        "id": entry.workflow_id,
        "name": workflow.name if workflow else None,
        "description": workflow.description if workflow else None,
        "path": path,
        "valid": entry.valid,
        "stale": entry.stale,
        "digest": workflow.digest if workflow else None,
        "issues": issues,
        "concurrency": concurrency,
        "triggers": states,
        "last_run": run_json(last_run) if last_run else None,
    }


def workflow_detail_json(
    entry: WorkflowEntry,
    *,
    project_dir: Path,
    triggers: Sequence[TriggerState],
    last_run: RunRecord | None,
) -> JSONObject:
    """Return the WorkflowDetail shape: a Workflow plus its inputs and steps.

    Both lists are empty when the entry has no current version.
    """
    workflow = entry.current
    inputs: list[JSONValue] = [_input_json(spec) for spec in workflow.inputs] if workflow else []
    steps: list[JSONValue] = [_step_def_json(step) for step in workflow.steps] if workflow else []
    summary = workflow_json(entry, project_dir=project_dir, triggers=triggers, last_run=last_run)
    return {**summary, "inputs": inputs, "steps": steps}


def error_json(code: str, message: str, details: JSONValue = None) -> JSONObject:
    """Return the Error shape, `{"error": {"code", "message", "details"}}`."""
    return {"error": {"code": code, "message": message, "details": details}}


def _attempt_json(attempt: AttemptRecord) -> JSONObject:
    return {
        "attempt": attempt.attempt,
        "status": attempt.status.value,
        "exit_code": attempt.exit_code,
        "error": attempt.error,
        "delay_before_ms": attempt.delay_before_ms,
        "started_at": iso_utc(attempt.started_at),
        "finished_at": _time(attempt.finished_at),
        "duration_ms": attempt.duration_ms,
        "log_bytes": attempt.log_bytes,
        "log_truncated": attempt.log_truncated,
    }


def _input_json(spec: InputSpec) -> JSONObject:
    return {
        "name": spec.name,
        "type": spec.type.value,
        "description": spec.description,
        "required": spec.required,
        "default": spec.default,
        "options": list(spec.options),
    }


def _step_def_json(step: Step) -> JSONObject:
    return {
        "id": step.id,
        "name": step.name,
        "uses": step.uses,
        "needs": list(step.needs),
        "implicit_needs": step.implicit_needs,
        "if": step.condition.source,
    }


def _display_path(path: Path, project_dir: Path) -> str:
    """Return `path` relative to the project directory, or absolute when it's outside."""
    try:
        return path.relative_to(project_dir).as_posix()
    except ValueError:
        return str(path)


def _time(value: datetime | None) -> str | None:
    return None if value is None else iso_utc(value)


def _enum(value: StrEnum | None) -> str | None:
    return None if value is None else value.value
