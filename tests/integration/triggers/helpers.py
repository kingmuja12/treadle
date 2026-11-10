"""Helpers for real-time trigger tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable


async def eventually(check: Callable[[], bool | Awaitable[bool]], *, within: float = 2.0) -> None:
    """Wait until `check` returns true, polling every 20 ms; fail after `within` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        outcome = check()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        if loop.time() > deadline:
            raise AssertionError(f"the condition didn't hold within {within}s")
        await asyncio.sleep(0.02)
