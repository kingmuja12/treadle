"""The CLI commands. Each returns its exit code."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
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
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypeAlias
from zoneinfo import ZoneInfo

from treadle import __version__
from treadle.cli.follow import _exit_code, _local, follow_run, render_summary
from treadle.cli.output import Output, _render_issue
from treadle.cli.scaffold import scaffold
from treadle.clock import SystemClock, iso_utc, parse_iso, to_ms
from treadle.config import Overrides, Settings, detect_timezone, load_settings
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
from treadle.store.db import connect, sqlite_version
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
# Withdraws a control request that the CLI stopped waiting for. The condition makes the
# withdrawal atomic: the lock holder never claims a withdrawn request, and a request that it
# claimed first keeps the answer that is on its way. The repository's `complete_control`
# has no such condition, so the CLI runs this one statement on a connection of its own.
_WITHDRAW: Final = (
    "UPDATE control_requests SET consumed_at = ?, result = ? WHERE id = ? AND consumed_at IS NULL"
)
_WITHDRAWN: Final = error_json("expired", "the CLI stopped waiting for an answer")


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
    """`treadle validate [PATH...]`: report issues in workflow files."""
    out = ctx.out
    settings = _settings(ctx, require_project=not args.paths)
    in_project = settings.config_path is not None
    files = _expand(args.paths, ctx.cwd) if args.paths else discover(settings.workflows_dir)
    loader = _loader(settings, ctx.environ, environment_checks=in_project)
    results = cross_check([_load(loader, path, ctx.cwd) for path in files])
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
        valid = sum(result.ok for result in results)
        summary = f"{valid} valid, {len(results) - valid} invalid"
        counts = [_counted(n, noun) for n, noun in ((errors, "error"), (warnings, "warning")) if n]
        if counts:
            summary += f" ({', '.join(counts)})"
        if not in_project:
            summary += "; machine-specific checks skipped (no treadle.toml)"
        out.line(summary)
    return ExitCode.FAILURE if failed else ExitCode.OK


def cmd_list(args: argparse.Namespace, ctx: CliContext) -> int:
    """`treadle list`: show the workflows with their triggers and last runs."""
    out = ctx.out
    settings = _settings(ctx)
    entries = _entries(settings, ctx)
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
    # Imported here: the daemon pulls in the HTTP stack, and every other command would pay
    # for that at startup.
    from treadle.daemon import serve

    level = args.log_level or ctx.overrides.log_level
    overrides = dataclasses.replace(ctx.overrides, host=args.host, port=args.port, log_level=level)
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
    try:
        return loader.load_file(path)
    except OSError as exc:
        raise ConfigError(f"can't read {_shown(path, cwd)}: {exc.strerror or exc}") from None


def _expand(paths: Sequence[Path], cwd: Path) -> list[Path]:
    """Return the files to validate: each file given, and the workflow files of each directory."""
    files: list[Path] = []
    for given in paths:
        path = cwd / given
        if path.is_dir():
            files.extend(discover(path))
        elif path.is_file():
            files.append(path)
        else:
            raise NotFoundError(f"{given} doesn't exist")
    return list(dict.fromkeys(files))


def _validated_json(result: LoadResult, cwd: Path) -> JSONObject:
    label = _shown(result.path, cwd)
    issues: list[JSONValue] = [issue.to_json(label) for issue in result.issues]
    return {"path": label, "workflow": result.workflow_id, "valid": result.ok, "issues": issues}


def _entries(settings: Settings, ctx: CliContext) -> list[WorkflowEntry]:
    """Load every workflow file. As in the daemon, the first file with an ID defines it."""
    loader = _loader(settings, ctx.environ, environment_checks=True)
    results = cross_check(
        [_load(loader, path, ctx.cwd) for path in discover(settings.workflows_dir)]
    )
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
    """Find the file of a workflow given by path, or by ID in the project's workflows."""
    if ref.endswith(_SUFFIXES) or os.sep in ref or "/" in ref:
        path = ctx.cwd / ref
        if not path.is_file():
            raise NotFoundError(f"{ref} doesn't exist")
        return path, _settings(ctx, require_project=False)
    settings = _settings(ctx)
    for suffix in _SUFFIXES:
        path = settings.workflows_dir / f"{ref}{suffix}"
        if path.is_file():
            return path, settings
    where = _shown(settings.workflows_dir, ctx.cwd)
    raise NotFoundError(f"workflow '{ref}' doesn't exist in {where}")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        raise UsageError(
            f"unknown time zone {name!r}; use an IANA name such as Europe/London"
        ) from None


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
    """Print the log lines; with `--follow`, poll until the run ends and no lines remain."""
    out, zone = ctx.out, ZoneInfo(settings.timezone)
    async with _history(settings) as reader:
        detail = await _run_detail(reader, args.run)
        run_id = detail.run.id
        if args.step is not None and args.step not in {step.step_id for step in detail.steps}:
            raise NotFoundError(f"run {run_id} has no step '{args.step}'")
        after_seq = 0
        while True:
            # The status is read before the lines: a run's lines commit before it finishes.
            finished = detail.run.status.terminal
            lines = await _all_logs(reader, run_id, args.step, args.attempt, after_seq)
            for line in lines:
                if args.json:
                    out.json_line(log_json(line))
                else:
                    out.line(_log_line(line, zone))
            after_seq = lines[-1].seq if lines else after_seq
            if not args.follow or (finished and not lines):
                return ExitCode.OK
            await asyncio.sleep(_LOGS_POLL)
            detail = await _run_detail(reader, str(run_id))


