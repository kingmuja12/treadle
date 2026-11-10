"""The trigger services on a real event loop, with real threads and the threaded store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.triggers.helpers import eventually
from tests.support.builders import make_run, make_watch, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeSubmitter, RecordingSink
from tests.support.project import ProjectFactory
from tests.unit.store.helpers import new_run
from tests.unit.triggers.helpers import StoreSubmitter, running, schedule, scheduled_slots, write

from treadle.clock import SystemClock
from treadle.config import Limits, Settings
from treadle.models import RunRequest, Submission, SubmitOutcome
from treadle.registry import WorkflowRegistry
from treadle.secrets import SecretStore
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore
from treadle.triggers.scheduler import Scheduler
from treadle.triggers.watch import WatchService

HELLO = "steps:\n  - id: hello\n    uses: log\n    with:\n      message: hi\n"


@pytest.fixture
async def threaded_store(settings: Settings) -> AsyncIterator[AsyncStore]:
    """An owner store with its writer thread and reader pool."""
    store = await AsyncStore.open(settings)
    try:
        yield store
    finally:
        await store.close()


async def test_a_watch_scans_in_a_thread_and_reports_a_new_file(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    write(root / "existing.txt")
    reported = asyncio.Event()

    def record(request: RunRequest) -> Submission:
        reported.set()
        return Submission(SubmitOutcome.QUEUED, make_run())

    submitter = FakeSubmitter(handler=record)
    service = WatchService(submitter=submitter, limits=Limits())
    trigger = make_watch(root, debounce=0.1, poll_interval=0.1)
    service.update("wf", make_workflow(triggers=[trigger]))

    async with running(service.run):
        await asyncio.sleep(0.3)
        write(root / "new.txt")
        await asyncio.wait_for(reported.wait(), timeout=2.0)

    [request] = submitter.requests
    assert request.trigger["paths"] == [str(root / "new.txt")]


async def test_the_registry_hot_reloads_through_threads(
    project: ProjectFactory, settings: Settings, threaded_store: AsyncStore
) -> None:
    secrets = SecretStore(settings.env_file, {})
    secrets.load()
    sink = RecordingSink()
    removed: list[str] = []

    async def on_removed(workflow_id: str) -> None:
        removed.append(workflow_id)

    registry = WorkflowRegistry(
        settings=settings,
        steps=builtin_registry(),
        secrets=secrets,
        store=threaded_store,
        sinks=[sink],
        on_removed=on_removed,
        clock=SystemClock(),
    )
    path = project.write_workflow("hello", HELLO)
    [loaded] = await registry.load_all()
    assert loaded.new is not None
    await threaded_store.write(lambda repo: repo.create_run(new_run(loaded.new)))

    async with running(lambda: registry.run(interval=0.05)):
        path.write_text(HELLO.replace("hi", "hello again"), encoding="utf-8")
        await eventually(lambda: len(sink.changes) == 2)
        stored = await registry.version(loaded.new.digest)
        assert stored is not None
        assert stored.source == HELLO
        path.unlink()
        await eventually(lambda: removed == ["hello"])

    assert sink.changes[-1].new is None
    assert registry.entries() == []


async def test_the_scheduler_fires_a_slot_on_the_wall_clock(threaded_store: AsyncStore) -> None:
    clock = FakeClock(datetime(2026, 11, 9, 11, 59, 59, 600000, tzinfo=UTC))
    trigger = schedule("0 12 * * *")
    workflow = make_workflow(triggers=[trigger])
    submitter = StoreSubmitter(threaded_store, workflow, clock=clock)
    scheduler = Scheduler(store=threaded_store, submitter=submitter, clock=clock)
    scheduler.update("wf", workflow)

    async def slot_stored() -> bool:
        return bool(await scheduled_slots(threaded_store))

    async with running(scheduler.run):
        # The submitter records a request before the writer thread commits its run, so a
        # read right after the request appears can miss the run. Wait for the run itself.
        await eventually(slot_stored, within=3.0)

    assert await scheduled_slots(threaded_store) == [datetime(2026, 11, 9, 12, tzinfo=UTC)]
    assert submitter.requests[0].trigger["catch_up"] is False
