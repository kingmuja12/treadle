"""Real processes through `run_process`, `terminate_group`, and `ProcessRegistry`."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import gc
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.support.builders import make_runtime
from tests.support.fakes import RecordingLog, RecordingProcesses

from treadle.config import Limits
from treadle.errors import StepError
from treadle.models import LogStream
from treadle.steps import procs
from treadle.steps.base import StepRuntime
from treadle.steps.procs import (
    LeftoverGroups,
    ProcessRegistry,
    ProcessResult,
    run_process,
    terminate_group,
)

PYTHON = sys.executable


class ReadyLog(RecordingLog):
    """Records lines, and sets `ready` when a process prints the line "ready"."""

    def __init__(self) -> None:
        super().__init__()
        self.ready = asyncio.Event()

    def write(self, stream: LogStream, text: str) -> None:
        super().write(stream, text)
        if text == "ready":
            self.ready.set()


CLEANUP_CHILD = """\
import os, signal, sys, time

def clean_up(signum, frame):
    time.sleep(0.3)
    open(os.environ["MARKER"], "w").close()
    sys.exit(0)

signal.signal(signal.SIGTERM, clean_up)
print("ready", flush=True)
while True:
    time.sleep(0.05)
"""


def runtime(tmp_path: Path, *, env: dict[str, str] | None = None, **kwargs: Any) -> StepRuntime:
    rt = make_runtime(tmp_path, **kwargs)
    return dataclasses.replace(rt, env={"PATH": os.environ["PATH"], **(env or {})})


def real(path: Path) -> str:
    return os.path.realpath(path)


def write_file(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def exists(path: Path) -> bool:
    return path.exists()


def tracked_group(processes: RecordingProcesses) -> int:
    """Return the one process group that was tracked and then released."""
    pgid = processes.history[0][1]
    assert processes.history == [("add", pgid), ("discard", pgid)]
    return pgid


async def group_gone(pgid: int) -> bool:
    """Wait up to 2 seconds for every member of a process group to be gone and reaped."""
    for _ in range(40):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # macOS reports zombies that way until they are reaped
            pass
        await asyncio.sleep(0.05)
    return False


def group_alive(pgid: int) -> bool:
    """True while the process group exists. Zombies alone count as gone, as for Treadle."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):  # PermissionError: only zombies (macOS)
        return False
    return True


