"""Ctrl-C while `treadle run` follows a run, in this process or in the daemon."""

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
from tests.integration.daemon.helpers import Daemon, eventually, kill_group, recorded_process_groups
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


@pytest.fixture
def project(tmp_path: Path) -> Iterator[ProjectFactory]:
    """A project whose step processes are killed afterward, whatever happens in the test."""
    project = ProjectFactory(tmp_path / "project")
    project.write_workflow("slow", SLOW)
    project.write_workflow("stubborn", STUBBORN)
    yield project
    for pgid in recorded_process_groups(project.settings().db_path):
        kill_group(pgid)


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
    assert err.splitlines()[0] == "Cancelling run #1 (press Ctrl-C again to force)"
    assert out.splitlines()[-1].startswith("Run #1 cancelled (user) in ")
    run = _run_json(project, treadle, 1)
    assert (run["status"], run["reason"]) == ("cancelled", "user")
    assert [step["status"] for step in run["steps"]] == ["cancelled", "succeeded"]


def test_a_second_ctrl_c_forces_the_run(
    project: ProjectFactory,
    treadle_script: Path,
    treadle: Treadle,
    interrupted: list[subprocess.Popen[str]],
) -> None:
    project.write_config('timezone = "UTC"\n[runs]\nkill_grace = "30s"\n')
    cli = start_treadle(treadle_script, ["run", "stubborn"], cwd=project.root)
    interrupted.append(cli)
    eventually((project.root / "stubborn.pid").exists)

    cli.send_signal(signal.SIGINT)
    time.sleep(0.3)
    started = time.monotonic()
    cli.send_signal(signal.SIGINT)
    cli.communicate(timeout=30)

    assert cli.returncode == 130
    assert time.monotonic() - started < 10  # well under the 30-second kill grace
    run = _run_json(project, treadle, 1)
    assert run["status"] == "cancelled"


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
