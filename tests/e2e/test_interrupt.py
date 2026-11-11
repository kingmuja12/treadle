"""Stop signals, forced cancels, closed pipes, and lost daemons while `treadle run` follows a run.

A third stop signal ends the command at once, even while a file operation holds it up. Every
test checks that no step process group outlives the command, before the fixture's teardown
kills whatever is left.
"""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from tests.e2e.conftest import Treadle, start_treadle
from tests.integration.daemon.conftest import STUBBORN
from tests.integration.daemon.helpers import (
    Daemon,
    child_environ,
    eventually,
    kill_group,
    kill_unfinished_steps,
    recorded_process_groups,
    surviving_process_groups,
)
from tests.support.project import ProjectFactory

from treadle.lock import InstanceLock

# The step waits until it's cancelled; the cleanup step runs because of always().
SLOW = """\
steps:
  - id: wait
    run: |
      echo $$ > "$TREADLE_PROJECT_DIR/wait.pid"
      sleep 30
  - id: cleanup
    if: always()
    run: echo cleaned up
"""

# Each step takes a moment, so the run is still going after its first lines print.
SLOWER = """\
steps:
  - id: one
    run: echo one
  - id: two
    run: sleep 1
  - id: three
    run: echo three
"""

# The step appends to a FIFO that nothing reads, so its worker thread waits in open() for a
# reader, and no cancel can interrupt it.
STUCK = """\
steps:
  - id: append
    uses: file
    with:
      action: append
      path: pipe
      content: hello
"""

# The step leaves a process in the background that survives SIGTERM and notes each one, so
# the end of the run waits out `[runs] kill_grace` for it while the run tracks its group.
# The step ends only once the process has set its trap, which SIGTERM could otherwise beat.
LINGER = """\
steps:
  - id: start
    run: |
      cd "$TREADLE_PROJECT_DIR"
      sh -c 'trap "touch got-term" TERM; touch ready; while :; do sleep 1; done' >/dev/null 2>&1 &
      while [ ! -e ready ]; do sleep 0.1; done
"""

QUICK = """\
steps:
  - id: note
    uses: log
    with:
      message: quick
"""


@pytest.fixture
def project(tmp_path: Path) -> Iterator[ProjectFactory]:
    """A project whose unfinished step processes are killed afterward, whatever happens."""
    project = ProjectFactory(tmp_path / "project")
    project.write_workflow("slow", SLOW)
    project.write_workflow("slower", SLOWER)
    project.write_workflow("stubborn", STUBBORN)
    yield project
    kill_unfinished_steps(project.settings().db_path)


@pytest.fixture
def interrupted(project: ProjectFactory) -> Iterator[list[subprocess.Popen[str]]]:
    """CLI processes that a test starts; each is killed with its group if it's still running."""
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        if process.poll() is None:
            kill_group(process.pid)
            process.wait(timeout=10)


def _read_line(process: subprocess.Popen[str], within: float = 20.0) -> str:
    """Read one line of the process's output, failing after `within` seconds."""
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], within)
    if not ready:
        raise AssertionError(f"no output within {within}s")
    return process.stdout.readline()


def _run_json(project: ProjectFactory, treadle: Treadle, run_id: int) -> dict[str, object]:
    shown = treadle("show", str(run_id), "--json", cwd=project.root)
    return dict(json.loads(shown.stdout))


def _survivors(project: ProjectFactory) -> list[int]:
    return surviving_process_groups(project.settings().db_path)


def _start_logged(
    script: Path, args: Sequence[str], *, cwd: Path, out: Path, err: Path
) -> subprocess.Popen[str]:
    """Start `treadle ARGS...` in its own process group, with its output in `out` and `err`.

    A test polls the files for the line that each stage prints, however the writes split.
    """
    with out.open("wb") as stdout, err.open("wb") as stderr:
        return subprocess.Popen(
            [str(script), *args],
            cwd=cwd,
            env=child_environ({"TZ": "UTC"}),
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )


def _printed(path: Path, text: str) -> None:
    """Wait until the output in `path` holds `text`."""
    eventually(lambda: text in path.read_text(encoding="utf-8"))


