"""The `file` step's worker thread under cancellation, with real time and a real FIFO.

Opening a FIFO for writing blocks until a reader opens it, which holds an `append` operation
open in its thread for as long as a test needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from tests.support.builders import make_runtime

from treadle.schema import validate
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


async def test_a_cancelled_step_waits_for_its_file_operation(tmp_path: Path) -> None:
    rt = make_runtime(tmp_path)

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
