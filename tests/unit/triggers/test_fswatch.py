from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.unit.triggers.helpers import write

from treadle.fswatch import (
    Change,
    Debouncer,
    FileState,
    RootUnavailable,
    TooManyEntries,
    diff,
    scan,
)
from treadle.globs import GlobMatcher
from treadle.models import WatchEvent

CREATED, MODIFIED, DELETED = WatchEvent.CREATED, WatchEvent.MODIFIED, WatchEvent.DELETED
EVERYTHING = GlobMatcher(["*"], [])
needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads directories regardless of their mode"
)


def names(snapshot: dict[str, FileState], root: Path) -> list[str]:
    return sorted(os.path.relpath(path, root) for path in snapshot)


def change(event: WatchEvent, rel: str, root: str = "/r") -> Change:
    return Change(event, f"{root}/{rel}", root, rel)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    for rel in ("a.pdf", "b.txt", ".hidden", "sub/c.pdf", "sub/deep/d.pdf", ".git/e.pdf"):
        write(root / rel)
    return root


@pytest.fixture
def locked(tree: Path) -> Iterator[Path]:
    """`tree/sub` without read permission, restored afterwards so cleanup works."""
    sub = tree / "sub"
    sub.chmod(0)
    yield sub
    sub.chmod(0o755)


