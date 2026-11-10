"""Batched, masked, capped log writing for one run."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from treadle.clock import Clock, to_ms
from treadle.config import Limits
from treadle.engine.masking import Masker
from treadle.models import LogStream
from treadle.steps.procs import TRUNCATION_MARKER
from treadle.store.async_store import AsyncStore
from treadle.store.repo import NewLogLine, storable_text
from treadle.units import format_size

# The most values that `::add-mask::` lines may add to one run's masker. Each new value
# makes the next mask compile the whole pattern again, so this bounds the cost.
MAX_RUNTIME_MASKS: Final = 1000
_log = logging.getLogger("treadle.engine")


class AttemptLog:
    """The log of one step attempt; implements StepLog.

    Lines are masked and cut at `limits.line_bytes`. After the attempt or run cap, one notice
    is written and further output is dropped and counted. So is the output after a value
    that the run can no longer mask, which `refused_mask` reports.
    """

    def __init__(self, sink: LogSink, step_id: str, attempt: int) -> None:
        """Write lines through `sink`, attributed to one attempt. Use `LogSink.attempt_log`."""
        self._sink = sink
        self._step_id = step_id
        self._attempt = attempt
        self._bytes = 0
        self._dropped = 0
        self._truncated = False
        self._refused_mask = False

    @property
    def bytes_written(self) -> int:
        """Bytes counted against the caps: the UTF-8 length of each masked line plus one."""
        return self._bytes

    @property
    def truncated(self) -> bool:
        """True once a cap dropped output."""
        return self._truncated

    @property
    def refused_mask(self) -> bool:
        """True once `add_mask` refused a value, because the run already masks the most."""
        return self._refused_mask

    def write(self, stream: LogStream, text: str) -> None:
        """Queue one line of process output."""
        sink = self._sink
        if self._truncated:
            self._dropped += _size(text)
            return
        line = sink._clean(text)
        size = _size(line)
        notice = sink._cap_notice(self._bytes, size)
        if notice is not None:
            self._truncated = True
            self._dropped += _size(text)
            sink._queue(self._step_id, self._attempt, LogStream.SYSTEM, notice)
            return
        self._bytes += size
        sink._run_bytes += size
        sink._queue(self._step_id, self._attempt, stream, line)

    def system(self, text: str) -> None:
        """Queue one system line, which the caps don't count."""
        self._sink.system(text, step_id=self._step_id, attempt=self._attempt)

    def add_mask(self, value: str) -> None:
        """Mask `value` for the rest of the run.

        A run masks at most `MAX_RUNTIME_MASKS` added values. A new value past that can't be
        masked, so the attempt's further output is dropped with one notice, and the executor
        fails the attempt; an unmasked value never reaches storage.
        """
        sink = self._sink
        if self._refused_mask:
            return
        if sink._runtime_masks < MAX_RUNTIME_MASKS:
            if sink._masker.add(value):
                sink._runtime_masks += 1
            return
        if sink._masker.knows(value):
            return
        self._refused_mask = True
        if not self._truncated:
            self._truncated = True
            notice = (
                f"output truncated: the run masks at most {MAX_RUNTIME_MASKS} values from "
                "::add-mask::, so further output from this step is dropped"
            )
            sink._queue(self._step_id, self._attempt, LogStream.SYSTEM, notice)