async def released(processes: RecordingProcesses, pgid: int) -> bool:
    """Wait up to 2 seconds for `processes` to stop tracking a process group."""
    for _ in range(40):
        if ("discard", pgid) in processes.history:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_both_streams_are_logged_line_by_line(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    script = "echo out; echo err >&2; printf 'crlf\\r\\n'; printf 'no newline'"
    result = await run_process(["sh", "-c", script], cwd=tmp_path, rt=rt)

    assert result == ProcessResult(exit_code=0, stdout_tail="")
    assert log.texts(LogStream.STDOUT) == ["out", "crlf", "no newline"]
    assert log.texts(LogStream.STDERR) == ["err"]


async def test_the_process_group_is_tracked_while_it_runs(tmp_path: Path) -> None:
    processes = RecordingProcesses()
    rt = runtime(tmp_path, processes=processes)

    await run_process(["true"], cwd=tmp_path, rt=rt)

    assert tracked_group(processes) > 0
    assert processes.live == set()


async def test_add_mask_lines_register_a_mask_and_are_hidden(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    script = "echo '::add-mask::s3cret-value'; echo after; echo '::add-mask::not-on-stderr' >&2"
    await run_process(["sh", "-c", script], cwd=tmp_path, rt=rt)

    assert log.masks == ["s3cret-value"]
    assert log.texts(LogStream.STDOUT) == ["::add-mask::***", "after"]
    assert log.texts(LogStream.STDERR) == ["::add-mask::not-on-stderr"]


async def test_capture_keeps_the_complete_lines_among_the_last_characters_of_stdout(
    tmp_path: Path,
) -> None:
    rt = runtime(tmp_path)
    text = "h\u00e9llo\nw\u00f6rld\nand m\u00f6re\n"
    code = f"import sys; sys.stdout.buffer.write({text!r}.encode('utf-8'))"

    whole_lines = await run_process([PYTHON, "-c", code], cwd=tmp_path, rt=rt, capture=15)
    split_line = await run_process([PYTHON, "-c", code], cwd=tmp_path, rt=rt, capture=12)
    all_text = await run_process([PYTHON, "-c", code], cwd=tmp_path, rt=rt, capture=100)

    assert whole_lines.stdout_tail == "w\u00f6rld\nand m\u00f6re\n"
    assert split_line.stdout_tail == "and m\u00f6re\n"
    assert all_text.stdout_tail == text


async def test_a_long_line_is_cut_after_the_slack(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log, limits=Limits(line_bytes=1024))

    await run_process([PYTHON, "-c", "print('x' * 5000); print('next')"], cwd=tmp_path, rt=rt)

    assert log.texts(LogStream.STDOUT) == ["x" * 2048 + " [line truncated]", "next"]


async def test_stdin_is_empty(tmp_path: Path) -> None:
    rt = runtime(tmp_path)

    async with asyncio.timeout(10):
        result = await run_process(["cat"], cwd=tmp_path, rt=rt)

    assert result.exit_code == 0


async def test_the_process_gets_exactly_the_runtime_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREADLE_TEST_PARENT_ONLY", "leaked")
    log = RecordingLog()
    rt = runtime(tmp_path, log=log, env={"ONLY": "1"})

    script = 'echo "$ONLY|${TREADLE_TEST_PARENT_ONLY-unset}"; pwd -P'
    await run_process(["sh", "-c", script], cwd=tmp_path, rt=rt)

    assert log.texts(LogStream.STDOUT) == ["1|unset", real(tmp_path)]


@pytest.mark.parametrize(
    ("script", "code"),
    [("exit 7", 7), ("kill -TERM $$", 128 + signal.SIGTERM), ("kill -KILL $$", 137)],
)
async def test_exit_codes_report_signals_as_128_plus_the_signal(
    tmp_path: Path, script: str, code: int
) -> None:
    result = await run_process(["sh", "-c", script], cwd=tmp_path, rt=runtime(tmp_path))

    assert result.exit_code == code


async def test_a_missing_program_or_directory_fails_to_start(tmp_path: Path) -> None:
    processes = RecordingProcesses()
    rt = runtime(tmp_path, processes=processes)
    missing = os.strerror(errno.ENOENT)

    with pytest.raises(StepError) as program:
        await run_process(["treadle-no-such-program"], cwd=tmp_path, rt=rt)
    with pytest.raises(StepError) as directory:
        await run_process(["true"], cwd=tmp_path / "gone", rt=rt)

    assert program.value.message == (
        f"cannot start treadle-no-such-program: {missing} (treadle-no-such-program)"
    )
    assert directory.value.message == f"cannot start true: {missing} ({tmp_path / 'gone'})"
    assert program.value.retryable is True
    assert processes.history == []


async def test_the_grace_period_lets_a_child_finish_after_its_shell_exits(
    tmp_path: Path,
) -> None:
    child = write_file(tmp_path / "child.py", CLEANUP_CHILD)
    marker = tmp_path / "cleaned-up"
    log, processes = ReadyLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes, kill_grace=5, env={"MARKER": str(marker)})
    # The shell runs the child in the foreground and dies at once on SIGTERM; the child takes
    # 0.3 seconds to clean up, which SIGKILL must not cut short.
    script = f'"{PYTHON}" "{child}"; echo unreachable'
    task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
    await asyncio.wait_for(log.ready.wait(), 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert exists(marker)
    assert "unreachable" not in log.texts()
    assert await group_gone(tracked_group(processes))


async def test_sigkill_follows_the_grace_period_when_sigterm_is_ignored(tmp_path: Path) -> None:
    log, processes = ReadyLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes, kill_grace=0.3)
    script = "trap '' TERM; sleep 100 & echo ready; sleep 100"
    task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
    await asyncio.wait_for(log.ready.wait(), 10)
    loop = asyncio.get_running_loop()
    started = loop.time()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.time() - started >= 0.25
    pgid = tracked_group(processes)
    assert await group_gone(pgid)


async def test_a_member_that_ignores_sigterm_doesnt_outlive_its_leader(tmp_path: Path) -> None:
    log, processes = ReadyLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes, kill_grace=0.3)
    script = "trap '' TERM; sleep 100 & trap - TERM; echo ready; sleep 100"
    task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
    await asyncio.wait_for(log.ready.wait(), 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pgid = tracked_group(processes)
    assert await group_gone(pgid)


async def test_a_second_cancellation_kills_the_group_without_waiting_out_the_grace(
    tmp_path: Path,
) -> None:
    log, processes = ReadyLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes, kill_grace=30)
    script = "trap '' TERM; sleep 100 & echo ready; sleep 100"
    task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
    await asyncio.wait_for(log.ready.wait(), 10)
    pgid = processes.history[0][1]
    task.cancel()
    await asyncio.sleep(0.2)  # the group gets SIGTERM, which it ignores
    loop = asyncio.get_running_loop()
    started = loop.time()

    task.cancel()  # as a forced cancel does
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.time() - started < 2.5  # well before the 30-second grace ends
    assert not group_alive(pgid)  # gone before the CancelledError arrived
    assert await released(processes, pgid)


