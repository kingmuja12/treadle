"""The CLI commands. Each returns its exit code."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import errno
import logging
import os
import platform
import re
import shlex
import signal
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Final, Literal, TypeAlias
from zoneinfo import ZoneInfo

from treadle import __version__
from treadle.cli.follow import _exit_code, _local, follow_run, render_summary
from treadle.cli.output import Output, _discard, _render_issue
from treadle.cli.scaffold import scaffold
from treadle.clock import SystemClock, iso_utc, parse_iso, to_ms
from treadle.config import (
    Overrides,
    Settings,
    detect_timezone,
    is_time_zone,
    load_settings,
    time_zone_hint,
)
from treadle.cron import describe_fires, parse_cron
from treadle.engine.masking import Masker
from treadle.errors import (
    BusyError,
    ConfigError,
    ConflictError,
    InputError,
    NoDatabaseError,
    NotFoundError,
    ShuttingDownError,
    TreadleError,
    UsageError,
    WorkflowInvalidError,
)
from treadle.lock import InstanceLock
from treadle.models import (
    CancelOutcome,
    CancelReason,
    LogLine,
    Origin,
    RunDetail,
    RunFilter,
    RunPage,
    RunReason,
    RunRecord,
    RunStatus,
    Submission,
    TriggerKind,
    TriggerState,
    WorkflowEntry,
)
from treadle.protocols import RunReader
from treadle.secrets import SecretStore
from treadle.serialize import error_json, log_json, run_detail_json, run_json, workflow_json
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore
from treadle.store.db import sqlite_version
from treadle.store.migrations import CURRENT_VERSION
from treadle.store.repo import Repository
from treadle.triggers.manager import static_trigger_states
from treadle.types import ExitCode, JSONObject, JSONValue, LockHolder, Severity
from treadle.units import format_duration
from treadle.workflow.graph import to_json, to_mermaid, to_text
from treadle.workflow.inputs import parse_input_pairs
from treadle.workflow.loader import LoadResult, WorkflowLoader, cross_check, discover

if TYPE_CHECKING:
    from treadle.runner import LocalRunner


@dataclass(slots=True)
class CliContext:
    """What every command receives besides its arguments."""

    cwd: Path
    environ: Mapping[str, str]
    out: Output
    overrides: Overrides


Command: TypeAlias = Callable[[argparse.Namespace, CliContext], int]

_RUN_REF: Final = re.compile(r"#?([0-9]+)")
_MAX_RUN_ID: Final = 2**63 - 1  # SQLite's largest integer
_CONTROL_POLL: Final = 0.2  # seconds between checks for a control request's result
_FOLLOW_POLL: Final = 0.3  # seconds between checks of a followed run's progress
_RUN_REPLY_WAIT: Final = 10.0  # how long `run` waits for the daemon to answer
# How much longer the CLI waits for a request that the lock holder claimed just before the
# CLI gave up on it, so the answer is already on its way.
_CLAIMED_WAIT: Final = 10.0
_MAINTENANCE_WAIT: Final = 2.0  # how long a maintenance holder may keep the lock
_PROBE_INTERVAL: Final = 0.1
# Seconds between checks that the process running a followed run still holds the lock.
_HOLDER_CHECK: Final = 1.0
_LOGS_POLL: Final = 0.5
_PAGE: Final = 1000
_RUNS_LIMIT: Final = 20  # the default of `treadle runs --limit`
# Workflow file names in sorted order, the order in which the first file defines an ID.
_SUFFIXES: Final = (".yaml", ".yml")
# A control request's error code, and the error that the CLI raises for it.
_CONTROL_ERRORS: Final[Mapping[str, type[TreadleError]]] = {
    "not_found": NotFoundError,
    "workflow_invalid": ConfigError,
    "conflict": ConflictError,
    "bad_request": UsageError,
    "shutting_down": ShuttingDownError,
    "busy": BusyError,
    "expired": BusyError,
}
# The result that a withdrawn control request keeps.
_WITHDRAWN: Final = error_json("expired", "the CLI stopped waiting for an answer")
# The signals that stop a command that runs a workflow or waits on the lock holder: Ctrl+C,
# `kill` or a supervisor's stop, and a terminal that closes.
_STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# A write to a pipe whose reader left fails with EPIPE, and to a terminal that hung up, EIO.
_CLOSED_OUTPUT: Final = frozenset({errno.EPIPE, errno.EIO})


def exit_code_for(status: RunStatus) -> ExitCode:
    """Map a terminal run status to the exit code of `treadle run`.

    Succeeded is 0, failed 1, cancelled or interrupted 5, timed out 6, and skipped 7.
    Raises ValueError for a status that isn't terminal.
    """
    return _exit_code(status)


async def resolve_run_ref(ref: str, reader: RunReader) -> int:
    """Resolve "42", "#42", "last", or "last:<workflow>" to a run ID. Raises NotFoundError.

    A malformed reference raises UsageError. A numeric reference isn't looked up, so the
    caller reports a run that doesn't exist.
    """
    text = ref.strip()
    match = _RUN_REF.fullmatch(text)
    if match is not None:
        run_id = int(match[1])
        if run_id > _MAX_RUN_ID:
            raise NotFoundError(f"run {run_id} doesn't exist")
        return run_id
    workflow_id: str | None
    if text == "last":
        workflow_id = None
    elif text.startswith("last:") and len(text) > len("last:"):
        workflow_id = text.removeprefix("last:")
    else:
        raise UsageError(f"invalid run reference {ref!r}; use 42, #42, last, or last:<workflow>")
    latest = await reader.latest_run_id(workflow_id)
    if latest is None:
        if workflow_id is None:
            raise NotFoundError("there are no runs yet")
        raise NotFoundError(f"workflow '{workflow_id}' has no runs")
    return latest


def cmd_init(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle init [DIR]`: create a project."""
    out = ctx.out
    target = ctx.cwd if args.dir is None else (ctx.cwd / args.dir).resolve()
    shown = _shown(target, ctx.cwd)
    if target.exists() and not target.is_dir():
        raise UsageError(f"{shown} isn't a directory")
    zone = detect_timezone(ctx.environ)
    if zone is None:
        out.warn("couldn't detect the local time zone, so treadle.toml uses UTC")
        zone = "UTC"
    try:
        results = scaffold(target, force=args.force, timezone=zone)
    except OSError as exc:
        raise TreadleError(f"can't create the project in {shown}: {exc.strerror or exc}") from None
    out.line(f"Treadle project in {shown}:")
    width = max(len(status) for _, status in results)
    for path, status in results:
        out.line(f"  {status:<{width}}  {path.relative_to(target).as_posix()}")
    out.line()
    out.line("Next steps:")
    if target != ctx.cwd:
        out.line(f"  cd {shlex.quote(shown)}")
    out.line("  treadle run hello -i name=Ada")
    out.line("  treadle serve")
    out.line()
    out.line("On a machine that other people use, set TREADLE_TOKEN in .env before you serve.")
    out.line("See .env.example.")
    return ExitCode.OK


