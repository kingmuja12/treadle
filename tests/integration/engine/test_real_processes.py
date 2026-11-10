"""The engine with real processes and threads, real time, and the threaded store."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import os
import random
import signal
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from typing import Any

import httpx
import pytest
from tests.support.project import ProjectFactory
from tests.unit.engine.helpers import Engine, Manager, Secrets, statuses, step_of

from treadle.clock import SystemClock
from treadle.config import Limits, Settings
from treadle.models import (
    AttemptStatus,
    LogStream,
    RunDetail,
    RunReason,
    RunRequest,
    RunStatus,
    StepStatus,
    TriggerKind,
    Workflow,
)
from treadle.secrets import SecretSnapshot
from treadle.steps import file
from treadle.steps.builtin import builtin_registry
from treadle.steps.procs import TRUNCATION_MARKER
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


def with_settings(engine: Engine, **sections: Any) -> Engine:
    """Return an engine like `engine` whose settings replace whole sections."""
    return Engine(
        store=engine.store,
        settings=dataclasses.replace(engine.settings, **sections),
        steps=builtin_registry(),
        clock=engine.clock,
        rng=random.Random(7),
        http=engine.deps.http,
        secrets=engine.secrets,
        environ=ENVIRON,
    )


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


async def test_a_forced_cancel_ends_the_run_without_waiting_out_the_kill_grace(
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
        await harness.manager.cancel(run.id)
        await asyncio.sleep(0.2)
        start = loop.time()
        await harness.manager.cancel(run.id)  # forces it
        status = await harness.manager.wait(run.id)
        took = loop.time() - start

    assert status is RunStatus.CANCELLED
    assert took < 2  # well before the 3-second kill grace
    assert await group_gone(pgid, within=1)  # the force sent SIGKILL at once


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


async def test_a_secret_cut_by_the_line_buffer_leaves_no_prefix_in_storage(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    # Masking the copies before the cut shrinks the line below line_bytes, so the part of
    # the secret that the line buffer kept would be stored unless the sink drops it.
    engine = with_settings(engine, limits=Limits(line_bytes=300))
    workflow = load_workflow(
        """
        steps:
          - id: repeat
            env:
              TOKEN: ${{ secrets.API_TOKEN }}
            run: python3 -c 'import os; print(os.environ["TOKEN"] * 100)'
        """
    )

    detail = await engine.execute(workflow)

    [line] = await engine.texts(detail.run.id, step_id="repeat", stream=LogStream.STDOUT)
    assert line.endswith(TRUNCATION_MARKER)
    assert set(line.removesuffix(TRUNCATION_MARKER)) == {"*"}


async def test_a_burst_of_add_mask_lines_never_stalls_the_event_loop(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: mint
            run: |
              python3 - <<'PY'
              tokens = [f"tok-{index:05d}-0123456789" for index in range(1000)]
              print("\\n".join(f"::add-mask::{token}" for token in tokens))
              print("\\n".join(f"made {token}" for token in tokens))
              PY
        """
    )
    loop = asyncio.get_running_loop()
    stalls: list[float] = []

    async def heartbeat() -> None:
        while True:
            before = loop.time()
            await asyncio.sleep(0.01)
            stalls.append(loop.time() - before)

    beat = asyncio.create_task(heartbeat())
    try:
        detail = await engine.execute(workflow)
    finally:
        beat.cancel()

    assert detail.run.status is RunStatus.SUCCEEDED
    texts = await engine.texts(detail.run.id, step_id="mint", stream=LogStream.STDOUT)
    assert texts == ["::add-mask::***"] * 1000 + ["made ***"] * 1000
    # Compiling the masks once per line stalled the loop for over 7 seconds; once per chunk
    # takes a fraction of that, even under coverage tracing.
    assert max(stalls) < 3