async def test_a_forced_cancellation_kills_the_group_without_a_grace_period(
    tmp_path: Path,
) -> None:
    log, processes = ReadyLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes, kill_grace=30)
    rt = dataclasses.replace(rt, forced=lambda: True)
    script = "trap '' TERM; sleep 100 & echo ready; sleep 100"
    task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
    await asyncio.wait_for(log.ready.wait(), 10)
    loop = asyncio.get_running_loop()
    started = loop.time()

    task.cancel()  # once, as a forced cancel that reaches a running step does
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.time() - started < 10  # a third of the 30-second grace
    pgid = tracked_group(processes)  # released before the CancelledError arrived
    assert not group_alive(pgid)


async def test_pipes_that_outlive_the_group_leave_no_error_for_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(procs, "_KILL_WAIT", 0.3)  # also how long a stop waits for the pipes
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: reports.append(context["message"]))
    log = ReadyLog()
    rt = runtime(tmp_path, log=log, kill_grace=0.3)
    # The child leaves the group, so stopping the group leaves both pipes open.
    child = (
        "import os, time; os.setsid(); print(os.getpid()); print('ready', flush=True); "
        "time.sleep(5)"
    )
    script = f'"{PYTHON}" -c "{child}" & sleep 100'
    try:
        task = asyncio.create_task(run_process(["sh", "-c", script], cwd=tmp_path, rt=rt))
        await asyncio.wait_for(log.ready.wait(), 10)
        escaped = int(log.texts(LogStream.STDOUT)[0])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        del task
        await asyncio.sleep(0.1)  # the cancelled pumps finish
        gc.collect()  # a future reports an exception that nothing retrieved when collected
        # Ending the child closes the pipes, so the subprocess transport closes too.
        os.kill(escaped, signal.SIGKILL)
        assert await group_gone(escaped)
        await asyncio.sleep(0.1)
    finally:
        loop.set_exception_handler(handler)

    assert reports == []


async def test_terminate_group_stops_a_process_that_exits_on_sigterm() -> None:
    proc = await asyncio.create_subprocess_exec("sleep", "100", start_new_session=True)

    async with asyncio.timeout(4):
        await terminate_group(proc, grace=30)

    assert proc.returncode == -signal.SIGTERM


async def test_terminate_group_without_grace_kills_at_once() -> None:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; echo ready; sleep 100",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert await proc.stdout.readline() == b"ready\n"

    # The kill usually lands while the shell forks `sleep`, which macOS lets escape a single
    # group signal; the pipe reaches its end only when no member of the group survived.
    async with asyncio.timeout(4):
        await terminate_group(proc, grace=0)
        assert await proc.stdout.read() == b""

    assert proc.returncode == -signal.SIGKILL
    assert await group_gone(proc.pid)