class TestScan:
    def test_without_recursion_only_the_root_files_count(self, tree: Path) -> None:
        snapshot = scan(tree, recursive=False, matcher=EVERYTHING, max_entries=100)
        assert names(snapshot, tree) == [".hidden", "a.pdf", "b.txt"]

    def test_recursion_enters_every_directory_the_matcher_doesnt_prune(self, tree: Path) -> None:
        snapshot = scan(tree, recursive=True, matcher=EVERYTHING, max_entries=100)
        assert names(snapshot, tree) == [
            ".git/e.pdf",
            ".hidden",
            "a.pdf",
            "b.txt",
            "sub/c.pdf",
            "sub/deep/d.pdf",
        ]

    def test_include_and_exclude_patterns_filter_files_and_prune_directories(
        self, tree: Path
    ) -> None:
        matcher = GlobMatcher(["*.pdf"], [".*", "deep"])
        snapshot = scan(tree, recursive=True, matcher=matcher, max_entries=100)
        assert names(snapshot, tree) == ["a.pdf", "sub/c.pdf"]

    def test_a_pattern_with_a_slash_matches_the_relative_path(self, tree: Path) -> None:
        matcher = GlobMatcher(["sub/**/*.pdf"], [])
        snapshot = scan(tree, recursive=True, matcher=matcher, max_entries=100)
        assert names(snapshot, tree) == ["sub/c.pdf", "sub/deep/d.pdf"]

    def test_an_entry_records_mtime_size_and_inode(self, tree: Path) -> None:
        path = tree / "a.pdf"
        os.utime(path, ns=(1_000_000_000, 2_000_000_000))
        snapshot = scan(tree, recursive=False, matcher=EVERYTHING, max_entries=100)
        info = path.stat()
        assert snapshot[str(path)] == FileState(2_000_000_000, 1, info.st_ino)

    def test_symlinks_are_ignored_and_never_followed(self, tree: Path, tmp_path: Path) -> None:
        outside = write(tmp_path / "outside" / "secret.pdf")
        (tree / "link.pdf").symlink_to(outside)
        (tree / "linkdir").symlink_to(outside.parent, target_is_directory=True)
        snapshot = scan(tree, recursive=True, matcher=EVERYTHING, max_entries=100)
        assert names(snapshot, tree) == [
            ".git/e.pdf",
            ".hidden",
            "a.pdf",
            "b.txt",
            "sub/c.pdf",
            "sub/deep/d.pdf",
        ]

    def test_other_file_types_are_ignored(self, tree: Path) -> None:
        os.mkfifo(tree / "pipe")
        snapshot = scan(tree, recursive=False, matcher=EVERYTHING, max_entries=100)
        assert str(tree / "pipe") not in snapshot

    def test_a_missing_root_is_unavailable(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing"
        with pytest.raises(RootUnavailable, match=re.escape(f"path missing: {missing}")):
            scan(missing, recursive=False, matcher=EVERYTHING, max_entries=100)

    def test_a_file_root_is_unavailable(self, tree: Path) -> None:
        with pytest.raises(RootUnavailable, match=r"not a directory: .*a\.pdf"):
            scan(tree / "a.pdf", recursive=False, matcher=EVERYTHING, max_entries=100)

    @needs_permissions
    def test_an_unreadable_root_is_unavailable(self, tree: Path, locked: Path) -> None:
        with pytest.raises(RootUnavailable, match=re.escape(f"permission denied: {locked}")):
            scan(locked, recursive=False, matcher=EVERYTHING, max_entries=100)

    def test_any_other_error_on_the_root_is_unavailable(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(path: str) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(os, "scandir", broken)
        with pytest.raises(RootUnavailable, match=r"can't read .*: Input/output error"):
            scan(tree, recursive=False, matcher=EVERYTHING, max_entries=100)

    @needs_permissions
    def test_an_unreadable_subdirectory_is_skipped_and_reported(
        self, tree: Path, locked: Path
    ) -> None:
        skipped: list[str] = []
        snapshot = scan(tree, recursive=True, matcher=EVERYTHING, max_entries=100, skipped=skipped)
        assert names(snapshot, tree) == [".git/e.pdf", ".hidden", "a.pdf", "b.txt"]
        assert skipped == [str(locked)]

    @needs_permissions
    def test_skipped_directories_are_optional_to_collect(self, tree: Path, locked: Path) -> None:
        snapshot = scan(tree, recursive=True, matcher=EVERYTHING, max_entries=100)
        assert str(locked / "c.pdf") not in snapshot

    def test_entries_that_vanish_during_the_scan_are_skipped(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_scandir = os.scandir

        class VanishingListing:
            """Lists a directory, then deletes a file and a directory before they're read."""

            def __init__(self, path: str) -> None:
                with real_scandir(path) as listing:
                    self.entries = list(listing)
                if path == str(tree):
                    (tree / "b.txt").unlink()
                    shutil.rmtree(tree / "sub")

            def __enter__(self) -> list[os.DirEntry[str]]:
                return self.entries

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(os, "scandir", VanishingListing)
        snapshot = scan(tree, recursive=True, matcher=EVERYTHING, max_entries=100)
        assert names(snapshot, tree) == [".git/e.pdf", ".hidden", "a.pdf"]

    def test_an_entry_whose_type_cant_be_read_is_skipped(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_scandir = os.scandir

        class Unreadable:
            """A directory entry whose type check fails, as `DirEntry` may on some systems."""

            name = "odd"
            path = str(tree / "odd")

            def is_dir(self, *, follow_symlinks: bool) -> bool:
                raise PermissionError(13, "Permission denied")

            def is_file(self, *, follow_symlinks: bool) -> bool:
                raise PermissionError(13, "Permission denied")

        class Listing:
            def __init__(self, path: str) -> None:
                with real_scandir(path) as listing:
                    self.entries = [*listing, Unreadable()]

            def __enter__(self) -> list[object]:
                return self.entries

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(os, "scandir", Listing)
        snapshot = scan(tree, recursive=False, matcher=EVERYTHING, max_entries=100)
        assert names(snapshot, tree) == [".hidden", "a.pdf", "b.txt"]

    def test_more_entries_than_the_limit_is_too_many(self, tree: Path) -> None:
        with pytest.raises(TooManyEntries, match=re.escape(f"more than 4 entries in {tree}")):
            scan(tree, recursive=False, matcher=EVERYTHING, max_entries=4)

    def test_entries_in_subdirectories_count_toward_the_limit(self, tree: Path) -> None:
        with pytest.raises(TooManyEntries):
            scan(tree, recursive=True, matcher=EVERYTHING, max_entries=7)

    def test_excluded_entries_count_toward_the_limit(self, tree: Path) -> None:
        nothing = GlobMatcher(["*.none"], [])
        with pytest.raises(TooManyEntries):
            scan(tree, recursive=False, matcher=nothing, max_entries=4)

    def test_exactly_the_limit_is_allowed(self, tree: Path) -> None:
        snapshot = scan(tree, recursive=False, matcher=EVERYTHING, max_entries=5)
        assert len(snapshot) == 3


class TestDiff:
    root = Path("/r")

    def test_new_missing_and_changed_paths(self) -> None:
        old = {"/r/a": FileState(1, 1, 1), "/r/b": FileState(1, 1, 2), "/r/c": FileState(1, 1, 3)}
        new = {"/r/a": FileState(1, 1, 1), "/r/b": FileState(2, 1, 2), "/r/d/e": FileState(1, 1, 4)}
        assert diff(old, new, self.root) == [
            change(MODIFIED, "b"),
            change(DELETED, "c"),
            change(CREATED, "d/e"),
        ]

    @pytest.mark.parametrize(
        "after",
        [FileState(2, 1, 1), FileState(1, 2, 1), FileState(1, 1, 2)],
        ids=["mtime", "size", "inode"],
    )
    def test_any_changed_field_is_a_modification(self, after: FileState) -> None:
        assert diff({"/r/a": FileState(1, 1, 1)}, {"/r/a": after}, self.root) == [
            change(MODIFIED, "a")
        ]

    def test_a_rename_is_a_delete_and_a_create(self) -> None:
        state = FileState(1, 1, 1)
        assert diff({"/r/old": state}, {"/r/new": state}, self.root) == [
            change(CREATED, "new"),
            change(DELETED, "old"),
        ]

    def test_equal_snapshots_have_no_changes(self) -> None:
        state = {"/r/a": FileState(1, 1, 1)}
        assert diff(state, dict(state), self.root) == []

    def test_changes_are_sorted_by_path(self) -> None:
        new = {f"/r/{name}": FileState(1, 1, 1) for name in ("z", "a", "m/b")}
        assert [c.rel for c in diff({}, new, self.root)] == ["a", "m/b", "z"]


class TestDebouncer:
    def test_a_batch_fires_after_the_quiet_period(self) -> None:
        debouncer = Debouncer(quiet=2.0, max_wait=60.0)
        assert debouncer.deadline() is None
        assert not debouncer.due(100.0)
        debouncer.add([change(CREATED, "a")], 10.0)
        debouncer.add([change(CREATED, "b")], 11.0)
        assert debouncer.deadline() == 13.0
        assert not debouncer.due(12.9)
        assert debouncer.due(13.0)
        assert debouncer.take() == [change(CREATED, "a"), change(CREATED, "b")]
        assert debouncer.deadline() is None

    def test_constant_churn_fires_at_the_maximum_wait(self) -> None:
        debouncer = Debouncer(quiet=5.0, max_wait=60.0)
        for second in range(0, 70, 2):
            debouncer.add([change(MODIFIED, "a")], 100.0 + second)
            if debouncer.due(100.0 + second):
                break
        assert second == 60
        assert debouncer.deadline() == 160.0

    def test_a_zero_quiet_period_fires_at_once(self) -> None:
        debouncer = Debouncer(quiet=0.0, max_wait=60.0)
        debouncer.add([change(CREATED, "a")], 5.0)
        assert debouncer.due(5.0)

    def test_take_starts_a_new_batch(self) -> None:
        debouncer = Debouncer(quiet=1.0, max_wait=60.0)
        debouncer.add([change(CREATED, "a")], 0.0)
        debouncer.take()
        debouncer.add([change(CREATED, "b")], 50.0)
        assert debouncer.deadline() == 51.0
        assert debouncer.take() == [change(CREATED, "b")]

    def test_the_batch_is_sorted_by_path(self) -> None:
        debouncer = Debouncer(quiet=1.0, max_wait=60.0)
        debouncer.add([change(CREATED, "z"), change(CREATED, "a")], 0.0)
        assert [c.rel for c in debouncer.take()] == ["a", "z"]

    @pytest.mark.parametrize(
        ("earlier", "later", "result"),
        [
            (CREATED, MODIFIED, CREATED),
            (CREATED, DELETED, None),
            (DELETED, CREATED, MODIFIED),
            (MODIFIED, MODIFIED, MODIFIED),
            (MODIFIED, DELETED, DELETED),
        ],
    )
    def test_events_for_one_path_coalesce(
        self, earlier: WatchEvent, later: WatchEvent, result: WatchEvent | None
    ) -> None:
        debouncer = Debouncer(quiet=1.0, max_wait=60.0)
        debouncer.add([change(earlier, "a")], 0.0)
        debouncer.add([change(later, "a")], 0.5)
        expected = [] if result is None else [change(result, "a")]
        assert debouncer.take() == expected

    def test_a_batch_that_cancels_out_is_never_due(self) -> None:
        debouncer = Debouncer(quiet=1.0, max_wait=60.0)
        debouncer.add([change(CREATED, "a"), change(DELETED, "a")], 0.0)
        assert debouncer.deadline() is None
        assert not debouncer.due(100.0)

    def test_the_first_change_after_an_empty_batch_restarts_the_maximum_wait(self) -> None:
        debouncer = Debouncer(quiet=1.0, max_wait=10.0)
        debouncer.add([change(CREATED, "a"), change(DELETED, "a")], 0.0)
        debouncer.add([change(CREATED, "b")], 30.0)
        assert debouncer.deadline() == 31.0
