"""Command helpers: exit codes, run references, and control requests, in virtual time."""

from __future__ import annotations

import asyncio

import pytest
from tests.support.builders import make_run
from tests.support.fakes import MemoryRunReader

from treadle.cli import commands
from treadle.cli.commands import exit_code_for, resolve_run_ref
from treadle.config import Settings
from treadle.errors import NotFoundError, UsageError
from treadle.models import RunStatus
from treadle.store.async_store import AsyncStore
from treadle.types import ExitCode, JSONObject


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (RunStatus.SUCCEEDED, ExitCode.OK),
        (RunStatus.FAILED, ExitCode.FAILURE),
        (RunStatus.CANCELLED, ExitCode.CANCELLED),
        (RunStatus.INTERRUPTED, ExitCode.CANCELLED),
        (RunStatus.TIMED_OUT, ExitCode.TIMED_OUT),
        (RunStatus.SKIPPED, ExitCode.SKIPPED),
    ],
)
def test_each_terminal_status_has_an_exit_code(status: RunStatus, code: ExitCode) -> None:
    assert exit_code_for(status) is code


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING])
def test_an_active_status_has_no_exit_code(status: RunStatus) -> None:
    with pytest.raises(ValueError, match="isn't terminal"):
        exit_code_for(status)


@pytest.fixture
def reader() -> MemoryRunReader:
    return MemoryRunReader(
        make_run(1, workflow_id="greet"),
        make_run(2, workflow_id="sync"),
        make_run(3, workflow_id="greet"),
    )


@pytest.mark.parametrize(
    ("ref", "run_id"),
    [("2", 2), ("#2", 2), (" 41 ", 41), ("last", 3), ("last:sync", 2), ("last:greet", 3)],
)
async def test_run_references(reader: MemoryRunReader, ref: str, run_id: int) -> None:
    assert await resolve_run_ref(ref, reader) == run_id


@pytest.mark.parametrize("ref", ["", "#", "-1", "2.5", "last:", "newest", "#last"])
async def test_malformed_run_references(reader: MemoryRunReader, ref: str) -> None:
    with pytest.raises(UsageError, match="invalid run reference"):
        await resolve_run_ref(ref, reader)


async def test_references_to_runs_that_cant_exist(reader: MemoryRunReader) -> None:
    with pytest.raises(NotFoundError, match="workflow 'other' has no runs"):
        await resolve_run_ref("last:other", reader)
    with pytest.raises(NotFoundError, match="there are no runs yet"):
        await resolve_run_ref("last", MemoryRunReader())
    with pytest.raises(NotFoundError, match="doesn't exist"):
        await resolve_run_ref(str(2**63), reader)


# A control request goes unanswered, or is answered, by a lock holder that these tests play
# themselves on the same virtual-time loop, through the store.

_PAYLOAD: JSONObject = {"workflow": "greet", "inputs": {}, "origin": "cli"}
_ANSWER: JSONObject = {"run_id": 1, "outcome": "queued"}


async def _claim(store: AsyncStore) -> list[int]:
    claimed = await store.write(lambda repo: repo.claim_controls(at_ms=0))
    return [request.id for request in claimed]


async def _answer(store: AsyncStore, request_id: int) -> None:
    await store.write(lambda repo: repo.complete_control(request_id, _ANSWER, at_ms=0))


async def test_an_answer_within_the_wait_is_returned(store: AsyncStore, settings: Settings) -> None:
    async def holder() -> None:
        await asyncio.sleep(1)
        (request_id,) = await _claim(store)
        await _answer(store, request_id)

    answered, _ = await asyncio.gather(
        commands._ask(store, settings, "run", _PAYLOAD, wait=10), holder()
    )

    assert answered == _ANSWER


async def test_an_unanswered_request_is_withdrawn_so_no_holder_acts_on_it(
    store: AsyncStore, settings: Settings
) -> None:
    answered = await commands._ask(store, settings, "run", _PAYLOAD, wait=2)

    assert answered is None
    assert await _claim(store) == []
    result = await store.read(lambda repo: repo.control_result(1))
    assert result is not None
    assert result["error"] == {
        "code": "expired",
        "message": "the CLI stopped waiting for an answer",
        "details": None,
    }


async def test_a_request_claimed_before_the_withdrawal_waits_for_its_answer(
    store: AsyncStore, settings: Settings
) -> None:
    async def slow_holder() -> None:
        await asyncio.sleep(0.5)
        (request_id,) = await _claim(store)
        await asyncio.sleep(5)  # past the CLI's wait, within the extra wait for a claim
        await _answer(store, request_id)

    answered, _ = await asyncio.gather(
        commands._ask(store, settings, "run", _PAYLOAD, wait=2), slow_holder()
    )

    assert answered == _ANSWER


async def test_a_claimed_request_without_an_answer_gives_up_in_the_end(
    store: AsyncStore, settings: Settings
) -> None:
    async def silent_holder() -> None:
        await asyncio.sleep(0.5)
        await _claim(store)

    loop = asyncio.get_running_loop()
    started = loop.time()
    answered, _ = await asyncio.gather(
        commands._ask(store, settings, "cancel", {"run_id": 1}, wait=2), silent_holder()
    )

    assert answered is None
    assert loop.time() - started == pytest.approx(2 + commands._CLAIMED_WAIT, abs=0.5)
    assert await store.read(lambda repo: repo.control_result(1)) is None


async def test_an_interrupted_wait_withdraws_the_request_at_once(
    store: AsyncStore, settings: Settings
) -> None:
    interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, interrupted.set)
    started = loop.time()

    answered = await commands._ask(
        store, settings, "run", _PAYLOAD, wait=10, interrupted=interrupted
    )

    assert answered is None
    assert loop.time() - started < 1
    assert await _claim(store) == []
