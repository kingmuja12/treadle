"""Run state, expression contexts, and step environments (section 5, "Environment")."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from treadle.clock import iso_utc
from treadle.models import RunRecord, Step, StepStatus, Workflow
from treadle.types import JSONObject, JSONValue

RESERVED_ENV_PREFIX: Final = "TREADLE_"
_FAILED: Final = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})
_NOT_SUCCESS: Final = _FAILED | {StepStatus.CANCELLED}


def filtered_environ(environ: Mapping[str, str], hidden: Collection[str]) -> dict[str, str]:
    """Return the process environment without `TREADLE_*` variables or the hidden names."""
    return {
        name: value
        for name, value in environ.items()
        if not name.startswith(RESERVED_ENV_PREFIX) and name not in hidden
    }


def merge_env(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge environment layers; later layers win."""
    merged: dict[str, str] = {}
    for layer in layers:
        merged.update(layer)
    return merged


@dataclass(slots=True)
class RunState:
    """The executor's in-memory state for one run.

    `inputs` are unmasked when the run was admitted in this process. `outputs` are unmasked,
    for downstream steps. `tolerated` holds steps whose failure `continue_on_error` excused.

    The keyword-only fields serve `base_values` and `conclude`: `project_dir` makes
    `workflow.path` relative, `errors` holds each failed step's unmasked error, and
    `attempts` counts the attempts each step started.
    """

    run: RunRecord
    workflow: Workflow
    inputs: JSONObject
    trigger: JSONObject
    secrets: Mapping[str, str]
    process_env: dict[str, str]
    workflow_env: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, StepStatus] = field(default_factory=dict)
    tolerated: set[str] = field(default_factory=set)
    outputs: dict[str, JSONObject] = field(default_factory=dict)
    cancelling: bool = False
    # Design note: section 4 gives these functions only the RunState, which has no project
    # directory and no step errors or attempt counts, so the state carries them.
    project_dir: Path | None = field(default=None, kw_only=True)
    errors: dict[str, str] = field(default_factory=dict, kw_only=True)
    attempts: dict[str, int] = field(default_factory=dict, kw_only=True)


class AncestorStatus:
    """Answers the status functions for one step from its ancestors; implements StatusView."""

    def __init__(self, state: RunState, step_id: str) -> None:
        self._state = state
        self._ancestors = state.workflow.ancestors.get(step_id, frozenset())

    def success(self) -> bool:
        """True when the run isn't cancelling and no ancestor failed without being tolerated.

        An ancestor fails by being failed, timed out, or cancelled. Skipped ancestors don't
        count against it.
        """
        return not self._state.cancelling and not self._any(_NOT_SUCCESS)

    def failure(self) -> bool:
        """True when some ancestor has a non-tolerated failed or timed-out status."""
        return self._any(_FAILED)

    def cancelled(self) -> bool:
        """True when the run is cancelling, for any reason."""
        return self._state.cancelling

    def _any(self, statuses: frozenset[StepStatus]) -> bool:
        """True when an ancestor that isn't tolerated has one of `statuses`."""
        state = self._state
        return any(
            state.statuses.get(ancestor) in statuses and ancestor not in state.tolerated
            for ancestor in self._ancestors
        )


def base_values(state: RunState) -> dict[str, JSONValue]:
    """Return the context roots that every expression sees.

    They are inputs, secrets, trigger, run, workflow, and `env` as the process environment.
    `secrets` holds only defined secrets, so a missing one raises instead of rendering empty.
    """
    run, workflow = state.run, state.workflow
    secrets: dict[str, JSONValue] = dict(state.secrets)
    env: dict[str, JSONValue] = dict(state.process_env)
    return {
        "inputs": dict(state.inputs),
        "secrets": secrets,
        "trigger": state.trigger,
        "run": {
            "id": run.id,
            "attempt": run.attempt,
            "started_at": iso_utc(run.started_at) if run.started_at is not None else None,
            "workflow": workflow.id,
            "trigger": run.trigger_kind.value,
            "rerun_of": run.rerun_of,
        },
        "workflow": {
            "id": workflow.id,
            "name": workflow.name,
            "description": workflow.description,
            "path": _relative(workflow.path, state.project_dir),
            "digest": workflow.short_digest,
        },
        "env": env,
    }


def step_values(
    state: RunState, step: Step, *, env: Mapping[str, str], result: JSONObject | None = None
) -> dict[str, JSONValue]:
    """Return the context roots for one step's expressions.

    That is `base_values` with `env` replaced, the step's ancestors under `steps`, and
    `result` when given. Each ancestor has its `outputs` and raw `status`.
    """
    values = base_values(state)
    step_env: dict[str, JSONValue] = dict(env)
    values["env"] = step_env
    ancestors = state.workflow.ancestors.get(step.id, frozenset())
    steps: dict[str, JSONValue] = {}
    for other in state.workflow.steps:
        if other.id in ancestors:
            status = state.statuses.get(other.id, StepStatus.PENDING)
            steps[other.id] = {"outputs": state.outputs.get(other.id, {}), "status": status.value}
    values["steps"] = steps
    if result is not None:
        values["result"] = result
    return values


def treadle_env(
    state: RunState,
    step: Step,
    attempt: int,
    *,
    run_dir: Path,
    attempt_dir: Path,
    output_path: Path,
    event_path: Path,
    project_dir: Path,
) -> dict[str, str]:
    """Return Treadle's own variables for one attempt, such as `TREADLE_RUN_ID`.

    `TREADLE_RUN_TMP` is `run_dir/shared`. The attempt directory holds `output_path` and the
    script file, and section 5's table gives it no variable, so `attempt_dir` isn't exported.
    """
    run = state.run
    return {
        "TREADLE_RUN_ID": str(run.id),
        "TREADLE_RUN_ATTEMPT": str(run.attempt),
        "TREADLE_WORKFLOW": state.workflow.id,
        "TREADLE_STEP_ID": step.id,
        "TREADLE_STEP_ATTEMPT": str(attempt),
        "TREADLE_TRIGGER": run.trigger_kind.value,
        "TREADLE_OUTPUT": str(output_path),
        "TREADLE_RUN_TMP": str(run_dir / "shared"),
        "TREADLE_EVENT_PATH": str(event_path),
        "TREADLE_PROJECT_DIR": str(project_dir),
    }


def _relative(path: Path, project_dir: Path | None) -> str:
    """Return `path` relative to the project directory, or absolute when it lies outside."""
    if project_dir is not None and path.is_relative_to(project_dir):
        return str(path.relative_to(project_dir))
    return str(path)
