"""The log sink: masking, line cuts, caps, batching, and the flush loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import pytest
from tests.support.builders import make_workflow
from tests.support.clock import FakeClock
from tests.support.vtime import current_loop
from tests.unit.engine.helpers import Engine

from treadle.config import Limits
from treadle.engine.logsink import LogSink
from treadle.engine.masking import Masker
from treadle.models import LogLine, LogStream
from treadle.steps.procs import LineBuffer
from treadle.store.async_store import AsyncStore

OUT, ERR, SYSTEM = LogStream.STDOUT, LogStream.STDERR, LogStream.SYSTEM
CUT = " [line truncated]"


@pytest.fixture
async def run_id(engine: Engine) -> int:
    """A stored run that log lines can belong to."""
    return (await engine.create(make_workflow())).id


def make_sink(
    store: AsyncStore,
    run_id: int,
    clock: FakeClock,
    *,
    masker: Masker | None = None,
    **limits: Any,
) -> LogSink:
    return LogSink(store, run_id, masker or Masker(), limits=Limits(**limits), clock=clock)


async def stored(store: AsyncStore, run_id: int) -> list[LogLine]:
    return await store.logs(run_id, limit=10_000)


async def texts(store: AsyncStore, run_id: int) -> list[str]:
    return [line.text for line in await stored(store, run_id)]


def emit(log: Any, buffer: LineBuffer, *chunks: bytes) -> None:
    """Feed chunks through a line buffer into a log, as `run_process` does for stdout."""
    lines = [line for chunk in chunks for line in buffer.feed(chunk)] + buffer.flush()
    for text, truncated in lines:
        log.write(OUT, text + CUT if truncated else text)


async def test_lines_are_masked_numbered_attributed_and_stored_on_flush(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = LogSink(store, run_id, Masker(["hunter22"]), limits=Limits(), clock=clock, first_seq=10)
    started = clock.now()
    log = sink.attempt_log("build", 2)

    log.write(OUT, "password hunter22")
    clock.jump(timedelta(seconds=1))
    log.write(ERR, "warning")
    log.system("from the step type")
    sink.system("run line hunter22")
    sink.system("step line", step_id="build")
    assert await stored(store, run_id) == []

    await sink.flush()

    lines = await stored(store, run_id)
    assert [(line.seq, line.step_id, line.attempt, line.stream, line.text) for line in lines] == [
        (10, "build", 2, OUT, "password ***"),
        (11, "build", 2, ERR, "warning"),
        (12, "build", 2, SYSTEM, "from the step type"),
        (13, None, None, SYSTEM, "run line ***"),
        (14, "build", None, SYSTEM, "step line"),
    ]
    assert lines[0].ts == started
    assert lines[1].ts == started + timedelta(seconds=1)


async def test_an_empty_flush_writes_nothing(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    await make_sink(store, run_id, clock).flush()

    assert await stored(store, run_id) == []


async def test_system_text_becomes_one_line_per_line_and_isnt_counted(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, step_log_bytes=1)
    log = sink.attempt_log("s", 1)

    log.system("Traceback:\n  frame\nError: boom\n")
    sink.system("")

    await sink.flush()
    assert await texts(store, run_id) == ["Traceback:", "  frame", "Error: boom", ""]
    assert (log.bytes_written, log.truncated) == (0, False)


async def test_bytes_count_the_utf8_length_plus_a_newline(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    log = make_sink(store, run_id, clock).attempt_log("s", 1)

    log.write(OUT, "é")
    log.write(OUT, "")

    assert log.bytes_written == 3 + 1


async def test_a_long_line_is_cut_after_masking(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, masker=Masker(["SECRETVALUE"]), line_bytes=20)
    log = sink.attempt_log("s", 1)

    log.write(OUT, "x" * 15 + "SECRETVALUE" + "y" * 10)  # the secret straddles byte 20
    log.write(OUT, "z" * 25)
    log.write(OUT, "q" * 20)

    await sink.flush()
    assert await texts(store, run_id) == [
        "x" * 15 + "***" + "yy" + CUT,
        "z" * 20 + CUT,
        "q" * 20,
    ]


async def test_a_cut_never_splits_a_character(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, line_bytes=21)

    sink.attempt_log("s", 1).write(OUT, "é" * 15)

    await sink.flush()
    assert await texts(store, run_id) == ["é" * 10 + CUT]


async def test_a_line_the_step_already_cut_keeps_one_marker(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, line_bytes=20)
    log = sink.attempt_log("s", 1)

    log.write(OUT, "y" * 30 + CUT)
    log.write(OUT, "short" + CUT)  # a short line that merely ends like a marker

    await sink.flush()
    assert await texts(store, run_id) == ["y" * 20 + CUT, "short" + CUT]


async def test_a_secret_that_crosses_a_chunk_boundary_is_masked(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, masker=Masker(["SECRET-VALUE"]))

    emit(sink.attempt_log("s", 1), LineBuffer(1024), b"token=SEC", b"RET-VAL", b"UE end\nnext")

    await sink.flush()
    assert await texts(store, run_id) == ["token=*** end", "next"]


async def test_a_secret_across_the_line_limit_is_masked_within_the_slack(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    secret = "SECRET-0123456789"
    sink = make_sink(store, run_id, clock, masker=Masker([secret]), line_bytes=20)
    line = ("x" * 15 + secret + "y" * 40).encode() + b"\n"

    emit(sink.attempt_log("s", 1), LineBuffer(20, slack=64), line[:9], line[9:])

    await sink.flush()
    [text] = await texts(store, run_id)
    assert text == "x" * 15 + "***" + "yy" + CUT


async def test_a_secret_that_the_line_buffer_cut_leaves_no_prefix(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    secret = "ABCDEFGHIJKLMNOP"  # longer than the buffer's slack
    sink = make_sink(store, run_id, clock, masker=Masker([secret]), line_bytes=20)
    line = ("x" * 18 + secret + "y" * 10).encode() + b"\n"

    emit(sink.attempt_log("s", 1), LineBuffer(20, slack=4), line)

    await sink.flush()
    [text] = await texts(store, run_id)
    assert text.endswith(CUT)
    assert "AB" not in text
    assert text.removesuffix(CUT) == "x" * len(text.removesuffix(CUT))


async def test_the_step_cap_writes_one_notice_and_counts_what_it_drops(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock, step_log_bytes=50)
    log = sink.attempt_log("s", 1)

    for index in range(5):
        log.write(OUT, f"line-{index}...")  # 9 characters plus a newline
    log.write(OUT, "this line passes the cap")
    log.write(OUT, "x")
    log.system("system lines still get through")

    await sink.aclose()
    assert (log.bytes_written, log.truncated) == (50, True)
    assert await texts(store, run_id) == [
        *(f"line-{index}..." for index in range(5)),
        "output truncated: further output from this step is dropped (limit 50 B)",
        "system lines still get through",
        f"dropped {len('this line passes the cap') + 1 + 2} bytes of output",
    ]
    lines = await stored(store, run_id)
    assert (lines[5].stream, lines[-1].stream, lines[-1].step_id) == (SYSTEM, SYSTEM, "s")


async def test_the_run_cap_spans_attempts(store: AsyncStore, run_id: int, clock: FakeClock) -> None:
    sink = make_sink(store, run_id, clock, run_log_bytes=2048)
    first, second = sink.attempt_log("a", 1), sink.attempt_log("b", 1)

    first.write(OUT, "x" * 2047)
    second.write(OUT, "y")
    second.write(OUT, "z")

    await sink.aclose()
    assert (first.truncated, second.truncated, second.bytes_written) == (False, True, 0)
    assert (await texts(store, run_id))[1:] == [
        "output truncated: further output from this run is dropped (limit 2.0 KiB per run)",
        "dropped 4 bytes of output",
    ]


async def test_add_mask_masks_the_rest_of_the_run(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock)
    first, second = sink.attempt_log("a", 1), sink.attempt_log("b", 1)

    first.write(OUT, "made value-1234")
    first.add_mask("value-1234")
    first.write(OUT, "again value-1234")
    second.write(OUT, "elsewhere value-1234")

    await sink.flush()
    assert await texts(store, run_id) == [
        "made value-1234",
        "again ***",
        "elsewhere ***",
    ]


async def test_lone_surrogates_are_stored_as_replacement_characters(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock)

    sink.attempt_log("s", 1).write(OUT, "bad \ud800 char")

    await sink.flush()
    assert await texts(store, run_id) == ["bad ? char"]


async def test_aclose_is_idempotent_and_later_lines_are_discarded(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = make_sink(store, run_id, clock)
    sink.system("before")

    await sink.aclose()
    sink.system("after")
    await sink.aclose()
    await sink.flush()

    assert await texts(store, run_id) == ["before"]


async def test_run_flushes_one_interval_after_the_first_queued_line(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    loop = current_loop()
    sink = make_sink(store, run_id, clock)
    flusher = asyncio.create_task(sink.run())
    await asyncio.sleep(10)  # an idle sink waits without flushing
    start = loop.time()

    sink.system("first")
    await asyncio.sleep(0.2)
    sink.system("second")
    await asyncio.sleep(0.04)
    assert await texts(store, run_id) == []
    await asyncio.sleep(0.02)
    assert await texts(store, run_id) == ["first", "second"]
    assert loop.time() - start == pytest.approx(0.26)

    flusher.cancel()
    await asyncio.wait({flusher})


async def test_run_flushes_early_once_a_batch_is_full(
    store: AsyncStore, run_id: int, clock: FakeClock
) -> None:
    sink = LogSink(store, run_id, Masker(), limits=Limits(), clock=clock, max_batch=3)
    flusher = asyncio.create_task(sink.run())

    for text in ("a", "b", "c"):
        sink.system(text)
    await asyncio.sleep(0.01)

    assert await texts(store, run_id) == ["a", "b", "c"]
    flusher.cancel()
    await asyncio.wait({flusher})


async def test_run_logs_a_failed_write_and_keeps_going(
    store: AsyncStore, run_id: int, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    sink = make_sink(store, run_id, clock)
    flusher = asyncio.create_task(sink.run())
    await store.close()

    with caplog.at_level(logging.ERROR, logger="treadle.engine"):
        sink.system("lost")
        await asyncio.sleep(1)
        sink.system("lost too")
        await asyncio.sleep(1)

    assert not flusher.done()
    assert [record.getMessage() for record in caplog.records] == [
        f"run={run_id}: couldn't write log lines",
        f"run={run_id}: couldn't write log lines",
    ]
    flusher.cancel()
    await asyncio.wait({flusher})
