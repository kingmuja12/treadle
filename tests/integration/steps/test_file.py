"""The `file` step's worker thread under cancellation, with real time.

Opening a FIFO for writing blocks until a reader opens it, which holds an `append` operation
open in its thread for as long as a test needs. A test that needs the operation to fail
blocks a replaced handler instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import gc
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from tests.support.builders import make_runtime
from tests.support.fakes import RecordingLog

from treadle.schema import validate
from treadle.steps import file
from treadle.steps.base import StepResult
from treadle.steps.file import FileStep
from treadle.types import JSONValue


@contextlib.contextmanager
def blocking_fifo(path: Path) -> Iterator[Callable[[], int]]:
    """Create a FIFO whose writers block until `release()` opens it for reading.

    `release()` returns the reader's descriptor. The FIFO is always released on exit, so a
    blocked worker thread can't outlive the test.
    """
    os.mkfifo(path)
    readers: list[int] = []

    def release() -> int:
        if not readers:
            readers.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
        return readers[0]

    try:
        yield release
    finally:
        os.close(release())


def append_params(path: str, content: str) -> dict[str, JSONValue]:
    value, issues = validate(
        FileStep().schema, {"action": "append", "path": path, "content": content}
    )
    assert issues == []
    return cast(dict[str, JSONValue], value)


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait up to 2 seconds for `condition` to hold, polling every 20 ms."""
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail("the condition never held")


async def test_a_cancelled_step_waits_for_its_file_operation(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = make_runtime(tmp_path, log=log)

    with blocking_fifo(rt.project_dir / "pipe") as release:
        task = asyncio.create_task(FileStep().run(append_params("pipe", "data"), rt))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.sleep(0.2)
        assert not task.done()

        reader = release()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert os.read(reader, 100) == b"data"
    assert log.texts() == ["the file operation finished after the step was cancelled"]


async def test_a_second_cancellation_stops_waiting(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)

    with blocking_fifo(rt.project_dir / "pipe"):
        task = asyncio.create_task(FileStep().run(append_params("pipe", "data"), rt))
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)


async def test_a_failure_after_a_second_cancellation_is_logged_and_never_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proceed = threading.Event()

    def broken_append(target: Path, params: object, rt: object) -> tuple[int, bool]:
        proceed.wait(5)
        raise BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE))

    monkeypatch.setitem(file._HANDLERS, "append", broken_append)  # type: ignore[index]
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: reports.append(context["message"]))
    log = RecordingLog()
    rt = make_runtime(tmp_path, log=log)
    try:
        task = asyncio.create_task(FileStep().run(append_params("notes.txt", "data"), rt))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        del task
        proceed.set()  # the operation fails after the step has ended
        await eventually(lambda: bool(log.texts()))
        gc.collect()  # a task reports an exception that nothing retrieved when collected
    finally:
        proceed.set()
        loop.set_exception_handler(handler)

    target = rt.project_dir / "notes.txt"  # pytest's tmp_path has no symlinks to resolve
    assert log.texts() == [
        "the file operation failed after the step was cancelled: "
        f"cannot append {target}: {os.strerror(errno.EPIPE)}"
    ]
    assert reports == []


def test_closing_the_event_loop_on_an_abandoned_operation_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proceed = threading.Event()

    def slow_append(target: Path, params: object, rt: object) -> tuple[int, bool]:
        proceed.wait(5)
        return 0, True

    monkeypatch.setitem(file._HANDLERS, "append", slow_append)  # type: ignore[index]
    reports: list[str] = []
    log = RecordingLog()
    rt = make_runtime(tmp_path, log=log)

    async def abandon() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _, context: reports.append(context["message"]))
        task = asyncio.create_task(FileStep().run(append_params("notes.txt", "data"), rt))
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # asyncio.run cancels the operation's task at once, and then waits for its thread.
        threading.Timer(0.5, proceed.set).start()

    try:
        asyncio.run(abandon())
    finally:
        proceed.set()

    assert (log.texts(), reports) == ([], [])


async def test_a_step_that_isnt_cancelled_returns_its_outputs(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)

    with blocking_fifo(rt.project_dir / "pipe") as release:
        task = asyncio.create_task(FileStep().run(append_params("pipe", "data"), rt))
        await asyncio.sleep(0.2)
        reader = release()
        result = await asyncio.wait_for(task, 5)
        assert os.read(reader, 100) == b"data"

    path = str(rt.project_dir / "pipe")
    assert result == StepResult({"path": path, "bytes": 4, "changed": True})
