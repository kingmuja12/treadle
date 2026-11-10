from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.support.clock import DEFAULT_START, FakeClock
from tests.support.vtime import VirtualTimeDeadlock, VirtualTimeLoop, current_loop


class InlineExecutor(concurrent.futures.Executor):
    """Runs work synchronously, so a unit test can use an explicit executor without threads."""

    def submit(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_result(fn(*args, **kwargs))
        return future


async def test_time_starts_at_zero_and_sleeps_complete_in_order() -> None:
    loop = current_loop()
    finished: list[tuple[str, float]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        finished.append((name, loop.time()))

    started = time.monotonic()
    assert loop.time() == 0.0
    async with asyncio.TaskGroup() as group:
        group.create_task(sleeper("slow", 3600))
        group.create_task(sleeper("fast", 1))
        group.create_task(sleeper("middle", 90))

    assert finished == [("fast", 1.0), ("middle", 90.0), ("slow", 3600.0)]
    assert time.monotonic() - started < 1.0


async def test_timeout_expires_at_the_exact_virtual_instant() -> None:
    loop = current_loop()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(5) as scope:
            await asyncio.sleep(10)

    assert scope.expired()
    assert loop.time() == 5.0


async def test_call_later_runs_in_time_order_and_fifo_for_ties() -> None:
    loop = current_loop()
    order: list[str] = []
    done = loop.create_future()
    loop.call_later(0.3, order.append, "c")
    loop.call_later(0.1, order.append, "a1")
    loop.call_later(0.2, order.append, "b")
    loop.call_later(0.1, order.append, "a2")
    loop.call_later(0.4, done.set_result, None)

    await done

    assert order == ["a1", "a2", "b", "c"]
    assert loop.time() == pytest.approx(0.4)


async def test_wait_returns_the_first_completed_task_without_waiting_for_others() -> None:
    loop = current_loop()
    fast = asyncio.create_task(asyncio.sleep(2))
    slow = asyncio.create_task(asyncio.sleep(60))

    done, pending = await asyncio.wait({fast, slow}, return_when=asyncio.FIRST_COMPLETED)

    assert (done, pending) == ({fast}, {slow})
    assert loop.time() == 2.0
    slow.cancel()


async def test_to_thread_runs_inline_on_the_loop_thread() -> None:
    assert await asyncio.to_thread(threading.get_ident) == threading.get_ident()
    with pytest.raises(ZeroDivisionError):
        await asyncio.to_thread(divmod, 1, 0)


async def test_an_explicit_executor_is_still_used() -> None:
    loop = current_loop()

    assert await loop.run_in_executor(InlineExecutor(), divmod, 7, 2) == (3, 1)


def test_waiting_with_nothing_scheduled_raises_a_deadlock() -> None:
    loop = VirtualTimeLoop(deadlock_wait=0.01)
    try:
        with pytest.raises(VirtualTimeDeadlock, match="nothing is scheduled"):
            loop.run_until_complete(loop.create_future())
    finally:
        loop.close()


def test_waiting_forever_behind_a_periodic_timer_fails_after_30_days() -> None:
    loop = VirtualTimeLoop()

    def tick() -> None:
        loop.call_later(3600, tick)

    loop.call_later(3600, tick)
    try:
        with pytest.raises(VirtualTimeDeadlock, match="passed 30 days"):
            loop.run_until_complete(loop.create_future())
        assert loop.time() > 30 * 86400
    finally:
        loop.close()


def test_advance_moves_time_forward_only() -> None:
    loop = VirtualTimeLoop()
    try:
        loop.advance(2.5)
        assert loop.time() == 2.5
        with pytest.raises(ValueError, match="backward"):
            loop.advance(-1)
    finally:
        loop.close()


def test_current_loop_requires_the_virtual_loop() -> None:
    async def probe() -> VirtualTimeLoop:
        return current_loop()

    with pytest.raises(TypeError, match="virtual-time event loop"):
        asyncio.run(probe())


async def test_fake_clock_follows_loop_time() -> None:
    clock = FakeClock()
    await asyncio.sleep(5)

    assert clock.now() == DEFAULT_START
    await asyncio.sleep(90)
    assert clock.now() == DEFAULT_START + timedelta(seconds=90)
    assert clock.now().tzinfo is UTC


async def test_fake_clock_jumps_without_moving_loop_time() -> None:
    loop = current_loop()
    clock = FakeClock(datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
    clock.now()

    clock.jump(timedelta(hours=8))
    assert clock.now() == datetime(2026, 3, 8, 14, 0, tzinfo=UTC)
    clock.jump(-30)
    assert clock.now() == datetime(2026, 3, 8, 13, 59, 30, tzinfo=UTC)
    assert loop.time() == 0.0


def test_fake_clock_stands_still_outside_a_loop() -> None:
    clock = FakeClock()

    assert clock.now() == DEFAULT_START
    clock.jump(timedelta(minutes=1))
    assert clock.now() == DEFAULT_START + timedelta(minutes=1)
