from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from treadle.errors import LockHeldError
from treadle.lock import InstanceLock
from treadle.types import ExitCode, LockHolder

SERVE = LockHolder(os.getpid(), "serve", "2026-11-09T12:00:00.000Z", "0.1.0", "127.0.0.1")
BOUND = LockHolder(os.getpid(), "serve", "2026-11-09T12:00:00.000Z", "0.1.0", "127.0.0.1", 8765)
RUN = LockHolder(os.getpid(), "run", "2026-11-09T12:05:00.000Z", "0.1.0")
UNKNOWN = LockHolder(0, "maintenance", "", "")


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".treadle"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def test_acquire_records_the_holder_and_release_removes_it(state_dir: Path) -> None:
    lock = InstanceLock(state_dir, SERVE)

    lock.acquire()
    try:
        assert json.loads((state_dir / "holder.json").read_text()) == SERVE.to_json()
        assert mode(state_dir) == 0o700
        assert mode(state_dir / "treadle.lock") == 0o600
        assert mode(state_dir / "holder.json") == 0o600
        assert InstanceLock.probe(state_dir) == SERVE
    finally:
        lock.release()

    assert files(state_dir) == ["treadle.lock"]
    assert InstanceLock.probe(state_dir) is None


def test_a_second_lock_fails_with_the_current_holder(state_dir: Path) -> None:
    with InstanceLock(state_dir, SERVE):
        with pytest.raises(LockHeldError) as raised:
            InstanceLock(state_dir, RUN).acquire()

        assert raised.value.holder == SERVE
        assert raised.value.exit_code == ExitCode.BUSY
        assert InstanceLock.probe(state_dir) == SERVE

    with InstanceLock(state_dir, RUN):
        assert InstanceLock.probe(state_dir) == RUN


def test_probe_without_a_lock_file_finds_no_holder(tmp_path: Path) -> None:
    assert InstanceLock.probe(tmp_path / "missing") is None
    assert InstanceLock.probe(tmp_path) is None


def test_a_holder_without_a_record_reads_as_maintenance(state_dir: Path) -> None:
    with InstanceLock(state_dir, SERVE):
        (state_dir / "holder.json").unlink()

        assert InstanceLock.probe(state_dir) == UNKNOWN
        with pytest.raises(LockHeldError) as raised:
            InstanceLock(state_dir, RUN).acquire()
        assert raised.value.holder is None


@pytest.mark.parametrize("record", ["{not json", '{"pid": "1"}', "[]", "\udcff"])
def test_a_malformed_record_reads_as_maintenance(state_dir: Path, record: str) -> None:
    with InstanceLock(state_dir, SERVE):
        (state_dir / "holder.json").write_text(record, errors="surrogateescape")

        assert InstanceLock.probe(state_dir) == UNKNOWN


def test_a_record_left_by_a_dead_holder_is_ignored_and_replaced(state_dir: Path) -> None:
    state_dir.mkdir()
    (state_dir / "treadle.lock").touch()
    (state_dir / "holder.json").write_text(json.dumps(RUN.to_json()))

    assert InstanceLock.probe(state_dir) is None
    with InstanceLock(state_dir, SERVE):
        assert InstanceLock.probe(state_dir) == SERVE


def test_update_rewrites_the_record_atomically(state_dir: Path) -> None:
    with InstanceLock(state_dir, SERVE) as lock:
        lock.update(BOUND)

        assert InstanceLock.probe(state_dir) == BOUND
        assert mode(state_dir / "holder.json") == 0o600
        assert files(state_dir) == ["holder.json", "treadle.lock"]


def test_a_failed_update_keeps_the_previous_record(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_half(value: Any, file: Any) -> None:
        file.write('{"pid": ')
        raise OSError("disk full")

    with InstanceLock(state_dir, SERVE) as lock:
        monkeypatch.setattr(json, "dump", write_half)
        with pytest.raises(OSError, match="disk full"):
            lock.update(BOUND)
        monkeypatch.undo()

        assert InstanceLock.probe(state_dir) == SERVE
        assert files(state_dir) == ["holder.json", "treadle.lock"]


def test_update_needs_the_lock(state_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="isn't held"):
        InstanceLock(state_dir, SERVE).update(BOUND)


def test_release_without_the_lock_does_nothing(state_dir: Path) -> None:
    lock = InstanceLock(state_dir, SERVE)
    lock.release()

    with lock:
        pass
    lock.release()

    assert InstanceLock.probe(state_dir) is None


def test_a_failure_while_recording_releases_the_lock(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(value: Any, file: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(json, "dump", refuse)
    with pytest.raises(OSError, match="read-only"):
        InstanceLock(state_dir, SERVE).acquire()
    monkeypatch.undo()

    assert InstanceLock.probe(state_dir) is None
    assert files(state_dir) == ["treadle.lock"]


def test_a_lock_file_that_cannot_be_locked_raises_the_os_error(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_locks(fd: int, operation: int) -> None:
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr("fcntl.flock", no_locks)

    with pytest.raises(OSError, match="not supported"):
        InstanceLock(state_dir, SERVE).acquire()
