"""`LocalRunner`: a foreground run with real processes, threads, and a second process.

Also what a local run leaves behind: no step process after a forced cancel, no cookies in
the shared HTTP client, and none of the daemon's HTTP stack imported.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from tests.integration.daemon.conftest import STUBBORN
from tests.integration.daemon.helpers import (
    child_environ,
    kill_unfinished_steps,
    surviving_process_groups,
)
from tests.integration.triggers.helpers import eventually
from tests.support.project import ProjectFactory
from tests.unit.store.helpers import DAY, Runs, project_workflow

from treadle import runner as runner_module
from treadle.clock import SystemClock, to_ms
from treadle.config import Settings
from treadle.errors import LockHeldError, NotFoundError
from treadle.lock import InstanceLock
from treadle.models import RunReason, RunStatus
from treadle.runner import LocalRunner
from treadle.steps.procs import ProcessRegistry
from treadle.store.async_store import AsyncStore


@pytest.fixture(autouse=True)
def _kill_leftover_steps(daemon_project: ProjectFactory) -> Iterator[None]:
    """Kill the step process groups of attempts that never finished, even after a failure."""
    yield
    kill_unfinished_steps(daemon_project.settings().db_path)


async def _seed(settings: Settings, workflow_id: str, make: Callable[[Runs], object]) -> None:
    """Create runs in the project's database before any lock holder opens it."""
    store = await AsyncStore.open(settings)
    try:
        workflow = project_workflow(settings.project_dir, workflow_id)
        await store.write(lambda repo: make(Runs(repo, workflow)))
    finally:
        await store.close()


async def test_a_run_executes_in_this_process_and_frees_the_lock(
    daemon_project: ProjectFactory,
) -> None:
    settings = daemon_project.settings()

    async with LocalRunner(settings) as runner:
        holder = InstanceLock.probe(settings.state_dir)
        submission = await runner.start("hello", {"name": "Ada"})
        assert submission.run is not None
        status = await runner.wait(submission.run.id)
        lines = await runner.store.logs(submission.run.id)

    assert holder is not None
    assert holder.kind == "run"
    assert status is RunStatus.SUCCEEDED
    assert [line.text for line in lines] == ["Hello, Ada!"]
    assert InstanceLock.probe(settings.state_dir) is None
    assert list(settings.tmp_dir.iterdir()) == []
    with pytest.raises(RuntimeError, match="isn't open"):
        _ = runner.store


async def test_a_cancel_from_another_process_reaches_the_run(
    daemon_project: ProjectFactory,
) -> None:
    settings = daemon_project.settings()

    async with LocalRunner(settings) as runner:
        submission = await runner.start("nap", {})
        assert submission.run is not None
        run_id = submission.run.id
        await eventually((daemon_project.root / f"nap-{run_id}.pid").exists, within=20)
        cancel = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "treadle",
            "-C",
            str(daemon_project.root),
            "cancel",
            str(run_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environ(),
        )
        out, err = await asyncio.wait_for(cancel.communicate(), 30)
        status = await asyncio.wait_for(runner.wait(run_id), 30)
        detail = await runner.store.get_run(run_id)

    assert (cancel.returncode, out.decode(), err.decode()) == (
        0,
        f"Cancel requested for run #{run_id}.\n",
        "",
    )
    assert status is RunStatus.CANCELLED
    assert detail is not None
    assert detail.run.reason is RunReason.USER
    assert surviving_process_groups(settings.db_path) == []


async def test_a_forced_cancel_leaves_no_process_behind_once_the_runner_closes(
    daemon_project: ProjectFactory,
) -> None:
    daemon_project.write_workflow("stubborn", STUBBORN)
    daemon_project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "30s"\n')
    settings = daemon_project.settings()
    loop = asyncio.get_running_loop()

    async with LocalRunner(settings) as runner:
        submission = await runner.start("stubborn", {})
        assert submission.run is not None
        run_id = submission.run.id
        await eventually((daemon_project.root / "stubborn.pid").exists, within=20)
        await runner.cancel(run_id)
        await asyncio.sleep(0.3)
        await runner.cancel(run_id, force=True)
        status = await asyncio.wait_for(runner.wait(run_id), 10)
        started = loop.time()

    # The step ignores SIGTERM and has 30 seconds of kill grace, yet nothing of it is left,
    # and closing doesn't wait out the grace.
    assert status is RunStatus.CANCELLED
    assert loop.time() - started < 5
    assert surviving_process_groups(settings.db_path) == []


async def test_kill_processes_sends_sigkill_to_the_groups_that_steps_track(
    daemon_project: ProjectFactory,
) -> None:
    daemon_project.write_workflow("stubborn", STUBBORN)
    daemon_project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "30s"\n')
    settings = daemon_project.settings()

    async with LocalRunner(settings) as runner:
        submission = await runner.start("stubborn", {})
        assert submission.run is not None
        run_id = submission.run.id
        await eventually((daemon_project.root / "stubborn.pid").exists, within=20)
        killed = runner.kill_processes()
        status = await asyncio.wait_for(runner.wait(run_id), 20)
        detail = await runner.store.get_run(run_id)
    after_closing = runner.kill_processes()

    # The step ignores SIGTERM, but no cancel was needed: SIGKILL ended its shell at once.
    assert killed == 1
    assert status is RunStatus.FAILED
    assert detail is not None
    assert detail.steps[0].error == "exit code 137"
    assert after_closing == 0
    assert surviving_process_groups(settings.db_path) == []


