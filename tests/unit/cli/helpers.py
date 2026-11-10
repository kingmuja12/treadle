"""Helpers for CLI tests: an in-process `treadle`, workflow texts, and a lock holder."""

from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import random
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import httpx
import pytest
from tests.support.project import ProjectFactory
from tests.unit.store.helpers import Runs, project_workflow

from treadle import __version__
from treadle.cli.main import main
from treadle.clock import SystemClock, iso_utc
from treadle.config import Settings
from treadle.control import ControlPoller
from treadle.engine.executor import Executor, ExecutorDeps
from treadle.engine.manager import RunManager
from treadle.lock import InstanceLock
from treadle.models import (
    CancelOutcome,
    CancelReason,
    ControlRequest,
    ManagerStats,
    RunRequest,
    Submission,
)
from treadle.protocols import RunControl
from treadle.registry import WorkflowRegistry
from treadle.secrets import SecretStore
from treadle.steps.builtin import builtin_registry
from treadle.steps.procs import ProcessRegistry
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository
from treadle.types import JSONObject, JSONValue, LockHolder

T = TypeVar("T")

# A workflow of `log` steps only, so runs need no subprocess.
GREET = """\
name: Greeting
inputs:
  name:
    type: string
    default: world
steps:
  - id: greet
    uses: log
    with:
      message: "Hello, ${{ inputs.name }}!"
  - id: done
    uses: log
    with:
      message: "greeted in run ${{ run.id }}"
"""

# `fetch` fails twice without a subprocess: the copy's source doesn't exist. `report` is then
# skipped, and `alert` runs because it checks failure().
FLAKY = """\
steps:
  - id: fetch
    uses: file
    retry: {attempts: 2, delay: 0s}
    with:
      action: copy
      source: missing.txt
      path: copy.txt
  - id: report
    uses: log
    with:
      message: never printed
  - id: alert
    needs: [fetch]
    if: failure()
    uses: log
    with:
      level: warning
      message: fetch failed
"""

# A run that lasts until something cancels it.
SLOW = """\
concurrency: skip
steps:
  - id: wait
    uses: sleep
    with:
      duration: 30s
"""

COUNT = """\
inputs:
  count:
    type: integer
    required: true
steps:
  - id: show
    uses: log
    with:
      message: "count is ${{ inputs.count }}"
"""

BROKEN = """\
name: Broken
stpes:
  - id: a
    run: echo hi
"""


class HungUp(io.StringIO):
    """A terminal that hung up: every write fails, as after SIGHUP."""

    def write(self, text: str) -> int:
        raise OSError(errno.EIO, "Input/output error")


@dataclass(frozen=True)
class Result:
    """What one `treadle` invocation returned and printed."""

    code: int
    out: str
    err: str

    def json(self) -> Any:
        """Parse stdout as one JSON document."""
        return json.loads(self.out)

    def json_lines(self) -> list[Any]:
        """Parse stdout as JSON Lines."""
        return [json.loads(line) for line in self.out.splitlines() if line.strip()]


class Cli:
    """Runs `treadle -C <project> ...` in-process through `main()` and captures its output."""

    def __init__(self, project: ProjectFactory, capsys: pytest.CaptureFixture[str]) -> None:
        self.project = project
        self._capsys = capsys

    def __call__(self, *args: str) -> Result:
        """Run one command in the project directory."""
        return self.anywhere("-C", str(self.project.root), *args)

    def anywhere(self, *args: str) -> Result:
        """Run one command with exactly these arguments."""
        self._capsys.readouterr()
        code = main(list(args))
        captured = self._capsys.readouterr()
        return Result(code, captured.out, captured.err)


def store_runs(settings: Settings, workflow_id: str, fn: Callable[[Runs], T]) -> T:
    """Open the project's store as its owner and create runs with `fn`, outside any holder."""

    async def seed() -> T:
        store = await AsyncStore.open(settings, inline=True)
        try:
            workflow = project_workflow(settings.project_dir, workflow_id)
            return await store.write(lambda repo: fn(Runs(repo, workflow)))
        finally:
            await store.close()

    return asyncio.run(seed())


def read_store(settings: Settings, fn: Callable[[Repository], T]) -> T:
    """Read the project's database as a client."""

    async def read() -> T:
        store = await AsyncStore.open(settings, mode="client", inline=True)
        try:
            return await store.read(fn)
        finally:
            await store.close()

    return asyncio.run(read())


