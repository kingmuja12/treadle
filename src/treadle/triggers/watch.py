"""Watch triggers: polling, debouncing, and degraded roots."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from treadle.clock import BlockingRunner, thread_runner
from treadle.config import Limits
from treadle.errors import TreadleError
from treadle.fswatch import Change, Debouncer, RootUnavailable, Snapshot, TooManyEntries, diff, scan
from treadle.globs import GlobMatcher
from treadle.models import RunRequest, TriggerKind, TriggerState, WatchTrigger, Workflow
from treadle.protocols import RunSubmitter
from treadle.types import JSONObject, JSONValue

MAX_PAYLOAD_CHANGES: Final = 1000
# A batch fires at the latest this long after its first change, so churn can't starve it.
_MIN_MAX_WAIT: Final = 60.0

# The directory that a root path names, as (st_dev, st_ino). It changes when a drive is
# mounted or unmounted at the root, when a root symlink is re-pointed, and when the root
# directory is deleted and created again.
_Identity: TypeAlias = tuple[int, int]

_log = logging.getLogger(__name__)


def watch_payload(trigger: WatchTrigger, changes: Sequence[Change]) -> JSONObject:
    """Return the `trigger` payload of a watch run, with at most `MAX_PAYLOAD_CHANGES` changes.

    Only changes whose event the trigger watches count. `paths` lists the changed files,
    and `total` and `truncated` describe the changes beyond the cap.
    """
    relevant = [change for change in changes if change.event in trigger.events]
    shown = relevant[:MAX_PAYLOAD_CHANGES]
    paths: list[JSONValue] = [change.path for change in shown]
    listed: list[JSONValue] = [
        {"event": change.event.value, "path": change.path, "root": change.root, "rel": change.rel}
        for change in shown
    ]
    return {
        "kind": TriggerKind.WATCH.value,
        "paths": paths,
        "changes": listed,
        "total": len(relevant),
        "truncated": len(relevant) > MAX_PAYLOAD_CHANGES,
    }


@dataclass(frozen=True, slots=True)
class _Scan:
    """One good scan of a root: the directory it read, its files, and its unreadable parts.

    `unreadable` holds the subdirectories that couldn't be listed.
    """

    identity: _Identity
    snapshot: Snapshot
    unreadable: frozenset[str]


@dataclass(slots=True)
class _Root:
    """One watched root. `baseline` is None until a good scan, and again after degrading.

    `identity` is the directory that the baseline describes, and `unreadable` holds the
    subdirectories that the baseline's scan couldn't list.
    """

    path: Path
    baseline: Snapshot | None = None
    identity: _Identity | None = None
    unreadable: frozenset[str] = frozenset()
    seen: bool = False
    error: str | None = None


class _Watcher:
    """The runtime state of one watch trigger: its roots' baselines and its pending batch."""

    def __init__(self, workflow_id: str, trigger: WatchTrigger, index: int) -> None:
        self.workflow_id = workflow_id
        self.trigger = trigger
        self.index = index
        self.roots = [_Root(path) for path in trigger.paths]
        self.matcher = GlobMatcher(trigger.include, trigger.exclude)
        self.debouncer = Debouncer(trigger.debounce, max(_MIN_MAX_WAIT, trigger.debounce))
        self.warned_unreadable = False

    @property
    def error(self) -> str | None:
        """The first degraded root's problem, or None while every root is healthy."""
        return next((root.error for root in self.roots if root.error is not None), None)


