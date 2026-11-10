"""Builders for store tests: workflows inside a project, `NewRun` values, and stored runs."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tests.support.builders import FIXED_TIME, make_workflow

from treadle.clock import to_ms
from treadle.models import RunRecord, RunStatus, TriggerKind, Workflow
from treadle.store.repo import NewRun, Repository, StepSeed

T0 = to_ms(FIXED_TIME)
SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def project_workflow(project_dir: Path, workflow_id: str = "wf", **kwargs: Any) -> Workflow:
    """Build a workflow whose file is `project_dir/workflows/<workflow_id>.yml`."""
    workflow = make_workflow(workflow_id=workflow_id, **kwargs)
    return dataclasses.replace(workflow, path=project_dir / "workflows" / f"{workflow_id}.yml")


def new_run(workflow: Workflow, **overrides: Any) -> NewRun:
    """Build a `NewRun` for a queued manual run created at T0; keywords replace fields."""
    fields: dict[str, Any] = {
        "workflow": workflow,
        "status": RunStatus.QUEUED,
        "reason": None,
        "trigger_kind": TriggerKind.MANUAL,
        "trigger_key": None,
        "trigger": {"kind": "manual", "origin": "cli"},
        "inputs": {},
        "attempt": 1,
        "rerun_of": None,
        "slot": None,
        "delivery": None,
        "cancel_queued": (),
        "replaced_active": None,
        "created_at_ms": T0,
    }
    return NewRun(**(fields | overrides))


class Runs:
    """Stores runs in a given state through the repository's own transitions."""

    def __init__(self, repo: Repository, workflow: Workflow) -> None:
        self.repo = repo
        self.workflow = workflow

    def queued(self, *, workflow: Workflow | None = None, **overrides: Any) -> RunRecord:
        """Create a queued run. Keywords replace `NewRun` fields."""
        result = self.repo.create_run(new_run(workflow or self.workflow, **overrides))
        assert result.outcome == "created"
        assert result.run is not None
        return result.run

    def running(
        self,
        *,
        steps: Sequence[str] = ("build", "test"),
        at_ms: int = T0,
        **overrides: Any,
    ) -> RunRecord:
        """Create a run and start it at `at_ms` with pending steps of the `fake` type."""
        run = self.queued(created_at_ms=at_ms, **overrides)
        seeds = [StepSeed(name, name, position, "fake") for position, name in enumerate(steps)]
        assert self.repo.start_run(run.id, seeds, at_ms=at_ms, pid=4321)
        return self.get(run.id)

    def finished(
        self,
        status: RunStatus = RunStatus.SUCCEEDED,
        *,
        at_ms: int = T0,
        error: str | None = None,
        steps: Sequence[str] = (),
        **overrides: Any,
    ) -> RunRecord:
        """Create a run that started and finished at `at_ms` with `status`."""
        run = self.running(steps=steps, at_ms=at_ms, **overrides)
        assert self.repo.finish_run(run.id, status, at_ms=at_ms, reason=None, error=error)
        return self.get(run.id)

    def get(self, run_id: int) -> RunRecord:
        """Return a stored run that must exist."""
        run = self.repo.get_run(run_id)
        assert run is not None
        return run
