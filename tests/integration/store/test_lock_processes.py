from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from treadle.errors import LockHeldError
from treadle.lock import InstanceLock
from treadle.types import LockHolder

HOLDER = LockHolder(os.getpid(), "serve", "2026-11-09T12:00:00.000Z", "0.1.0", "127.0.0.1", 8765)

# The holder takes the lock as a foreground `treadle run`. With an extra argument it first
# starts a long-running child of its own, as a step might, and prints that child's PID. Then
# it reports "locked" and holds the lock until its stdin closes or it is killed.
HOLDER_SCRIPT = """
import os, subprocess, sys
from pathlib import Path
from treadle.lock import InstanceLock
from treadle.types import LockHolder

holder = LockHolder(os.getpid(), "run", "2026-11-09T12:00:00.000Z", "0.1.0")
with InstanceLock(Path(sys.argv[1]), holder):
    if len(sys.argv) > 2:
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print(sleeper.pid, flush=True)
    print("locked", flush=True)
    sys.stdin.read()
"""


def start_holder(state_dir: Path, *extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", HOLDER_SCRIPT, str(state_dir), *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def read_line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    return process.stdout.readline()


def stop(process: subprocess.Popen[str]) -> None:
    process.kill()
    process.wait(timeout=10)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".treadle"


@pytest.fixture
def holder(state_dir: Path) -> Iterator[subprocess.Popen[str]]:
    """Another process that holds the lock on `state_dir`."""
    process = start_holder(state_dir)
    try:
        assert read_line(process) == "locked\n"
        yield process
    finally:
        stop(process)


def test_another_process_holding_the_lock_blocks_acquire(
    state_dir: Path, holder: subprocess.Popen[str]
) -> None:
    current = InstanceLock.probe(state_dir)

    assert current is not None
    assert (current.pid, current.kind) == (holder.pid, "run")
    with pytest.raises(LockHeldError) as raised:
        InstanceLock(state_dir, HOLDER).acquire()
    assert raised.value.holder == current
    assert str(raised.value) == (
        f"another Treadle process holds the state directory lock (run, pid {holder.pid})"
    )


def test_the_lock_frees_when_its_holder_dies(
    state_dir: Path, holder: subprocess.Popen[str]
) -> None:
    stop(holder)

    # The killed holder never removed holder.json, but the OS released its lock.
    assert (state_dir / "holder.json").exists()
    assert InstanceLock.probe(state_dir) is None
    with InstanceLock(state_dir, HOLDER):
        assert InstanceLock.probe(state_dir) == HOLDER


def test_a_clean_exit_releases_the_lock_and_removes_the_record(
    state_dir: Path, holder: subprocess.Popen[str]
) -> None:
    assert holder.stdin is not None
    holder.stdin.close()
    assert holder.wait(timeout=10) == 0

    assert not (state_dir / "holder.json").exists()
    assert InstanceLock.probe(state_dir) is None


def test_a_process_left_behind_by_a_dead_holder_doesnt_keep_the_lock(state_dir: Path) -> None:
    process = start_holder(state_dir, "with-a-child")
    sleeper_pid: int | None = None
    try:
        sleeper_pid = int(read_line(process))
        assert read_line(process) == "locked\n"

        stop(process)

        # The holder's own child outlives it; the lock descriptor must not have leaked into it.
        assert InstanceLock.probe(state_dir) is None
    finally:
        stop(process)
        if sleeper_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(sleeper_pid, signal.SIGKILL)
