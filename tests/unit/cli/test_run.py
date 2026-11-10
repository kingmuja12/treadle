"""`treadle run` in the foreground: progress, exit codes, inputs, and re-runs."""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable, Iterator

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import (
    BROKEN,
    COUNT,
    FLAKY,
    GREET,
    SLOW,
    Cli,
    read_store,
    store_runs,
)
from tests.unit.store.helpers import T0, Runs

from treadle.cli import commands
from treadle.models import RunStatus
from treadle.types import ExitCode

_DURATION = r"\d+(\.\d+)?m?s"


def test_a_run_prints_its_progress_and_succeeds(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    result = cli("run", "greet")

    assert result.code == ExitCode.OK
    lines = result.out.splitlines()
    assert lines[0] == "Run #1  greet  (manual)"
    assert re.fullmatch(rf"  ✓ greet   {_DURATION}", lines[1])
    assert re.fullmatch(rf"  ✓ done    {_DURATION}", lines[2])
    assert re.fullmatch(rf"Run #1 succeeded in {_DURATION}\.", lines[3])
    assert len(lines) == 4


def test_a_failed_run_leads_with_the_failure(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("flaky", FLAKY)

    result = cli("run", "flaky")

    assert result.code == ExitCode.FAILURE
    lines = result.out.splitlines()
    assert lines[0] == "Run #1  flaky  (manual)"
    assert re.fullmatch(
        r"  ✗ fetch    attempt 1/2: source .*missing\.txt doesn't exist; retrying in 0ms", lines[1]
    )
    assert re.fullmatch(
        rf"  ✗ fetch    attempt 2/2: source .*missing\.txt doesn't exist \({_DURATION}\)", lines[2]
    )
    assert lines[3] == "  ⊘ report   skipped (upstream_failed)"
    assert re.fullmatch(rf"  ✓ alert    {_DURATION}", lines[4])
    assert lines[5] == "--- fetch, attempt 2: last 20 lines ---"
    assert re.fullmatch(r"attempt 2 failed: source .*missing\.txt doesn't exist", lines[6])
    assert re.fullmatch(rf"Run #1 failed in {_DURATION}\. Details: treadle show 1", lines[-1])


def test_verbose_streams_the_log_lines(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    result = cli("run", "greet", "-v", "-i", "name=Ada")

    assert result.code == ExitCode.OK
    assert "  │ greet#1 system: [info] Hello, Ada!" in result.out.splitlines()
    assert "  │ done#1 system: [info] greeted in run 1" in result.out.splitlines()


def test_json_prints_the_run_events(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    result = cli("run", "greet", "--json", "-v")

    assert result.code == ExitCode.OK
    events = result.json_lines()
    assert events[0]["event"] == "run_created"
    assert events[0]["run"]["workflow"] == "greet"
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["run"]["status"] == "succeeded"
    assert events[-1]["exit_code"] == 0
    kinds = {event["event"] for event in events}
    assert kinds == {"run_created", "step", "log", "run_finished"}
    finished_steps = [
        event["step"]["id"]
        for event in events
        if event["event"] == "step" and event["step"]["status"] == "succeeded"
    ]
    assert finished_steps == ["greet", "done"]


def test_inputs_are_strings_coerced_by_the_workflow(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("count", COUNT)

    good, bad, missing = (
        cli("run", "count", "-i", "count=3", "-v"),
        cli("run", "count", "-i", "count=three"),
        cli("run", "count"),
    )

    assert good.code == ExitCode.OK
    assert "  │ show#1 system: [info] count is 3" in good.out.splitlines()
    assert bad.code == missing.code == ExitCode.USAGE
    assert bad.err == "error: input 'count' must be an integer\n"
    assert missing.err == "error: input 'count' is required\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("count", "-i", "count"), "error: input 'count' has no value (write count=VALUE)\n"),
        ((), "error: name the WORKFLOW to run, or re-run one with --rerun RUN\n"),
        (("--rerun", "1", "count"), "error: --rerun replaces WORKFLOW and its inputs;"),
        (("count", "--detach"), "error: --detach needs a running daemon;"),
    ],
)
def test_usage_errors(
    cli: Cli, project: ProjectFactory, args: tuple[str, ...], message: str
) -> None:
    project.write_workflow("count", COUNT)

    result = cli("run", *args)

    assert result.code == ExitCode.USAGE
    assert result.err.startswith(message)


def test_an_unknown_workflow_exits_4(cli: Cli) -> None:
    result = cli("run", "ghost")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == "error: workflow 'ghost' doesn't exist\n"


def test_an_invalid_workflow_exits_3_with_its_issues(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("broken", BROKEN)

    result = cli("run", "broken")

    assert result.code == ExitCode.CONFIG
    lines = result.err.splitlines()
    assert lines[0] == "error: workflow 'broken' is invalid"
    assert "workflows/broken.yml:2:1: error[E101]: unknown key 'stpes' in workflow" in lines
    assert " 2 | stpes:" in lines


def test_a_run_that_outlasts_its_workflow_timeout_exits_6(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow(
        "slow",
        "timeout: 1s\nsteps:\n  - id: nap\n    uses: sleep\n    with:\n      duration: 30s\n",
    )

    result = cli("run", "slow")

    assert result.code == ExitCode.TIMED_OUT
    assert "Run #1 timed_out (timeout) in " in result.out


def test_a_rerun_is_a_new_run_of_the_current_version(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)
    cli("run", "greet", "-i", "name=Ada")

    result = cli("run", "--rerun", "last")

    assert result.code == ExitCode.OK
    assert result.out.startswith("Run #2  greet  (manual)")
    rerun = read_store(project.settings(), lambda repo: repo.get_run(2))
    assert rerun is not None
    assert (rerun.rerun_of, rerun.attempt, rerun.inputs) == (1, 2, {"name": "Ada"})


def test_rerunning_an_unknown_run_exits_4(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)
    cli("run", "greet")

    result = cli("run", "--rerun", "#9")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == "error: run 9 doesn't exist\n"


def test_a_run_leaves_the_lock_free_and_no_scratch_directory(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("greet", GREET)

    cli("run", "greet")

    settings = project.settings()
    assert not settings.holder_path.exists()
    assert list(settings.tmp_dir.iterdir()) == []
    run = read_store(settings, lambda repo: repo.get_run(1))
    assert run is not None
    assert run.status is RunStatus.SUCCEEDED


def test_a_run_first_recovers_what_a_crashed_process_left(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("greet", GREET)
    own_group = os.getpgrp()  # a group that exists; recovery reports it and never signals it

    def crashed(runs: Runs) -> None:
        run = runs.running(steps=("build",))
        runs.repo.start_attempt(run.id, "build", 1, at_ms=T0, delay_before_ms=0)
        runs.repo.set_attempt_pgid(run.id, "build", 1, own_group)

    store_runs(project.settings(), "greet", crashed)

    result = cli("run", "greet")

    assert result.code == ExitCode.OK
    assert result.out.startswith("Run #2  greet  (manual)\n")
    assert result.err.splitlines() == [
        "warning: run=1 was interrupted: the Treadle process stopped while it ran",
        f"warning: process group {own_group} of an interrupted run still exists; Treadle "
        "doesn't signal it, because its ID may belong to another process now",
    ]


@contextlib.contextmanager
def _pressed_at_once(callback: Callable[[], None]) -> Iterator[None]:
    """Stand in for the SIGINT handler: Ctrl-C is pressed as soon as it's installed."""
    callback()
    yield


def test_ctrl_c_cancels_the_run_and_exits_130(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("slow", SLOW)
    monkeypatch.setattr(commands, "_on_interrupt", _pressed_at_once)

    result = cli("run", "slow")

    assert result.code == ExitCode.INTERRUPTED
    assert result.err == "Cancelling run #1 (press Ctrl-C again to force)\n"
    assert "Run #1 cancelled (user) in " in result.out
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert run.status is RunStatus.CANCELLED


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("runs", "--limit", "many"), "--limit: expected a whole number, got 'many'"),
        (("runs", "--before", "0"), "--before: must be at least 1"),
        (("cancel", "1", "--wait", "soon"), "--wait: expected a number of seconds, got 'soon'"),
        (("cancel", "1", "--wait", "-1"), "--wait: must be a non-negative number of seconds"),
        (("cancel", "1", "--wait", "inf"), "--wait: must be a non-negative number of seconds"),
    ],
)
def test_bad_option_values_are_usage_errors(cli: Cli, args: tuple[str, ...], message: str) -> None:
    result = cli(*args)

    assert result.code == ExitCode.USAGE
    assert message in result.err