class WatchService:
    """Supervises one polling task per watch trigger key.

    Scans run through `blocking`. A root that disappears degrades the trigger without
    emitting deletes, and the next good scan takes a silent baseline. A root that becomes a
    different directory takes a silent baseline at once, and files under a subdirectory that
    can't be read report no changes until it can.
    """

    def __init__(
        self,
        *,
        submitter: RunSubmitter,
        limits: Limits,
        blocking: BlockingRunner = thread_runner,
    ) -> None:
        self._submitter = submitter
        self._limits = limits
        self._blocking = blocking
        self._watchers: dict[str, _Watcher] = {}
        self._changed = asyncio.Event()

    def update(self, workflow_id: str, workflow: Workflow | None) -> None:
        """Swap a workflow's watches synchronously; the supervisor reconciles the tasks.

        A watch whose key is unchanged keeps its watcher, and with it its baselines.
        """
        triggers = {} if workflow is None else _watches_of(workflow)
        for key in [key for key, w in self._watchers.items() if w.workflow_id == workflow_id]:
            if key not in triggers:
                del self._watchers[key]
        for key, (index, trigger) in triggers.items():
            existing = self._watchers.get(key)
            if existing is None:
                self._watchers[key] = _Watcher(workflow_id, trigger, index)
            else:
                existing.index = index
        self._changed.set()

    def states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return the watch trigger states, including degraded roots."""
        chosen = [w for w in self._watchers.values() if workflow_id in (None, w.workflow_id)]
        chosen.sort(key=lambda w: (w.workflow_id, w.index))
        return [_watch_state(w.workflow_id, w.trigger, w.error) for w in chosen]

    async def run(self) -> None:
        """Supervise the watch tasks until cancelled.

        It starts a task for each new key and stops the tasks of removed keys after every
        `update`, and stops every task when it's cancelled.
        """
        running: dict[str, tuple[_Watcher, asyncio.Task[None]]] = {}
        try:
            while True:
                self._changed.clear()
                await self._reconcile(running)
                await self._changed.wait()
        finally:
            tasks = [task for _, task in running.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reconcile(self, running: dict[str, tuple[_Watcher, asyncio.Task[None]]]) -> None:
        stopped = []
        for key, (watcher, task) in list(running.items()):
            if self._watchers.get(key) is not watcher:
                task.cancel()
                stopped.append(task)
                del running[key]
        for key, watcher in self._watchers.items():
            if key not in running:
                task = asyncio.create_task(self._watch(watcher), name=f"watch {key}")
                running[key] = (watcher, task)
        await asyncio.gather(*stopped, return_exceptions=True)

    async def _watch(self, watcher: _Watcher) -> None:
        """Scan every `poll_interval` and at each debounce deadline; fire batches once quiet.

        The scan at a batch's deadline confirms the quiet period, so a file that is still
        growing between polls holds the batch back. These extra scans happen only while a
        batch waits, at most once per `debounce`.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                changes = await self._poll(watcher)
            except Exception:
                _log.exception("watch %s: the scan failed", watcher.trigger.key)
                changes = []
            next_poll = loop.time() + watcher.trigger.poll_interval
            watcher.debouncer.add(changes, loop.time())
            if watcher.debouncer.due(loop.time()):
                await self._fire(watcher, watcher.debouncer.take())
            wake = next_poll
            deadline = watcher.debouncer.deadline()
            if deadline is not None:
                wake = min(wake, deadline)
            await asyncio.sleep(max(0.0, wake - loop.time()))

    async def _poll(self, watcher: _Watcher) -> list[Change]:
        """Scan every root and return the changes since its baseline."""
        changes: list[Change] = []
        unreadable: list[str] = []
        for root in watcher.roots:
            scan_root = functools.partial(
                _scan_root,
                root.path,
                recursive=watcher.trigger.recursive,
                matcher=watcher.matcher,
                max_entries=self._limits.watch_max_entries,
            )
            try:
                scanned = await self._blocking(scan_root)
            except (RootUnavailable, TooManyEntries) as exc:
                _degrade(watcher, root, f"degraded: {exc}")
                continue
            if scanned is None:
                _log.debug("watch %s: %s changed during the scan", watcher.trigger.key, root.path)
                continue
            if root.error is not None:
                _log.info("watch %s: %s is available again", watcher.trigger.key, root.path)
                root.error = None
            unreadable.extend(sorted(scanned.unreadable))
            changes.extend(_changes(watcher, root, scanned))
        # Unreadable directories are logged once per watcher, not on every poll: they tend
        # to stay unreadable, and their files report no changes meanwhile.
        if unreadable and not watcher.warned_unreadable:
            watcher.warned_unreadable = True
            _log.warning(
                "watch %s: skipping unreadable directories, such as %s",
                watcher.trigger.key,
                unreadable[0],
            )
        return changes

    async def _fire(self, watcher: _Watcher, batch: list[Change]) -> None:
        """Submit one run for a batch, unless the trigger watches none of its events."""
        trigger = watcher.trigger
        payload = watch_payload(trigger, batch)
        if not payload["total"]:
            return
        request = RunRequest(
            workflow_id=watcher.workflow_id,
            kind=TriggerKind.WATCH,
            trigger=payload,
            trigger_key=trigger.key,
        )
        try:
            submission = await self._submitter.submit(request)
        except Exception as exc:
            _log.warning(
                "watch %s: the run wasn't created: %s",
                trigger.key,
                exc,
                exc_info=not isinstance(exc, TreadleError),
            )
            return
        run_id = submission.run.id if submission.run is not None else None
        _log.info(
            "watch %s: %s change(s); run %s %s",
            trigger.key,
            payload["total"],
            run_id,
            submission.outcome.value,
        )


