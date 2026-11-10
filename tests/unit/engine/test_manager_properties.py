"""Concurrency properties of the run manager under randomized bursts at the same instant.

Seeded bursts of submits and cancels hit the actor at one virtual instant each. Across every
seed, at most one run per workflow is active, `max_concurrent` is never exceeded, and a
terminal status never changes.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar, Literal

import pytest
from tests.support.builders import make_step, make_workflow
from tests.unit.engine.helpers import MANUAL, Engine, Manager

from treadle.clock import to_ms
from treadle.config import RunSettings
from treadle.engine.manager import RunManager
from treadle.errors import StepError
from treadle.models import (
    CancelReason,
    Concurrency,
    ConcurrencyPolicy,
    RunRecord,
    RunRequest,
    RunStatus,
    Step,
    TriggerKind,
    Workflow,
)
from treadle.schema import Obj
from treadle.steps.base import StepResult, StepRuntime
from treadle.store.repo import Repository
from treadle.types import Issue, JSONValue, YamlPath

CAP = 2
BURSTS = 40


class Chore:
    """A step type that sleeps a seeded random time, sometimes fails, and checks the cap."""

    name = "chore"
    output_names: ClassVar[frozenset[str] | None] = None
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.manager: RunManager | None = None
        self.over_cap: list[int] = []

    @property
    def schema(self) -> Obj:
        return Obj("chore step", {})

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        assert self.manager is not None
        if self.manager.stats().active > CAP:
            self.over_cap.append(rt.run_id)
        await asyncio.sleep(self.rng.choice([0.0, 0.5, 1.0, 2.0, 5.0]))
        if self.rng.random() < 0.2:
            raise StepError("the chore failed")
        return StepResult()


def workflows() -> list[Workflow]:
    def chores(*, cleanup: bool) -> list[Step]:
        steps = [make_step("work", uses="chore")]
        if cleanup:
            steps.append(make_step("cleanup", uses="chore", condition="always()"))
        return steps

    policies = {
        "queued": (Concurrency(ConcurrencyPolicy.QUEUE, 2), True),
        "long-queue": (Concurrency(ConcurrencyPolicy.QUEUE, 5), False),
        "skipping": (Concurrency(ConcurrencyPolicy.SKIP), False),
        "replacing": (Concurrency(ConcurrencyPolicy.REPLACE), True),
    }
    return [
        make_workflow(*chores(cleanup=cleanup), workflow_id=name, concurrency=concurrency)
        for name, (concurrency, cleanup) in policies.items()
    ]


async def stored_runs(engine: Engine, run_ids: list[int]) -> list[RunRecord]:
    def read(repo: Repository) -> list[RunRecord]:
        found = [repo.get_run(run_id) for run_id in run_ids]
        return [run for run in found if run is not None]

    return await engine.store.read(read)


def burst(
    rng: random.Random, manager: RunManager, names: list[str], seen: list[int]
) -> list[Awaitable[object]]:
    """Return 1-5 commands that the test sends at one virtual instant."""
    commands: list[Awaitable[object]] = []
    for _ in range(rng.randint(1, 5)):
        if seen and rng.random() < 0.35:
            run_id = rng.choice(seen)
            reason = rng.choice([CancelReason.USER, CancelReason.REPLACED])
            commands.append(manager.cancel(run_id, reason, force=rng.random() < 0.3))
        else:
            request = RunRequest(rng.choice(names), TriggerKind.MANUAL, MANUAL)
            commands.append(manager.submit(request))
    return commands


def overlaps(runs: list[RunRecord], now_ms: int) -> tuple[int, int]:
    """Return the most runs active at once overall, and per workflow."""
    intervals = [
        (to_ms(run.started_at), to_ms(run.finished_at) if run.finished_at else now_ms, run)
        for run in runs
        if run.started_at is not None
    ]
    most_overall = most_per_workflow = 0
    for start, _, _ in intervals:
        active = [run for begin, end, run in intervals if begin <= start < end]
        most_overall = max(most_overall, len(active))
        per_workflow: dict[str, int] = {}
        for run in active:
            per_workflow[run.workflow_id] = per_workflow.get(run.workflow_id, 0) + 1
        most_per_workflow = max(most_per_workflow, *per_workflow.values(), 0)
    return most_overall, most_per_workflow


@pytest.mark.parametrize("seed", range(8))
async def test_randomized_bursts_keep_the_admission_invariants(
    make_engine: Callable[..., Engine], seed: int
) -> None:
    rng = random.Random(seed)
    chore = Chore(random.Random(seed + 1000))
    engine = make_engine(chore, runs=RunSettings(max_concurrent=CAP))
    items = workflows()
    names = [workflow.id for workflow in items]
    seen: list[int] = []
    terminal: dict[int, RunStatus] = {}

    async with Manager(engine, *items) as harness:
        manager = harness.manager
        chore.manager = manager
        for _ in range(BURSTS):
            await asyncio.sleep(rng.choice([0.0, 0.5, 1.0, 3.0]))
            results = await asyncio.gather(*burst(rng, manager, names, seen))
            for result in results:
                run = getattr(result, "run", None)
                if run is not None and run.id not in seen:
                    seen.append(run.id)
            for run in await stored_runs(engine, seen):
                if run.status.terminal:
                    assert terminal.setdefault(run.id, run.status) is run.status
        for run_id in seen:
            await manager.wait(run_id)
        final = await stored_runs(engine, seen)

    assert chore.over_cap == []
    assert all(run.status.terminal for run in final)
    for run in final:
        assert terminal.setdefault(run.id, run.status) is run.status
    most_overall, most_per_workflow = overlaps(final, to_ms(engine.clock.now()))
    assert most_overall <= CAP
    assert most_per_workflow <= 1
    assert len(seen) > 20  # the bursts did real work
