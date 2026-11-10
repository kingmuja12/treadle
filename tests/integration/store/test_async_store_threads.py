from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable

import pytest
from tests.support.builders import FIXED_TIME

from treadle.clock import to_ms
from treadle.config import Settings
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository
from treadle.types import JSONObject

T0 = to_ms(FIXED_TIME)
WAIT = 5.0


@pytest.fixture
async def threaded(settings: Settings) -> AsyncIterator[AsyncStore]:
    """An owner store with its writer thread and reader pool."""
    store = await AsyncStore.open(settings)
    try:
        yield store
    finally:
        await store.close()


def enqueue(n: int) -> Callable[[Repository], tuple[str, int]]:
    """Return a write that enqueues request `n` and reports its thread and request ID."""

    def write(repo: Repository) -> tuple[str, int]:
        request_id = repo.enqueue_control("run", {"n": n}, at_ms=T0)
        return threading.current_thread().name, request_id

    return write


def pool(thread_name: str) -> str:
    """Return the pool prefix of a ThreadPoolExecutor thread name, such as `x_0`."""
    return thread_name.rsplit("_", 1)[0]


async def test_writes_run_on_one_thread_in_submission_order(threaded: AsyncStore) -> None:
    results = await asyncio.gather(*(threaded.write(enqueue(n)) for n in range(50)))

    assert {pool(name) for name, _ in results} == {"treadle-db-write"}
    assert [request_id for _, request_id in results] == list(range(1, 51))
    claimed = await threaded.write(lambda repo: repo.claim_controls(at_ms=T0, limit=100))
    assert [request.payload for request in claimed] == [{"n": n} for n in range(50)]


async def test_reads_run_on_reader_threads(threaded: AsyncStore) -> None:
    names = await asyncio.gather(
        *(threaded.read(lambda repo: threading.current_thread().name) for _ in range(10))
    )

    assert {pool(name) for name in names} == {"treadle-db-read"}


async def test_a_cancelled_caller_lets_its_write_commit(threaded: AsyncStore) -> None:
    started, release = threading.Event(), threading.Event()

    def slow_write(repo: Repository) -> int:
        started.set()
        release.wait(WAIT)
        return repo.enqueue_control("run", {"slow": True}, at_ms=T0)

    caller = asyncio.create_task(threaded.write(slow_write))
    assert await asyncio.to_thread(started.wait, WAIT)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()

    # The writer runs jobs in order, so this claim sees the cancelled caller's commit.
    claimed = await threaded.write(lambda repo: repo.claim_controls(at_ms=T0))
    assert [request.payload for request in claimed] == [{"slow": True}]


async def test_a_read_keeps_its_snapshot_while_a_write_commits(threaded: AsyncStore) -> None:
    request_id = await threaded.write(lambda repo: repo.enqueue_control("run", {}, at_ms=T0))
    reading, committed = threading.Event(), threading.Event()

    def read_twice(repo: Repository) -> tuple[JSONObject | None, JSONObject | None]:
        before = repo.control_result(request_id)
        reading.set()
        committed.wait(WAIT)
        return before, repo.control_result(request_id)

    reader = asyncio.create_task(threaded.read(read_twice))
    assert await asyncio.to_thread(reading.wait, WAIT)
    await threaded.write(lambda repo: repo.complete_control(request_id, {"ok": True}, at_ms=T0))
    committed.set()

    assert await reader == (None, None)
    assert await threaded.read(lambda repo: repo.control_result(request_id)) == {"ok": True}


async def test_close_finishes_submitted_writes_then_checkpoints(settings: Settings) -> None:
    store = await AsyncStore.open(settings)
    writes = [asyncio.create_task(store.write(enqueue(n))) for n in range(20)]
    await asyncio.sleep(0)  # each task submits its job to the writer thread

    await store.close()

    assert [(await write)[1] for write in writes] == list(range(1, 21))
    wal = settings.db_path.with_name("treadle.db-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    client = await AsyncStore.open(settings, mode="client")
    try:
        claimed = await client.write(lambda repo: repo.claim_controls(at_ms=T0))
        assert len(claimed) == 20
    finally:
        await client.close()