async def test_a_secret_that_the_capture_window_cuts_leaves_no_part_in_storage(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    # The last 80 characters of stdout start 9 characters into the secret.
    engine = with_settings(engine, limits=Limits(stdout_capture_bytes=80))
    workflow = load_workflow(
        """
        steps:
          - id: tail
            env:
              TOKEN: ${{ secrets.API_TOKEN }}
            with:
              capture_stdout: true
            run: |
              python3 -c 'import os; print("x" * 100 + os.environ["TOKEN"] + "y" * 50)'
              python3 -c 'print("z" * 20)'
        """
    )

    detail = await engine.execute(workflow)

    stdout = step_of(detail, "tail").outputs["stdout"]
    assert stdout == "z" * 20
    assert not any(SECRET[start : start + 5] in str(stdout) for start in range(len(SECRET) - 4))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def gone(pid: int, *, within: float = 3.0) -> bool:
    """Wait for a process to exit and be reaped."""
    for _ in range(round(within / 0.05)):
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def child_pid(detail: RunDetail, step_id: str) -> int:
    return int(str(step_of(detail, step_id).outputs["child"]))


async def test_a_background_process_lives_until_its_run_ends(
    engine: Engine, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: start
            run: |
              sleep 30 >/dev/null 2>&1 &
              echo "child=$!" >> "$TREADLE_OUTPUT"
          - id: use
            run: kill -0 "${{ steps.start.outputs.child }}"
        """
    )
    detail: RunDetail | None = None
    try:
        detail = await engine.execute(workflow)

        assert statuses(detail) == {"start": "succeeded", "use": "succeeded"}
        assert await gone(child_pid(detail, "start"))
        texts = await engine.texts(detail.run.id, stream=LogStream.SYSTEM)
        assert (
            "processes that this step started in the background are still running; "
            "Treadle stops them when the run ends"
        ) in texts
        assert "stopped the background processes of 1 step attempt" in texts
        assert len(engine.processes) == 0
        use = await engine.texts(detail.run.id, step_id="use", stream=LogStream.SYSTEM)
        assert not any("background" in text for text in use)
    finally:
        if detail is not None and alive(pid := child_pid(detail, "start")):
            os.kill(pid, signal.SIGKILL)


async def test_a_forced_shutdown_kills_a_background_process_too(
    engine: Engine, engine_settings: Settings, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: start
            run: |
              trap '' TERM
              sleep 30 >/dev/null 2>&1 &
              echo "$!" > "$TREADLE_RUN_TMP/child"
          - id: hang
            run: sleep 100
        """
    )
    pid: int | None = None
    try:
        async with Manager(engine, workflow) as harness:
            run = await harness.submit()
            await wait_for(lambda: recorded_pgid(engine_settings, run.id, "hang") is not None)
            pid = int((engine_settings.tmp_dir / f"run-{run.id}" / "shared" / "child").read_text())
            await harness.manager.shutdown(grace=0.3)

        assert await gone(pid)
    finally:
        if pid is not None and alive(pid):
            os.kill(pid, signal.SIGKILL)


MOVE_WORKFLOW = """
    steps:
      - id: archive
        uses: file
        timeout: 1s
        retry: {attempts: 3, delay: 0s}
        with:
          action: move
          source: incoming.tar
          path: archive/incoming.tar
"""


@pytest.mark.parametrize("fails", [False, True], ids=["finishes", "fails"])
async def test_a_file_operation_that_outlasts_the_step_timeout_isnt_retried(
    engine: Engine,
    engine_settings: Settings,
    load_workflow: Callable[..., Workflow],
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    move = file._HANDLERS["move"]

    def slow_move(target: Any, params: Any, rt: Any) -> tuple[int, bool]:
        time.sleep(1.5)  # a large move across devices, in the step's worker thread
        if fails:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return move(target, params, rt)

    monkeypatch.setitem(file._HANDLERS, "move", slow_move)  # type: ignore[index]
    project = engine_settings.project_dir
    (project / "incoming.tar").write_text("data")

    detail = await engine.execute(load_workflow(MOVE_WORKFLOW))

    archive = step_of(detail, "archive")
    assert (archive.status, archive.error) == (StepStatus.TIMED_OUT, "timed out after 1s")
    assert [attempt.status for attempt in archive.attempts] == [AttemptStatus.TIMED_OUT]
    texts = await engine.texts(detail.run.id, step_id="archive", stream=LogStream.SYSTEM)
    target = project / "archive" / "incoming.tar"
    if fails:
        late = f"cannot move {target}: {os.strerror(errno.ENOSPC)}"
        assert f"the file operation failed after the step was cancelled: {late}" in texts
        assert (project / "incoming.tar").exists()
    else:
        assert "the file operation finished after the step was cancelled" in texts
        assert target.read_text() == "data"