def cmd_validate(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle validate [PATH...]`: report issues in workflow files.

    A symbolic link with a workflow file's name, found in a directory, gets W060 because
    every command skips it; it counts as a warning but neither as valid nor as invalid. A
    file given by its path is checked as it is.
    """
    out = ctx.out
    settings = _settings(ctx, require_project=not args.paths)
    in_project = settings.config_path is not None
    found = _expand(args.paths, ctx.cwd) if args.paths else _directory(settings.workflows_dir)
    loader = _loader(settings, ctx.environ, environment_checks=in_project)
    paths = [item for item in found if isinstance(item, Path)]
    checked = cross_check([_load(loader, path, ctx.cwd) for path in paths])
    in_order = iter(checked)
    results = [next(in_order) if isinstance(item, Path) else item for item in found]
    issues = [issue for result in results for issue in result.issues]
    errors = sum(issue.severity is Severity.ERROR for issue in issues)
    warnings = len(issues) - errors
    failed = bool(errors or (args.strict and warnings))
    if args.json:
        files_json: list[JSONValue] = [_validated_json(result, ctx.cwd) for result in results]
        out.json({"valid": not failed, "errors": errors, "warnings": warnings, "files": files_json})
        return ExitCode.FAILURE if failed else ExitCode.OK
    for result in results:
        label = _shown(result.path, ctx.cwd)
        for issue in result.issues:
            out.line(_render_issue(out, issue, label, result.source))
            out.line()
    if not results:
        where = "the given paths" if args.paths else _shown(settings.workflows_dir, ctx.cwd)
        out.line(f"No workflow files in {where}.")
    else:
        valid = sum(result.ok for result in checked)
        summary = f"{valid} valid, {len(checked) - valid} invalid"
        counts = [_counted(n, noun) for n, noun in ((errors, "error"), (warnings, "warning")) if n]
        if counts:
            summary += f" ({', '.join(counts)})"
        if not in_project:
            summary += "; machine-specific checks skipped (no treadle.toml)"
        out.line(summary)
    return ExitCode.FAILURE if failed else ExitCode.OK


def cmd_list(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle list`: show the workflows with their triggers and last runs.

    Each symbolic link with a workflow file's name gets a warning on stderr, because every
    command skips it.
    """
    out = ctx.out
    settings = _settings(ctx)
    skipped: list[LoadResult] = []
    entries = _entries(settings, ctx, skipped=skipped)
    for result in skipped:
        for issue in result.issues:
            out.warn(f"{_shown(result.path, ctx.cwd)}: {issue.message}")
    last_runs = asyncio.run(_last_runs(settings))
    now = SystemClock().now()
    triggers = {
        entry.workflow_id: static_trigger_states(entry.current, now=now) if entry.current else []
        for entry in entries
    }
    if args.json:
        workflows: list[JSONValue] = [
            workflow_json(
                entry,
                project_dir=settings.project_dir,
                triggers=triggers[entry.workflow_id],
                last_run=last_runs.get(entry.workflow_id),
            )
            for entry in entries
        ]
        out.json({"workflows": workflows})
        return ExitCode.OK
    if not entries:
        out.line(f"No workflows in {_shown(settings.workflows_dir, ctx.cwd)}.")
        return ExitCode.OK
    zone = ZoneInfo(settings.timezone)
    rows = [
        _workflow_row(
            out, entry, triggers[entry.workflow_id], last_runs.get(entry.workflow_id), zone
        )
        for entry in entries
    ]
    out.table(["ID", "NAME", "TRIGGERS", "NEXT FIRE", "LAST RUN", "STATE"], rows)
    invalid = sum(not entry.valid for entry in entries)
    if invalid:
        out.line()
        noun = "workflow is" if invalid == 1 else "workflows are"
        out.line(f"{invalid} {noun} invalid; `treadle validate` shows the issues.")
    return ExitCode.OK


def cmd_run(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle run WORKFLOW`: start a run here or in the daemon, and follow it."""
    if args.rerun is not None and (args.workflow is not None or args.inputs):
        raise UsageError("--rerun replaces WORKFLOW and its inputs; give only --rerun RUN")
    if args.rerun is None and args.workflow is None:
        raise UsageError("name the WORKFLOW to run, or re-run one with --rerun RUN")
    settings = _settings(ctx)
    inputs = parse_input_pairs(args.inputs)
    try:
        return asyncio.run(_start_run(args, ctx, settings, inputs))
    except WorkflowInvalidError as exc:
        return _invalid_workflow(exc, settings, ctx)
    except NotFoundError:
        link = None if args.workflow is None else _linked_file(settings, args.workflow)
        if link is None:
            raise
        raise _linked_workflow(args.workflow, link, settings, ctx) from None


def cmd_runs(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle runs`: list run history."""
    out = ctx.out
    settings = _settings(ctx)
    flt = RunFilter(
        workflow_id=args.workflow,
        statuses=frozenset(RunStatus(status) for status in args.statuses),
        trigger_kind=None if args.trigger is None else TriggerKind(args.trigger),
        text=args.grep,
        before_id=args.before,
        limit=args.limit,
    )
    page = asyncio.run(_list_runs(settings, flt))
    if args.json:
        runs: list[JSONValue] = [run_json(run) for run in page.runs]
        out.json({"runs": runs, "next_before_id": page.next_before_id})
        return ExitCode.OK
    if not page.runs:
        out.line("No runs match." if _filtered(flt) else "No runs yet.")
        return ExitCode.OK
    zone = ZoneInfo(settings.timezone)
    rows = [
        [
            str(run.id),
            run.workflow_id,
            out.status(run.status.value),
            run.trigger_kind.value,
            "-" if run.started_at is None else _local(run.started_at, zone),
            _run_duration(run),
        ]
        for run in page.runs
    ]
    out.table(["ID", "WORKFLOW", "STATUS", "TRIGGER", "STARTED", "DURATION"], rows)
    if page.next_before_id is not None:
        out.line(f"Older runs: {_older_runs(args, page.next_before_id)}")
    return ExitCode.OK


def cmd_show(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle show RUN`: summarize one run, leading with the failure."""
    settings = _settings(ctx)
    return asyncio.run(_show(args, ctx, settings))


def cmd_logs(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle logs RUN`: print a run's logs, optionally following them."""
    settings = _settings(ctx)
    return asyncio.run(_logs(args, ctx, settings))


def cmd_cancel(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle cancel RUN`: cancel a queued or running run."""
    settings = _settings(ctx)
    return asyncio.run(_cancel(args, ctx, settings))


def cmd_serve(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle serve`: run the daemon."""
    with _ignoring_sighup():
        # Imported here: the daemon pulls in the HTTP stack, and every other command would
        # pay for that at startup.
        from treadle.daemon import serve

        level = args.log_level or ctx.overrides.log_level
        overrides = dataclasses.replace(
            ctx.overrides, host=args.host, port=args.port, log_level=level
        )
        settings = load_settings(overrides, cwd=ctx.cwd, environ=ctx.environ)
        return int(serve(settings, environ=ctx.environ))


def cmd_cron(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle cron EXPR`: show upcoming fire times with DST notes."""
    out = ctx.out
    name = args.tz or _settings(ctx, require_project=False).timezone
    zone = _zone(name)
    spec = parse_cron(args.expression)
    fires = describe_fires(spec, zone, _after(args.after, zone), args.count)
    if args.json:
        listed: list[JSONValue] = [
            {
                "local": fire.local.isoformat(timespec="seconds"),
                "utc": iso_utc(fire.at),
                "note": fire.note,
            }
            for fire in fires
        ]
        out.json({"expression": args.expression, "timezone": name, "fires": listed})
        return ExitCode.OK
    rows = [
        [f"{fire.local:%Y-%m-%d %H:%M %Z}", f"{fire.at:%Y-%m-%dT%H:%M:%SZ}", fire.note or ""]
        for fire in fires
    ]
    out.table(["LOCAL", "UTC", "NOTE"], rows)
    return ExitCode.OK


def cmd_graph(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle graph WORKFLOW`: print the step graph."""
    out = ctx.out
    path, settings = _graph_file(args.workflow, ctx)
    loader = _loader(settings, ctx.environ, environment_checks=settings.config_path is not None)
    result = _load(loader, path, ctx.cwd)
    if not result.ok or result.workflow is None:
        label = _shown(path, ctx.cwd)
        out.error(f"workflow '{result.workflow_id or path.stem}' is invalid")
        for issue in result.issues:
            out.line(_render_issue(out, issue, label, result.source), err=True)
        return ExitCode.CONFIG
    workflow = result.workflow
    if args.format == "json":
        out.json(to_json(workflow))
    else:
        out.line(to_mermaid(workflow) if args.format == "mermaid" else to_text(workflow))
    return ExitCode.OK


def cmd_version(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle version`: print versions of Treadle, Python, SQLite, and the schema."""
    sqlite = ".".join(str(part) for part in sqlite_version())
    info: JSONObject = {
        "treadle": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "sqlite": sqlite,
        "schema_version": CURRENT_VERSION,
    }
    if args.json:
        ctx.out.json(info)
    else:
        ctx.out.line(
            f"treadle {__version__} (Python {info['python']}, {sys.platform}, SQLite {sqlite}, "
            f"schema {CURRENT_VERSION})"
        )
    return ExitCode.OK


COMMANDS: Final[Mapping[str, Command]] = {
    "init": cmd_init,
    "validate": cmd_validate,
    "list": cmd_list,
    "run": cmd_run,
    "runs": cmd_runs,
    "show": cmd_show,
    "logs": cmd_logs,
    "cancel": cmd_cancel,
    "serve": cmd_serve,
    "cron": cmd_cron,
    "graph": cmd_graph,
    "version": cmd_version,
}


# Settings, files, and workflows


def _settings(ctx: CliContext, *, require_project: bool = True) -> Settings:
    return load_settings(
        ctx.overrides, cwd=ctx.cwd, environ=ctx.environ, require_project=require_project
    )


def _shown(path: Path, base: Path) -> str:
    """Show `path` relative to `base` when it's inside it, else as it is."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _loader(
    settings: Settings, environ: Mapping[str, str], *, environment_checks: bool
) -> WorkflowLoader:
    """Return a loader for the project; machine-specific checks read the project's `.env`."""
    secrets = SecretStore(settings.env_file, environ).load() if environment_checks else None
    return WorkflowLoader(
        builtin_registry(),
        project_dir=settings.project_dir,
        default_timezone=settings.timezone,
        limits=settings.limits,
        secrets=secrets,
        state_dir=settings.state_dir,
        workflows_dir=settings.workflows_dir,
        environment_checks=environment_checks,
    )


def _load(loader: WorkflowLoader, path: Path, cwd: Path) -> LoadResult:
    """Load one workflow file. A file that can't be read raises ConfigError.

    Any other failure is a bug in Treadle. It keeps its traceback, and a note names the
    file, which `main` prints with the internal error.
    """
    try:
        return loader.load_file(path)
    except OSError as exc:
        raise ConfigError(f"can't read {_shown(path, cwd)}: {exc.strerror or exc}") from None
    except Exception as exc:
        exc.add_note(f"while loading {_shown(path, cwd)}")
        raise


def _expand(paths: Sequence[Path], cwd: Path) -> list[Path | LoadResult]:
    """Return what to validate: each file given, and what `_directory` finds in each directory.

    A file given twice, or given and also found in a directory, counts once.
    """
    found: list[Path | LoadResult] = []
    for given in paths:
        path = cwd / given
        if path.is_dir():
            found.extend(_directory(path))
        elif path.is_file():
            found.append(path)
        else:
            raise NotFoundError(f"{given} doesn't exist")
    return list(dict.fromkeys(found))


def _directory(workflows_dir: Path) -> list[Path | LoadResult]:
    """Return a directory's workflow files to load, in sorted order.

    Each symbolic link with a workflow file's name comes in its sorted place as the result
    that `discover` reports for it, with W060.
    """
    skipped: list[LoadResult] = []
    files = discover(workflows_dir, skipped=skipped)
    return sorted(
        [*files, *skipped], key=lambda item: item if isinstance(item, Path) else item.path
    )


def _validated_json(result: LoadResult, cwd: Path) -> JSONObject:
    label = _shown(result.path, cwd)
    issues: list[JSONValue] = [issue.to_json(label) for issue in result.issues]
    return {"path": label, "workflow": result.workflow_id, "valid": result.ok, "issues": issues}


def _entries(
    settings: Settings, ctx: CliContext, *, skipped: list[LoadResult] | None = None
) -> list[WorkflowEntry]:
    """Load every workflow file. As in the daemon, the first file with an ID defines it.

    Symbolic links are skipped, as `discover` does; `skipped`, when given, receives the
    result that `discover` reports for each.
    """
    loader = _loader(settings, ctx.environ, environment_checks=True)
    files = discover(settings.workflows_dir, skipped=skipped)
    results = cross_check([_load(loader, path, ctx.cwd) for path in files])
    now = SystemClock().now()
    entries: dict[str, WorkflowEntry] = {}
    for result in results:
        workflow_id = result.workflow_id or result.path.stem
        entries.setdefault(
            workflow_id,
            WorkflowEntry(
                workflow_id=workflow_id,
                path=result.path,
                current=result.workflow if result.ok else None,
                issues=result.issues,
                source=result.source,
                stale=False,
                loaded_at=now,
            ),
        )
    return [entries[workflow_id] for workflow_id in sorted(entries)]


def _invalid_workflow(exc: WorkflowInvalidError, settings: Settings, ctx: CliContext) -> int:
    """Report a workflow that can't run, with its file's issues as `treadle validate` shows them.

    A daemon's refusal carries no issues, so they come from loading the files here.
    """
    out = ctx.out
    out.error(str(exc))
    entry = next((e for e in _entries(settings, ctx) if e.workflow_id == exc.workflow_id), None)
    issues = exc.issues or (entry.issues if entry is not None else ())
    if entry is None or not issues:
        out.line("`treadle validate` shows why.", err=True)
        return ExitCode.CONFIG
    label = _shown(entry.path, ctx.cwd)
    for issue in issues:
        out.line(_render_issue(out, issue, label, entry.source), err=True)
    return ExitCode.CONFIG


def _linked_file(settings: Settings, workflow_id: str) -> Path | None:
    """Return the symbolic link that would define `workflow_id` if Treadle followed links."""
    for suffix in _SUFFIXES:
        path = settings.workflows_dir / f"{workflow_id}{suffix}"
        if path.is_symlink():
            return path
    return None


def _linked_workflow(
    workflow_id: str, link: Path, settings: Settings, ctx: CliContext
) -> NotFoundError:
    """The error for a workflow whose only file is a symbolic link, which Treadle skips."""
    where = _shown(settings.workflows_dir, ctx.cwd)
    return NotFoundError(
        f"workflow '{workflow_id}' doesn't exist: {_shown(link, ctx.cwd)} is a symbolic link, "
        f"which Treadle skips; copy the file into {where} instead"
    )


def _workflow_row(
    out: Output,
    entry: WorkflowEntry,
    triggers: Sequence[TriggerState],
    last_run: RunRecord | None,
    zone: tzinfo,
) -> list[str]:
    fires = [state.next_fire for state in triggers if state.next_fire is not None]
    last = "-" if last_run is None else f"#{last_run.id} {out.status(last_run.status.value)}"
    return [
        entry.workflow_id,
        entry.current.name if entry.current else "-",
        ", ".join(state.summary for state in triggers) or "-",
        _local(min(fires), zone, seconds=False) if fires else "-",
        last,
        "stale" if entry.stale else "ok" if entry.valid else "invalid",
    ]


def _graph_file(ref: str, ctx: CliContext) -> tuple[Path, Settings]:
    """Find the file of a workflow given by path, or by ID in the project's workflows.

    By ID, a symbolic link counts as missing, as it does for every other command.
    """
    if ref.endswith(_SUFFIXES) or os.sep in ref or "/" in ref:
        path = ctx.cwd / ref
        if not path.is_file():
            raise NotFoundError(f"{ref} doesn't exist")
        return path, _settings(ctx, require_project=False)
    settings = _settings(ctx)
    for suffix in _SUFFIXES:
        path = settings.workflows_dir / f"{ref}{suffix}"
        if path.is_file() and not path.is_symlink():
            return path, settings
    link = _linked_file(settings, ref)
    if link is not None:
        raise _linked_workflow(ref, link, settings, ctx)
    where = _shown(settings.workflows_dir, ctx.cwd)
    raise NotFoundError(f"workflow '{ref}' doesn't exist in {where}")


def _zone(name: str) -> ZoneInfo:
    """Return the zone that `--tz` names, spelled as in the time zone database."""
    if not is_time_zone(name):
        raise UsageError(f"unknown time zone {name!r}; {time_zone_hint(name)}")
    return ZoneInfo(name)


def _after(text: str | None, zone: tzinfo) -> datetime:
    """Return `--after` as an aware time; a value without an offset is wall time in `zone`."""
    if text is None:
        return SystemClock().now()
    try:
        return parse_iso(text, default_tz=zone)
    except ValueError:
        raise UsageError(
            f"--after: invalid date or time {text!r}; use ISO 8601, such as 2026-03-07T02:30"
        ) from None


# The run history


class _NoRuns:
    """The run history of a project that has no database yet: empty. Implements RunReader."""

    async def get_run(self, run_id: int) -> RunDetail | None:
        return None

    async def list_runs(self, flt: RunFilter) -> RunPage:
        return RunPage((), None)

    async def logs(
        self,
        run_id: int,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        after_seq: int = 0,
        limit: int = 1000,
        tail: bool = False,
    ) -> list[LogLine]:
        return []

    async def last_runs(self) -> dict[str, RunRecord]:
        return {}

    async def latest_run_id(self, workflow_id: str | None = None) -> int | None:
        return None


@contextlib.asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[AsyncStore]:
    """Open the store as a client, which never migrates, and close it afterward."""
    store = await AsyncStore.open(settings, mode="client")
    try:
        yield store
    finally:
        await store.close()


@contextlib.asynccontextmanager
async def _history(settings: Settings) -> AsyncIterator[RunReader]:
    """Read the run history; a project without a database has an empty one."""
    try:
        store = await AsyncStore.open(settings, mode="client")
    except NoDatabaseError:
        yield _NoRuns()
        return
    try:
        yield store
    finally:
        await store.close()


async def _last_runs(settings: Settings) -> dict[str, RunRecord]:
    async with _history(settings) as reader:
        return await reader.last_runs()


async def _list_runs(settings: Settings, flt: RunFilter) -> RunPage:
    async with _history(settings) as reader:
        return await reader.list_runs(flt)


async def _run_detail(reader: RunReader, ref: str) -> RunDetail:
    run_id = await resolve_run_ref(ref, reader)
    detail = await reader.get_run(run_id)
    if detail is None:
        raise NotFoundError(f"run {run_id} doesn't exist")
    return detail


async def _show(args: argparse.Namespace, ctx: CliContext, settings: Settings) -> int:
    async with _history(settings) as reader:
        detail = await _run_detail(reader, args.run)
        if args.json:
            ctx.out.json(run_detail_json(detail))
        else:
            await render_summary(reader, detail, ctx.out, timezone=ZoneInfo(settings.timezone))
    return ExitCode.OK


async def _logs(args: argparse.Namespace, ctx: CliContext, settings: Settings) -> int:
    """Print the log lines page by page as they're read.

    With `--follow`, poll until the run ends and no lines remain. A running run whose lock
    holder stopped without finishing it ends the command with BusyError, because nothing
    runs it any more.
    """
    out, zone = ctx.out, ZoneInfo(settings.timezone)
    async with _history(settings) as reader:
        detail = await _run_detail(reader, args.run)
        run_id = detail.run.id
        if args.step is not None and args.step not in {step.step_id for step in detail.steps}:
            raise NotFoundError(f"run {run_id} has no step '{args.step}'")
        lock = _LockWatch(settings.state_dir)
        after_seq = 0
        while True:
            # The status is read before the lines: a run's lines commit before it finishes.
            status = detail.run.status
            printed = False
            async for page in _log_pages(reader, run_id, args.step, args.attempt, after_seq):
                for line in page:
                    if args.json:
                        out.json_line(log_json(line))
                    else:
                        out.line(_log_line(line, zone))
                after_seq, printed = page[-1].seq, True
            if not args.follow or (status.terminal and not printed):
                return ExitCode.OK
            if status is RunStatus.RUNNING and lock.released():
                raise BusyError(
                    f"no Treadle process is running run #{run_id} any more; it will be marked "
                    "interrupted when Treadle starts again"
                )
            await asyncio.sleep(_LOGS_POLL)
            detail = await _run_detail(reader, str(run_id))


async def _log_pages(
    reader: RunReader, run_id: int, step_id: str | None, attempt: int | None, after_seq: int
) -> AsyncIterator[list[LogLine]]:
    """Yield the run's log lines after `after_seq`, one page at a time, as each is read."""
    while True:
        page = await reader.logs(
            run_id, step_id=step_id, attempt=attempt, after_seq=after_seq, limit=_PAGE
        )
        if page:
            yield page
            after_seq = page[-1].seq
        if len(page) < _PAGE:
            return


def _log_line(line: LogLine, zone: tzinfo) -> str:
    """`HH:MM:SS step#attempt stream | text`, with `-` for a run-level line."""
    where = "-" if line.step_id is None else line.step_id
    if line.step_id is not None and line.attempt is not None:
        where = f"{line.step_id}#{line.attempt}"
    return f"{line.ts.astimezone(zone):%H:%M:%S} {where} {line.stream} | {line.text}"


def _filtered(flt: RunFilter) -> bool:
    return bool(flt.workflow_id or flt.statuses or flt.trigger_kind or flt.text or flt.before_id)


def _older_runs(args: argparse.Namespace, before_id: int) -> str:
    """The `treadle runs` command for the next page, with the same filters."""
    words = ["treadle", "runs"]
    if args.workflow is not None:
        words += ["--workflow", args.workflow]
    for status in args.statuses:
        words += ["--status", status]
    if args.trigger is not None:
        words += ["--trigger", args.trigger]
    if args.grep is not None:
        words += ["--grep", args.grep]
    if args.limit != _RUNS_LIMIT:
        words += ["--limit", str(args.limit)]
    words += ["--before", str(before_id)]
    return shlex.join(words)


def _run_duration(run: RunRecord) -> str:
    if run.duration_ms is not None:
        return format_duration(run.duration_ms / 1000)
    return "running" if run.status is RunStatus.RUNNING else "-"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


# Running and cancelling, here or through the lock holder


class _Unanswered(Enum):
    """Why a control request has no result.

    WITHDRAWN: the CLI withdrew it, so no lock holder acts on it. CLAIMED: the lock holder
    claimed it first, so it may still act on it.
    """

    WITHDRAWN = "withdrawn"
    CLAIMED = "claimed"


class _Signals:
    """The stop signals that arrived while a command ran or waited, in order.

    `event` is set by the first one.
    """

    def __init__(self) -> None:
        self.received: list[int] = []
        self.event = asyncio.Event()

    def __call__(self, signum: int) -> None:
        """Record a stop signal; `_on_stop_signals` calls this on the event loop."""
        self.received.append(signum)
        self.event.set()

    def exit_code(self) -> int:
        """Return 128 plus the first signal's number, as a shell reports it: 130 after Ctrl+C."""
        return 128 + self.received[0]


class _RunStops(_Signals):
    """Handles stop signals during a local run: the first cancels the run, the next forces it.

    Ctrl+C cancels it for the user. SIGTERM and SIGHUP cancel it with the reason `shutdown`,
    because the process that runs it was told to stop. A signal that arrives before the run
    exists takes effect as soon as `follow` names the run.
    """

    def __init__(self, runner: LocalRunner, out: Output) -> None:
        super().__init__()
        self._runner = runner
        self._out = out
        self._run_id: int | None = None
        self._cancels: set[asyncio.Task[CancelOutcome]] = set()

    def __call__(self, signum: int) -> None:
        """Record a stop signal and cancel the run, once there is one."""
        super().__call__(signum)
        if self._run_id is not None:
            self._cancel(self._run_id, first=len(self.received) == 1)

    def follow(self, run_id: int) -> None:
        """Cancel `run_id` on the next signal, or now if one arrived already."""
        self._run_id = run_id
        if self.received:
            self._cancel(run_id, first=True)

    async def settled(self) -> None:
        """Wait until the run manager answered every cancel that a signal requested."""
        await asyncio.gather(*self._cancels, return_exceptions=True)

    def _cancel(self, run_id: int, *, first: bool) -> None:
        signum = self.received[0]
        if first:
            _tell(self._out, _cancelling(run_id, signum))
        reason = CancelReason.USER if signum == signal.SIGINT else CancelReason.SHUTDOWN
        force = len(self.received) > 1
        cancel = self._runner.cancel(run_id, force=force, reason=reason)
        self._cancels.add(asyncio.get_running_loop().create_task(cancel))


def _cancelling(run_id: int, signum: int) -> str:
    """The message for the first stop signal during a local run."""
    if signum == signal.SIGINT:
        return f"Cancelling run #{run_id} (press Ctrl+C again to force)"
    return f"Cancelling run #{run_id} ({signal.Signals(signum).name}; signal again to force)"


def _closed_output(exc: OSError) -> bool:
    """True when a write failed because nothing reads the stream any more."""
    return isinstance(exc, BrokenPipeError) or exc.errno in _CLOSED_OUTPUT


def _tell(out: Output, text: str, *, error: bool = False) -> None:
    """Write a message to stderr after a stop signal, as an error when `error` is set.

    A terminal that hung up, as SIGHUP announces, takes no more output; stderr then goes to
    /dev/null instead of the write failing, so the command still exits with the signal's code.
    """
    try:
        if error:
            out.error(text)
        else:
            out.line(text, err=True)
    except OSError:
        _discard(sys.stderr)


async def _start_run(
    args: argparse.Namespace, ctx: CliContext, settings: Settings, inputs: Mapping[str, str]
) -> int:
    """Run in this process, or hand the run to the daemon, by who holds the lock."""
    holder = await _settled_holder(settings)
    if holder is None:
        if args.detach:
            raise UsageError("--detach needs a running daemon; start `treadle serve` first")
        return await _run_here(args, ctx, settings, inputs)
    if holder.kind == "serve":
        return await _run_in_daemon(args, ctx, settings, inputs, holder)
    if holder.kind == "run":
        state = _shown(settings.state_dir, settings.project_dir)
        raise BusyError(
            f"another `treadle run` (pid {holder.pid}) holds {state}; "
            "start `treadle serve` to run workflows concurrently"
        )
    raise BusyError(_maintaining(holder, settings))


async def _settled_holder(settings: Settings) -> LockHolder | None:
    """Return the lock holder, waiting up to 2 seconds for a maintenance holder to finish."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MAINTENANCE_WAIT
    holder = InstanceLock.probe(settings.state_dir)
    while holder is not None and holder.kind == "maintenance" and loop.time() < deadline:
        await asyncio.sleep(_PROBE_INTERVAL)
        holder = InstanceLock.probe(settings.state_dir)
    return holder


def _maintaining(holder: LockHolder, settings: Settings) -> str:
    who = "another Treadle process"
    if holder.pid:
        who += f" (pid {holder.pid})"
    return f"{who} is maintaining {_shown(settings.state_dir, settings.project_dir)}; try again"


async def _run_here(
    args: argparse.Namespace, ctx: CliContext, settings: Settings, inputs: Mapping[str, str]
) -> int:
    """Run the workflow in this process, which holds the lock as `run`, and follow the run."""
    # Imported here: the runner pulls in the engine and the HTTP client, which the read-only
    # commands don't need.
    from treadle.runner import LocalRunner

    runner = LocalRunner(settings, environ=ctx.environ)
    with _masked_logging(runner._log_masker):
        async with runner:
            stops = _RunStops(runner, ctx.out)
            # The handlers come before the run, so no stop signal can end the process with
            # the run still going.
            with _on_stop_signals(stops):
                if args.rerun is not None:
                    run_id = await resolve_run_ref(args.rerun, runner.store)
                    submission = await runner.start_rerun(run_id)
                else:
                    submission = await runner.start(args.workflow, inputs)
                run = _created(submission)
                return await _follow_here(runner, run.id, args, ctx.out, stops)


async def _follow_here(
    runner: LocalRunner, run_id: int, args: argparse.Namespace, out: Output, stops: _RunStops
) -> int:
    """Follow a run in this process until it ends, while `stops` handles stop signals.

    After a stop signal the command still waits for the run to end, so cleanup steps run,
    and then exits 128 plus the signal's number: 130 after Ctrl+C. When the reader of the
    output goes away, as with `treadle run X | head -1`, the run goes on without output, as
    a daemon's run would, and the command exits 1 once it ends.
    """
    stops.follow(run_id)
    status: RunStatus | None = None
    try:
        run = await follow_run(
            runner.store,
            run_id,
            out,
            verbose=args.verbose,
            json_lines=args.json,
            poll=_FOLLOW_POLL,
        )
        status = run.status
    except OSError as exc:
        if not _closed_output(exc):
            raise
        _discard(sys.stdout)
        await runner.wait(run_id)
    await stops.settled()
    if stops.received:
        return stops.exit_code()
    return ExitCode.FAILURE if status is None else exit_code_for(status)


async def _run_in_daemon(
    args: argparse.Namespace,
    ctx: CliContext,
    settings: Settings,
    inputs: Mapping[str, str],
    holder: LockHolder,
) -> int:
    """Ask the daemon to start the run through a control request, then follow it.

    A stop signal while the CLI waits for the answer withdraws the request; when the daemon
    took it first, the run it starts continues there. A stop signal while following stops
    following. Either way, the command exits 128 plus the signal's number.
    """
    out = ctx.out
    async with _client(settings) as store:
        payload: JSONObject
        if args.rerun is not None:
            original = (await _run_detail(store, args.rerun)).run
            workflow_id = original.workflow_id
            payload = {"rerun_of": original.id, "origin": Origin.CLI.value}
        else:
            workflow_id = args.workflow
            payload = {"workflow": workflow_id, "inputs": dict(inputs), "origin": Origin.CLI.value}
        signals = _Signals()
        with _on_stop_signals(signals):
            reply = await _ask(
                store, "run", payload, wait=_RUN_REPLY_WAIT, interrupted=signals.event
            )
            if isinstance(reply, _Unanswered):
                return _no_run(reply, signals, holder, workflow_id, out)
            run_id, outcome = _started_run(reply, workflow_id)
            if signals.received:
                return await _left_to_daemon(store, settings, holder, run_id, out, signals)
            if args.detach:
                return await _detached(store, run_id, outcome, args, out)
            return await _follow_daemon(store, settings, holder, run_id, args, out, signals)


def _no_run(
    reply: _Unanswered, signals: _Signals, holder: LockHolder, workflow_id: str, out: Output
) -> int:
    """Report a run request without an answer; raises BusyError unless a signal stopped it.

    A withdrawn request starts no run. A request that the daemon claimed may still start one.
    """
    if reply is _Unanswered.WITHDRAWN:
        if signals.received:
            _tell(out, "Stopped before the daemon started a run.")
            return signals.exit_code()
        raise BusyError(f"the daemon (pid {holder.pid}) didn't respond")
    message = (
        f"the daemon (pid {holder.pid}) took the request but didn't answer, so it may still "
        f"start the run; `treadle runs --workflow {workflow_id}` lists it"
    )
    if not signals.received:
        raise BusyError(message)
    _tell(out, message, error=True)
    return signals.exit_code()


def _started_run(result: JSONObject, workflow_id: str) -> tuple[int, JSONValue]:
    """Return the run ID and outcome from the daemon's answer, or raise what it refused."""
    if "error" in result:
        raise _control_error(result, workflow_id)
    run_id, outcome = result.get("run_id"), result.get("outcome")
    if not isinstance(run_id, int):
        raise TreadleError(f"the daemon didn't create a run ({outcome})")
    return run_id, outcome


async def _left_to_daemon(
    store: AsyncStore,
    settings: Settings,
    holder: LockHolder,
    run_id: int,
    out: Output,
    signals: _Signals,
) -> int:
    """Report that the CLI stopped following a daemon's run, and return the signal's code.

    The report says whether the run goes on in the daemon, already finished, or lost its
    daemon.
    """
    status = (await _run_detail(store, str(run_id))).run.status
    if status.terminal:
        _tell(out, f"Run #{run_id} already finished ({status}).")
    elif InstanceLock.probe(settings.state_dir) is None:
        _tell(out, _daemon_stopped(holder, run_id, status), error=True)
    else:
        message = (
            f"Run #{run_id} continues in the daemon. Follow it with `treadle logs {run_id} -f`."
        )
        _tell(out, message)
    return signals.exit_code()


def _daemon_stopped(holder: LockHolder, run_id: int, status: RunStatus) -> str:
    """Say what becomes of a run that is still active when its daemon stopped."""
    stopped = f"the daemon (pid {holder.pid}) stopped"
    if status is RunStatus.QUEUED:
        return f"{stopped}; run #{run_id} stays queued until `treadle serve` starts again"
    return f"{stopped}; run #{run_id} will be marked interrupted when Treadle starts again"


async def _detached(
    store: AsyncStore, run_id: int, outcome: JSONValue, args: argparse.Namespace, out: Output
) -> int:
    """Report a run that the daemon queued or skipped, without following it."""
    run = (await _run_detail(store, str(run_id))).run
    skipped = outcome == "skipped"
    if args.json:
        out.json_line({"event": "run_created", "run": run_json(run)})
    elif skipped:
        reason = f" ({run.reason})" if run.reason else ""
        out.line(f"Run #{run.id} skipped{reason}")
    else:
        out.line(f"Run #{run.id} queued")
    return ExitCode.SKIPPED if skipped else ExitCode.OK


async def _follow_daemon(
    store: AsyncStore,
    settings: Settings,
    holder: LockHolder,
    run_id: int,
    args: argparse.Namespace,
    out: Output,
    signals: _Signals,
) -> int:
    """Follow a daemon's run until it ends, a stop signal arrives, or the daemon goes away.

    A daemon that stops holding the lock before the run ends, such as one that the system
    killed, ends the command with BusyError, because the run no longer progresses.
    """
    follow = asyncio.create_task(
        follow_run(
            store, run_id, out, verbose=args.verbose, json_lines=args.json, poll=_FOLLOW_POLL
        )
    )
    stopped = asyncio.create_task(signals.event.wait())
    released = asyncio.create_task(_until_released(_LockWatch(settings.state_dir)))
    try:
        await asyncio.wait({follow, stopped, released}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        released.cancel()
    if follow.done():
        return exit_code_for(follow.result().status)
    follow.cancel()
    await asyncio.wait([follow])
    if signals.received:
        return await _left_to_daemon(store, settings, holder, run_id, out, signals)
    status = (await _run_detail(store, str(run_id))).run.status
    if status.terminal:
        return exit_code_for(status)
    raise BusyError(_daemon_stopped(holder, run_id, status))


class _LockWatch:
    """Tells whether the process that held the state directory's lock went away.

    `released` probes the lock at most every `_HOLDER_CHECK` seconds, and is True once two
    probes in a row found it free, so a holder that hands the lock to the next one, such as
    a daemon that restarts at once, isn't taken for gone.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._free = 0  # probes in a row that found the lock free
        self._due = 0.0  # the loop time of the next probe

    def released(self) -> bool:
        """Probe the lock when a probe is due, and return whether the holder went away."""
        now = asyncio.get_running_loop().time()
        if now >= self._due:
            self._due = now + _HOLDER_CHECK
            free = InstanceLock.probe(self._state_dir) is None
            self._free = self._free + 1 if free else 0
        return self._free >= 2


async def _until_released(watch: _LockWatch) -> None:
    """Return once `watch` finds that the lock holder went away."""
    while True:
        if watch.released():
            return
        await asyncio.sleep(_HOLDER_CHECK)


async def _cancel(args: argparse.Namespace, ctx: CliContext, settings: Settings) -> int:
    """Cancel through the lock holder, or in the database when no process holds the lock.

    A stop signal while the CLI waits for the lock holder withdraws the request and exits
    128 plus the signal's number; a holder that claimed it first still gets to answer.
    """
    out = ctx.out
    holder = await _settled_holder(settings)
    if holder is None:
        _cancelled(await _cancel_here(args.run, settings), args, out)
        return ExitCode.OK
    if holder.kind == "maintenance":
        raise BusyError(_maintaining(holder, settings))
    signals = _Signals()
    async with _client(settings) as store:
        detail = await _run_detail(store, args.run)
        run_id = detail.run.id
        payload: JSONObject = {"run_id": run_id, "force": args.force}
        if args.wait == 0:
            await _enqueue(store, "cancel", payload)
            if args.json:
                out.json(_outcome_json(run_id, detail.run.status.value, None, args.force))
            else:
                out.line("Cancel requested.")
            return ExitCode.OK
        with _on_stop_signals(signals):
            reply = await _ask(store, "cancel", payload, wait=args.wait, interrupted=signals.event)
    if isinstance(reply, _Unanswered):
        return _no_cancel(reply, signals, holder, run_id, out)
    if "error" in reply:
        raise _control_error(reply, None)
    _cancelled(_cancel_outcome(reply, run_id), args, out)
    return signals.exit_code() if signals.received else ExitCode.OK


def _no_cancel(
    reply: _Unanswered, signals: _Signals, holder: LockHolder, run_id: int, out: Output
) -> int:
    """Report a cancel request without an answer; raises BusyError unless a signal stopped it.

    A withdrawn request cancels nothing. A request that the holder claimed may still cancel.
    """
    who = "the daemon" if holder.kind == "serve" else "the running `treadle run`"
    if reply is _Unanswered.WITHDRAWN:
        if signals.received:
            _tell(out, "Stopped before the lock holder answered; the cancel request was withdrawn.")
            return signals.exit_code()
        raise BusyError(f"{who} (pid {holder.pid}) didn't respond")
    message = (
        f"{who} (pid {holder.pid}) took the cancel request but didn't answer, so it may still "
        f"cancel run #{run_id}"
    )
    if not signals.received:
        raise BusyError(message)
    _tell(out, message, error=True)
    return signals.exit_code()


async def _cancel_here(ref: str, settings: Settings) -> CancelOutcome:
    """Cancel a queued run in the database, holding the lock for maintenance."""
    from treadle.runner import _recover

    if not settings.db_path.exists():
        run_id = await resolve_run_ref(ref, _NoRuns())
        raise NotFoundError(f"run {run_id} doesn't exist")
    clock = SystemClock()
    holder = LockHolder(os.getpid(), "maintenance", iso_utc(clock.now()), __version__)
    with InstanceLock(settings.state_dir, holder):
        store = await AsyncStore.open(settings)
        try:
            await _recover(store, settings, clock)
            run_id = await resolve_run_ref(ref, store)
            at_ms = to_ms(clock.now())
            return await store.write(lambda repo: _cancel_stored(repo, run_id, at_ms))
        finally:
            await store.close()


def _cancel_stored(repo: Repository, run_id: int, at_ms: int) -> CancelOutcome:
    run = repo.get_run(run_id)
    if run is None:
        raise NotFoundError(f"run {run_id} doesn't exist")
    if run.status is RunStatus.QUEUED and repo.cancel_queued(
        [run_id], reason=RunReason.USER, at_ms=at_ms
    ):
        return CancelOutcome(run_id, RunStatus.CANCELLED, accepted=True, forced=False)
    return CancelOutcome(run_id, run.status, accepted=False, forced=False)


def _cancel_outcome(result: JSONObject, run_id: int) -> CancelOutcome:
    """Read a cancel request's result, which has the CancelOutcome shape."""
    status = result.get("status")
    if not isinstance(status, str) or status not in {member.value for member in RunStatus}:
        raise TreadleError(f"the lock holder answered with an unknown status {status!r}")
    return CancelOutcome(
        run_id=run_id,
        status=RunStatus(status),
        accepted=result.get("accepted") is True,
        forced=result.get("forced") is True,
    )


def _outcome_json(run_id: int, status: str, accepted: bool | None, forced: bool) -> JSONObject:
    """The CancelOutcome shape; `accepted` is null when the CLI didn't wait for an answer."""
    return {"run_id": run_id, "status": status, "accepted": accepted, "forced": forced}


def _cancelled(outcome: CancelOutcome, args: argparse.Namespace, out: Output) -> None:
    run = f"run #{outcome.run_id}"
    if args.json:
        status = outcome.status.value
        out.json(_outcome_json(outcome.run_id, status, outcome.accepted, outcome.forced))
    elif outcome.accepted and outcome.status is RunStatus.CANCELLED:
        out.line(f"Cancelled queued {run}.")
    elif outcome.accepted:
        out.line(f"Cancel requested for {run}{' (forced)' if outcome.forced else ''}.")
    elif outcome.status.terminal:
        out.line(f"{run.capitalize()} already finished ({outcome.status}).")
    else:
        out.line(f"{run.capitalize()} is already finishing.")


async def _enqueue(store: AsyncStore, kind: Literal["run", "cancel"], payload: JSONObject) -> int:
    """Insert a control request for the lock holder and return its ID."""
    at_ms = to_ms(SystemClock().now())
    return await store.write(lambda repo: repo.enqueue_control(kind, payload, at_ms=at_ms))


async def _ask(
    store: AsyncStore,
    kind: Literal["run", "cancel"],
    payload: JSONObject,
    *,
    wait: float,
    interrupted: asyncio.Event | None = None,
) -> JSONObject | _Unanswered:
    """Send a control request and return its result, or why it has none.

    After `wait` seconds without an answer, or as soon as `interrupted` is set, the CLI
    withdraws the request, so a lock holder that comes back later never acts on a request
    that the CLI gave up on, and returns WITHDRAWN. When the holder claimed the request just
    before that, its answer is on its way, so the CLI waits up to `_CLAIMED_WAIT` seconds
    more for it, and returns CLAIMED when none comes: the holder may still act on it.
    """
    request_id = await _enqueue(store, kind, payload)
    result = await _result(store, request_id, wait, interrupted)
    if result is not None:
        return result
    at_ms = to_ms(SystemClock().now())
    if await store.write(lambda repo: repo.withdraw_control(request_id, _WITHDRAWN, at_ms=at_ms)):
        return _Unanswered.WITHDRAWN
    result = await _result(store, request_id, _CLAIMED_WAIT)
    return _Unanswered.CLAIMED if result is None else result


async def _result(
    store: AsyncStore, request_id: int, wait: float, interrupted: asyncio.Event | None = None
) -> JSONObject | None:
    """Poll for a control request's result for up to `wait` seconds, or until interrupted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        result = await store.read(lambda repo: repo.control_result(request_id))
        stop = interrupted is not None and interrupted.is_set()
        if result is not None or stop or loop.time() >= deadline:
            return result
        await asyncio.sleep(_CONTROL_POLL)


def _control_error(result: JSONObject, workflow_id: str | None) -> TreadleError:
    """Turn a control request's error result into the error that the CLI reports."""
    error = result.get("error")
    error = error if isinstance(error, dict) else {}
    code, message, details = error.get("code"), str(error.get("message", "")), error.get("details")
    if code == "invalid_inputs" and isinstance(details, dict):
        return InputError({name: str(problem) for name, problem in details.items()})
    if code == "workflow_invalid" and workflow_id is not None:
        return WorkflowInvalidError(workflow_id, ())
    kind = _CONTROL_ERRORS.get(code, TreadleError) if isinstance(code, str) else TreadleError
    return kind(message or f"the lock holder refused the request ({code})")


def _created(submission: Submission) -> RunRecord:
    if submission.run is None:
        raise TreadleError(f"no run was created ({submission.outcome})")
    return submission.run


@contextlib.contextmanager
def _on_stop_signals(callback: Callable[[int], None]) -> Iterator[None]:
    """Call `callback(signum)` on the event loop for each stop signal while the block runs.

    The stop signals are SIGINT (Ctrl+C), SIGTERM, and SIGHUP. Their previous handlers come
    back afterward. Outside the main thread, signals can't be handled, and each keeps its
    usual effect.
    """
    loop = asyncio.get_running_loop()
    with contextlib.ExitStack() as restore:
        for number in _STOP_SIGNALS:
            previous = signal.getsignal(number)
            try:
                loop.add_signal_handler(number, callback, number)
            except (ValueError, RuntimeError):
                break
            # The callbacks run in reverse, so the loop lets go of the signal first.
            if previous is not None:
                restore.callback(signal.signal, number, previous)
            restore.callback(loop.remove_signal_handler, number)
        yield


@contextlib.contextmanager
def _ignoring_sighup() -> Iterator[None]:
    """Ignore SIGHUP while the block runs, until the daemon handles it itself.

    The daemon loads every workflow file when it starts, so a reload requested before then,
    such as one that a service manager sends right after a restart, has nothing to add. The
    previous handler comes back afterward. Outside the main thread, nothing changes.
    """
    try:
        previous = signal.signal(signal.SIGHUP, _ignore_signal)
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_DFL if previous is None else previous)


def _ignore_signal(signum: int, frame: FrameType | None) -> None:
    """A signal handler that does nothing."""


@contextlib.contextmanager
def _masked_logging(masker: Callable[[], Masker]) -> Iterator[None]:
    """Mask every log record that reaches the root logger's handlers while the block runs.

    A run renders secrets, so a log record written during it, such as an unexpected error's
    traceback, could hold one.
    """
    from treadle.runner import _MaskingFilter

    masking = _MaskingFilter(masker)
    handlers = list(logging.getLogger().handlers)
    for handler in handlers:
        handler.addFilter(masking)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(masking)
