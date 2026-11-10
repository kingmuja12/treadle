"""`main()`: dispatch, error reporting, exit codes, and logging."""

from __future__ import annotations

import argparse
import io
import logging
from pathlib import Path

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import GREET, Cli, HungUp

from treadle import __version__
from treadle.cli import commands
from treadle.cli.commands import CliContext
from treadle.cli.main import main
from treadle.errors import WorkflowInvalidError
from treadle.types import ExitCode
from treadle.workflow.loader import LoadResult, WorkflowLoader


def test_no_command_prints_the_usage_and_exits_2(cli: Cli) -> None:
    result = cli.anywhere()

    assert result.code == ExitCode.USAGE
    assert result.err.startswith("usage: treadle")
    assert "error: name a command; `treadle --help` lists them" in result.err


def test_help_exits_0(cli: Cli) -> None:
    result = cli.anywhere("--help")

    assert result.code == ExitCode.OK
    assert "COMMAND" in result.out


def test_a_bad_option_is_a_usage_error(cli: Cli) -> None:
    result = cli("runs", "--limit", "500")

    assert result.code == ExitCode.USAGE
    assert "--limit: must be between 1 and 200" in result.err


def test_the_version_flag_prints_what_the_version_command_prints(cli: Cli) -> None:
    flag, command = cli.anywhere("--version"), cli.anywhere("version")

    assert flag.code == command.code == ExitCode.OK
    assert flag.out == command.out
    assert flag.out.startswith(f"treadle {__version__} (Python ")


def test_a_missing_start_directory_is_a_usage_error(cli: Cli) -> None:
    result = cli.anywhere("-C", "/nonexistent/treadle-project", "list")

    assert result.code == ExitCode.USAGE
    assert result.err == "error: -C /nonexistent/treadle-project: no such directory\n"


def test_a_command_without_a_project_exits_3_with_the_init_hint(cli: Cli, tmp_path: Path) -> None:
    result = cli.anywhere("-C", str(tmp_path), "runs")

    assert result.code == ExitCode.CONFIG
    assert "no treadle.toml found" in result.err
    assert "run `treadle init`" in result.err


def test_config_issues_are_rendered_under_the_error(cli: Cli, project: ProjectFactory) -> None:
    project.write_config(
        """\
        timezone = "UTC"
        [server]
        port = 99999
        [runs]
        max_concurent = 3
        """
    )

    result = cli("list")

    assert result.code == ExitCode.CONFIG
    lines = result.err.splitlines()
    assert lines[0].startswith("error: invalid configuration in ")
    assert "treadle.toml: error[E104]: must be between 0 and 65535 (at server.port)" in lines
    assert (
        "treadle.toml: error[E101]: unknown key 'max_concurent' in [runs] (at runs.max_concurent)"
        in lines
    )
    assert "   = help: did you mean 'max_concurrent'?" in lines


def test_an_unexpected_error_is_an_internal_error(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(args: argparse.Namespace, ctx: CliContext) -> int:
        raise ValueError("boom")

    monkeypatch.setitem(commands.COMMANDS, "list", broken)

    plain, debug = cli("list"), cli.anywhere("-C", str(cli.project.root), "--debug", "list")

    assert plain.code == debug.code == ExitCode.FAILURE
    assert plain.err == "internal error: ValueError: boom\n"
    assert debug.err.startswith("internal error: ValueError: boom\nTraceback (most recent call")


def test_ctrl_c_exits_130(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(args: argparse.Namespace, ctx: CliContext) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(commands.COMMANDS, "list", interrupted)

    assert cli("list").code == ExitCode.INTERRUPTED


def test_a_treadle_error_prints_its_message_and_returns_its_exit_code(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid(args: argparse.Namespace, ctx: CliContext) -> int:
        raise WorkflowInvalidError("wf", ())

    monkeypatch.setitem(commands.COMMANDS, "list", invalid)

    result = cli("list")

    assert result.code == ExitCode.CONFIG
    assert result.err == "error: workflow 'wf' is invalid\n"


def test_warnings_read_like_cli_messages(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def warns(args: argparse.Namespace, ctx: CliContext) -> int:
        logging.getLogger("treadle.engine").warning("run=%d needs attention", 7)
        logging.getLogger("treadle.engine").info("not shown")
        logging.getLogger("treadle.registry").warning("workflows/x.yml has warnings")
        return 0

    monkeypatch.setitem(commands.COMMANDS, "list", warns)

    result = cli("list")

    assert result.err == "warning: run=7 needs attention\n"


def test_debug_shows_every_log_record(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def logs(args: argparse.Namespace, ctx: CliContext) -> int:
        logging.getLogger("treadle.registry").debug("loaded %s", "hello")
        return 0

    monkeypatch.setitem(commands.COMMANDS, "list", logs)

    result = cli.anywhere("-C", str(cli.project.root), "--debug", "list")

    assert result.err.endswith(" DEBUG treadle.registry loaded hello\n")


def test_main_leaves_the_logging_configuration_as_it_found_it(cli: Cli) -> None:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level

    cli("--debug", "version")

    assert root.handlers == handlers
    assert root.level == level


def test_main_runs_with_the_process_arguments_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["treadle", "version", "--json"])

    assert main() == ExitCode.OK
    assert '"treadle": "0.1.0"' in capsys.readouterr().out


def test_config_issues_name_a_config_in_a_parent_directory_by_its_full_path(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_config('timezone = "UTC"\n[server]\nport = -1\n')
    below = project.root / "workflows"

    result = cli.anywhere("-C", str(below), "list")

    assert result.code == ExitCode.CONFIG
    assert f"{project.config_path}: error[E104]: must be between 0 and 65535" in result.err


class _ClosedPipe(io.StringIO):
    """Standard output whose reader went away, as with `treadle logs 1 | head`."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize("stream", [_ClosedPipe, HungUp])
def test_output_that_nobody_reads_ends_the_command_quietly(
    cli: Cli,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stream: type[io.StringIO],
) -> None:
    monkeypatch.setattr("sys.stdout", stream())

    code = main(["-C", str(cli.project.root), "version"])

    assert code == ExitCode.FAILURE
    assert capsys.readouterr().err == ""


def test_an_internal_error_while_loading_a_file_names_the_file(
    cli: Cli, project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.write_workflow("greet", GREET)

    def broken(self: WorkflowLoader, path: Path) -> LoadResult:
        raise ValueError("boom")

    monkeypatch.setattr(WorkflowLoader, "load_file", broken)

    plain = cli("validate")
    debug = cli.anywhere("-C", str(project.root), "--debug", "validate")

    assert plain.code == debug.code == ExitCode.FAILURE
    assert plain.err == "internal error: ValueError: boom\n  while loading workflows/greet.yml\n"
    assert debug.err.startswith(plain.err + "Traceback (most recent call last):")
