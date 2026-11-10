"""Seams for time and randomness, and helpers for UTC timestamps.

Code reads wall time through a `Clock` and blocks through a `BlockingRunner`, so tests can
swap in a fake clock and run blocking work inline. Sleeps and timeouts use asyncio directly;
tests make them deterministic with a virtual-time event loop.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import UTC as UTC
from datetime import datetime, timedelta, tzinfo
from typing import Any, Final, Protocol, TypeVar

T = TypeVar("T")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND: Final = timedelta(milliseconds=1)


class Clock(Protocol):
    """A source of wall-clock time."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""


class SystemClock:
    """The real wall clock."""

    def now(self) -> datetime:
        """Return `datetime.now(UTC)`."""
        return datetime.now(UTC)


class RandomSource(Protocol):
    """A source of uniform random floats, such as `random.Random`."""

    def random(self) -> float:
        """Return a float in [0.0, 1.0)."""


def default_random() -> RandomSource:
    """Return a `random.Random` seeded by the operating system."""
    return random.Random()  # noqa: S311 - retry jitter needs no cryptographic randomness


class BlockingRunner(Protocol):
    """Runs a blocking function without blocking the event loop, and returns its result."""

    async def __call__(self, fn: Callable[..., T], /, *args: Any) -> T:
        """Call `fn(*args)` and return its result, re-raising any exception."""


async def thread_runner(fn: Callable[..., T], /, *args: Any) -> T:
    """Run `fn(*args)` in a worker thread with `asyncio.to_thread`."""
    return await asyncio.to_thread(fn, *args)


async def inline_runner(fn: Callable[..., T], /, *args: Any) -> T:
    """Run `fn(*args)` on the event-loop thread. Tests use it for deterministic ordering."""
    return fn(*args)


def to_ms(dt: datetime) -> int:
    """Return epoch milliseconds for a timezone-aware datetime, rounding down."""
    return (dt - _EPOCH) // _MILLISECOND


def from_ms(ms: int) -> datetime:
    """Return the timezone-aware UTC datetime for epoch milliseconds."""
    return _EPOCH + timedelta(milliseconds=ms)


def iso_utc(dt: datetime) -> str:
    """Format a timezone-aware datetime as ISO 8601 UTC with milliseconds and a `Z` suffix."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: str, *, default_tz: tzinfo = UTC) -> datetime:
    """Parse an ISO 8601 date or datetime and return it in UTC.

    Accepts a `Z` suffix, numeric offsets, and plain dates (midnight). A value without an
    offset is wall time in `default_tz`. Raises ValueError on anything else.
    """
    parsed = datetime.fromisoformat(text.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(UTC)
