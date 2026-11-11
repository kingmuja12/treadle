"""A forced cancel stops a `shell` step's process group at once, under virtual time.

That holds for a running step's group, and for a group that an earlier step left running in
the background. The step's process is faked where Treadle meets the operating system, at
`_spawn` and `os.killpg`, so the real executor, `shell` step, and process code run while
virtual time drives every wait.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from tests.support.fakes import FakeStep, Hang
from tests.support.vtime import current_loop
from tests.unit.engine.helpers import Engine, Manager, statuses

from treadle.config import RunSettings
from treadle.models import CancelReason, RunReason, RunStatus, Workflow
from treadle.steps import procs

KILL_GRACE = 30.0


class StubbornGroup:
    """A step's process group, faked at `os.killpg`, whose members ignore SIGTERM.

    It stands in for the process that a `shell` step starts. SIGKILL ends the group: both
    pipes reach their end, and the leader's exit status is -SIGKILL. `signals` holds each
    signal, other than 0, that reached the group while it lived.
    """

    # No process group can have this ID, so a signal that the real `os.killpg` sends it
    # after the test can't reach a real process.
    pid = 2**31 - 1

    def __init__(self) -> None:
        self.signals: list[int] = []
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._ended = asyncio.Event()

    async def spawn(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> StubbornGroup:
        """Start the group, in place of `treadle.steps.procs._spawn`."""
        return self

    def killpg(self, pgid: int, sig: int) -> None:
        """Signal the group, in place of `os.killpg`."""
        if pgid != self.pid or self._ended.is_set():
            raise ProcessLookupError(pgid)
        if sig != 0:
            self.signals.append(sig)
        if sig == signal.SIGKILL:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._ended.set()

    async def wait(self) -> int:
        """Return the leader's exit status once the group has ended."""
        await self._ended.wait()
        return -signal.SIGKILL


class LingeringGroup(StubbornGroup):
    """A stubborn group whose leader exits at once, with 0, and leaves a member behind.

    It stands in for a `shell` step such as `server >/dev/null 2>&1 &`: the step succeeds,
    and the run keeps tracking the group, which ignores SIGTERM, until the run ends.
    """

    async def spawn(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> LingeringGroup:
        """Start the group, whose pipes are closed already, in place of `procs._spawn`."""
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        return self

    async def wait(self) -> int:
        """Return the exit status of the leader, which didn't wait for the group."""
        return 0


def faked(monkeypatch: pytest.MonkeyPatch, group: StubbornGroup) -> StubbornGroup:
    """Make every `shell` step of the test start `group` in place of a process."""
    monkeypatch.setattr(procs, "_spawn", group.spawn)
    monkeypatch.setattr(os, "killpg", group.killpg)
    return group


@pytest.fixture
async def group(monkeypatch: pytest.MonkeyPatch) -> StubbornGroup:
    """The stubborn group that the test's `shell` step starts in place of a process."""
    return faked(monkeypatch, StubbornGroup())


@pytest.fixture
async def lingering(monkeypatch: pytest.MonkeyPatch) -> StubbornGroup:
    """The group that the test's `shell` step leaves running in the background."""
    return faked(monkeypatch, LingeringGroup())


@pytest.fixture
def engine(make_engine: Callable[..., Engine]) -> Engine:
    """An engine whose steps' processes get 30 seconds between SIGTERM and SIGKILL."""
    return make_engine(runs=RunSettings(kill_grace=KILL_GRACE))


def stubborn_workflow(
    load_workflow: Callable[..., Workflow],
    *,
    condition: str = "success()",
    cleanup_timeout: str = "5m",
) -> Workflow:
    """Return a workflow whose one `shell` step ignores SIGTERM.

    With `condition="always()"`, the step is a cleanup step, which a cancel leaves running.
    """
    return load_workflow(
        f"""
        cleanup_timeout: {cleanup_timeout}
        steps:
          - id: stubborn
            if: {condition}
            run: trap '' TERM; sleep 30
        """
    )


@pytest.mark.parametrize("condition", ["success()", "always()"], ids=["step", "cleanup-step"])
async def test_a_single_forced_cancel_kills_a_running_step_at_once(
    engine: Engine, load_workflow: Callable[..., Workflow], group: StubbornGroup, condition: str
) -> None:
    launched = await engine.launch(stubborn_workflow(load_workflow, condition=condition))
    await asyncio.sleep(5)
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER, force=True)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took < 1  # one poll of the group, not the 30-second kill grace
    assert group.signals == [signal.SIGKILL]
    assert (detail.run.status, statuses(detail)) == (RunStatus.CANCELLED, {"stubborn": "cancelled"})