async def test_closing_inside_the_block_frees_the_lock_and_the_block_end_does_nothing(
    daemon_project: ProjectFactory,
) -> None:
    settings = daemon_project.settings()

    async with LocalRunner(settings) as runner:
        await runner.aclose()
        holder = InstanceLock.probe(settings.state_dir)
        await runner.aclose()

    assert holder is None
    assert not settings.holder_path.exists()
    with pytest.raises(RuntimeError, match="isn't open"):
        _ = runner.store


async def test_a_tracked_group_that_no_step_forgets_delays_closing_only_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "_LEFTOVER_WAIT", 0.3)
    gone = await asyncio.create_subprocess_exec("true")
    await gone.wait()
    processes = ProcessRegistry()
    processes.add(gone.pid)  # tracked, but the group has no members
    loop = asyncio.get_running_loop()
    started = loop.time()

    await runner_module._kill_leftovers(processes)

    assert 0.25 < loop.time() - started < 2


async def test_a_tracked_group_that_is_still_alive_gets_sigkill(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(runner_module, "_LEFTOVER_WAIT", 0.3)
    alive = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    processes = ProcessRegistry()
    processes.add(alive.pid)  # a group that a step still tracks after its run ended
    caplog.set_level(logging.INFO, logger="treadle.runner")
    try:
        await runner_module._kill_leftovers(processes)
        code = await asyncio.wait_for(alive.wait(), 5)
    finally:
        if alive.returncode is None:
            alive.kill()
            await alive.wait()

    assert code == -signal.SIGKILL
    assert caplog.messages == ["killed 1 process group that cancelled steps left running"]


async def test_the_shared_http_client_keeps_no_cookies(
    daemon_project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nSet-Cookie: session=abc; Path=/\r\n"
            b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    daemon_project.write_workflow(
        "fetch",
        f"steps:\n  - id: get\n    uses: http\n    with:\n      url: http://127.0.0.1:{port}/\n",
    )
    clients: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.__init__

    def recording(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording)

    try:
        async with LocalRunner(daemon_project.settings()) as runner:
            submission = await runner.start("fetch", {})
            assert submission.run is not None
            status = await runner.wait(submission.run.id)
    finally:
        server.close()
        await server.wait_closed()

    assert status is RunStatus.SUCCEEDED
    assert len(clients) == 1
    assert list(clients[0].cookies.jar) == []


def test_a_local_run_doesnt_import_the_http_stack(daemon_project: ProjectFactory) -> None:
    script = textwrap.dedent(
        """
        import sys
        from treadle.cli.main import main

        code = main(["-C", sys.argv[1], "run", "hello"])
        stack = ("uvicorn", "starlette", "jinja2")
        imported = sorted(name for name in stack if name in sys.modules)
        print(code, imported)
        """
    )

    done = subprocess.run(
        [sys.executable, "-c", script, str(daemon_project.root)],
        capture_output=True,
        text=True,
        env=child_environ(),
        timeout=60,
        check=False,
    )

    assert done.stdout.splitlines()[-1] == "0 []", done.stderr


async def test_opening_recovers_crashed_runs_and_leaves_queued_ones(
    daemon_project: ProjectFactory,
) -> None:
    settings = daemon_project.settings()
    await _seed(settings, "hello", lambda runs: (runs.running(), runs.queued()))
    leftover = settings.tmp_dir / "run-1"
    leftover.mkdir(parents=True)
    stray = settings.tmp_dir / "run-2"
    stray.write_text("not a directory")

    async with LocalRunner(settings) as runner:
        crashed = await runner.store.get_run(1)
        queued = await runner.store.get_run(2)
        busy = LocalRunner(settings)
        with pytest.raises(LockHeldError):
            await busy.__aenter__()

    assert crashed is not None
    assert queued is not None
    assert (crashed.run.status, crashed.run.reason) == (
        RunStatus.INTERRUPTED,
        RunReason.EXECUTOR_STOPPED,
    )
    assert queued.run.status is RunStatus.QUEUED
    assert not leftover.exists()
    assert not stray.exists()


async def test_closing_runs_a_bounded_retention_pass(daemon_project: ProjectFactory) -> None:
    settings = daemon_project.settings()
    old = to_ms(SystemClock().now()) - 40 * DAY

    def seed(runs: Runs) -> None:
        for _ in range(620):
            runs.finished(RunStatus.SUCCEEDED, at_ms=old)

    await _seed(settings, "hello", seed)

    async with LocalRunner(settings):
        pass

    conn = sqlite3.connect(settings.db_path)
    try:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    finally:
        conn.close()
    assert remaining == 620 - 5 * 100  # five batches of 100, then the next run's turn


async def test_the_runner_must_be_open_to_run_and_opens_only_once(
    daemon_project: ProjectFactory,
) -> None:
    runner = LocalRunner(daemon_project.settings())

    with pytest.raises(RuntimeError, match="isn't open"):
        await runner.start("hello", {})
    await runner.__aexit__(None, None, None)  # closing a runner that isn't open does nothing
    async with runner:
        with pytest.raises(RuntimeError, match="already open"):
            await runner.__aenter__()
        with pytest.raises(NotFoundError, match="run 99 doesn't exist"):
            await runner.cancel(99)


async def test_a_failed_retention_pass_only_warns(
    daemon_project: ProjectFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the disk is full")

    monkeypatch.setattr("treadle.runner.run_retention", broken)

    async with LocalRunner(daemon_project.settings()):
        pass

    assert "retention failed; the next run or the daemon tries again" in caplog.text