def _changes(watcher: _Watcher, root: _Root, scanned: _Scan) -> list[Change]:
    """Diff a good scan against the root's baseline, then make it the new baseline.

    A root without a baseline reports nothing, except that `initial_scan` reports every file
    as created on the root's first good scan. A root that is now a different directory, such
    as the empty mount point that an unmounted drive leaves behind, takes a new baseline
    silently too. Files under a subdirectory that can't be listed keep their baseline state.
    """
    baseline, snapshot = root.baseline, scanned.snapshot
    replaced = baseline is not None and scanned.identity != root.identity
    if baseline is not None and not replaced:
        snapshot = _keep_unreadable(baseline, snapshot, scanned.unreadable)
        baseline = _admit_readable(baseline, snapshot, root.unreadable - scanned.unreadable)
    root.baseline, root.identity, root.unreadable = snapshot, scanned.identity, scanned.unreadable
    if replaced:
        _log.warning(
            "watch %s: %s is a different directory now; taking a new baseline",
            watcher.trigger.key,
            root.path,
        )
        return []
    if baseline is not None:
        return diff(baseline, snapshot, root.path)
    # `initial_scan` applies to a root's first good scan, even when the root was missing at
    # first. A root that comes back after degrading gets a silent baseline.
    first, root.seen = not root.seen, True
    if first and watcher.trigger.initial_scan:
        return diff({}, snapshot, root.path)
    return []


def _keep_unreadable(
    baseline: Snapshot, snapshot: Snapshot, unreadable: Collection[str]
) -> Snapshot:
    """Add the baseline's files under directories that the scan couldn't list, unchanged."""
    if not unreadable:
        return snapshot
    return snapshot | _inside(baseline, unreadable)


def _admit_readable(baseline: Snapshot, snapshot: Snapshot, readable: Collection[str]) -> Snapshot:
    """Add the files of readable-again directories that the baseline holds no files for.

    The baseline never saw those files, so they can't be told apart from files that were
    there all along: like a root that comes back, such a directory joins without events.
    """
    unknown = [directory for directory in readable if not _inside(baseline, [directory])]
    if not unknown:
        return baseline
    return baseline | _inside(snapshot, unknown)


def _inside(snapshot: Snapshot, directories: Iterable[str]) -> Snapshot:
    """Return the entries of `snapshot` that are under any of `directories`."""
    prefixes = tuple(directory + os.sep for directory in directories)
    return {path: state for path, state in snapshot.items() if path.startswith(prefixes)}


def _degrade(watcher: _Watcher, root: _Root, error: str) -> None:
    """Drop a root's baseline without events, and log when the problem first appears."""
    # TooManyEntries degrades the trigger like a missing root, so the scan after it takes a
    # silent baseline too.
    if root.error != error:
        _log.warning("watch %s: %s", watcher.trigger.key, error)
    root.error = error
    root.baseline, root.identity, root.unreadable = None, None, frozenset()


def _scan_root(
    path: Path, *, recursive: bool, matcher: GlobMatcher, max_entries: int
) -> _Scan | None:
    """Scan a root, or return None when it became a different directory during the scan.

    The root's identity is read before and after the scan, in the same blocking call, so a
    scan that overlaps an unmount or a re-pointed symlink is never diffed. Raises
    RootUnavailable or TooManyEntries.
    """
    before = _identity(path)
    unreadable: list[str] = []
    snapshot = scan(
        path, recursive=recursive, matcher=matcher, max_entries=max_entries, skipped=unreadable
    )
    if before is None or _identity(path) != before:
        return None
    return _Scan(before, snapshot, frozenset(unreadable))


def _identity(path: Path) -> _Identity | None:
    """Return the directory that `path` names, following a symlink, or None on an error."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _watches_of(workflow: Workflow) -> dict[str, tuple[int, WatchTrigger]]:
    """Return a workflow's watches by key, with their positions among its triggers."""
    return {
        trigger.key: (index, trigger)
        for index, trigger in enumerate(workflow.triggers)
        if isinstance(trigger, WatchTrigger)
    }


def _watch_state(workflow_id: str, trigger: WatchTrigger, error: str | None) -> TriggerState:
    """Return a watch's state: a summary such as "watch ~/Downloads", and its health."""
    summary = "watch " + ", ".join(_display(path) for path in trigger.paths)
    return TriggerState(workflow_id, trigger.key, TriggerKind.WATCH, summary, None, error)


def _display(path: Path) -> str:
    """Show a path under the home directory with a leading "~"."""
    try:
        inside = path.relative_to(os.path.expanduser("~"))
    except ValueError:
        return str(path)
    return "~" if inside == Path() else f"~/{inside}"