async def test_a_forced_cancel_after_a_cancel_kills_a_running_cleanup_step_at_once(
    engine: Engine, load_workflow: Callable[..., Workflow], group: StubbornGroup
) -> None:
    launched = await engine.launch(stubborn_workflow(load_workflow, condition="always()"))
    await asyncio.sleep(5)
    launched.handle.request_cancel(CancelReason.USER)
    await asyncio.sleep(5)
    assert group.signals == []  # the cancel left the cleanup step running
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER, force=True)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took < 1
    assert group.signals == [signal.SIGKILL]
    assert statuses(detail) == {"stubborn": "cancelled"}


async def test_the_cleanup_timeout_kills_a_running_cleanup_step_at_once(
    engine: Engine, load_workflow: Callable[..., Workflow], group: StubbornGroup
) -> None:
    workflow = stubborn_workflow(load_workflow, condition="always()", cleanup_timeout="10s")
    launched = await engine.launch(workflow)
    await asyncio.sleep(5)
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took < 10 + 1  # the cleanup timeout, then one poll of the group
    assert group.signals == [signal.SIGKILL]
    assert statuses(detail) == {"stubborn": "cancelled"}


async def test_a_forced_cancel_kills_a_step_that_is_already_stopping_at_once(
    engine: Engine, load_workflow: Callable[..., Workflow], group: StubbornGroup
) -> None:
    launched = await engine.launch(stubborn_workflow(load_workflow))
    await asyncio.sleep(5)
    launched.handle.request_cancel(CancelReason.USER)
    await asyncio.sleep(5)
    assert group.signals == [signal.SIGTERM]  # which the group ignores
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER, force=True)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took < 1
    assert group.signals == [signal.SIGTERM, signal.SIGKILL]
    assert statuses(detail) == {"stubborn": "cancelled"}


async def test_a_cancel_that_isnt_forced_waits_out_the_kill_grace(
    engine: Engine, load_workflow: Callable[..., Workflow], group: StubbornGroup
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: stubborn
            run: trap '' TERM; sleep 30
          - id: cleanup
            if: always()
            uses: fake
        """
    )
    launched = await engine.launch(workflow)
    await asyncio.sleep(5)
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took == pytest.approx(KILL_GRACE, abs=0.5)
    assert group.signals == [signal.SIGTERM, signal.SIGKILL]
    assert statuses(detail) == {"stubborn": "cancelled", "cleanup": "succeeded"}


async def test_the_forced_stage_of_a_shutdown_kills_a_running_cleanup_step_at_once(
    engine: Engine,
    load_workflow: Callable[..., Workflow],
    group: StubbornGroup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The shutdown's own SIGKILL can miss a member that is being forked as it lands, so the
    # step's stop must not wait out the kill grace either. Without that SIGKILL, only the
    # step's stop ends the group.
    monkeypatch.setattr(engine.executor, "kill_processes", lambda: 0)
    loop = current_loop()

    async with Manager(engine, stubborn_workflow(load_workflow, condition="always()")) as harness:
        run = await harness.submit()
        await asyncio.sleep(5)
        start = loop.time()
        await harness.manager.shutdown(grace=15)
        took = loop.time() - start

    assert took < 15 + 1  # the shutdown grace, then one poll of the group
    assert group.signals == [signal.SIGKILL]
    detail = await engine.detail(run.id)
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    assert statuses(detail) == {"stubborn": "cancelled"}


def leaving_workflow(load_workflow: Callable[..., Workflow]) -> Workflow:
    """Return a workflow whose `shell` step leaves a process in the background for `next`."""
    return load_workflow(
        """
        steps:
          - id: leave
            run: sleep 30 >/dev/null 2>&1 &
          - id: next
            uses: fake
        """
    )


async def test_a_forced_cancel_kills_a_background_process_of_an_earlier_step_at_once(
    engine: Engine,
    load_workflow: Callable[..., Workflow],
    fake: FakeStep,
    lingering: StubbornGroup,
) -> None:
    fake.script("next", Hang())
    launched = await engine.launch(leaving_workflow(load_workflow))
    await asyncio.sleep(5)
    assert lingering.signals == []  # the group outlived its step, and the run tracks it
    start = current_loop().time()

    launched.handle.request_cancel(CancelReason.USER, force=True)
    detail = await engine.finish(launched)
    took = current_loop().time() - start

    assert took < 1  # not the 30-second kill grace
    assert lingering.signals == [signal.SIGKILL]
    assert statuses(detail) == {"leave": "succeeded", "next": "cancelled"}


async def test_a_background_process_gets_the_kill_grace_when_the_run_isnt_forced(
    engine: Engine, load_workflow: Callable[..., Workflow], lingering: StubbornGroup
) -> None:
    start = current_loop().time()

    detail = await engine.execute(leaving_workflow(load_workflow))
    took = current_loop().time() - start

    assert took == pytest.approx(KILL_GRACE, abs=0.5)
    assert lingering.signals == [signal.SIGTERM, signal.SIGKILL]
    assert statuses(detail) == {"leave": "succeeded", "next": "succeeded"}
