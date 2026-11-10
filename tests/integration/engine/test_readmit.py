"""Re-admission at startup with the real workflow registry and the threaded store."""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from tests.support.project import ProjectFactory
from tests.unit.engine.helpers import Engine, Secrets
from tests.unit.store.helpers import new_run

from treadle.clock import SystemClock
from treadle.config import Settings
from treadle.engine.manager import RunManager
from treadle.models import RunReason, RunStatus
from treadle.registry import WorkflowRegistry
from treadle.secrets import SecretSnapshot, SecretStore
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore

WORKFLOW = """
    steps:
      - id: say
        uses: log
        with:
          message: hello
"""


@pytest.fixture
async def threaded_store(settings: Settings) -> AsyncIterator[AsyncStore]:
    opened = await AsyncStore.open(settings)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def engine(
    settings: Settings,
    threaded_store: AsyncStore,
    mock_http: Callable[..., httpx.AsyncClient],
    secret_snapshot: Callable[..., SecretSnapshot],
) -> AsyncIterator[Engine]:
    """An engine with the built-in step types, the system clock, and the threaded store."""
    client = mock_http(lambda request: httpx.Response(599))
    yield Engine(
        store=threaded_store,
        settings=settings,
        steps=builtin_registry(),
        clock=SystemClock(),
        rng=random.Random(7),
        http=client,
        secrets=Secrets(secret_snapshot(), frozenset()),
    )
    await client.aclose()


async def load(settings: Settings, store: AsyncStore) -> WorkflowRegistry:
    """Load the workflows as a starting daemon does."""
    registry = WorkflowRegistry(
        settings=settings,
        steps=builtin_registry(),
        secrets=SecretStore(settings.env_file, {}),
        store=store,
        sinks=(),
        on_removed=None,
        clock=SystemClock(),
    )
    await registry.load_all()
    return registry


@contextlib.asynccontextmanager
async def serving(engine: Engine, registry: WorkflowRegistry) -> AsyncIterator[RunManager]:
    """Run a run manager over `registry` while the block runs, then shut it down."""
    manager = RunManager(
        store=engine.store,
        executor=engine.executor,
        workflows=registry,
        secrets=lambda: engine.secrets.snapshot,
        hidden_env=registry.hidden_env,
        settings=engine.settings,
        clock=engine.clock,
    )
    actor = asyncio.create_task(manager.run())
    try:
        yield manager
    finally:
        await manager.shutdown(grace=0)
        await actor


async def test_a_workflow_deleted_between_sessions_has_its_queued_run_cancelled(
    project: ProjectFactory, settings: Settings, threaded_store: AsyncStore, engine: Engine
) -> None:
    store = threaded_store
    deploy = project.write_workflow("deploy", WORKFLOW)
    project.write_workflow("other", WORKFLOW)
    first_session = await load(settings, store)
    entry = first_session.get("deploy")
    assert entry is not None
    current = entry.current
    assert current is not None
    created = await store.write(lambda repo: repo.create_run(new_run(current)))
    assert created.run is not None
    deploy.unlink()  # while Treadle is stopped

    registry = await load(settings, store)
    async with serving(engine, registry) as manager:
        assert await manager.readmit() == 0

    stored = await store.get_run(created.run.id)
    assert stored is not None
    assert (stored.run.status, stored.run.reason) == (
        RunStatus.CANCELLED,
        RunReason.WORKFLOW_REMOVED,
    )
    assert stored.steps == ()


async def test_a_readmitted_run_of_a_byte_identical_workflow_runs_as_its_own_workflow(
    project: ProjectFactory, settings: Settings, threaded_store: AsyncStore, engine: Engine
) -> None:
    twin = """
        steps:
          - id: say
            uses: log
            with:
              message: "workflow=${{ workflow.id }}"
    """
    project.write_workflow("a", twin)
    project.write_workflow("b", twin)  # the same bytes, so the same digest
    registry = await load(settings, threaded_store)
    entry = registry.get("b")
    assert entry is not None
    later = entry.current
    assert later is not None
    created = await threaded_store.write(lambda repo: repo.create_run(new_run(later)))
    assert created.run is not None
    run_id = created.run.id

    async with serving(engine, registry) as manager:
        assert await manager.readmit() == 1
        status = await manager.wait(run_id)

    assert status is RunStatus.SUCCEEDED
    assert await engine.texts(run_id, step_id="say") == ["[info] workflow=b"]
