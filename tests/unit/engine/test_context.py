"""Environments, the status functions over ancestors, and expression context roots."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.builders import make_run, make_step, make_workflow

from treadle.engine.context import (
    RESERVED_ENV_PREFIX,
    AncestorStatus,
    RunState,
    base_values,
    filtered_environ,
    merge_env,
    step_values,
    treadle_env,
)
from treadle.models import RunStatus, StepStatus, TriggerKind, Workflow
from treadle.types import JSONObject

S = StepStatus


def test_filtered_environ_drops_treadle_variables_and_hidden_names() -> None:
    environ = {
        "PATH": "/bin",
        "TREADLE_TOKEN": "t",
        "TREADLE_SECRET_X": "s",
        "TREADLE_": "bare",
        "HOOK_SECRET": "h",
        "treadle_lower": "kept",
    }

    assert RESERVED_ENV_PREFIX == "TREADLE_"
    assert filtered_environ(environ, {"HOOK_SECRET", "ABSENT"}) == {
        "PATH": "/bin",
        "treadle_lower": "kept",
    }


def test_merge_env_lets_later_layers_win() -> None:
    assert merge_env() == {}
    assert merge_env({"A": "1", "B": "1"}, {"B": "2"}, {"C": "3"}, {"A": "4"}) == {
        "A": "4",
        "B": "2",
        "C": "3",
    }


def diamond() -> Workflow:
    """a -> (b, c) -> d, plus an unrelated root e."""
    return make_workflow(
        make_step("a"),
        make_step("b", needs=["a"]),
        make_step("c", needs=["a"]),
        make_step("d", needs=["b", "c"]),
        make_step("e", needs=[]),
    )


def state_of(workflow: Workflow, **statuses: StepStatus) -> RunState:
    every = {step.id: S.PENDING for step in workflow.steps}
    return RunState(
        run=make_run(1, status=RunStatus.RUNNING),
        workflow=workflow,
        inputs={},
        trigger={},
        secrets={},
        process_env={},
        statuses=every | statuses,
    )


@pytest.mark.parametrize(
    ("ancestor", "success", "failure"),
    [
        (S.SUCCEEDED, True, False),
        (S.SKIPPED, True, False),
        (S.FAILED, False, True),
        (S.TIMED_OUT, False, True),
        (S.CANCELLED, False, False),
    ],
)
def test_status_functions_follow_the_ancestor_statuses(
    ancestor: StepStatus, success: bool, failure: bool
) -> None:
    state = state_of(diamond(), a=ancestor, b=S.SUCCEEDED, c=S.SUCCEEDED)
    view = AncestorStatus(state, "d")

    assert (view.success(), view.failure(), view.cancelled()) == (success, failure, False)


@pytest.mark.parametrize("status", [S.FAILED, S.TIMED_OUT])
def test_a_tolerated_failure_counts_as_passing(status: StepStatus) -> None:
    state = state_of(diamond(), a=S.SUCCEEDED, b=status, c=S.SUCCEEDED)
    state.tolerated.add("b")
    view = AncestorStatus(state, "d")

    assert (view.success(), view.failure()) == (True, False)


def test_status_functions_look_only_at_ancestors() -> None:
    state = state_of(diamond(), a=S.SUCCEEDED, b=S.FAILED, e=S.FAILED)

    assert AncestorStatus(state, "c").success()
    assert not AncestorStatus(state, "c").failure()
    assert AncestorStatus(state, "d").failure()
    assert AncestorStatus(state, "a").success()  # a root has no ancestors


def test_a_cancelling_run_is_cancelled_and_never_successful() -> None:
    state = state_of(diamond(), a=S.SUCCEEDED)
    state.cancelling = True
    view = AncestorStatus(state, "b")

    assert (view.success(), view.failure(), view.cancelled()) == (False, False, True)


def test_the_view_reads_statuses_when_asked() -> None:
    state = state_of(diamond())
    view = AncestorStatus(state, "b")

    state.statuses["a"] = S.FAILED

    assert view.failure()


def running_state(workflow: Workflow, *, project_dir: Path | None) -> RunState:
    run = make_run(
        42,
        workflow_id=workflow.id,
        status=RunStatus.RUNNING,
        trigger_kind=TriggerKind.WEBHOOK,
        attempt=2,
        rerun_of=41,
        started_at=datetime(2026, 11, 9, 2, 30, 0, 123000, tzinfo=UTC),
    )
    return RunState(
        run=run,
        workflow=workflow,
        inputs={"name": "Ada", "n": 3},
        trigger={"kind": "webhook", "body": {"ref": "main"}},
        secrets={"API": "s3cret"},
        process_env={"PATH": "/bin"},
        workflow_env={"DEST": "/backup"},
        project_dir=project_dir,
    )


def test_base_values_hold_every_root_but_steps() -> None:
    workflow = dataclasses.replace(
        make_workflow(name="Nightly", description="Mirrors files."),
        path=Path("/project/workflows/wf.yml"),
    )

    values = base_values(running_state(workflow, project_dir=Path("/project")))

    assert values == {
        "inputs": {"name": "Ada", "n": 3},
        "secrets": {"API": "s3cret"},
        "trigger": {"kind": "webhook", "body": {"ref": "main"}},
        "run": {
            "id": 42,
            "attempt": 2,
            "started_at": "2026-11-09T02:30:00.123Z",
            "workflow": "wf",
            "trigger": "webhook",
            "rerun_of": 41,
        },
        "workflow": {
            "id": "wf",
            "name": "Nightly",
            "description": "Mirrors files.",
            "path": "workflows/wf.yml",
            "digest": workflow.digest[:12],
        },
        "env": {"PATH": "/bin"},
    }


@pytest.mark.parametrize("project_dir", [Path("/elsewhere"), None])
def test_a_workflow_outside_the_project_keeps_its_absolute_path(project_dir: Path | None) -> None:
    workflow = dataclasses.replace(make_workflow(), path=Path("/project/workflows/wf.yml"))

    values = base_values(running_state(workflow, project_dir=project_dir))

    assert isinstance(values["workflow"], dict)
    assert values["workflow"]["path"] == "/project/workflows/wf.yml"


def test_a_run_that_hasnt_started_has_no_start_time() -> None:
    state = running_state(make_workflow(), project_dir=None)
    state.run = dataclasses.replace(state.run, started_at=None, rerun_of=None)

    run = base_values(state)["run"]

    assert isinstance(run, dict)
    assert (run["started_at"], run["rerun_of"]) == (None, None)


def test_step_values_add_ancestors_env_and_result() -> None:
    state = state_of(diamond(), a=S.SUCCEEDED, b=S.FAILED, c=S.SKIPPED, e=S.SUCCEEDED)
    outputs: dict[str, JSONObject] = {"a": {"tag": "v1"}, "b": {"exit_code": 2}, "e": {"x": 1}}
    state.outputs.update(outputs)
    step = diamond().step("d")

    values = step_values(state, step, env={"STEP": "1"}, result={"raw": True})

    assert values["env"] == {"STEP": "1"}
    assert values["steps"] == {
        "a": {"outputs": {"tag": "v1"}, "status": "succeeded"},
        "b": {"outputs": {"exit_code": 2}, "status": "failed"},
        "c": {"outputs": {}, "status": "skipped"},
    }
    assert values["result"] == {"raw": True}
    assert "result" not in step_values(state, step, env={})


def test_a_root_step_sees_no_steps() -> None:
    state = state_of(diamond())

    assert step_values(state, diamond().step("e"), env={})["steps"] == {}


def test_treadle_env_holds_the_ten_variables(tmp_path: Path) -> None:
    state = running_state(make_workflow(make_step("build")), project_dir=tmp_path)
    run_dir = tmp_path / ".treadle" / "tmp" / "run-42"

    variables = treadle_env(
        state,
        state.workflow.step("build"),
        3,
        run_dir=run_dir,
        attempt_dir=run_dir / "build-3",
        output_path=run_dir / "build-3" / "output",
        event_path=run_dir / "event.json",
        project_dir=tmp_path,
    )

    assert variables == {
        "TREADLE_RUN_ID": "42",
        "TREADLE_RUN_ATTEMPT": "2",
        "TREADLE_WORKFLOW": "wf",
        "TREADLE_STEP_ID": "build",
        "TREADLE_STEP_ATTEMPT": "3",
        "TREADLE_TRIGGER": "webhook",
        "TREADLE_OUTPUT": str(run_dir / "build-3" / "output"),
        "TREADLE_RUN_TMP": str(run_dir / "shared"),
        "TREADLE_EVENT_PATH": str(run_dir / "event.json"),
        "TREADLE_PROJECT_DIR": str(tmp_path),
    }