class LogSink:
    """Queues a run's log lines and writes them to the store in batches.

    Each line gets the next `seq` when it's queued, so the stored order is the queue order.
    `flush` holds a lock, so when it returns, every line queued before the call is committed.
    """

    def __init__(
        self,
        store: AsyncStore,
        run_id: int,
        masker: Masker,
        *,
        limits: Limits,
        clock: Clock,
        first_seq: int = 1,
        flush_interval: float = 0.25,
        max_batch: int = 500,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._masker = masker
        self._limits = limits
        self._clock = clock
        self._next_seq = first_seq
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._run_bytes = 0
        self._runtime_masks = 0  # values that `add_mask` added to the masker
        self._pending: list[NewLogLine] = []
        self._attempts: list[AttemptLog] = []
        self._has_lines = asyncio.Event()
        self._batch_full = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closed = False

    def attempt_log(self, step_id: str, attempt: int) -> AttemptLog:
        """Return the log for one step attempt."""
        log = AttemptLog(self, step_id, attempt)
        self._attempts.append(log)
        return log

    def system(self, text: str, *, step_id: str | None = None, attempt: int | None = None) -> None:
        """Queue a system line, optionally attributed to a step attempt.

        Text with several lines, such as a traceback, becomes one system line per line.
        """
        for line in text.splitlines() or [""]:
            self._queue(step_id, attempt, LogStream.SYSTEM, self._clean(line))

    async def flush(self) -> None:
        """Write every queued line now."""
        async with self._lock:
            batch, self._pending = self._pending, []
            self._has_lines.clear()
            self._batch_full.clear()
            if batch:
                await self._store.write(lambda repo: repo.append_logs(self._run_id, batch))

    async def run(self) -> None:
        """Flush every `flush_interval`, or early at `max_batch` lines, until cancelled.

        The interval starts when a line is queued, so an idle sink doesn't wake up. A failed
        write is logged and its lines are lost; the loop keeps going.
        """
        while True:
            await self._has_lines.wait()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._flush_interval):
                    await self._batch_full.wait()
            try:
                await self.flush()
            except Exception:
                _log.exception("run=%d: couldn't write log lines", self._run_id)

    async def aclose(self) -> None:
        """Flush for the last time, including a "dropped N bytes" line per capped attempt.

        Lines queued afterwards are discarded. Closing twice does nothing.
        """
        if self._closed:
            return
        for log in self._attempts:
            if log._dropped:
                message = f"dropped {log._dropped} bytes of output"
                self._queue(log._step_id, log._attempt, LogStream.SYSTEM, message)
        self._closed = True
        await self.flush()

    def _queue(
        self, step_id: str | None, attempt: int | None, stream: LogStream, text: str
    ) -> None:
        """Give a cleaned line the next `seq` and queue it for the next flush."""
        if self._closed:
            return
        now = to_ms(self._clock.now())
        self._pending.append(NewLogLine(self._next_seq, now, step_id, attempt, stream, text))
        self._next_seq += 1
        self._has_lines.set()
        if len(self._pending) >= self._max_batch:
            self._batch_full.set()

    def _clean(self, text: str) -> str:
        """Mask a line, then cut it after `limits.line_bytes` bytes and mark the cut.

        A line that the step's line buffer already cut keeps one marker. Only a cut line ends
        with the marker after more than `limits.line_bytes` bytes. The buffer can cut through
        a secret longer than its slack, and the masker can't recognize the kept prefix, so
        such a line also loses its last `longest - 1` characters before the cut.
        """
        limit = self._limits.line_bytes
        kept = text.removesuffix(TRUNCATION_MARKER)
        cut_before = kept != text and _stored_length(kept) > limit
        if cut_before:
            text = kept
        masked = self._masker.mask(text)
        if cut_before:
            masked = masked[: len(masked) - max(self._masker.longest - 1, 0)]
        line = storable_text(masked)
        data = line.encode("utf-8")
        if not cut_before and len(data) <= limit:
            return line
        return data[:limit].decode("utf-8", "ignore") + TRUNCATION_MARKER

    def _cap_notice(self, attempt_bytes: int, size: int) -> str | None:
        """Return the notice for the cap that a line of `size` bytes would pass, or None."""
        limits = self._limits
        if attempt_bytes + size > limits.step_log_bytes:
            limit = format_size(limits.step_log_bytes)
            return f"output truncated: further output from this step is dropped (limit {limit})"
        if self._run_bytes + size > limits.run_log_bytes:
            # Unlike the step cap's notice, the run cap's notice names the run and its own
            # limit, so it's clear which setting to raise.
            limit = format_size(limits.run_log_bytes)
            return (
                f"output truncated: further output from this run is dropped (limit {limit} per run)"
            )
        return None


def _size(text: str) -> int:
    """Return the bytes a line counts against the caps: its stored length plus a newline."""
    return _stored_length(text) + 1


def _stored_length(text: str) -> int:
    """Return the UTF-8 length of `text` in storage.

    A lone surrogate counts 3 bytes, as the U+FFFD that replaces it in storage does.
    """
    return len(text.encode("utf-8", "surrogatepass"))