async def _all_logs(
    reader: RunReader, run_id: int, step_id: str | None, attempt: int | None, after_seq: int
) -> list[LogLine]:
    lines: list[LogLine] = []
    while True:
        page = await reader.logs(
            run_id, step_id=step_id, attempt=attempt, after_seq=after_seq, limit=_PAGE
        )
        lines.extend(page)
        if len(page) < _PAGE:
            return lines
        after_seq = page[-1].seq


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
    # Imported here: the runner pulls in the engine and the HTTP client, which the read-only
    # commands don't need.
    from treadle.runner import LocalRunner

    runner = LocalRunner(settings, environ=ctx.environ)
    with _masked_logging(runner._log_masker):
        async with runner:
            if args.rerun is not None:
                run_id = await resolve_run_ref(args.rerun, runner.store)
                submission = await runner.start_rerun(run_id)
            else:
                submission = await runner.start(args.workflow, inputs)
            run = _created(submission)
            return await _follow_here(runner, run.id, args, ctx.out)


async def _follow_here(
    runner: LocalRunner, run_id: int, args: argparse.Namespace, out: Output
) -> int:
    """Follow a run in this process. Ctrl-C cancels it, a second Ctrl-C forces it.

    After a Ctrl-C the command still waits for the run to end, so cleanup steps run, and
    then exits 130.
    """
    loop = asyncio.get_running_loop()
    presses = 0
    cancels: set[asyncio.Task[CancelOutcome]] = set()

    def interrupt() -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            out.line(f"Cancelling run #{run_id} (press Ctrl-C again to force)", err=True)
        cancels.add(loop.create_task(runner.cancel(run_id, force=presses > 1)))

    with _on_interrupt(interrupt):
        run = await follow_run(
            runner.store,
            run_id,
            out,
            verbose=args.verbose,
            json_lines=args.json,
            poll=_FOLLOW_POLL,
        )
    await asyncio.gather(*cancels, return_exceptions=True)
    return ExitCode.INTERRUPTED if presses else exit_code_for(run.status)


async def _run_in_daemon(
    args: argparse.Namespace,
    ctx: CliContext,
    settings: Settings,
    inputs: Mapping[str, str],
    holder: LockHolder,
) -> int:
    """Ask the daemon to start the run through a control request, then follow it.

    Ctrl-C while the CLI waits for the answer withdraws the request; when the daemon took
    it first, the run it starts continues there. Ctrl-C while following stops following.
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
        interrupted = asyncio.Event()
        with _on_interrupt(interrupted.set):
            result = await _ask(
                store, settings, "run", payload, wait=_RUN_REPLY_WAIT, interrupted=interrupted
            )
            if result is None and interrupted.is_set():
                out.line("Stopped before the daemon started a run.", err=True)
                return ExitCode.INTERRUPTED
            run_id, outcome = _started_run(result, workflow_id, holder)
            if interrupted.is_set():
                return _left_to_daemon(run_id, out)
            if args.detach:
                return await _detached(store, run_id, outcome, args, out)
            return await _follow_daemon(store, run_id, args, out, interrupted)


def _started_run(
    result: JSONObject | None, workflow_id: str, holder: LockHolder
) -> tuple[int, JSONValue]:
    """Return the run ID and outcome from the daemon's answer, or raise what it refused."""
    if result is None:
        raise BusyError(f"the daemon (pid {holder.pid}) didn't respond")
    if "error" in result:
        raise _control_error(result, workflow_id)
    run_id, outcome = result.get("run_id"), result.get("outcome")
    if not isinstance(run_id, int):
        raise TreadleError(f"the daemon didn't create a run ({outcome})")
    return run_id, outcome