async def test_terminate_group_returns_at_once_when_the_group_is_gone() -> None:
    proc = await asyncio.create_subprocess_exec("true", start_new_session=True)
    await proc.wait()
    loop = asyncio.get_running_loop()
    started = loop.time()

    await terminate_group(proc, grace=30)

    assert loop.time() - started < 1
    assert proc.returncode == 0


async def test_kill_all_signals_live_groups() -> None:
    proc = await asyncio.create_subprocess_exec("sleep", "100", start_new_session=True)
    registry = ProcessRegistry()
    registry.add(proc.pid)

    assert registry.kill_all() == 1
    assert await proc.wait() == -signal.SIGKILL
    assert registry.kill_all() == 0


async def test_a_background_child_keeps_its_group_tracked_after_the_call(tmp_path: Path) -> None:
    log, processes = RecordingLog(), RecordingProcesses()
    rt = runtime(tmp_path, log=log, processes=processes)

    result = await run_process(["sh", "-c", "sleep 30 >/dev/null 2>&1 &"], cwd=tmp_path, rt=rt)

    pgid = processes.history[0][1]
    try:
        assert result.exit_code == 0
        assert processes.history == [("add", pgid)]
        assert log.texts(LogStream.SYSTEM) == [
            "processes that this step started in the background are still running; "
            "Treadle stops them when the run ends"
        ]
    finally:
        os.killpg(pgid, signal.SIGKILL)
    assert await group_gone(pgid)


async def test_leftover_groups_forget_an_empty_group_and_stop_the_rest() -> None:
    registry = ProcessRegistry()
    leftovers = LeftoverGroups(registry)
    brief = await asyncio.create_subprocess_exec("sleep", "0.2", start_new_session=True)
    lasting = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    for proc in (brief, lasting):
        registry.add(proc.pid)
        leftovers.adopt(proc.pid)

    await asyncio.sleep(1.5)
    assert len(registry) == 1  # the brief group is gone and forgotten

    assert await leftovers.stop(grace=1) == 1
    assert len(registry) == 0
    assert await lasting.wait() == -signal.SIGTERM
    assert await brief.wait() == 0
    assert await leftovers.stop(grace=1) == 0


async def test_leftover_groups_watch_only_while_they_hold_a_group() -> None:
    registry = ProcessRegistry()
    leftovers = LeftoverGroups(registry)
    brief = await asyncio.create_subprocess_exec("true", start_new_session=True)
    registry.add(brief.pid)
    leftovers.adopt(brief.pid)
    await brief.wait()

    await asyncio.sleep(1.2)  # the watch forgets the group, and ends with nothing to watch
    assert len(registry) == 0
    lasting = await asyncio.create_subprocess_exec("sleep", "30", start_new_session=True)
    registry.add(lasting.pid)
    leftovers.adopt(lasting.pid)

    assert await leftovers.stop(grace=1) == 1
    assert await lasting.wait() == -signal.SIGTERM


async def test_a_forced_stop_kills_the_leftover_groups_without_a_grace_period() -> None:
    registry = ProcessRegistry()
    leftovers = LeftoverGroups(registry)
    stubborn = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; echo ready; sleep 100",
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert stubborn.stdout is not None
    assert await asyncio.wait_for(stubborn.stdout.readline(), 10) == b"ready\n"
    registry.add(stubborn.pid)
    leftovers.adopt(stubborn.pid)
    loop = asyncio.get_running_loop()
    started = loop.time()

    assert await leftovers.stop(grace=30, forced=True) == 1

    assert loop.time() - started < 10  # a third of the 30-second grace
    assert await stubborn.wait() == -signal.SIGKILL
    assert len(registry) == 0


async def test_leftover_groups_count_only_the_groups_that_they_stop() -> None:
    registry = ProcessRegistry()
    leftovers = LeftoverGroups(registry)
    finished = await asyncio.create_subprocess_exec("true", start_new_session=True)
    registry.add(finished.pid)
    leftovers.adopt(finished.pid)
    await finished.wait()  # gone before the watch notices

    assert await leftovers.stop(grace=1) == 0
    assert len(registry) == 0
