"""`treadle run` and `treadle cancel` through the lock holder's control requests."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Iterator

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import (
    BROKEN,
    COUNT,
    GREET,
    SLOW,
    Cli,
    InProcessDaemon,
    control_rows,
    read_store,
    store_runs,
)

from treadle import __version__
from treadle.cli import commands
from treadle.clock import SystemClock, iso_utc
from treadle.lock import InstanceLock
from treadle.models import ControlRequest, RunStatus
from treadle.types import ExitCode, JSONObject, LockHolder


def _eventually(check: Callable[[], bool], *, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"the condition didn't hold within {within}s")
        time.sleep(0.05)


def _status(project: ProjectFactory, run_id: int) -> RunStatus | None:
    run = read_store(project.settings(), lambda repo: repo.get_run(run_id))
    return None if run is None else run.status


def test_a_run_is_started_by_the_daemon_and_followed(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    with InProcessDaemon(project.settings()):
        result = cli("run", "greet", "-i", "name=Ada")

    assert result.code == ExitCode.OK
    assert result.out.startswith("Run #1  greet  (manual)\n")
    assert "Run #1 succeeded in " in result.out
    (request,) = control_rows(project.settings())
    assert (request["kind"], request["consumed"]) == ("run", True)
    assert request["result"] == {"run_id": 1, "outcome": "queued"}


def test_detach_returns_once_the_daemon_queued_the_run(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    with InProcessDaemon(project.settings()):
        text = cli("run", "greet", "--detach")
        as_json = cli("run", "greet", "--detach", "--json")

    assert text.code == as_json.code == ExitCode.OK
    assert text.out == "Run #1 queued\n"
    (event,) = as_json.json_lines()
    assert event["event"] == "run_created"
    assert event["run"]["id"] == 2


def test_a_skipped_run_exits_7_and_cancel_reaches_the_active_run(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("slow", SLOW)

    with InProcessDaemon(project.settings()):
        first = cli("run", "slow", "--detach")
        _eventually(lambda: _status(project, 1) is RunStatus.RUNNING)
        second = cli("run", "slow", "--detach")
        cancel = cli("cancel", "1")
        _eventually(lambda: _status(project, 1) is RunStatus.CANCELLED)
        again = cli("cancel", "1", "--json")

    assert first.out == "Run #1 queued\n"
    assert (second.code, second.out) == (ExitCode.SKIPPED, "Run #2 skipped (already_active)\n")
    assert (cancel.code, cancel.out) == (ExitCode.OK, "Cancel requested for run #1.\n")
    assert again.json() == {"run_id": 1, "status": "cancelled", "accepted": False, "forced": False}


def test_a_rerun_goes_to_the_daemon(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    with InProcessDaemon(project.settings()):
        cli("run", "greet")
        result = cli("run", "--rerun", "1", "--json")

    assert result.code == ExitCode.OK
    created = result.json_lines()[0]["run"]
    assert (created["id"], created["rerun_of"], created["attempt"]) == (2, 1, 2)
    requests = control_rows(project.settings())
    assert requests[1]["result"] == {"run_id": 2, "outcome": "queued"}


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (("ghost",), ExitCode.NOT_FOUND, "error: workflow 'ghost' doesn't exist\n"),
        (("count", "-i", "count=x"), ExitCode.USAGE, "error: input 'count' must be an integer\n"),
        (("--rerun", "5"), ExitCode.NOT_FOUND, "error: run 5 doesn't exist\n"),
    ],
)
def test_the_daemon_refusals_map_to_exit_codes(
    cli: Cli, project: ProjectFactory, args: tuple[str, ...], code: int, message: str
) -> None:
    project.write_workflow("count", COUNT)

    with InProcessDaemon(project.settings()):
        result = cli("run", *args)

    assert (result.code, result.err) == (code, message)


def test_an_invalid_workflow_shows_the_issues_of_its_file(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("broken", BROKEN)

    with InProcessDaemon(project.settings()):
        result = cli("run", "broken")

    assert result.code == ExitCode.CONFIG
    assert result.err.startswith("error: workflow 'broken' is invalid\n")
    assert "workflows/broken.yml:2:1: error[E101]: unknown key 'stpes' in workflow" in result.err


def test_an_unanswered_request_is_withdrawn(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    monkeypatch.setattr(commands, "_RUN_REPLY_WAIT", 0.3)

    with InProcessDaemon(project.settings(), poll=False) as daemon:
        result = cli("run", "greet")
        handled = daemon.process_requests()

    assert result.code == ExitCode.BUSY
    assert result.err == f"error: the daemon (pid {os.getpid()}) didn't respond\n"
    assert handled == 0
    (request,) = control_rows(project.settings())
    assert request["consumed"] is True
    assert request["result"] == {
        "error": {
            "code": "expired",
            "message": "the CLI stopped waiting for an answer",
            "details": None,
        }
    }
    assert read_store(project.settings(), lambda repo: repo.latest_run_id()) is None


def test_an_unanswered_cancel_is_withdrawn_too(cli: Cli, project: ProjectFactory) -> None:
    settings = project.settings()
    store_runs(settings, "wf", lambda runs: runs.queued())

    with InProcessDaemon(settings, poll=False) as daemon:
        result = cli("cancel", "1", "--wait", "0.3")
        handled = daemon.process_requests()

    assert result.code == ExitCode.BUSY
    assert result.err == f"error: the daemon (pid {os.getpid()}) didn't respond\n"
    assert handled == 0
    assert _status(project, 1) is RunStatus.QUEUED


def test_cancel_without_waiting_only_sends_the_request(cli: Cli, project: ProjectFactory) -> None:
    settings = project.settings()
    store_runs(settings, "wf", lambda runs: runs.queued())

    with InProcessDaemon(settings, poll=False) as daemon:
        text = cli("cancel", "1", "--wait", "0")
        as_json = cli("cancel", "1", "--wait", "0", "--json", "--force")
        handled = daemon.process_requests()

    assert (text.code, text.out) == (ExitCode.OK, "Cancel requested.\n")
    assert as_json.json() == {"run_id": 1, "status": "queued", "accepted": None, "forced": True}
    assert handled == 2
    assert _status(project, 1) is RunStatus.CANCELLED


def test_a_foreground_run_holder_refuses_runs_but_handles_cancels(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("greet", GREET)
    settings = project.settings()
    store_runs(settings, "greet", lambda runs: runs.queued())

    with InProcessDaemon(settings, kind="run"):
        refused = cli("run", "greet")
        cancelled = cli("cancel", "1")

    assert refused.code == ExitCode.BUSY
    assert refused.err == (
        f"error: another `treadle run` (pid {os.getpid()}) holds .treadle; "
        "start `treadle serve` to run workflows concurrently\n"
    )
    assert (cancelled.code, cancelled.out) == (ExitCode.OK, "Cancelled queued run #1.\n")


def test_a_maintenance_holder_makes_run_and_cancel_wait_then_give_up(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    monkeypatch.setattr(commands, "_MAINTENANCE_WAIT", 0.2)
    holder = LockHolder(os.getpid(), "maintenance", iso_utc(SystemClock().now()), __version__)

    with InstanceLock(project.settings().state_dir, holder):
        run, cancel = cli("run", "greet"), cli("cancel", "1")

    message = (
        f"error: another Treadle process (pid {os.getpid()}) is maintaining .treadle; try again\n"
    )
    assert (run.code, run.err) == (ExitCode.BUSY, message)
    assert (cancel.code, cancel.err) == (ExitCode.BUSY, message)


@contextlib.contextmanager
def _pressed_at_once(callback: Callable[[], None]) -> Iterator[None]:
    """Stand in for the SIGINT handler: Ctrl-C is pressed as soon as it's installed."""
    callback()
    yield


