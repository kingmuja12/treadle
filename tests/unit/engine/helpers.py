"""Harnesses for engine tests: an executor over a store, and a running run manager.

Integration tests import these too, with a threaded store and the real step types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx
from tests.support.fakes import FakeStep, FakeWorkflowSource
from tests.unit.store.helpers import new_run

from treadle.clock import Clock, RandomSource, to_ms
from treadle.config import Settings
from treadle.engine.executor import Executor, ExecutorDeps, RunHandle
from treadle.engine.manager import RunManager
from treadle.models import (
    LogLine,
    LogStream,
    RunDetail,
    RunRecord,
    RunRequest,
    RunStatus,
    StepRecord,
    TriggerKind,
    Workflow,
    WorkflowEntry,
)
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepRegistry
from treadle.steps.procs import ProcessRegistry
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository, StepSeed
from treadle.types import JSONObject, JSONValue

MANUAL: JSONObject = {"kind": "manual", "origin": "cli"}
# The process environment the executor inherits. The TREADLE_* variables and the hidden
# webhook secret never reach a step.
ENVIRON: Mapping[str, str] = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/ada",
    "LANG": "C.UTF-8",
    "TREADLE_TOKEN": "token-value-that-is-long-enough!",
    "TREADLE_SECRET_API_TOKEN": "api-token-from-env",
    "TREADLE_DEBUG": "1",
    "HOOK_SECRET": "hook-secret-value-0123",
}
HIDDEN = frozenset({"HOOK_SECRET"})


@dataclass
class Secrets:
    """What the executor's `secrets` and `hidden_env` callables return; tests may change it."""

    snapshot: SecretSnapshot
    hidden: frozenset[str] = HIDDEN


@dataclass(frozen=True)
class Launched:
    """A run that executes in a task, with the handle that cancels it."""

    run_id: int
    handle: RunHandle
    task: asyncio.Task[RunStatus]


