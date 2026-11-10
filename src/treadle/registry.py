"""The workflow registry: loads every workflow and hot-reloads changes.

Section 6 ("Hot reload") defines how valid, invalid, and deleted files apply.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Final

from treadle.clock import BlockingRunner, Clock, thread_runner
from treadle.config import Settings
from treadle.fswatch import Change, Debouncer, RootUnavailable, Snapshot, TooManyEntries, diff, scan
from treadle.globs import GlobMatcher
from treadle.models import RegistryChange, WatchEvent, Workflow, WorkflowEntry
from treadle.protocols import TriggerSink
from treadle.secrets import SecretStore
from treadle.steps.base import StepRegistry
from treadle.store.async_store import AsyncStore
from treadle.types import Issue, Severity
from treadle.workflow.loader import LoadResult, WorkflowLoader, cross_check, workflow_id_for
from treadle.yamlio import load_yaml

_DEBOUNCE: Final = 0.5
_MAX_WAIT: Final = 60.0
_MAX_ISSUES: Final = 50
# Section 2's workflow files: `*.yml` and `*.yaml`, except names that start with "." or "_".
# Design note: section 6 polls the directory with `fswatch.scan`, which never follows
# symlinks, so a symlinked workflow file is ignored at startup and on reload alike.
_FILES: Final = GlobMatcher(("*.yml", "*.yaml"), (".*", "_*"))

_log = logging.getLogger(__name__)


class WorkflowRegistry:
    """The current version of every workflow file; implements WorkflowSource.

    An invalid edit keeps the last good version (and marks it stale), and a deleted file is
    removed and reported to `on_removed`. Changes go to every sink synchronously.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        steps: StepRegistry,
        secrets: SecretStore,
        store: AsyncStore,
        sinks: Sequence[TriggerSink],
        on_removed: Callable[[str], Awaitable[None]] | None,
        clock: Clock,
        blocking: BlockingRunner = thread_runner,
    ) -> None:
        self._settings = settings
        self._steps = steps
        self._secrets = secrets
        self._store = store
        self._sinks = tuple(sinks)
        self._on_removed = on_removed
        self._clock = clock
        self._blocking = blocking
        self._entries: dict[str, WorkflowEntry] = {}
        self._results: dict[Path, LoadResult] = {}  # the newest load of each file
        self._snapshot: Snapshot = {}
        self._debouncer = Debouncer(_DEBOUNCE, _MAX_WAIT)
        self._reload = asyncio.Event()
        self._lock = asyncio.Lock()
        self._scan_problem: str | None = None
        self._config_stamp: tuple[int, int, int] | None = None
        self._config_seen = False

    async def load_all(self) -> list[RegistryChange]:
        """Load every workflow file and apply the valid ones.

        A webhook name belongs to the first valid workflow in path order that uses it, and
        an invalid file has no current version.
        """
        async with self._lock:
            await self._check_side_files()
            snapshot = await self._scan()
            self._snapshot = {} if snapshot is None else snapshot
            loaded = [await self._load(Path(path)) for path in sorted(self._snapshot)]
            results = [result for result in loaded if result is not None]
            # Design note: the first file in path order defines a workflow ID, and `_refresh`
            # logs each duplicate instead of storing E058 on the entry, so a duplicate takes
            # over cleanly when the first file goes. Only those files go to `cross_check`.
            primaries: dict[str, LoadResult] = {}
            for result in results:
                primaries.setdefault(_workflow_id(result.path), result)
            checked = {result.path: result for result in cross_check(list(primaries.values()))}
            self._results = {result.path: checked.get(result.path, result) for result in results}
            ids = {_workflow_id(path) for path in self._results} | set(self._entries)
            return [change for wid in sorted(ids) for change in await self._refresh(wid)]

    async def poll(self) -> list[RegistryChange]:
        """Reload the files that changed since the last poll, and `.env` when it changed.

        Changes that `run` is still debouncing apply now as well.
        """
        async with self._lock:
            await self._check_side_files()
            self._debouncer.add(await self._scan_changes(), _loop_time())
            return await self._apply(self._debouncer.take())

    async def run(self, *, interval: float = 1.0) -> None:
        """Poll every `interval` seconds, or at once after `request_reload`, until cancelled.

        Changed files apply after 0.5 seconds without further changes. A failed poll is
        logged, and polling goes on.
        """
        while True:
            try:
                if self._reload.is_set():
                    self._reload.clear()
                    await self.poll()
                else:
                    await self._tick()
            except Exception:
                _log.exception("reloading the workflows failed; trying again")
            timeout = interval
            deadline = self._debouncer.deadline()
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - _loop_time()))
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await self._reload.wait()

    def request_reload(self) -> None:
        """Poll now (SIGHUP)."""
        self._reload.set()

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        """Return the entry for one workflow ID, or None."""
        return self._entries.get(workflow_id)

    def entries(self) -> list[WorkflowEntry]:
        """Return every entry, sorted by workflow ID."""
        return [self._entries[workflow_id] for workflow_id in sorted(self._entries)]

    async def version(self, digest: str) -> Workflow | None:
        """Return a stored workflow version, parsed again, or None when it no longer loads."""
        # Design note: a digest that a current version has is that version's exact bytes, so
        # it's returned without parsing the stored source again.
        for entry in self._entries.values():
            if entry.current is not None and entry.current.digest == digest:
                return entry.current
        stored = await self._store.read(lambda repo: repo.workflow_version(digest))
        if stored is None:
            return None
        loader = self._loader(environment_checks=False)
        path = self._settings.project_dir / stored.path
        result = await self._blocking(loader.load_text, stored.source, path)
        return result.workflow if result.ok else None

    def current_digests(self) -> frozenset[str]:
        """Return the digests of the current versions, which retention keeps."""
        return frozenset(workflow.digest for workflow in self._current())

    def hidden_env(self) -> frozenset[str]:
        """Return `token_env` plus every `secret_env` of the current versions."""
        names = {hook.secret_env for workflow in self._current() for hook in workflow.webhooks()}
        return frozenset({self._settings.server.token_env, *names})

    async def _tick(self) -> None:
        """Scan once, and apply the debounced changes that are due."""
        async with self._lock:
            await self._check_side_files()
            self._debouncer.add(await self._scan_changes(), _loop_time())
            if self._debouncer.due(_loop_time()):
                await self._apply(self._debouncer.take())

    async def _apply(self, changes: Sequence[Change]) -> list[RegistryChange]:
        """Reload changed files one at a time: deletions first, then the rest in path order.

        Each valid file's webhook names are checked against the current versions of every
        other workflow, including those that earlier files in the batch changed.
        """
        emitted: list[RegistryChange] = []
        for change in sorted(changes, key=lambda c: (c.event is not WatchEvent.DELETED, c.path)):
            path = Path(change.path)
            result = None if change.event is WatchEvent.DELETED else await self._load(path)
            if result is None:
                self._results.pop(path, None)
            else:
                self._results[path] = self._claim_hooks(result)
            emitted.extend(await self._refresh(_workflow_id(path)))
        return emitted

    async def _refresh(self, workflow_id: str) -> list[RegistryChange]:
        """Rebuild one workflow's entry from its newest file, and emit the change it makes."""
        old = self._entries.get(workflow_id)
        old_current = None if old is None else old.current
        paths = sorted(path for path in self._results if _workflow_id(path) == workflow_id)
        if not paths:
            return await self._remove(workflow_id, old)
        primary = self._results[paths[0]]
        for duplicate in paths[1:]:
            _log.warning(
                "%s: duplicate workflow ID '%s': %s uses it, so this file is ignored",
                self._display(duplicate),
                workflow_id,
                paths[0].name,
            )
        current = primary.workflow if primary.ok else old_current
        stale = not primary.ok and current is not None
        self._entries[workflow_id] = WorkflowEntry(
            workflow_id=workflow_id,
            path=primary.path,
            current=current,
            issues=primary.issues,
            source=primary.source,
            stale=stale,
            loaded_at=self._clock.now(),
        )
        self._log_issues(primary, stale=stale)
        if _same_version(old_current, current):
            return []
        return [self._emit(RegistryChange(workflow_id, old_current, current))]

    async def _remove(self, workflow_id: str, old: WorkflowEntry | None) -> list[RegistryChange]:
        """Drop a workflow whose files are gone, and report it to `on_removed`."""
        # Design note: `on_removed` runs even for a workflow that never had a current version,
        # because queued runs of an earlier session may still exist for it.
        if old is None:
            return []
        del self._entries[workflow_id]
        changes = []
        if old.current is not None:
            changes.append(self._emit(RegistryChange(workflow_id, old.current, None)))
        _log.info("workflow %s was removed", workflow_id)
        if self._on_removed is not None:
            try:
                await self._on_removed(workflow_id)
            except Exception:
                _log.exception("workflow %s: cancelling its queued runs failed", workflow_id)
        return changes

    def _emit(self, change: RegistryChange) -> RegistryChange:
        if change.new is not None:
            _log.info(
                "workflow %s: version %s is current", change.workflow_id, change.new.short_digest
            )
        for sink in self._sinks:
            sink.apply(change)
        return change

    def _claim_hooks(self, result: LoadResult) -> LoadResult:
        """Add E305 for each webhook name that another workflow's current version uses."""
        workflow = result.workflow if result.ok else None
        if workflow is None:
            return result
        owners = {
            hook.name: entry.workflow_id
            for entry in self._entries.values()
            if entry.current is not None and entry.workflow_id != workflow.id
            for hook in entry.current.webhooks()
        }
        conflicts = [
            _hook_conflict(result, number, hook.name, owners[hook.name])
            for number, hook in enumerate(workflow.webhooks())
            if hook.name in owners
        ]
        if not conflicts:
            return result
        return dataclasses.replace(result, issues=_sorted_issues([*result.issues, *conflicts]))

    async def _load(self, path: Path) -> LoadResult | None:
        """Load one file, or return None when it vanished or can't be read."""
        # Design note: an unreadable file counts as deleted, as the loader's OSError contract
        # suggests; the warning tells the user why the workflow went away.
        try:
            return await self._blocking(self._loader().load_file, path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            _log.warning("can't read %s (%s); treating it as deleted", self._display(path), exc)
            return None

    def _loader(self, *, environment_checks: bool = True) -> WorkflowLoader:
        """Return a loader that checks against the current secrets."""
        settings = self._settings
        return WorkflowLoader(
            self._steps,
            project_dir=settings.project_dir,
            default_timezone=settings.timezone,
            limits=settings.limits,
            secrets=self._secrets.snapshot,
            state_dir=settings.state_dir,
            workflows_dir=settings.workflows_dir,
            environment_checks=environment_checks,
        )

    async def _scan(self) -> Snapshot | None:
        """Scan the workflows directory, or return None (logged once) when it can't be read."""
        # Design note: like a watch root, an unreadable workflows directory emits no
        # deletions; the loaded workflows stay until a scan works again.
        scan_dir = functools.partial(
            scan,
            self._settings.workflows_dir,
            recursive=False,
            matcher=_FILES,
            max_entries=self._settings.limits.watch_max_entries,
        )
        try:
            snapshot = await self._blocking(scan_dir)
        except (RootUnavailable, TooManyEntries) as exc:
            problem = str(exc)
            if problem != self._scan_problem:
                _log.warning("can't scan the workflows directory (%s)", problem)
            self._scan_problem = problem
            return None
        if self._scan_problem is not None:
            _log.info("the workflows directory can be scanned again")
            self._scan_problem = None
        return snapshot

    async def _scan_changes(self) -> list[Change]:
        snapshot = await self._scan()
        if snapshot is None:
            return []
        changes = diff(self._snapshot, snapshot, self._settings.workflows_dir)
        self._snapshot = snapshot
        return changes

    async def _check_side_files(self) -> None:
        """Reload `.env` when it changed, and log once when the config file changed."""
        if await self._blocking(self._secrets.reload_if_changed):
            _log.info("reloaded the secrets from %s", self._settings.env_file)
        config = self._settings.config_path
        if config is None:
            return
        stamp = await self._blocking(_stamp, config)
        if self._config_seen and stamp != self._config_stamp:
            _log.warning("%s changed; restart Treadle to apply it", config.name)
        self._config_stamp, self._config_seen = stamp, True

    def _log_issues(self, result: LoadResult, *, stale: bool) -> None:
        """Log a file's issues, rendered in the CLI format."""
        if not result.issues:
            return
        display = self._display(result.path)
        rendered = "\n".join(issue.render(display, result.source) for issue in result.issues)
        if result.ok:
            headline = f"{display} has warnings"
        elif stale:
            headline = f"{display} is invalid; keeping the last good version"
        else:
            headline = f"{display} is invalid"
        _log.warning("%s\n%s", headline, rendered)

    def _display(self, path: Path) -> str:
        """Show a path relative to the project directory when it's inside it."""
        if path.is_relative_to(self._settings.project_dir):
            return str(path.relative_to(self._settings.project_dir))
        return str(path)

    def _current(self) -> Iterable[Workflow]:
        return (entry.current for entry in self._entries.values() if entry.current is not None)


def _workflow_id(path: Path) -> str:
    """Return the workflow ID of a file; a file with an invalid ID is keyed by its stem."""
    # Design note: keying such a file by its stem lists it, with its E057, in `entries()`.
    return workflow_id_for(path) or path.stem


def _same_version(old: Workflow | None, new: Workflow | None) -> bool:
    if old is None or new is None:
        return old is new
    return (old.digest, old.path) == (new.digest, new.path)


def _hook_conflict(result: LoadResult, number: int, name: str, owner: str) -> Issue:
    """E305 at the name of webhook `number`, located by parsing the file's source again."""
    path = ("triggers", "webhook", number, "name")
    document = load_yaml(result.source or "")[0]
    position = None if document is None else document.position(path)
    message = f"webhook name '{name}' is already used by workflow '{owner}'"
    hint = "webhook names are unique across workflows; rename one of them"
    return Issue(Severity.ERROR, "E305", message, path, position, hint)


def _sorted_issues(issues: Iterable[Issue]) -> tuple[Issue, ...]:
    """Sort by position, issues about the whole file first, and keep at most 50."""

    def order(issue: Issue) -> tuple[int, int, int]:
        position = issue.position
        return (0, 0, 0) if position is None else (1, position.line, position.column)

    return tuple(sorted(issues, key=order)[:_MAX_ISSUES])


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """Identify a file's version by (mtime, size, inode), or None when it's missing."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def _loop_time() -> float:
    return asyncio.get_running_loop().time()
