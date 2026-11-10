"""Polling snapshots, diffs, and debouncing for watch triggers and hot reload."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from treadle.globs import GlobMatcher
from treadle.models import WatchEvent


@dataclass(frozen=True, slots=True)
class FileState:
    """What a scan records about one regular file."""

    mtime_ns: int
    size: int
    inode: int


@dataclass(frozen=True, slots=True)
class Change:
    """One file change. `path` and `root` are absolute; `rel` uses "/" separators."""

    event: WatchEvent
    path: str
    root: str
    rel: str


Snapshot: TypeAlias = dict[str, FileState]

# How a later event for the same path merges into an earlier one (section 6). None drops the
# path from the batch; any pair not listed keeps the later event.
_MERGED: Final[dict[tuple[WatchEvent, WatchEvent], WatchEvent | None]] = {
    (WatchEvent.CREATED, WatchEvent.MODIFIED): WatchEvent.CREATED,
    (WatchEvent.CREATED, WatchEvent.DELETED): None,
    (WatchEvent.DELETED, WatchEvent.CREATED): WatchEvent.MODIFIED,
}


class RootUnavailable(Exception):
    """The watched root is missing, isn't a directory, or can't be read."""


class TooManyEntries(Exception):
    """A scan found more files than `limits.watch_max_entries`."""


def scan(
    root: Path,
    *,
    recursive: bool,
    matcher: GlobMatcher,
    max_entries: int,
    skipped: list[str] | None = None,
) -> Snapshot:
    """Snapshot the regular files under `root` that `matcher` includes, by absolute path.

    Symlinks are never followed, vanished entries are skipped, and unreadable subdirectories
    are skipped; their paths are appended to `skipped` when it is given. Directories are
    entered only when `recursive` is true and `matcher` doesn't prune them. Raises
    RootUnavailable or TooManyEntries.
    """
    # Design note: `max_entries` counts every directory entry the scan visits, not only the
    # files it keeps, so the limit bounds the cost of each poll as well as its memory.
    snapshot: Snapshot = {}
    visited = 0
    pending = [("", str(root))]
    while pending:
        rel_dir, directory = pending.pop()
        try:
            entries = _list_dir(directory)
        except OSError as exc:
            if not rel_dir:
                raise RootUnavailable(_unavailable(directory, exc)) from exc
            if isinstance(exc, PermissionError) and skipped is not None:
                skipped.append(directory)
            continue
        visited += len(entries)
        if visited > max_entries:
            raise TooManyEntries(f"more than {max_entries} entries in {root}")
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if _is_dir(entry):
                if recursive and not matcher.prune(rel):
                    pending.append((rel, entry.path))
            elif _is_file(entry) and matcher.matches(rel):
                state = _file_state(entry)
                if state is not None:
                    snapshot[entry.path] = state
    return snapshot


def diff(old: Snapshot, new: Snapshot, root: Path) -> list[Change]:
    """Compare two snapshots and return the changes, sorted by path.

    New paths are created, missing ones deleted, and a changed (mtime, size, inode) is
    modified.
    """
    if old == new:
        return []
    base = str(root)
    changes: list[Change] = []
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        if before is None:
            event = WatchEvent.CREATED
        elif after is None:
            event = WatchEvent.DELETED
        else:
            event = WatchEvent.MODIFIED
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        changes.append(Change(event, path, base, rel))
    return changes


class Debouncer:
    """Batches changes until `quiet` seconds pass without one, or `max_wait` since the first.

    Events for one path coalesce by the table in section 6.
    """

    def __init__(self, quiet: float, max_wait: float) -> None:
        self._quiet = quiet
        self._max_wait = max_wait
        self._batch: dict[str, Change] = {}
        self._first = 0.0
        self._last = 0.0

    def add(self, changes: Iterable[Change], now: float) -> None:
        """Add changes observed at monotonic time `now`."""
        for change in changes:
            if not self._batch:
                self._first = now
            self._last = now
            merged = _coalesce(self._batch.get(change.path), change)
            if merged is None:
                del self._batch[change.path]
            else:
                self._batch[change.path] = merged

    def due(self, now: float) -> bool:
        """True when the current batch should fire."""
        deadline = self.deadline()
        return deadline is not None and now >= deadline

    def deadline(self) -> float | None:
        """Return when the current batch becomes due, or None without a batch."""
        if not self._batch:
            return None
        return min(self._last + self._quiet, self._first + self._max_wait)

    def take(self) -> list[Change]:
        """Return the coalesced batch sorted by path, and start a new one."""
        batch = [self._batch[path] for path in sorted(self._batch)]
        self._batch = {}
        return batch


def _coalesce(earlier: Change | None, later: Change) -> Change | None:
    """Merge a later change into the batch's earlier change for the same path."""
    if earlier is None:
        return later
    pair = (earlier.event, later.event)
    if pair not in _MERGED:
        return later
    event = _MERGED[pair]
    return None if event is None else dataclasses.replace(later, event=event)


def _list_dir(directory: str) -> list[os.DirEntry[str]]:
    with os.scandir(directory) as entries:
        return list(entries)


def _is_dir(entry: os.DirEntry[str]) -> bool:
    """True for a real directory; a symlink to one doesn't count."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _is_file(entry: os.DirEntry[str]) -> bool:
    """True for a regular file; a symlink to one doesn't count."""
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _file_state(entry: os.DirEntry[str]) -> FileState | None:
    """Return the entry's state, or None when it vanished since the directory was listed."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return FileState(info.st_mtime_ns, info.st_size, info.st_ino)


def _unavailable(root: str, exc: OSError) -> str:
    """Describe why a root can't be scanned, for the degraded trigger state."""
    if isinstance(exc, FileNotFoundError):
        return f"path missing: {root}"
    if isinstance(exc, NotADirectoryError):
        return f"not a directory: {root}"
    if isinstance(exc, PermissionError):
        return f"permission denied: {root}"
    return f"can't read {root}: {exc.strerror or exc}"