class Engine:
    """An executor over a store, with helpers that create runs and read them back."""

    def __init__(
        self,
        *,
        store: AsyncStore,
        settings: Settings,
        steps: StepRegistry,
        clock: Clock,
        rng: RandomSource,
        http: httpx.AsyncClient,
        secrets: Secrets,
        environ: Mapping[str, str] = ENVIRON,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.secrets = secrets
        self.processes = ProcessRegistry()
        self.deps = ExecutorDeps(
            store=store,
            steps=steps,
            settings=settings,
            secrets=lambda: secrets.snapshot,
            hidden_env=lambda: secrets.hidden,
            environ=environ,
            clock=clock,
            rng=rng,
            processes=self.processes,
            http=http,
        )
        self.executor = Executor(self.deps)

    async def create(
        self,
        workflow: Workflow,
        *,
        inputs: JSONObject | None = None,
        trigger: JSONObject | None = None,
    ) -> RunRecord:
        """Store a queued manual run of `workflow`, as the run manager would."""
        new = new_run(
            workflow,
            inputs=inputs or {},
            trigger=trigger or MANUAL,
            created_at_ms=to_ms(self.clock.now()),
        )
        result = await self.store.write(lambda repo: repo.create_run(new))
        assert result.run is not None
        return result.run

    async def start(self, workflow: Workflow, **kwargs: Any) -> RunRecord:
        """Store a run that is `running`, with its steps `pending`."""
        queued = await self.create(workflow, **kwargs)
        seeds = [StepSeed(step.id, step.name, step.position, step.uses) for step in workflow.steps]
        now = to_ms(self.clock.now())

        def start(repo: Repository) -> RunRecord | None:
            assert repo.start_run(queued.id, seeds, at_ms=now, pid=1)
            return repo.get_run(queued.id)

        run = await self.store.write(start)
        assert run is not None
        return run

    async def launch(
        self,
        workflow: Workflow,
        *,
        inputs: JSONObject | None = None,
        trigger: JSONObject | None = None,
    ) -> Launched:
        """Start executing a new run of `workflow` in a task."""
        run = await self.start(workflow, inputs=inputs, trigger=trigger)
        handle = RunHandle(run.id)
        task = asyncio.create_task(
            self.executor.execute(
                run, workflow, handle, inputs=inputs or {}, trigger=trigger or MANUAL
            )
        )
        return Launched(run.id, handle, task)

    async def execute(
        self,
        workflow: Workflow,
        *,
        inputs: JSONObject | None = None,
        trigger: JSONObject | None = None,
    ) -> RunDetail:
        """Execute a new run of `workflow` to the end and return the stored run."""
        launched = await self.launch(workflow, inputs=inputs, trigger=trigger)
        return await self.finish(launched)

    async def finish(self, launched: Launched) -> RunDetail:
        """Wait for a launched run, and return the stored run, whose status `execute` returned."""
        status = await launched.task
        detail = await self.detail(launched.run_id)
        assert detail.run.status is status
        return detail

    async def detail(self, run_id: int) -> RunDetail:
        detail = await self.store.get_run(run_id)
        assert detail is not None
        return detail

    async def logs(self, run_id: int, **kwargs: Any) -> list[LogLine]:
        return await self.store.logs(run_id, limit=100_000, **kwargs)

    async def texts(
        self, run_id: int, *, stream: LogStream | None = None, **kwargs: Any
    ) -> list[str]:
        """Return the stored log text, optionally of one stream."""
        lines = await self.logs(run_id, **kwargs)
        return [line.text for line in lines if stream in (None, line.stream)]


class Manager:
    """A run manager whose actor runs while the `async with` block runs.

    Leaving the block shuts the manager down with no grace and waits for the actor.
    """

    def __init__(
        self,
        engine: Engine,
        *items: Workflow | WorkflowEntry,
        executor: Any = None,
        source: FakeWorkflowSource | None = None,
    ) -> None:
        self.engine = engine
        self.source = source or FakeWorkflowSource()
        for item in items:
            self.source.add(item)
        self.manager = RunManager(
            store=engine.store,
            executor=executor or engine.executor,
            workflows=self.source,
            secrets=lambda: engine.secrets.snapshot,
            hidden_env=lambda: engine.secrets.hidden,
            settings=engine.settings,
            clock=engine.clock,
        )
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Manager:
        self._task = asyncio.create_task(self.manager.run())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.manager.shutdown(grace=0)
        await self.task

    @property
    def task(self) -> asyncio.Task[None]:
        assert self._task is not None
        return self._task

    async def submit(
        self,
        workflow_id: str = "wf",
        *,
        inputs: Mapping[str, JSONValue] | None = None,
        **kwargs: Any,
    ) -> RunRecord:
        """Submit a manual run and return the stored run, which must exist."""
        request = RunRequest(workflow_id, TriggerKind.MANUAL, MANUAL, inputs=inputs or {}, **kwargs)
        submission = await self.manager.submit(request)
        assert submission.run is not None
        return submission.run

    async def run_to_end(self, workflow_id: str = "wf", **kwargs: Any) -> RunDetail:
        """Submit a manual run, wait until it's terminal, and return it."""
        run = await self.submit(workflow_id, **kwargs)
        await self.manager.wait(run.id)
        return await self.engine.detail(run.id)


class ScriptedExecutor:
    """An executor double: each run waits until `release` is set, then succeeds.

    `behavior` is "finish", "raise" (fail at once with an unexpected error), "finalizing"
    (mark the handle finalizing first), or "deaf" (ignore cancel requests and force).
    """

    def __init__(self, store: AsyncStore, clock: Clock, behavior: str = "finish") -> None:
        self.store = store
        self.clock = clock
        self.behavior = behavior
        self.release = asyncio.Event()
        self.handles: dict[int, RunHandle] = {}
        self.kills = 0

    async def execute(
        self,
        run: RunRecord,
        workflow: Workflow,
        handle: RunHandle,
        *,
        inputs: JSONObject,
        trigger: JSONObject,
    ) -> RunStatus:
        self.handles[run.id] = handle
        if self.behavior == "raise":
            raise RuntimeError("executor bug")
        if self.behavior == "finalizing":
            handle.finalizing = True
        await self.release.wait()
        now = to_ms(self.clock.now())
        await self.store.write(
            lambda repo: repo.finish_run(
                run.id, RunStatus.SUCCEEDED, at_ms=now, reason=None, error=None
            )
        )
        return RunStatus.SUCCEEDED

    def kill_processes(self) -> int:
        self.kills += 1
        return 0


def step_of(detail: RunDetail, step_id: str) -> StepRecord:
    """Return one step of a stored run."""
    return next(step for step in detail.steps if step.step_id == step_id)


def statuses(detail: RunDetail) -> dict[str, str]:
    """Return each step's status, followed by its skip reason after a colon."""
    return {
        step.step_id: step.status.value + (f":{step.skip_reason.value}" if step.skip_reason else "")
        for step in detail.steps
    }


def call_times(step: FakeStep, step_id: str) -> list[float]:
    """Return the loop times at which one step's attempts started."""
    return [call.time for call in step.calls_for(step_id)]