def _attempt_status(db_path: Path, step_id: str) -> str | None:
    """Return the status of run 1's last attempt of `step_id`, or None before it's stored."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM step_attempts WHERE run_id = 1 AND step_id = ? "
            "ORDER BY attempt DESC LIMIT 1",
            (step_id,),
        ).fetchone()
    except sqlite3.Error:  # the schema isn't there yet
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _drained(fifo: Path) -> bytes:
    """Read what a FIFO holds without waiting for a writer: b"" when none has it open."""
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return os.read(fd, 1024)
    finally:
        os.close(fd)


def test_ctrl_c_cancels_a_local_run_and_waits_for_its_cleanup(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    cli = start_treadle(treadle_script, ["run", "slow"], cwd=project.root)
    interrupted.append(cli)
    eventually((project.root / "wait.pid").exists)

    cli.send_signal(signal.SIGINT)
    out, err = cli.communicate(timeout=30)

    assert cli.returncode == 130
    assert err == "Cancelling run #1 (press Ctrl+C again to force)\n"
    assert out.splitlines()[-1].startswith("Run #1 cancelled (user) in ")
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("cancelled", "user")
    assert [step["status"] for step in run["steps"]] == ["cancelled", "succeeded"]
    assert _survivors(project) == []


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_sighup_cancel_a_local_run_and_wait_for_its_cleanup(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
    number: signal.Signals,
) -> None:
    cli = start_treadle(treadle_script, ["run", "slow"], cwd=project.root)
    interrupted.append(cli)
    eventually((project.root / "wait.pid").exists)

    cli.send_signal(number)
    out, err = cli.communicate(timeout=30)

    assert cli.returncode == 128 + number
    assert err == f"Cancelling run #1 ({number.name}; signal again to force)\n"
    assert out.splitlines()[-1].startswith("Run #1 cancelled (shutdown) in ")
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("cancelled", "shutdown")
    assert [step["status"] for step in run["steps"]] == ["cancelled", "succeeded"]
    assert _survivors(project) == []


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_a_second_stop_signal_forces_the_run_and_kills_its_processes(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
    number: signal.Signals,
) -> None:
    project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "30s"\n')
    cli = start_treadle(treadle_script, ["run", "stubborn"], cwd=project.root)
    interrupted.append(cli)
    eventually((project.root / "stubborn.pid").exists)

    cli.send_signal(number)
    time.sleep(0.3)
    started = time.monotonic()
    cli.send_signal(number)
    _, err = cli.communicate(timeout=30)

    assert cli.returncode == 128 + number
    assert time.monotonic() - started < 10  # well under the 30-second kill grace
    first = (
        "Cancelling run #1 (press Ctrl+C again to force)"
        if number == signal.SIGINT
        else f"Cancelling run #1 ({number.name}; signal again to force)"
    )
    assert err == f"{first}\n"  # and no traceback
    # The step ignores SIGTERM, yet nothing of it outlives the command.
    assert _survivors(project) == []
    assert _run_json(project, treadle, 1)["status"] == "cancelled"


def test_a_third_ctrl_c_ends_a_run_whose_file_operation_is_stuck(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
    tmp_path: Path,
) -> None:
    project.write_workflow("stuck", STUCK)
    fifo = project.root / "pipe"
    os.mkfifo(fifo)
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    cli = _start_logged(treadle_script, ["run", "stuck"], cwd=project.root, out=out, err=err)
    interrupted.append(cli)
    eventually(lambda: _attempt_status(project.settings().db_path, "append") == "running")

    cli.send_signal(signal.SIGINT)
    _printed(err, "Cancelling run #1 (press Ctrl+C again to force)\n")
    cli.send_signal(signal.SIGINT)
    # The forced run ended, but the operation's thread still waits for a reader of the FIFO.
    _printed(out, "Run #1 cancelled (user) in ")
    cli.send_signal(signal.SIGINT)
    code = cli.wait(timeout=20)

    assert code == 130
    assert err.read_text(encoding="utf-8") == "Cancelling run #1 (press Ctrl+C again to force)\n"
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("cancelled", "user")
    assert [step["status"] for step in run["steps"]] == ["cancelled"]
    # Nothing outlives the command: no step process, and no thread that waits to write.
    assert _survivors(project) == []
    assert _drained(fifo) == b""
    assert InstanceLock.probe(project.settings().state_dir) is None


def test_one_sigterm_ends_a_command_that_only_waits_for_a_stuck_file_operation(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
    tmp_path: Path,
) -> None:
    project.write_workflow("stuck", STUCK)
    fifo = project.root / "pipe"
    os.mkfifo(fifo)
    state_dir = project.settings().state_dir
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    cli = _start_logged(treadle_script, ["run", "stuck"], cwd=project.root, out=out, err=err)
    interrupted.append(cli)
    eventually(lambda: _attempt_status(project.settings().db_path, "append") == "running")

    # Another terminal cancels the run and then forces it, so the run ends while the
    # operation's thread still waits for a reader of the FIFO.
    first = treadle("cancel", "1", cwd=project.root)
    forced = treadle("cancel", "1", "--force", cwd=project.root)
    _printed(out, "Run #1 cancelled (user) in ")
    # The command released the lock as its runner closed, so only the thread holds it up.
    eventually(lambda: InstanceLock.probe(state_dir) is None)
    cli.send_signal(signal.SIGTERM)  # as `kill`, `systemctl stop`, or `docker stop` sends it
    code = cli.wait(timeout=20)

    assert (first.returncode, forced.returncode) == (0, 0)
    assert code == 128 + signal.SIGTERM
    assert err.read_text(encoding="utf-8") == ""  # there was no run left to cancel
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("cancelled", "user")
    assert _survivors(project) == []
    assert _drained(fifo) == b""  # no thread is left to write to it


def test_a_third_stop_signal_kills_what_the_run_tracks_and_leaves_the_run_to_recovery(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
    tmp_path: Path,
) -> None:
    project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "60s"\n')
    project.write_workflow("linger", LINGER)
    project.write_workflow("quick", QUICK)
    settings = project.settings()
    out, err = tmp_path / "out.txt", tmp_path / "err.txt"
    cli = _start_logged(treadle_script, ["run", "linger"], cwd=project.root, out=out, err=err)
    interrupted.append(cli)
    try:
        # The step ended, and the end of the run sent SIGTERM to the process that it left.
        eventually((project.root / "got-term").exists)
        cli.send_signal(signal.SIGINT)
        _printed(err, "Cancelling run #1 (press Ctrl+C again to force)\n")
        # An ending run takes no cancel, so no output shows the next signal. Signals of two
        # kinds can't merge into one delivery, as two SIGINTs sent together can.
        cli.send_signal(signal.SIGTERM)
        cli.send_signal(signal.SIGHUP)
        code = cli.wait(timeout=20)
        left = _survivors(project)
    finally:
        for pgid in recorded_process_groups(settings.db_path):
            kill_group(pgid)
    left_behind = _run_json(project, treadle, 1)
    after = treadle("run", "quick", cwd=project.root)
    recovered = _run_json(project, treadle, 1)

    assert code == 130  # at once, long before the 60 seconds of kill grace end
    assert err.read_text(encoding="utf-8") == "Cancelling run #1 (press Ctrl+C again to force)\n"
    assert left == []
    assert left_behind["status"] == "running"  # until the next lock holder recovers it
    assert (after.returncode, after.stderr) == (
        0,
        "warning: run=1 was interrupted: the Treadle process stopped while it ran\n",
    )
    assert (recovered["status"], recovered["reason"]) == ("interrupted", "executor_stopped")
    assert [step["status"] for step in recovered["steps"]] == ["succeeded"]
    assert list(settings.tmp_dir.iterdir()) == []


def test_cancel_then_cancel_force_from_another_terminal_stops_the_run_at_once(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "30s"\n')
    cli = start_treadle(treadle_script, ["run", "stubborn"], cwd=project.root)
    interrupted.append(cli)
    eventually((project.root / "stubborn.pid").exists)

    first = treadle("cancel", "1", cwd=project.root)
    forced = treadle("cancel", "1", "--force", cwd=project.root)
    started = time.monotonic()
    _, err = cli.communicate(timeout=30)

    assert (first.returncode, first.stdout) == (0, "Cancel requested for run #1.\n")
    assert (forced.returncode, forced.stdout) == (0, "Cancel requested for run #1 (forced).\n")
    assert cli.returncode == 5
    assert time.monotonic() - started < 10
    assert err == ""
    assert _survivors(project) == []
    assert _run_json(project, treadle, 1)["status"] == "cancelled"


def test_a_closed_pipe_lets_a_local_run_finish(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    cli = start_treadle(treadle_script, ["run", "slower"], cwd=project.root)
    interrupted.append(cli)
    assert cli.stdout is not None
    assert cli.stderr is not None

    header = _read_line(cli)  # what `| head -1` reads before it exits
    cli.stdout.close()
    code = cli.wait(timeout=30)

    assert header == "Run #1  slower  (manual)\n"
    assert code == 1
    assert cli.stderr.read() == ""
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("succeeded", None)
    assert [step["status"] for step in run["steps"]] == ["succeeded"] * 3


def test_ctrl_c_stops_following_a_daemon_run_and_leaves_it_running(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    daemon = Daemon(project).start()
    try:
        daemon.wait_ready()
        cli = start_treadle(treadle_script, ["run", "slow"], cwd=project.root)
        interrupted.append(cli)
        header = _read_line(cli)  # following has begun
        eventually((project.root / "wait.pid").exists)

        cli.send_signal(signal.SIGINT)
        rest, err = cli.communicate(timeout=30)
        still_running = daemon.run_status(1)
    finally:
        daemon.cleanup()

    assert cli.returncode == 130
    assert header == "Run #1  slow  (manual)\n"
    assert "Run #1" not in rest  # nothing reported the run as finished
    assert err == "Run #1 continues in the daemon. Follow it with `treadle logs 1 -f`.\n"
    assert still_running is not None
    assert still_running["status"] == "running"


@pytest.mark.parametrize("command", [["run", "slow"], ["logs", "1", "-f"]])
def test_a_follower_stops_when_the_daemon_dies(
    project: ProjectFactory,
    treadle_script: Path,
    interrupted: list[subprocess.Popen[str]],
    command: list[str],
) -> None:
    daemon = Daemon(project).start()
    try:
        daemon.wait_ready()
        runner = start_treadle(treadle_script, ["run", "slow"], cwd=project.root)
        interrupted.append(runner)
        _read_line(runner)  # the daemon created run 1
        eventually((project.root / "wait.pid").exists)
        follower = runner
        if command[0] == "logs":
            follower = start_treadle(treadle_script, command, cwd=project.root)
            interrupted.append(follower)

        kill_group(daemon.pid)  # as an out-of-memory kill would
        daemon.wait_exit()
        started = time.monotonic()
        _, err = follower.communicate(timeout=30)
    finally:
        daemon.cleanup()

    assert follower.returncode == 8
    assert time.monotonic() - started < 10
    if command[0] == "run":
        assert err == (
            f"error: the daemon (pid {daemon.pid}) stopped; run #1 will be marked interrupted "
            "when Treadle starts again\n"
        )
    else:
        assert err == (
            "error: no Treadle process is running run #1 any more; it will be marked "
            "interrupted when Treadle starts again\n"
        )


def test_a_follower_sees_the_next_daemon_interrupt_the_run(
    project: ProjectFactory,
    treadle_script: Path,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    first = Daemon(project, name="first").start()
    second = Daemon(project, name="second")
    try:
        first.wait_ready()
        cli = start_treadle(treadle_script, ["run", "slow"], cwd=project.root)
        interrupted.append(cli)
        _read_line(cli)
        eventually((project.root / "wait.pid").exists)

        # The follower pauses while the daemon restarts, however long that takes, so it next
        # finds the new daemon holding the lock.
        cli.send_signal(signal.SIGSTOP)
        try:
            kill_group(first.pid)
            first.wait_exit()
            second.start().wait_ready()
        finally:
            cli.send_signal(signal.SIGCONT)
        _, err = cli.communicate(timeout=30)
    finally:
        first.cleanup()
        second.cleanup()

    assert cli.returncode == 5
    assert err == ""
