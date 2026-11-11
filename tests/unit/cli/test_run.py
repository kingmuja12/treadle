"""`treadle run` in the foreground: progress, exit codes, inputs, re-runs, and stop signals."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import os
import re
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn, cast

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import (
    BROKEN,
    COUNT,
    FLAKY,
    GREET,
    SLOW,
    Cli,
    HungUp,
    read_store,
    store_runs,
)
from tests.unit.store.helpers import T0, Runs

from treadle import runner as runner_module
from treadle.cli import commands
from treadle.cli.main import main
from treadle.cli.output import Output
from treadle.models import CancelOutcome, CancelReason, RunReason, RunStatus
from treadle.runner import LocalRunner
from treadle.steps.procs import ProcessRegistry
from treadle.types import ExitCode

_DURATION = r"\d+(\.\d+)?m?s"

# The middle step takes a moment, so the run is still going when its first lines print.
SLOWER = """\
steps:
  - id: one
    uses: log
    with:
      message: one
  - id: two
    uses: sleep
    with:
      duration: 500ms
  - id: three
    uses: log
    with:
      message: three
"""


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
def _pressed_at_once(callback: Callable[[int], None]) -> Iterator[None]:
    """Stand in for the stop-signal handlers: Ctrl+C is pressed as soon as they're installed."""
    callback(signal.SIGINT)
    yield


