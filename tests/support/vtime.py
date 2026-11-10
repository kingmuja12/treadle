"""A virtual-time asyncio event loop, so sleeps, timeouts, and timers run instantly.

The loop wraps a real selector. When the loop would block waiting for a timer, the selector
advances virtual time to that timer instead, so `asyncio.sleep`, `asyncio.timeout`,
`loop.call_later`, and `asyncio.wait` become instant and deterministic while the real
cancellation machinery runs unchanged.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import selectors
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

T = TypeVar("T")

MAX_VIRTUAL_TIME = 30 * 86400.0


class VirtualTimeDeadlock(RuntimeError):
    """The test can never finish.

    Either nothing is scheduled and no I/O arrives, or virtual time passed 30 days while
    periodic timers kept the loop busy.
    """


class _VirtualSelector(selectors.BaseSelector):
    """Delegates to a real selector, but turns a blocking timeout into a virtual-time jump."""

    def __init__(self, advance: Callable[[float], None], deadlock_wait: float) -> None:
        self._inner = selectors.DefaultSelector()
        self._advance = advance
        self._deadlock_wait = deadlock_wait

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._inner.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._inner.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            ready = self._inner.select(self._deadlock_wait)
            if not ready:
                raise VirtualTimeDeadlock(
                    f"nothing is scheduled and no I/O arrived within {self._deadlock_wait:g}s"
                )
            return ready
        self._advance(timeout)
        return []

    def close(self) -> None:
        self._inner.close()

    def get_map(self) -> Mapping[Any, selectors.SelectorKey]:
        return self._inner.get_map()


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock starts at 0.0 and moves only when the loop would wait.

    `deadlock_wait` is how long the loop waits for real I/O, with nothing scheduled, before it
    raises VirtualTimeDeadlock.
    """

    def __init__(self, *, deadlock_wait: float = 5.0) -> None:
        self._virtual_now = 0.0
        super().__init__(_VirtualSelector(self.advance, deadlock_wait))

    def time(self) -> float:
        """Return virtual seconds since the loop was created."""
        return self._virtual_now

    def advance(self, seconds: float) -> None:
        """Move virtual time forward. Raises VirtualTimeDeadlock past 30 days."""
        if seconds < 0:
            raise ValueError("virtual time can't move backward")
        self._virtual_now += seconds
        if self._virtual_now > MAX_VIRTUAL_TIME:
            raise VirtualTimeDeadlock(
                f"virtual time passed 30 days ({self._virtual_now:.0f}s); the test waits forever"
            )

    def run_in_executor(
        self,
        executor: concurrent.futures.Executor | None,
        func: Callable[..., T],
        *args: Any,
    ) -> asyncio.Future[T]:
        """Run `func` inline when `executor` is None, so `asyncio.to_thread` is deterministic."""
        if executor is not None:
            return super().run_in_executor(executor, func, *args)
        future: asyncio.Future[T] = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


def current_loop() -> VirtualTimeLoop:
    """Return the running loop, which must be a VirtualTimeLoop."""
    loop = asyncio.get_running_loop()
    if not isinstance(loop, VirtualTimeLoop):
        raise TypeError("this test needs the virtual-time event loop (tests/unit)")
    return loop