def _left_to_daemon(run_id: int, out: Output) -> int:
    out.line(
        f"Run #{run_id} continues in the daemon. Follow it with `treadle logs {run_id} -f`.",
        err=True,
    )
    return ExitCode.INTERRUPTED


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
    run_id: int,
    args: argparse.Namespace,
    out: Output,
    interrupted: asyncio.Event,
) -> int:
    """Follow a daemon's run until it ends, or until `interrupted` is set by Ctrl-C."""
    follow = asyncio.create_task(
        follow_run(
            store, run_id, out, verbose=args.verbose, json_lines=args.json, poll=_FOLLOW_POLL
        )
    )
    stopped = asyncio.create_task(interrupted.wait())
    await asyncio.wait({follow, stopped}, return_when=asyncio.FIRST_COMPLETED)
    stopped.cancel()
    if follow.done():
        return exit_code_for(follow.result().status)
    follow.cancel()
    await asyncio.wait([follow])
    return _left_to_daemon(run_id, out)


async def _cancel(args: argparse.Namespace, ctx: CliContext, settings: Settings) -> int:
    """Cancel through the lock holder, or in the database when no process holds the lock."""
    holder = await _settled_holder(settings)
    if holder is None:
        _cancelled(await _cancel_here(args.run, settings), args, ctx.out)
        return ExitCode.OK
    if holder.kind == "maintenance":
        raise BusyError(_maintaining(holder, settings))
    async with _client(settings) as store:
        detail = await _run_detail(store, args.run)
        run_id = detail.run.id
        payload: JSONObject = {"run_id": run_id, "force": args.force}
        if args.wait == 0:
            await _enqueue(store, "cancel", payload)
            if args.json:
                ctx.out.json(_outcome_json(run_id, detail.run.status.value, None, args.force))
            else:
                ctx.out.line("Cancel requested.")
            return ExitCode.OK
        result = await _ask(store, settings, "cancel", payload, wait=args.wait)
    if result is None:
        who = "the daemon" if holder.kind == "serve" else "the running `treadle run`"
        raise BusyError(f"{who} (pid {holder.pid}) didn't respond")
    if "error" in result:
        raise _control_error(result, None)
    _cancelled(_cancel_outcome(result, run_id), args, ctx.out)
    return ExitCode.OK


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
    settings: Settings,
    kind: Literal["run", "cancel"],
    payload: JSONObject,
    *,
    wait: float,
    interrupted: asyncio.Event | None = None,
) -> JSONObject | None:
    """Send a control request and return its result, or None when the lock holder is silent.

    After `wait` seconds without an answer, or as soon as `interrupted` is set, the CLI
    withdraws the request, so a lock holder that comes back later never acts on a request
    that the CLI gave up on. When the holder claimed the request just before that, its
    answer is on its way, and the CLI waits a little longer for it.
    """
    request_id = await _enqueue(store, kind, payload)
    result = await _result(store, request_id, wait, interrupted)
    if result is not None:
        return result
    at_ms = to_ms(SystemClock().now())
    if await asyncio.to_thread(_withdraw, settings.db_path, request_id, at_ms):
        return None
    return await _result(store, request_id, _CLAIMED_WAIT)


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


def _withdraw(db_path: Path, request_id: int, at_ms: int) -> bool:
    """Withdraw an unclaimed control request; False when the lock holder already claimed it."""
    conn = connect(db_path)
    try:
        cursor = conn.execute(_WITHDRAW, (at_ms, json.dumps(_WITHDRAWN), request_id))
        return cursor.rowcount > 0
    finally:
        conn.close()


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
def _on_interrupt(callback: Callable[[], None]) -> Iterator[None]:
    """Call `callback` on the event loop for each Ctrl-C while the block runs.

    The previous SIGINT handler comes back afterward. Outside the main thread, signals can't
    be handled, and Ctrl-C keeps its usual effect.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    try:
        loop.add_signal_handler(signal.SIGINT, callback)
    except (ValueError, RuntimeError):
        yield
        return
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def _masked_logging(masker: Callable[[], Masker]) -> Iterator[None]:
    """Mask every log record that reaches the root logger's handlers while the block runs.

    A run renders secrets, so a log record written during it, such as an unexpected error's
    traceback, could hold one.
    """
    from treadle.daemon import MaskingFilter

    masking = MaskingFilter(masker)
    handlers = list(logging.getLogger().handlers)
    for handler in handlers:
        handler.addFilter(masking)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(masking)
