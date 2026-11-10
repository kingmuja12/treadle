"""The control channel between two real processes, as `treadle cancel` and the daemon use it."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

from tests.support.fakes import FakeRunControl
from tests.support.project import ProjectFactory
from tests.unit.triggers.helpers import running

from treadle.clock import SystemClock
from treadle.config import Settings
from treadle.control import ControlPoller
from treadle.models import CancelReason
from treadle.store.async_store import AsyncStore

# A CLI process: it opens the store as a client, enqueues a request, and polls for the result.
CLIENT = textwrap.dedent(
    """
    import asyncio, json, sys
    from pathlib import Path
    from treadle.clock import SystemClock, to_ms
    from treadle.config import Overrides, load_settings
    from treadle.store.async_store import AsyncStore

    async def main():
        settings = load_settings(Overrides(), cwd=Path(sys.argv[1]), environ={})
        store = await AsyncStore.open(settings, mode="client")
        kind, payload = sys.argv[2], json.loads(sys.argv[3])
        at_ms = to_ms(SystemClock().now())
        request_id = await store.write(
            lambda repo: repo.enqueue_control(kind, payload, at_ms=at_ms)
        )
        try:
            for _ in range(200):
                result = await store.read(lambda repo: repo.control_result(request_id))
                if result is not None:
                    print(json.dumps(result))
                    return
                await asyncio.sleep(0.02)
            print("no result")
        finally:
            await store.close()

    asyncio.run(main())
    """
)


async def ask(project: ProjectFactory, kind: str, payload: dict[str, object]) -> object:
    """Run the client process and return the result it printed."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CLIENT,
        str(project.root),
        kind,
        json.dumps(payload),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


async def test_another_process_cancels_and_starts_runs_through_the_store(
    project: ProjectFactory, settings: Settings
) -> None:
    store = await AsyncStore.open(settings)
    control = FakeRunControl()
    poller = ControlPoller(
        store=store, control=control, clock=SystemClock(), accept_runs=True, interval=0.05
    )
    try:
        async with running(poller.run):
            cancelled = await ask(project, "cancel", {"run_id": 42, "force": True})
            started = await ask(project, "run", {"workflow": "hello", "inputs": {"n": "3"}})
    finally:
        await store.close()

    assert cancelled == {"run_id": 42, "accepted": True, "status": "running", "forced": True}
    assert started == {"run_id": 1, "outcome": "queued"}
    assert control.cancels == [(42, CancelReason.USER, True)]
    assert control.requests[0].inputs == {"n": "3"}


async def test_a_foreground_run_answers_run_requests_from_another_process_as_busy(
    project: ProjectFactory, settings: Settings
) -> None:
    store = await AsyncStore.open(settings)
    poller = ControlPoller(
        store=store,
        control=FakeRunControl(),
        clock=SystemClock(),
        accept_runs=False,
        interval=0.05,
    )
    try:
        async with running(poller.run):
            result = await ask(project, "run", {"workflow": "hello"})
    finally:
        await store.close()

    assert isinstance(result, dict)
    assert result["error"]["code"] == "busy"
