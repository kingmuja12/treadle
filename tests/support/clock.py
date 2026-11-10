"""A fake wall clock that follows the event loop's (virtual) time."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class FakeClock:
    """A `Clock` whose `now()` is `start + (loop time - loop time at the first call) + jumps`.

    `jump` simulates laptop sleep or an NTP step: wall time moves, loop time doesn't. Outside a
    running loop, wall time stands still at the last loop time seen.
    """

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self.start = start
        self._jumps = timedelta()
        self._base: float | None = None
        self._loop_time = 0.0

    def now(self) -> datetime:
        """Return the current fake wall time, timezone-aware UTC."""
        with contextlib.suppress(RuntimeError):
            self._loop_time = asyncio.get_running_loop().time()
        if self._base is None:
            self._base = self._loop_time
        return self.start + timedelta(seconds=self._loop_time - self._base) + self._jumps

    def jump(self, delta: timedelta | float) -> None:
        """Move wall time by `delta` (seconds or a timedelta, negative for backward)."""
        self._jumps += delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