def test_ctrl_c_before_the_daemon_answers_withdraws_the_request(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    monkeypatch.setattr(commands, "_on_interrupt", _pressed_at_once)

    with InProcessDaemon(project.settings(), poll=False) as daemon:
        result = cli("run", "greet")
        handled = daemon.process_requests()

    assert result.code == ExitCode.INTERRUPTED
    assert result.err == "Stopped before the daemon started a run.\n"
    assert handled == 0
    assert control_rows(project.settings())[0]["consumed"] is True


def _answer(result: JSONObject) -> Callable[[ControlRequest], JSONObject]:
    return lambda request: result


def _error(code: str, message: str = "") -> JSONObject:
    return {"error": {"code": code, "message": message, "details": None}}


@pytest.mark.parametrize(
    ("answer", "code", "output"),
    [
        (
            {"run_id": 1, "accepted": False, "status": "running", "forced": False},
            ExitCode.OK,
            "Run #1 is already finishing.\n",
        ),
        (
            {"run_id": 1, "accepted": True, "status": "running", "forced": True},
            ExitCode.OK,
            "Cancel requested for run #1 (forced).\n",
        ),
        (
            {"run_id": 1, "accepted": True, "status": "weird", "forced": False},
            ExitCode.FAILURE,
            "error: the lock holder answered with an unknown status 'weird'\n",
        ),
        (
            _error("shutting_down", "Treadle is shutting down"),
            ExitCode.BUSY,
            "error: Treadle is shutting down\n",
        ),
        (
            _error("mystery"),
            ExitCode.FAILURE,
            "error: the lock holder refused the request (mystery)\n",
        ),
    ],
)
def test_cancel_reports_the_lock_holders_answer(
    cli: Cli, project: ProjectFactory, answer: JSONObject, code: int, output: str
) -> None:
    settings = project.settings()
    store_runs(settings, "wf", lambda runs: runs.running())

    with InProcessDaemon(settings, answers=_answer(answer)):
        result = cli("cancel", "1")

    assert result.code == code
    assert (result.out if code == ExitCode.OK else result.err) == output


@pytest.mark.parametrize(
    ("answer", "code", "message"),
    [
        (
            {"run_id": None, "outcome": "duplicate"},
            ExitCode.FAILURE,
            "error: the daemon didn't create a run (duplicate)\n",
        ),
        (_error("busy", "the daemon is busy"), ExitCode.BUSY, "error: the daemon is busy\n"),
        (_error("conflict", "run 4 is running"), ExitCode.FAILURE, "error: run 4 is running\n"),
        (
            _error("expired"),
            ExitCode.BUSY,
            "error: the lock holder refused the request (expired)\n",
        ),
    ],
)
def test_run_reports_the_daemons_refusal(
    cli: Cli, project: ProjectFactory, answer: JSONObject, code: int, message: str
) -> None:
    project.write_workflow("greet", GREET)

    with InProcessDaemon(project.settings(), answers=_answer(answer)):
        result = cli("run", "greet")

    assert (result.code, result.err) == (code, message)


def test_a_workflow_that_only_the_daemon_finds_invalid_points_at_validate(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("greet", GREET)

    with InProcessDaemon(project.settings(), answers=_answer(_error("workflow_invalid", "x"))):
        result = cli("run", "greet")

    assert result.code == ExitCode.CONFIG
    assert result.err == "error: workflow 'greet' is invalid\n`treadle validate` shows why.\n"


def test_a_holder_whose_record_isnt_written_yet_counts_as_maintenance(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    monkeypatch.setattr(commands, "_MAINTENANCE_WAIT", 0.2)
    settings = project.settings()
    holder = LockHolder(os.getpid(), "serve", iso_utc(SystemClock().now()), __version__)

    with InstanceLock(settings.state_dir, holder):
        settings.holder_path.unlink()
        result = cli("run", "greet")

    assert result.code == ExitCode.BUSY
    assert result.err == "error: another Treadle process is maintaining .treadle; try again\n"