class InProcessDaemon:
    """A lock holder whose run manager and control poller run on a background event loop.

    The CLI delegates `run` and `cancel` to it as it would to `treadle serve`. It starts no
    trigger and no HTTP server. With `poll=False`, nothing answers control requests until a
    test calls `process_requests`. With `answers`, each request gets the result that the
    function returns instead, like a lock holder that a test scripts. With `delay`, the
    holder claims each request at once but acts on it, and answers, only `delay` seconds
    later, like a slow lock holder.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        kind: Literal["serve", "run"] = "serve",
        poll: bool = True,
        answers: Callable[[ControlRequest], JSONObject] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.settings = settings
        self.pid = os.getpid()
        self._kind = kind
        self._poll = poll
        self._answers = answers
        self._delay = delay
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._poller: ControlPoller | None = None
        self.manager: RunManager | None = None

    def __enter__(self) -> InProcessDaemon:
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("the in-process daemon didn't start within 20 seconds")
        if self._failure is not None:
            raise self._failure
        return self

    def __exit__(self, *exc: object) -> None:
        if self._loop is not None and self._stopping is not None:
            self._loop.call_soon_threadsafe(self._stopping.set)
        self._thread.join(timeout=20)
        if self._thread.is_alive():
            raise RuntimeError("the in-process daemon didn't stop within 20 seconds")

    def call(self, make: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run a coroutine on the daemon's event loop and return its result."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(make(), self._loop).result(timeout=20)

    def process_requests(self) -> int:
        """Answer the pending control requests once, and return how many were handled."""
        assert self._poller is not None
        return self.call(self._poller.process_once)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # reported to the test through __enter__
            self._failure = exc
            self._ready.set()

    async def _serve(self) -> None:
        settings, clock = self.settings, SystemClock()
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        holder = LockHolder(
            self.pid, self._kind, iso_utc(clock.now()), __version__, "127.0.0.1", 8765
        )
        with InstanceLock(settings.state_dir, holder):
            store = await AsyncStore.open(settings)
            try:
                await self._run(store, clock)
            finally:
                await store.close()

    async def _run(self, store: AsyncStore, clock: SystemClock) -> None:
        settings = self.settings
        secrets = SecretStore(settings.env_file, {})
        secrets.load()
        steps = builtin_registry()
        registry = WorkflowRegistry(
            settings=settings,
            steps=steps,
            secrets=secrets,
            store=store,
            sinks=[],
            on_removed=None,
            clock=clock,
        )
        await registry.load_all()
        async with httpx.AsyncClient() as http:
            deps = ExecutorDeps(
                store=store,
                steps=steps,
                settings=settings,
                secrets=lambda: secrets.snapshot,
                hidden_env=registry.hidden_env,
                environ=dict(os.environ),
                clock=clock,
                rng=random.Random(1234),
                processes=ProcessRegistry(),
                http=http,
            )
            manager = RunManager(
                store=store,
                executor=Executor(deps),
                workflows=registry,
                secrets=lambda: secrets.snapshot,
                hidden_env=registry.hidden_env,
                settings=settings,
                clock=clock,
            )
            self.manager = manager
            actor = asyncio.create_task(manager.run())
            control: RunControl = manager if not self._delay else _Delayed(manager, self._delay)
            self._poller = ControlPoller(
                store=store,
                control=control,
                clock=clock,
                accept_runs=self._kind == "serve",
                interval=0.05,
            )
            polling = None
            if self._answers is not None:
                polling = asyncio.create_task(self._answer(store, self._answers))
            elif self._poll:
                polling = asyncio.create_task(self._poller.run())
            self._ready.set()
            assert self._stopping is not None
            await self._stopping.wait()
            if polling is not None:
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
            await manager.shutdown(grace=2.0)
            await actor

    async def _answer(
        self, store: AsyncStore, answers: Callable[[ControlRequest], JSONObject]
    ) -> None:
        """Answer every control request with a scripted result."""
        while True:
            claimed = await store.write(lambda repo: repo.claim_controls(at_ms=0))
            for request in claimed:
                result = answers(request)
                await store.write(
                    lambda repo, request=request, result=result: repo.complete_control(
                        request.id, result, at_ms=0
                    )
                )
            await asyncio.sleep(0.05)


class _Delayed:
    """A RunControl that waits `delay` seconds before it passes each call on."""

    def __init__(self, control: RunControl, delay: float) -> None:
        self._control = control
        self._delay = delay

    async def submit(self, request: RunRequest) -> Submission:
        await asyncio.sleep(self._delay)
        return await self._control.submit(request)

    async def cancel(
        self, run_id: int, reason: CancelReason = CancelReason.USER, *, force: bool = False
    ) -> CancelOutcome:
        await asyncio.sleep(self._delay)
        return await self._control.cancel(run_id, reason, force=force)

    async def rerun(self, run_id: int) -> Submission:
        await asyncio.sleep(self._delay)
        return await self._control.rerun(run_id)

    def stats(self) -> ManagerStats:
        return self._control.stats()


def control_rows(settings: Settings) -> list[dict[str, JSONValue]]:
    """Return every control request as stored: id, kind, consumed, and the parsed result."""
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, consumed_at, result FROM control_requests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row[0],
            "kind": row[1],
            "consumed": row[2] is not None,
            "result": None if row[3] is None else json.loads(row[3]),
        }
        for row in rows
    ]
