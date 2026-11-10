"""Stop signals, forced cancels, closed pipes, and lost daemons while `treadle run` follows a run.

Every test checks that no step process group outlives the command, before the fixture's
teardown kills whatever is left.
"""

from __future__ import annotations

import json
import select
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.e2e.conftest import Treadle, start_treadle
from tests.integration.daemon.conftest import STUBBORN
from tests.integration.daemon.helpers import (
    Daemon,
    eventually,
    kill_group,
    kill_unfinished_steps,
    surviving_process_groups,
)
from tests.support.project import ProjectFactory

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
