"""Re-admission at startup with the real workflow registry and the threaded store."""

from __future__ import annotations

import asyncio
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


async def test_a_workflow_deleted_between_sessions_has_its_queued_run_cancelled(
    project: ProjectFactory,
    settings: Settings,
    threaded_store: AsyncStore,
    mock_http: Callable[..., httpx.AsyncClient],
    secret_snapshot: Callable[..., SecretSnapshot],
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
    client = mock_http(lambda request: httpx.Response(599))
    engine = Engine(
        store=store,
        settings=settings,
        steps=builtin_registry(),
        clock=SystemClock(),
        rng=random.Random(7),
        http=client,
        secrets=Secrets(secret_snapshot(), frozenset()),
    )
    manager = RunManager(
        store=store,
        executor=engine.executor,
        workflows=registry,
        secrets=lambda: engine.secrets.snapshot,
        hidden_env=registry.hidden_env,
        settings=settings,
        clock=engine.clock,
    )
    actor = asyncio.create_task(manager.run())
    try:
        assert await manager.readmit() == 0
    finally:
        await manager.shutdown(grace=0)
        await actor
        await client.aclose()

    stored = await store.get_run(created.run.id)
    assert stored is not None
    assert (stored.run.status, stored.run.reason) == (
        RunStatus.CANCELLED,
        RunReason.WORKFLOW_REMOVED,
    )
    assert stored.steps == ()
