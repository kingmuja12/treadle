"""`treadle cancel` when no process holds the state directory."""

from __future__ import annotations

from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import Cli, read_store, store_runs

from treadle.models import RunReason, RunStatus
from treadle.types import ExitCode


def test_a_queued_run_is_cancelled_in_the_database(cli: Cli, project: ProjectFactory) -> None:
    settings = project.settings()
    store_runs(settings, "wf", lambda runs: runs.queued())

    result = cli("cancel", "1")

    assert result.code == ExitCode.OK
    assert result.out == "Cancelled queued run #1.\n"
    run = read_store(settings, lambda repo: repo.get_run(1))
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.CANCELLED, RunReason.USER)
    assert not settings.holder_path.exists()


def test_a_finished_run_is_left_alone(cli: Cli, project: ProjectFactory) -> None:
    store_runs(project.settings(), "wf", lambda runs: runs.finished(RunStatus.SUCCEEDED))

    text, as_json = cli("cancel", "last"), cli("cancel", "last", "--json")

    assert text.code == as_json.code == ExitCode.OK
    assert text.out == "Run #1 already finished (succeeded).\n"
    assert as_json.json() == {
        "run_id": 1,
        "status": "succeeded",
        "accepted": False,
        "forced": False,
    }


def test_a_run_that_a_crashed_process_left_running_is_recovered_first(
    cli: Cli, project: ProjectFactory
) -> None:
    store_runs(project.settings(), "wf", lambda runs: runs.running())

    result = cli("cancel", "1")

    assert result.code == ExitCode.OK
    assert result.out == "Run #1 already finished (interrupted).\n"
    assert "run=1 was interrupted" in result.err


def test_an_unknown_run_exits_4(cli: Cli, project: ProjectFactory) -> None:
    store_runs(project.settings(), "wf", lambda runs: runs.queued())

    result = cli("cancel", "7")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == "error: run 7 doesn't exist\n"


def test_without_a_database_nothing_exists(cli: Cli, project: ProjectFactory) -> None:
    numbered, latest = cli("cancel", "3"), cli("cancel", "last")

    assert numbered.code == latest.code == ExitCode.NOT_FOUND
    assert numbered.err == "error: run 3 doesn't exist\n"
    assert latest.err == "error: there are no runs yet\n"
    assert not project.settings().db_path.exists()