def test_ctrl_c_cancels_the_run_and_exits_130(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("slow", SLOW)
    monkeypatch.setattr(commands, "_on_stop_signals", _pressed_at_once)

    result = cli("run", "slow")

    assert result.code == ExitCode.INTERRUPTED
    assert result.err == "Cancelling run #1 (press Ctrl+C again to force)\n"
    assert "Run #1 cancelled (user) in " in result.out
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert run.status is RunStatus.CANCELLED


# The cleanup step runs after a cancel, until a second Ctrl+C forces the run.
CLEANUP = """\
steps:
  - id: wait
    uses: sleep
    with:
      duration: 30s
  - id: cleanup
    needs: [wait]
    if: always()
    uses: sleep
    with:
      duration: 30s
"""


def test_a_second_ctrl_c_forces_the_run_and_cuts_its_cleanup_short(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("cleanup", CLEANUP)

    @contextlib.contextmanager
    def pressed_twice(callback: Callable[[int], None]) -> Iterator[None]:
        loop = asyncio.get_running_loop()
        timers = [loop.call_later(delay, callback, signal.SIGINT) for delay in (0.3, 0.8)]
        try:
            yield
        finally:
            for timer in timers:
                timer.cancel()

    monkeypatch.setattr(commands, "_on_stop_signals", pressed_twice)

    result = cli("run", "cleanup")

    assert result.code == ExitCode.INTERRUPTED
    assert result.err == "Cancelling run #1 (press Ctrl+C again to force)\n"  # only once
    detail = read_store(project.settings(), lambda repo: repo.get_run_detail(1))
    assert detail is not None
    assert detail.run.status is RunStatus.CANCELLED
    assert [step.status.value for step in detail.steps] == ["cancelled", "cancelled"]


def _signalled_at_once(
    number: int,
) -> Callable[[Callable[[int], None]], contextlib.AbstractContextManager[None]]:
    """Stand in for the stop-signal handlers: `number` arrives as soon as they're installed."""

    @contextlib.contextmanager
    def handlers(callback: Callable[[int], None]) -> Iterator[None]:
        callback(number)
        yield

    return handlers


@pytest.mark.parametrize(
    ("number", "code", "reason", "message"),
    [
        (
            signal.SIGTERM,
            143,
            RunReason.SHUTDOWN,
            "Cancelling run #1 (SIGTERM; signal again to force)",
        ),
        (
            signal.SIGHUP,
            129,
            RunReason.SHUTDOWN,
            "Cancelling run #1 (SIGHUP; signal again to force)",
        ),
        (signal.SIGINT, 130, RunReason.USER, "Cancelling run #1 (press Ctrl+C again to force)"),
    ],
)
def test_a_stop_signal_cancels_the_run_and_exits_128_plus_its_number(
    cli: Cli,
    project: ProjectFactory,
    monkeypatch: pytest.MonkeyPatch,
    number: int,
    code: int,
    reason: RunReason,
    message: str,
) -> None:
    project.write_workflow("slow", SLOW)
    monkeypatch.setattr(commands, "_on_stop_signals", _signalled_at_once(number))

    result = cli("run", "slow")

    assert (result.code, result.err) == (code, f"{message}\n")
    assert f"Run #1 cancelled ({reason}) in " in result.out
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.CANCELLED, reason)


def test_a_hung_up_terminal_doesnt_stop_the_cancellation(
    project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("slow", SLOW)
    monkeypatch.setattr(commands, "_on_stop_signals", _signalled_at_once(signal.SIGHUP))
    monkeypatch.setattr("sys.stdout", HungUp())
    monkeypatch.setattr("sys.stderr", HungUp())

    code = main(["-C", str(project.root), "run", "slow"])

    assert code == 128 + signal.SIGHUP
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)


def test_stop_signals_are_handled_until_the_runner_has_closed(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    holder = project.settings().holder_path
    held_when_removed: list[bool] = []

    @contextlib.contextmanager
    def installed(callback: Callable[[int], None]) -> Iterator[None]:
        yield
        held_when_removed.append(holder.exists())

    monkeypatch.setattr(commands, "_on_stop_signals", installed)

    result = cli("run", "greet")

    assert result.code == ExitCode.OK
    # The handlers go only once the runner has closed and released the lock, so a third
    # signal still ends the process while it closes, and while it waits for a file
    # operation that a forced cancel left running.
    assert held_when_removed == [False]


def test_a_stop_signal_while_the_runner_closes_only_sets_the_exit_code(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    handlers: list[Callable[[int], None]] = []
    kill_leftovers = runner_module._kill_leftovers

    @contextlib.contextmanager
    def installed(callback: Callable[[int], None]) -> Iterator[None]:
        handlers.append(callback)
        yield

    async def signalled_while_closing(processes: ProcessRegistry) -> None:
        handlers[0](signal.SIGTERM)
        await kill_leftovers(processes)

    monkeypatch.setattr(commands, "_on_stop_signals", installed)
    monkeypatch.setattr(runner_module, "_kill_leftovers", signalled_while_closing)

    result = cli("run", "greet")

    assert result.code == 128 + signal.SIGTERM
    assert result.err == ""  # the run had ended, so there was nothing to cancel
    assert re.fullmatch(rf"Run #1 succeeded in {_DURATION}\.", result.out.splitlines()[-1])


class _Exited(Exception):
    """What the stand-in for `os._exit` raises, so that a test sees the exit and its code."""


def _exit_by_raising(code: int) -> NoReturn:
    raise _Exited(code)


class _RecordingRunner:
    """The part of `LocalRunner` that `_RunStops` uses, recording each call in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def cancel(
        self, run_id: int, *, force: bool = False, reason: CancelReason = CancelReason.USER
    ) -> CancelOutcome:
        self.calls.append(("cancel", run_id, force, reason))
        return CancelOutcome(run_id, RunStatus.RUNNING, accepted=True, forced=force)

    def kill_processes(self) -> int:
        self.calls.append(("kill",))
        return 1


@pytest.fixture
def recording_runner(monkeypatch: pytest.MonkeyPatch) -> _RecordingRunner:
    """A runner that records its calls; meanwhile `os._exit` raises _Exited instead of exiting."""
    monkeypatch.setattr(os, "_exit", _exit_by_raising)
    return _RecordingRunner()


def _stops(runner: _RecordingRunner, err: io.StringIO) -> commands._RunStops:
    """The stop-signal handler of a local run, with its messages going to `err`."""
    return commands._RunStops(cast(LocalRunner, runner), Output(io.StringIO(), err, color=False))


async def test_a_third_ctrl_c_kills_the_step_processes_and_exits_130_at_once(
    recording_runner: _RecordingRunner,
) -> None:
    err = io.StringIO()
    stops = _stops(recording_runner, err)
    stops.follow(4)

    stops(signal.SIGINT)
    stops(signal.SIGINT)
    await stops.settled()
    with pytest.raises(_Exited) as exited:
        stops(signal.SIGINT)

    assert exited.value.args == (ExitCode.INTERRUPTED,)
    assert recording_runner.calls == [
        ("cancel", 4, False, CancelReason.USER),
        ("cancel", 4, True, CancelReason.USER),
        ("kill",),
    ]
    assert err.getvalue() == "Cancelling run #4 (press Ctrl+C again to force)\n"


@pytest.mark.parametrize(
    "numbers",
    [
        (signal.SIGTERM, signal.SIGTERM, signal.SIGTERM),
        (signal.SIGHUP, signal.SIGINT, signal.SIGTERM),
    ],
)
async def test_the_third_stop_signal_of_any_kind_exits_130(
    recording_runner: _RecordingRunner, numbers: tuple[signal.Signals, ...]
) -> None:
    stops = _stops(recording_runner, io.StringIO())
    stops.follow(4)
    first, second, third = numbers

    stops(first)
    stops(second)
    await stops.settled()
    with pytest.raises(_Exited) as exited:
        stops(third)

    assert exited.value.args == (ExitCode.INTERRUPTED,)
    assert recording_runner.calls == [
        ("cancel", 4, False, CancelReason.SHUTDOWN),
        ("cancel", 4, True, CancelReason.SHUTDOWN),
        ("kill",),
    ]


async def test_three_stop_signals_before_the_run_exists_exit_without_cancelling(
    recording_runner: _RecordingRunner,
) -> None:
    err = io.StringIO()
    stops = _stops(recording_runner, err)

    stops(signal.SIGINT)
    stops(signal.SIGINT)
    with pytest.raises(_Exited) as exited:
        stops(signal.SIGINT)

    assert exited.value.args == (ExitCode.INTERRUPTED,)
    assert recording_runner.calls == [("kill",)]
    assert err.getvalue() == ""


async def test_after_following_ends_stop_signals_only_count_toward_the_third(
    recording_runner: _RecordingRunner,
) -> None:
    err = io.StringIO()
    stops = _stops(recording_runner, err)
    stops.follow(4)
    stops.stop_following()

    stops(signal.SIGTERM)
    stops(signal.SIGINT)
    await stops.settled()
    counted = (list(recording_runner.calls), stops.exit_code())
    with pytest.raises(_Exited) as exited:
        stops(signal.SIGINT)

    assert counted == ([], 128 + signal.SIGTERM)
    assert exited.value.args == (ExitCode.INTERRUPTED,)
    assert recording_runner.calls == [("kill",)]
    assert err.getvalue() == ""


@pytest.mark.parametrize(
    "signum",
    [signal.SIGTERM, signal.SIGHUP, signal.SIGINT],
    ids=["SIGTERM", "SIGHUP", "SIGINT"],
)
async def test_once_the_runner_has_closed_one_stop_signal_exits_at_once(
    recording_runner: _RecordingRunner, signum: signal.Signals
) -> None:
    err = io.StringIO()
    stops = _stops(recording_runner, err)
    stops.follow(4)
    stops.stop_following()
    stops.runner_closed()

    with pytest.raises(_Exited) as exited:
        stops(signum)

    # Only the wait for a worker thread, such as a stuck file operation's, was left.
    assert exited.value.args == (128 + signum,)
    assert recording_runner.calls == [("kill",)]
    assert err.getvalue() == ""


async def test_a_stop_signal_after_the_runner_has_closed_exits_with_the_first_signals_code(
    recording_runner: _RecordingRunner,
) -> None:
    stops = _stops(recording_runner, io.StringIO())
    stops.follow(4)
    stops(signal.SIGINT)
    await stops.settled()
    stops.stop_following()
    stops.runner_closed()

    with pytest.raises(_Exited) as exited:
        stops(signal.SIGTERM)

    assert exited.value.args == (128 + signal.SIGINT,)
    assert recording_runner.calls == [("cancel", 4, False, CancelReason.USER), ("kill",)]


def test_a_stop_signal_after_the_runner_has_closed_ends_the_command_at_once(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)
    exits: list[object] = []

    @contextlib.contextmanager
    def installed(callback: Callable[[int], None]) -> Iterator[None]:
        yield
        # The latest moment that the handler sees a signal: the runner has closed, and the
        # command waited for its worker threads, as it waits for a stuck file operation.
        try:
            callback(signal.SIGTERM)
        except _Exited as exited:
            exits.append(exited.args[0])

    monkeypatch.setattr(commands, "_on_stop_signals", installed)
    monkeypatch.setattr(os, "_exit", _exit_by_raising)

    result = cli("run", "greet")

    assert result.code == ExitCode.OK
    assert exits == [128 + signal.SIGTERM]


class _ReaderLeaves(io.StringIO):
    """Standard output whose reader goes away after the first line, as with `| head -1`.

    `left` is called when the first write fails.
    """

    def __init__(self, left: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self._left = left
        self._closed = False

    def write(self, text: str) -> int:
        if self.getvalue():
            if not self._closed:
                self._closed = True
                self._left()
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        return super().write(text)


def test_a_closed_pipe_lets_the_run_finish_and_exits_1(
    project: ProjectFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_workflow("slower", SLOWER)
    stdout = _ReaderLeaves()
    monkeypatch.setattr("sys.stdout", stdout)

    code = main(["-C", str(project.root), "run", "slower"])

    assert code == ExitCode.FAILURE
    assert stdout.getvalue() == "Run #1  slower  (manual)\n"
    assert capsys.readouterr().err == ""
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.SUCCEEDED, None)


class _DiskFull(io.StringIO):
    """Standard output that fails after the first line for a reason other than a closed pipe."""

    def write(self, text: str) -> int:
        if self.getvalue():
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(text)


def test_another_write_error_is_an_internal_error(
    project: ProjectFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_workflow("slower", SLOWER)
    monkeypatch.setattr("sys.stdout", _DiskFull())

    code = main(["-C", str(project.root), "run", "slower"])

    assert code == ExitCode.FAILURE
    assert capsys.readouterr().err == (
        "internal error: OSError: [Errno 28] No space left on device\n"
    )


def test_ctrl_c_after_the_pipe_closed_still_cancels_the_run(
    project: ProjectFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project.write_workflow("slower", SLOWER)
    handlers: list[Callable[[int], None]] = []

    @contextlib.contextmanager
    def installed(callback: Callable[[int], None]) -> Iterator[None]:
        handlers.append(callback)
        yield

    monkeypatch.setattr(commands, "_on_stop_signals", installed)
    monkeypatch.setattr("sys.stdout", _ReaderLeaves(lambda: handlers[0](signal.SIGINT)))

    code = main(["-C", str(project.root), "run", "slower"])

    assert code == ExitCode.INTERRUPTED
    assert capsys.readouterr().err == "Cancelling run #1 (press Ctrl+C again to force)\n"
    run = read_store(project.settings(), lambda repo: repo.get_run(1))
    assert run is not None
    assert (run.status, run.reason) == (RunStatus.CANCELLED, RunReason.USER)


def test_a_run_warns_when_other_users_can_read_env_or_the_state_directory(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("greet", GREET)
    settings = project.settings()
    assert settings.env_file is not None
    settings.env_file.chmod(0o644)
    settings.state_dir.mkdir(mode=0o755)
    settings.state_dir.chmod(0o755)

    result = cli("run", "greet")

    assert result.code == ExitCode.OK
    env, state = settings.env_file, settings.state_dir
    assert result.err.splitlines() == [
        f"warning: {state} is accessible by other users; run `chmod 700 {state}`",
        f"warning: {env} is readable by other users; run `chmod 600 {env}`",
    ]


def test_a_private_project_runs_without_warnings(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    first, second = cli("run", "greet"), cli("run", "greet")

    assert (first.code, first.err) == (ExitCode.OK, "")
    assert (second.code, second.err) == (ExitCode.OK, "")


def test_a_workflow_whose_file_is_a_symbolic_link_doesnt_exist(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    target = tmp_path / "shared.yml"
    target.write_text(GREET)
    (project.workflows_dir / "linked.yml").symlink_to(target)

    result = cli("run", "linked")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == (
        "error: workflow 'linked' doesn't exist: workflows/linked.yml is a symbolic link, "
        "which Treadle skips; copy the file into workflows instead\n"
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("runs", "--limit", "many"), "--limit: expected a whole number, got 'many'"),
        (("runs", "--before", "0"), f"--before: must be between 1 and {2**63 - 1}"),
        # Beyond SQLite's integers, which used to end in an internal OverflowError.
        (("runs", "--before", str(2**63)), f"--before: must be between 1 and {2**63 - 1}"),
        (("logs", "1", "--attempt", str(2**63)), f"--attempt: must be between 1 and {2**63 - 1}"),
        (
            ("logs", "1", "--step", "a", "--attempt", "99999999999999999999"),
            f"--attempt: must be between 1 and {2**63 - 1}",
        ),
        (("cancel", "1", "--wait", "soon"), "--wait: expected a number of seconds, got 'soon'"),
        (("cancel", "1", "--wait", "-1"), "--wait: must be a non-negative number of seconds"),
        (("cancel", "1", "--wait", "inf"), "--wait: must be a non-negative number of seconds"),
    ],
)
def test_bad_option_values_are_usage_errors(cli: Cli, args: tuple[str, ...], message: str) -> None:
    result = cli(*args)

    assert result.code == ExitCode.USAGE
    assert message in result.err
