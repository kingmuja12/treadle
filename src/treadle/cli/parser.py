"""The argument parser: global options, then one subcommand with its own options."""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable
from pathlib import Path
from typing import Final, TypeAlias

from treadle.cli.commands import _MAX_RUN_ID, _RUNS_LIMIT
from treadle.models import RunStatus, TriggerKind

_RUN_HELP: Final = "a run: 42, #42, last, or last:<workflow>"
_LOG_LEVELS: Final = ("debug", "info", "warning", "error")
_GRAPH_FORMATS: Final = ("text", "mermaid", "json")


def build_parser() -> argparse.ArgumentParser:
    """Return the parser. Global options come before the subcommand."""
    parser = argparse.ArgumentParser(
        prog="treadle",
        description="A local, code-first workflow automation engine.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", type=Path, metavar="PATH", help="use this config file")
    parser.add_argument(
        "-C", dest="directory", type=Path, metavar="DIR", help="run as if started in DIR"
    )
    parser.add_argument("--workflows-dir", type=Path, metavar="DIR", help="override workflows_dir")
    parser.add_argument("--state-dir", type=Path, metavar="DIR", help="override state_dir")
    parser.add_argument("--no-color", action="store_true", help="disable color")
    parser.add_argument(
        "--debug", action="store_true", help="show tracebacks and log at debug level"
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for add in _COMMANDS:
        add(commands)
    return parser


_Commands: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"


def _command(commands: _Commands, name: str, summary: str) -> argparse.ArgumentParser:
    return commands.add_parser(name, help=summary, description=summary, allow_abbrev=False)


def _json(parser: argparse.ArgumentParser, what: str = "print JSON") -> None:
    parser.add_argument("--json", action="store_true", help=what)


def _init(commands: _Commands) -> None:
    parser = _command(commands, "init", "Create a project with example workflows.")
    parser.add_argument(
        "dir", nargs="?", type=Path, metavar="DIR", help="the project directory (default: .)"
    )
    parser.add_argument("--force", action="store_true", help="replace existing files")


def _validate(commands: _Commands) -> None:
    parser = _command(commands, "validate", "Check workflow files and report their issues.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        metavar="PATH",
        help="files or directories (default: the project's workflows)",
    )
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    _json(parser)


def _list(commands: _Commands) -> None:
    parser = _command(commands, "list", "Show the workflows, their triggers, and last runs.")
    _json(parser)


def _run(commands: _Commands) -> None:
    parser = _command(commands, "run", "Run a workflow now and follow its progress.")
    parser.add_argument("workflow", nargs="?", metavar="WORKFLOW", help="the workflow ID")
    parser.add_argument(
        "-i",
        "--input",
        dest="inputs",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="an input value (repeatable)",
    )
    parser.add_argument("--rerun", metavar="RUN", help=f"re-run {_RUN_HELP}, instead of WORKFLOW")
    parser.add_argument(
        "--detach", action="store_true", help="return once the daemon queued the run"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="stream the log lines too")
    _json(parser, "print JSON Lines events")


def _runs(commands: _Commands) -> None:
    parser = _command(commands, "runs", "List the run history, newest first.")
    parser.add_argument("--workflow", metavar="ID", help="only this workflow's runs")
    parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        default=[],
        choices=[status.value for status in RunStatus],
        metavar="STATUS",
        help="only runs with this status (repeatable)",
    )
    parser.add_argument(
        "--trigger",
        choices=[kind.value for kind in TriggerKind],
        metavar="KIND",
        help="only runs started this way: manual, schedule, watch, or webhook",
    )
    parser.add_argument("--grep", metavar="TEXT", help="match the run error or any log line")
    parser.add_argument(
        "--limit",
        type=_integer(1, 200),
        default=_RUNS_LIMIT,
        metavar="N",
        help=f"at most N runs (1-200, default: {_RUNS_LIMIT})",
    )
    parser.add_argument(
        "--before", type=_integer(1, _MAX_RUN_ID), metavar="ID", help="only runs older than ID"
    )
    _json(parser)


def _show(commands: _Commands) -> None:
    parser = _command(commands, "show", "Summarize a run, leading with its failure.")
    parser.add_argument("run", metavar="RUN", help=_RUN_HELP)
    _json(parser)


def _logs(commands: _Commands) -> None:
    parser = _command(commands, "logs", "Print a run's log lines.")
    parser.add_argument("run", metavar="RUN", help=_RUN_HELP)
    parser.add_argument("--step", metavar="ID", help="only this step's lines")
    parser.add_argument(
        "--attempt", type=_integer(1, _MAX_RUN_ID), metavar="N", help="only this attempt's lines"
    )
    parser.add_argument(
        "-f", "--follow", action="store_true", help="keep printing until the run ends"
    )
    _json(parser, "print JSON Lines")


def _cancel(commands: _Commands) -> None:
    parser = _command(commands, "cancel", "Cancel a queued or running run.")
    parser.add_argument("run", metavar="RUN", help=_RUN_HELP)
    parser.add_argument("--force", action="store_true", help="stop the run without cleanup")
    parser.add_argument(
        "--wait",
        type=_seconds,
        default=5.0,
        metavar="SECONDS",
        help="how long to wait for the lock holder's answer (default: 5; 0 doesn't wait)",
    )
    _json(parser)


def _serve(commands: _Commands) -> None:
    parser = _command(commands, "serve", "Run the daemon: triggers, the API, and the dashboard.")
    parser.add_argument("--host", metavar="HOST", help="the address to listen on")
    parser.add_argument(
        "--port", type=_integer(0, 65535), metavar="PORT", help="the port (0 picks a free one)"
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        metavar="LEVEL",
        help="debug, info, warning, or error",
    )


def _cron(commands: _Commands) -> None:
    parser = _command(commands, "cron", "Show when a cron expression fires, with DST notes.")
    parser.add_argument("expression", metavar="EXPR", help='such as "30 2 * * *" or @daily')
    parser.add_argument("--tz", metavar="ZONE", help="an IANA time zone (default: the project's)")
    parser.add_argument(
        "-n",
        dest="count",
        type=_integer(1, 100),
        default=5,
        metavar="COUNT",
        help="how many fires (1-100, default: 5)",
    )
    parser.add_argument("--after", metavar="ISO", help="start after this time (default: now)")
    _json(parser)


def _graph(commands: _Commands) -> None:
    parser = _command(commands, "graph", "Print a workflow's step graph.")
    parser.add_argument("workflow", metavar="WORKFLOW", help="a workflow ID or a file path")
    parser.add_argument(
        "--format", choices=_GRAPH_FORMATS, default="text", help="text, mermaid, or json"
    )


def _version(commands: _Commands) -> None:
    parser = _command(commands, "version", "Print the versions of Treadle and its runtime.")
    _json(parser)


_COMMANDS: Final[tuple[Callable[[_Commands], None], ...]] = (
    _init,
    _validate,
    _list,
    _run,
    _runs,
    _show,
    _logs,
    _cancel,
    _serve,
    _cron,
    _graph,
    _version,
)


def _integer(low: int, high: int | None = None) -> Callable[[str], int]:
    """Return an argument type that accepts whole numbers from `low` to `high`."""

    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
        if value < low or (high is not None and value > high):
            limits = f"between {low} and {high}" if high is not None else f"at least {low}"
            raise argparse.ArgumentTypeError(f"must be {limits}")
        return value

    return parse


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number of seconds")
    return value
