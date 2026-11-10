"""The `treadle` console script."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Final

from treadle.cli.commands import COMMANDS, CliContext, _closed_output, cmd_version
from treadle.cli.output import Output, _discard, _render_issue, use_color
from treadle.cli.parser import build_parser
from treadle.config import CONFIG_FILENAME, Overrides, find_config
from treadle.errors import ConfigError, CronError, TreadleError, UsageError
from treadle.types import ExitCode

# The registry logs every file's issues as it loads the workflows. The daemon log needs
# them, but `treadle validate` and `treadle list` report them to a terminal user, so other
# commands show them only under `--debug`.
_REGISTRY_LOGGER: Final = "treadle.registry"
_DEBUG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s %(message)s"


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch through `COMMANDS`, and return the exit code.

    A TreadleError prints its message (and rendered issues) and returns its `exit_code`.
    KeyboardInterrupt returns 130, and output that nobody reads any more, such as a closed
    pipe or a terminal that hung up, returns 1 without a message. Any other exception prints
    `internal error: <type>: <message>` and the notes it carries, such as the file it was
    loading, with the traceback only under `--debug`, and returns 1.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, or a usage error that argparse already printed
        return _exit_status(exc.code)
    environ = dict(os.environ)
    out = Output(
        sys.stdout, sys.stderr, color=use_color(sys.stdout, environ, disabled=args.no_color)
    )
    # The daemon configures its own logging.
    logs = contextlib.nullcontext() if args.command == "serve" else _cli_logging(args.debug)
    with logs:
        try:
            ctx = _context(args, environ, out)
            if args.version:
                return cmd_version(argparse.Namespace(json=False), ctx)
            if args.command is None:
                parser.print_usage(sys.stderr)
                out.error("name a command; `treadle --help` lists them")
                return ExitCode.USAGE
            return COMMANDS[args.command](args, ctx)
        except TreadleError as exc:
            _report(out, exc, args, environ)
            return int(exc.exit_code)
        except KeyboardInterrupt:
            return ExitCode.INTERRUPTED
        except OSError as exc:
            if not _closed_output(exc):
                return _internal_error(out, exc, debug=args.debug)
            # The reader of the output went away, as `treadle logs 42 | head` does, or the
            # terminal hung up.
            _discard(sys.stdout)
            _discard(sys.stderr)
            return ExitCode.FAILURE
        except Exception as exc:
            return _internal_error(out, exc, debug=args.debug)


def _internal_error(out: Output, exc: Exception, *, debug: bool) -> int:
    """Report an exception that Treadle didn't expect, and return 1.

    The notes it carries, such as the file it was loading, follow the message, and the
    traceback follows them under `--debug`.
    """
    out.line(f"internal error: {type(exc).__name__}: {exc}", err=True)
    for note in getattr(exc, "__notes__", ()):
        out.line(f"  {note}", err=True)
    if debug:
        out.line(traceback.format_exc().rstrip(), err=True)
    return ExitCode.FAILURE


def _context(args: argparse.Namespace, environ: Mapping[str, str], out: Output) -> CliContext:
    """Build the context: `-C` moves the starting directory, and `--debug` the log level."""
    cwd = _start_directory(args.directory)
    overrides = Overrides(
        config=args.config,
        workflows_dir=args.workflows_dir,
        state_dir=args.state_dir,
        log_level="debug" if args.debug else None,
    )
    return CliContext(cwd=cwd, environ=environ, out=out, overrides=overrides)


def _start_directory(directory: Path | None) -> Path:
    cwd = Path.cwd()
    if directory is None:
        return cwd
    target = (cwd / directory.expanduser()).resolve()
    if not target.is_dir():
        raise UsageError(f"-C {directory}: no such directory")
    return target


def _report(
    out: Output, exc: TreadleError, args: argparse.Namespace, environ: Mapping[str, str]
) -> None:
    """Print an error, the caret line of a cron error, and the issues of a config error."""
    out.error(str(exc))
    if isinstance(exc, CronError) and exc.offset is not None:
        lead = " " * min(max(exc.offset, 0), len(exc.source))
        out.line(f"  {exc.source}\n  {lead}^", err=True)
    if isinstance(exc, ConfigError) and exc.issues:
        label = _config_label(args, environ)
        for issue in exc.issues:
            out.line(_render_issue(out, issue, label, None), err=True)


def _config_label(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    """Name the config file that a ConfigError's issues come from, relative when it's nearby.

    A ConfigError comes after `-C` was accepted, so the starting directory exists.
    """
    cwd = _start_directory(args.directory)
    explicit = args.config or environ.get("TREADLE_CONFIG")
    found = cwd / Path(explicit).expanduser() if explicit else find_config(cwd)
    if found is None:
        return CONFIG_FILENAME
    with contextlib.suppress(ValueError):
        return found.resolve().relative_to(cwd).as_posix()
    return str(found)


@contextlib.contextmanager
def _cli_logging(debug: bool) -> Iterator[None]:
    """Show log records on stderr while a command runs: warnings, or everything with `--debug`.

    Without `--debug`, a record reads like the CLI's own messages, "warning: ...". The root
    logger gets its handler and level back afterward.
    """
    handler = logging.StreamHandler(sys.stderr)
    if debug:
        handler.setFormatter(logging.Formatter(_DEBUG_FORMAT))
    else:
        handler.setFormatter(_CliFormatter())
        handler.addFilter(_quiet_registry)
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.WARNING)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


class _CliFormatter(logging.Formatter):
    """Formats a record as `warning: message`, with its traceback, if any, below."""

    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname.lower()}: {super().format(record)}"


def _quiet_registry(record: logging.LogRecord) -> bool:
    """Drop the registry's warnings about workflow files, which other commands report."""
    return not record.name.startswith(_REGISTRY_LOGGER) or record.levelno >= logging.ERROR


def _exit_status(code: object) -> int:
    """Turn a SystemExit code into an exit status, as the interpreter would."""
    if code is None:
        return ExitCode.OK
    return code if isinstance(code, int) else ExitCode.FAILURE
