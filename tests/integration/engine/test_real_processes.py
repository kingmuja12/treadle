"""The engine with real shell processes, real time, and the threaded store."""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing

import httpx
import pytest
from tests.support.project import ProjectFactory
from tests.unit.engine.helpers import Engine, Manager, Secrets, statuses, step_of

from treadle.clock import SystemClock
from treadle.config import Settings
from treadle.models import (
    AttemptStatus,
    LogStream,
    RunReason,
    RunRequest,
    RunStatus,
    TriggerKind,
    Workflow,
)
from treadle.secrets import SecretSnapshot
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore
from treadle.types import JSONValue

# Every masked form of this value differs: it has a slash, a space, and a quote.
SECRET = 's3cr3t/Value 42"x'
ENVIRON = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}


@pytest.fixture
def engine_settings(project: ProjectFactory) -> Settings:
    """Project settings with a short kill grace, so tests don't wait 5 seconds."""
    project.write_config('timezone = "UTC"\n\n[runs]\nkill_grace = "3s"\n')
    return project.settings()


@pytest.fixture
async def threaded_store(engine_settings: Settings) -> AsyncIterator[AsyncStore]:
    store = await AsyncStore.open(engine_settings)
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def engine(
    threaded_store: AsyncStore,
    engine_settings: Settings,
    mock_http: Callable[..., httpx.AsyncClient],
    secret_snapshot: Callable[..., SecretSnapshot],
) -> AsyncIterator[Engine]:
    """An engine with the built-in step types, the system clock, and a threaded store."""
    client = mock_http(lambda request: httpx.Response(599))
    yield Engine(
        store=threaded_store,
        settings=engine_settings,
        steps=builtin_registry(),
        clock=SystemClock(),
        rng=random.Random(7),
        http=client,
        secrets=Secrets(secret_snapshot(API_TOKEN=SECRET), frozenset()),
        environ=ENVIRON,
    )
    await client.aclose()


def recorded_pgid(settings: Settings, run_id: int, step_id: str) -> int | None:
    with closing(sqlite3.connect(settings.db_path)) as conn:
        row = conn.execute(
            "SELECT pgid FROM step_attempts WHERE run_id = ? AND step_id = ?", (run_id, step_id)
        ).fetchone()
    return None if row is None else row[0]


async def wait_for(condition: Callable[[], bool], *, within: float = 5.0) -> None:
    """Poll `condition` every 20 ms, failing the test after `within` seconds."""
    for _ in range(round(within / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail("the condition never held")


async def group_gone(pgid: int, *, within: float = 3.0) -> bool:
    """Wait for every member of a process group to be gone and reaped."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # macOS reports zombies this way until they're reaped
            pass
        await asyncio.sleep(0.05)
    return False


async def test_a_cancelled_shell_step_has_its_process_group_killed(
    engine: Engine, engine_settings: Settings, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: work
            run: |
              sleep 100 &
              sleep 100
          - id: cleanup
            if: always()
            run: echo cleaned up
        """
    )

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await wait_for(lambda: len(engine.processes) == 1)
        await wait_for(lambda: recorded_pgid(engine_settings, run.id, "work") is not None)
        pgid = recorded_pgid(engine_settings, run.id, "work")
        assert pgid is not None
        os.killpg(pgid, 0)  # the group is alive while the step runs

        outcome = await harness.manager.cancel(run.id)
        status = await harness.manager.wait(run.id)

    assert outcome.accepted
    assert status is RunStatus.CANCELLED
    assert await group_gone(pgid)
    assert len(engine.processes) == 0
    detail = await engine.detail(run.id)
    assert (detail.run.reason, statuses(detail)) == (
        RunReason.USER,
        {"work": "cancelled", "cleanup": "succeeded"},
    )
    assert step_of(detail, "work").attempts[0].status is AttemptStatus.CANCELLED
    assert await engine.texts(run.id, step_id="cleanup", stream=LogStream.STDOUT) == ["cleaned up"]
    assert not (engine_settings.tmp_dir / f"run-{run.id}").exists()


async def test_a_forced_shutdown_kills_a_process_that_ignores_sigterm(
    engine: Engine, engine_settings: Settings, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: stubborn
            run: |
              trap '' TERM
              sleep 100
        """
    )
    loop = asyncio.get_running_loop()

    async with Manager(engine, workflow) as harness:
        run = await harness.submit()
        await wait_for(lambda: recorded_pgid(engine_settings, run.id, "stubborn") is not None)
        pgid = recorded_pgid(engine_settings, run.id, "stubborn")
        assert pgid is not None
        await asyncio.sleep(0.2)  # let the shell install its trap
        start = loop.time()
        await harness.manager.shutdown(grace=0.3)
        took = loop.time() - start

    assert took < 2.5  # well before the 3-second kill grace
    assert await group_gone(pgid)
    detail = await engine.detail(run.id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)


async def test_a_secret_printed_by_a_real_process_is_masked_in_storage(
    engine: Engine,
    engine_settings: Settings,
    threaded_store: AsyncStore,
    load_workflow: Callable[..., Workflow],
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: leak
            env:
              TOKEN: ${{ secrets.API_TOKEN }}
            run: |
              echo "token is $TOKEN"
              echo "token on stderr $TOKEN" >&2
              python3 - <<'PY'
              import json, os, urllib.parse as u
              t = os.environ["TOKEN"]
              print(json.dumps({"t": t}), u.quote(t, safe=""), u.quote_plus(t))
              PY
              MADE=$(python3 -c 'print("made-" + "at-run-time")')
              echo "::add-mask::$MADE"
              echo "later $MADE"
              echo "result=$TOKEN" >> "$TREADLE_OUTPUT"
              echo "made=$MADE" >> "$TREADLE_OUTPUT"
              cat "$TREADLE_EVENT_PATH"
              echo
              exit 3
        """
    )
    trigger: dict[str, JSONValue] = {"kind": "webhook", "body": {"token": SECRET}}

    async with Manager(engine, workflow) as harness:
        submission = await harness.manager.submit(RunRequest("wf", TriggerKind.WEBHOOK, trigger))
        assert submission.run is not None
        await harness.manager.wait(submission.run.id)
    detail = await engine.detail(submission.run.id)

    leak = step_of(detail, "leak")
    assert leak.outputs == {"exit_code": 3, "result": "***", "made": "***"}
    assert leak.error == "exit code 3"
    assert await engine.texts(detail.run.id, step_id="leak", stream=LogStream.STDOUT) == [
        "token is ***",
        '{"t": "***"} *** ***',
        "::add-mask::***",
        "later ***",
        '{"kind": "webhook", "body": {"token": "***"}}',
        "",
    ]
    assert await engine.texts(detail.run.id, step_id="leak", stream=LogStream.STDERR) == [
        "token on stderr ***"
    ]
    assert detail.run.trigger == {"kind": "webhook", "body": {"token": "***"}}

    await threaded_store.close()  # checkpoints the WAL into the database file
    for path in engine_settings.state_dir.glob("treadle.db*"):
        data = path.read_bytes()
        assert SECRET.encode() not in data
        assert b"made-at-run-time" not in data
