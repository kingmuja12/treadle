"""Child processes: process groups, pipe draining, line splitting, and termination."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import math
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from treadle.errors import StepError
from treadle.models import LogStream
from treadle.steps.base import StepRuntime

_CHUNK: Final = 64 * 1024
_KILL_WAIT: Final = 2.0
_POLL_INTERVAL: Final = 0.05
_ADD_MASK: Final = "::add-mask::"
_TRUNCATED: Final = " [line truncated]"


class ProcessRegistry:
    """The live process groups of this process. Implements `ProcessTracker`."""

    def __init__(self) -> None:
        self._groups: set[int] = set()

    def add(self, pgid: int) -> None:
        """Start tracking a process group."""
        self._groups.add(pgid)

    def discard(self, pgid: int) -> None:
        """Stop tracking a process group. Unknown IDs are ignored."""
        self._groups.discard(pgid)

    def kill_all(self, sig: int = signal.SIGKILL) -> int:
        """Send `sig` to every tracked group and return how many groups received it."""
        return sum(_signal_group(pgid, sig) for pgid in list(self._groups))

    def __len__(self) -> int:
        """Return the number of tracked groups."""
        return len(self._groups)


class LineBuffer:
    """Splits a byte stream into UTF-8 lines (decoded with `errors="replace"`).

    A line longer than `limit` bytes keeps its first `limit + slack` bytes, so masking can see
    a secret that crosses the cut, and is marked truncated; the rest of it is dropped.
    """

    def __init__(self, limit: int, *, slack: int = 1024) -> None:
        self._limit = limit
        self._keep = limit + slack
        self._line = bytearray()  # the kept prefix, plus one byte that may be the "\r" of "\r\n"
        self._size = 0  # the full length of the current line so far

    def feed(self, data: bytes) -> list[tuple[str, bool]]:
        """Add bytes and return the completed lines as (text, truncated), without `\\n`."""
        *complete, partial = data.split(b"\n")
        lines = []
        for part in complete:
            self._add(part)
            lines.append(self._take(newline=True))
        self._add(partial)
        return lines

    def flush(self) -> list[tuple[str, bool]]:
        """Return the final partial line, if any."""
        return [self._take(newline=False)] if self._size else []

    def _add(self, data: bytes) -> None:
        room = self._keep + 1 - len(self._line)
        if room > 0:
            self._line += data[:room]
        self._size += len(data)

    def _take(self, *, newline: bool) -> tuple[str, bool]:
        line, size = bytes(self._line), self._size
        self._line.clear()
        self._size = 0
        if newline and size == len(line) and line.endswith(b"\r"):
            line, size = line[:-1], size - 1
        return line[: self._keep].decode("utf-8", "replace"), size > self._limit


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """A finished process. A signal-killed process reports `128 + signal`."""

    exit_code: int
    stdout_tail: str


async def terminate_group(proc: asyncio.subprocess.Process, *, grace: float) -> None:
    """Stop a process group: SIGTERM, up to `grace` seconds, SIGKILL, then up to 2 seconds.

    The grace period lasts until every member of the group has exited, not only the leader,
    so a child that is still cleaning up after its shell exits isn't killed early. SIGKILL
    then goes to whatever is left of the group, including members that ignore SIGTERM, and
    repeats while any member is left, because a child that is being forked when the group
    is signalled can miss the signal (macOS lets it escape). The final 2 seconds also cover
    collecting the leader's exit status.
    """
    if _signal_group(proc.pid, signal.SIGTERM):
        await _wait_for_group(proc.pid, grace)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_KILL_WAIT):
            await _wait_for_group(proc.pid, _KILL_WAIT, signal_each_poll=signal.SIGKILL)
            await proc.wait()


async def run_process(
    argv: Sequence[str], *, cwd: Path, rt: StepRuntime, capture: int = 0
) -> ProcessResult:
    """Run a process in its own session and stream both pipes to `rt.log`.

    The process gets exactly `rt.env` and an empty stdin. Both pipes are read in 64 KiB
    chunks and drained even after log caps, `::add-mask::` lines on stdout register masks,
    and `capture` keeps the last characters of stdout. The call returns once the process has
    exited and both pipes are closed, so a background child that keeps a pipe open keeps the
    call waiting. On cancellation, the group is terminated under `asyncio.shield` and the
    CancelledError is re-raised. A process that can't start, such as a missing program or
    working directory, raises a retryable StepError.
    """
    proc = await _spawn(argv, cwd, rt.env)
    rt.processes.add(proc.pid)
    # The pumps run under shield, so they keep draining while a cancelled process shuts down;
    # a full pipe would otherwise hide its end-of-file and delay `wait()`.
    drained = asyncio.gather(
        _pump(cast(asyncio.StreamReader, proc.stdout), LogStream.STDOUT, rt, capture),
        _pump(cast(asyncio.StreamReader, proc.stderr), LogStream.STDERR, rt, 0),
    )
    try:
        stdout_tail, _ = await asyncio.shield(drained)
        code = await proc.wait()
    except BaseException:
        await asyncio.shield(_stop(proc, drained, rt))
        raise
    rt.processes.discard(proc.pid)
    return ProcessResult(128 - code if code < 0 else code, stdout_tail)


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a process group, returning False when it no longer exists.

    Signal 0 only checks that the group exists.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # gone, or only zombies remain (macOS)
        return False
    return True


async def _wait_for_group(pgid: int, grace: float, *, signal_each_poll: int = 0) -> None:
    """Return once no member of the process group is left, polling for up to `grace` seconds.

    Process-group exit has no event to wait on, so the group is polled every 50 ms. Each poll
    sends `signal_each_poll`; the default, 0, only checks that the group exists.
    """
    for _ in range(math.ceil(grace / _POLL_INTERVAL)):
        if not _signal_group(pgid, signal_each_poll):
            return
        await asyncio.sleep(_POLL_INTERVAL)


async def _spawn(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> asyncio.subprocess.Process:
    # Design note: a start failure is retryable, because a missing working directory may be
    # a drive that isn't mounted yet; the step's retry policy bounds the cost.
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        where = f" ({exc.filename})" if exc.filename else ""
        raise StepError(f"cannot start {argv[0]}: {exc.strerror or exc}{where}") from exc


async def _stop(
    proc: asyncio.subprocess.Process, drained: asyncio.Future[tuple[str, str]], rt: StepRuntime
) -> None:
    """Terminate the group, let the pipes finish draining, and stop tracking the group."""
    try:
        await terminate_group(proc, grace=rt.kill_grace)
        await asyncio.wait([drained], timeout=_KILL_WAIT)
    finally:
        drained.cancel()
        rt.processes.discard(proc.pid)


async def _pump(
    stream: asyncio.StreamReader, kind: LogStream, rt: StepRuntime, capture: int
) -> str:
    """Log each line of one pipe until end-of-file, and return its last `capture` characters."""
    lines = LineBuffer(rt.limits.line_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    tail = ""
    while chunk := await stream.read(_CHUNK):
        _emit(lines.feed(chunk), kind, rt)
        if capture:
            tail = (tail + decoder.decode(chunk))[-capture:]
    _emit(lines.flush(), kind, rt)
    return (tail + decoder.decode(b"", final=True))[-capture:] if capture else ""


def _emit(lines: list[tuple[str, bool]], kind: LogStream, rt: StepRuntime) -> None:
    for text, truncated in lines:
        if kind is LogStream.STDOUT and text.startswith(_ADD_MASK):
            rt.log.add_mask(text[len(_ADD_MASK) :])
            text = f"{_ADD_MASK}***"
        rt.log.write(kind, text + _TRUNCATED if truncated else text)
