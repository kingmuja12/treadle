from __future__ import annotations

import dataclasses
import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from tests.support.builders import make_runtime, static_template

from treadle.errors import PathNotAllowedError, StepConfigError, StepError
from treadle.schema import validate
from treadle.steps.base import StepResult, StepRuntime
from treadle.steps.file import FileStep, resolve_path
from treadle.types import Issue, JSONValue, Severity


@pytest.fixture
def rt(tmp_path: Path) -> StepRuntime:
    return make_runtime(tmp_path)


@pytest.fixture
def project(rt: StepRuntime) -> Path:
    return rt.project_dir


@pytest.fixture
def umask() -> Iterator[None]:
    previous = os.umask(0o022)
    yield
    os.umask(previous)


async def perform(rt: StepRuntime, **raw: object) -> StepResult:
    """Validate and check the parameters as the executor does, then run the step."""
    value, issues = validate(FileStep().schema, raw)
    assert issues == []
    params = cast(dict[str, JSONValue], value)
    assert FileStep().check(params, ()) == []
    return await FileStep().run(params, rt)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def entries(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


async def test_write_creates_a_file_relative_to_the_project(rt: StepRuntime, project: Path) -> None:
    result = await perform(rt, action="write", path="notes/today.txt", content="héllo\n")

    target = project / "notes" / "today.txt"
    assert result.outputs == {"path": str(target), "bytes": 7, "changed": True}
    assert target.read_text(encoding="utf-8") == "héllo\n"


async def test_write_defaults_to_an_empty_file(rt: StepRuntime, project: Path) -> None:
    result = await perform(rt, action="write", path="empty")

    assert (project / "empty").read_bytes() == b""
    assert result.outputs["bytes"] == 0


async def test_write_replaces_the_file_atomically(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = project / "data.txt"
    target.write_text("old")
    before = target.stat().st_ino
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        synced.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)

    await perform(rt, action="write", path="data.txt", content="new", overwrite=True)

    assert target.read_text() == "new"
    assert target.stat().st_ino != before
    assert len(synced) == 1
    assert "data.txt" in entries(project)
    assert [name for name in entries(project) if name.endswith(".tmp")] == []


async def test_a_failed_write_keeps_the_original(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = project / "data.txt"
    target.write_text("old")

    def full_disk(source: object, destination: object) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(os, "replace", full_disk)

    with pytest.raises(StepError) as caught:
        await perform(rt, action="write", path="data.txt", content="new", overwrite=True)

    assert caught.value.message == f"cannot write {target}: {os.strerror(errno.ENOSPC)}"
    assert caught.value.retryable is True
    assert target.read_text() == "old"
    assert [name for name in entries(project) if name.endswith(".tmp")] == []


async def test_write_refuses_an_existing_file_without_overwrite(
    rt: StepRuntime, project: Path
) -> None:
    (project / "keep.txt").write_text("old")

    with pytest.raises(StepConfigError) as caught:
        await perform(rt, action="write", path="keep.txt", content="new")

    target = project / "keep.txt"
    assert caught.value.message == f"{target} already exists; set overwrite: true to replace it"
    assert caught.value.retryable is False
    assert target.read_text() == "old"


@pytest.mark.usefixtures("umask")
async def test_write_modes(rt: StepRuntime, project: Path) -> None:
    await perform(rt, action="write", path="default.txt")
    await perform(rt, action="write", path="private.txt", mode="0600")
    await perform(rt, action="write", path="script.sh", mode="755")
    (project / "shared.txt").write_text("old")
    (project / "shared.txt").chmod(0o640)
    await perform(rt, action="write", path="shared.txt", content="new", overwrite=True)

    assert mode_of(project / "default.txt") == 0o644
    assert mode_of(project / "private.txt") == 0o600
    assert mode_of(project / "script.sh") == 0o755
    assert mode_of(project / "shared.txt") == 0o640


@pytest.mark.usefixtures("umask")
async def test_a_written_file_never_exists_with_broader_permissions(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes_at_creation: list[int] = []
    real_open = os.open

    def open_and_record(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        modes_at_creation.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor

    monkeypatch.setattr(os, "open", open_and_record)
    (project / "group.txt").write_text("old")
    (project / "group.txt").chmod(0o640)

    await perform(rt, action="write", path="secret.txt", content="s3cret", mode="0600")
    await perform(rt, action="write", path="group.txt", content="new", overwrite=True)
    await perform(rt, action="write", path="plain.txt", content="hi")

    assert modes_at_creation == [0o600, 0o600, 0o644]
    final = [mode_of(project / name) for name in ("secret.txt", "group.txt", "plain.txt")]
    assert final == [0o600, 0o640, 0o644]


async def test_parents_are_created_unless_disabled(rt: StepRuntime, project: Path) -> None:
    await perform(rt, action="write", path="a/b/c.txt", content="x")

    with pytest.raises(StepError) as caught:
        await perform(rt, action="write", path="missing/c.txt", parents=False)

    assert (project / "a" / "b" / "c.txt").read_text() == "x"
    assert caught.value.message == (
        f"cannot write {project / 'missing' / 'c.txt'}: {os.strerror(errno.ENOENT)}"
    )
    assert caught.value.retryable is True


async def test_append_creates_and_extends_a_file(rt: StepRuntime, project: Path) -> None:
    first = await perform(rt, action="append", path="logs/run.log", content="one\n")
    second = await perform(rt, action="append", path="logs/run.log", content="twö\n")

    assert (project / "logs" / "run.log").read_text(encoding="utf-8") == "one\ntwö\n"
    assert (first.outputs["bytes"], second.outputs["bytes"]) == (4, 5)
    assert second.outputs["changed"] is True


async def test_copy_a_file_with_its_mode(rt: StepRuntime, project: Path) -> None:
    source = project / "src.sh"
    source.write_text("echo hi")
    source.chmod(0o750)

    result = await perform(rt, action="copy", source="src.sh", path="out/dst.sh")

    target = project / "out" / "dst.sh"
    assert result.outputs == {"path": str(target), "bytes": 0, "changed": True}
    assert target.read_text() == "echo hi"
    assert mode_of(target) == 0o750
    assert source.exists()


async def test_copy_refuses_an_existing_destination_unless_overwrite(
    rt: StepRuntime, project: Path
) -> None:
    (project / "src.txt").write_text("new")
    (project / "dst.txt").write_text("old")

    with pytest.raises(StepConfigError, match="already exists"):
        await perform(rt, action="copy", source="src.txt", path="dst.txt")
    assert (project / "dst.txt").read_text() == "old"

    await perform(rt, action="copy", source="src.txt", path="dst.txt", overwrite=True)
    assert (project / "dst.txt").read_text() == "new"


async def test_copy_a_directory_needs_recursive(rt: StepRuntime, project: Path) -> None:
    (project / "tree").mkdir()

    with pytest.raises(StepConfigError) as caught:
        await perform(rt, action="copy", source="tree", path="copy")

    assert (
        caught.value.message == f"{project / 'tree'} is a directory; set recursive: true to copy it"
    )


async def test_a_recursive_copy_keeps_symlinks_as_links(rt: StepRuntime, project: Path) -> None:
    tree = project / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "file.txt").write_text("deep")
    (tree / "link").symlink_to("sub/file.txt")

    await perform(rt, action="copy", source="tree", path="copy", recursive=True)

    copy = project / "copy"
    assert (copy / "sub" / "file.txt").read_text() == "deep"
    assert os.readlink(copy / "link") == "sub/file.txt"


async def test_an_overwriting_directory_copy_merges(rt: StepRuntime, project: Path) -> None:
    (project / "tree").mkdir()
    (project / "tree" / "a.txt").write_text("new a")
    (project / "copy").mkdir()
    (project / "copy" / "a.txt").write_text("old a")
    (project / "copy" / "b.txt").write_text("old b")

    await perform(rt, action="copy", source="tree", path="copy", recursive=True, overwrite=True)

    assert (project / "copy" / "a.txt").read_text() == "new a"
    assert (project / "copy" / "b.txt").read_text() == "old b"


async def test_copy_refuses_a_directory_into_itself(rt: StepRuntime, project: Path) -> None:
    (project / "tree").mkdir()

    with pytest.raises(StepConfigError, match="into itself"):
        await perform(rt, action="copy", source="tree", path="tree/inner", recursive=True)

    assert entries(project / "tree") == []


async def test_copy_reports_a_missing_source(rt: StepRuntime, project: Path) -> None:
    with pytest.raises(StepError) as caught:
        await perform(rt, action="copy", source="nope.txt", path="dst.txt")

    assert caught.value.message == f"source {project / 'nope.txt'} doesn't exist"
    assert caught.value.retryable is True


async def test_copy_may_read_from_the_state_directory(rt: StepRuntime, project: Path) -> None:
    (rt.state_dir / "note.txt").write_text("state")

    await perform(rt, action="copy", source=".treadle/note.txt", path="backup.txt")

    assert (project / "backup.txt").read_text() == "state"


async def test_move_a_file(rt: StepRuntime, project: Path) -> None:
    (project / "a.txt").write_text("moving")

    result = await perform(rt, action="move", source="a.txt", path="dir/b.txt")

    assert not (project / "a.txt").exists()
    assert (project / "dir" / "b.txt").read_text() == "moving"
    assert result.outputs == {"path": str(project / "dir" / "b.txt"), "bytes": 0, "changed": True}


async def test_move_refuses_an_existing_destination_unless_overwrite(
    rt: StepRuntime, project: Path
) -> None:
    (project / "a.txt").write_text("new")
    (project / "b.txt").write_text("old")

    with pytest.raises(StepConfigError, match="set overwrite: true"):
        await perform(rt, action="move", source="a.txt", path="b.txt")

    await perform(rt, action="move", source="a.txt", path="b.txt", overwrite=True)
    assert (project / "b.txt").read_text() == "new"
    assert not (project / "a.txt").exists()


async def test_move_a_directory_without_recursive(rt: StepRuntime, project: Path) -> None:
    (project / "tree").mkdir()
    (project / "tree" / "f").write_text("x")

    await perform(rt, action="move", source="tree", path="moved")

    assert (project / "moved" / "f").read_text() == "x"
    assert not (project / "tree").exists()


async def test_move_a_symlink_moves_the_link(rt: StepRuntime, project: Path) -> None:
    (project / "target.txt").write_text("kept")
    (project / "link").symlink_to("target.txt")

    await perform(rt, action="move", source="link", path="renamed")

    assert os.readlink(project / "renamed") == "target.txt"
    assert not (project / "link").is_symlink()
    assert (project / "target.txt").read_text() == "kept"


def on_another_device(monkeypatch: pytest.MonkeyPatch, *sources: Path) -> None:
    """Make `os.replace` fail with EXDEV for `sources`, as if they were on another device."""
    real_replace = os.replace

    def replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        if Path(source) in sources:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)


async def test_move_across_filesystems_copies_then_deletes(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "tree" / "sub").mkdir(parents=True)
    (project / "tree" / "sub" / "f").write_text("x")
    (project / "tree" / "link").symlink_to("sub/f")
    (project / "file.txt").write_text("y")
    (project / "old.txt").write_text("old")
    on_another_device(monkeypatch, project / "tree", project / "file.txt")

    await perform(rt, action="move", source="tree", path="moved")
    await perform(rt, action="move", source="file.txt", path="old.txt", overwrite=True)

    assert (project / "moved" / "sub" / "f").read_text() == "x"
    assert os.readlink(project / "moved" / "link") == "sub/f"
    assert (project / "old.txt").read_text() == "y"
    assert entries(project) == [".treadle", "moved", "old.txt"]


async def test_a_move_across_filesystems_never_merges_into_a_directory(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "tree").mkdir()
    (project / "tree" / "new.txt").write_text("new")
    (project / "full").mkdir()
    (project / "full" / "keep.txt").write_text("keep")
    on_another_device(monkeypatch, project / "tree")

    with pytest.raises(StepError) as caught:
        await perform(rt, action="move", source="tree", path="full", overwrite=True)

    assert caught.value.message.startswith(f"cannot move {project / 'full'}: ")
    assert entries(project / "full") == ["keep.txt"]
    assert (project / "tree" / "new.txt").read_text() == "new"
    assert entries(project) == [".treadle", "full", "tree"]


async def test_other_move_errors_fail_the_attempt(
    rt: StepRuntime, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(source: object, destination: object) -> None:
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    (project / "a.txt").write_text("x")
    monkeypatch.setattr(os, "replace", denied)

    with pytest.raises(StepError) as caught:
        await perform(rt, action="move", source="a.txt", path="b.txt")

    assert caught.value.message == f"cannot move {project / 'b.txt'}: {os.strerror(errno.EACCES)}"
    assert (project / "a.txt").exists()


async def test_delete_a_file_and_a_tree(rt: StepRuntime, project: Path) -> None:
    (project / "f.txt").write_text("x")
    (project / "tree" / "sub").mkdir(parents=True)
    (project / "tree" / "sub" / "g").write_text("y")

    first = await perform(rt, action="delete", path="f.txt")
    with pytest.raises(StepConfigError) as caught:
        await perform(rt, action="delete", path="tree")
    await perform(rt, action="delete", path="tree", recursive=True)

    assert first.outputs == {"path": str(project / "f.txt"), "bytes": 0, "changed": True}
    assert (
        caught.value.message
        == f"{project / 'tree'} is a directory; set recursive: true to delete it"
    )
    assert entries(project) == [".treadle"]


async def test_delete_a_missing_path(rt: StepRuntime, project: Path) -> None:
    with pytest.raises(StepError) as caught:
        await perform(rt, action="delete", path="ghost")
    result = await perform(rt, action="delete", path="ghost", missing_ok=True)

    assert caught.value.message == f"cannot delete {project / 'ghost'}: {os.strerror(errno.ENOENT)}"
    assert result.outputs == {"path": str(project / "ghost"), "bytes": 0, "changed": False}


async def test_delete_removes_a_symlink_without_following_it(
    rt: StepRuntime, project: Path
) -> None:
    (project / "dir").mkdir()
    (project / "dir" / "keep.txt").write_text("kept")
    (project / "file.txt").write_text("kept")
    (project / "dir-link").symlink_to("dir")
    (project / "file-link").symlink_to("file.txt")
    (project / "dangling").symlink_to("nowhere")

    await perform(rt, action="delete", path="dir-link", recursive=True)
    await perform(rt, action="delete", path="file-link")
    await perform(rt, action="delete", path="dangling")

    assert entries(project) == [".treadle", "dir", "file.txt"]
    assert (project / "dir" / "keep.txt").read_text() == "kept"


async def test_mkdir(rt: StepRuntime, project: Path) -> None:
    created = await perform(rt, action="mkdir", path="a/b", mode="0700")
    existing = await perform(rt, action="mkdir", path="a/b", mode="0755")

    assert created.outputs == {"path": str(project / "a" / "b"), "bytes": 0, "changed": True}
    assert existing.outputs["changed"] is False
    assert mode_of(project / "a" / "b") == 0o700


@pytest.mark.usefixtures("umask")
async def test_mkdir_without_a_mode_follows_the_umask(rt: StepRuntime, project: Path) -> None:
    result = await perform(rt, action="mkdir", path="plain")

    assert result.outputs["changed"] is True
    assert mode_of(project / "plain") == 0o755


async def test_mkdir_failures(rt: StepRuntime, project: Path) -> None:
    (project / "file").write_text("x")

    with pytest.raises(StepError) as missing:
        await perform(rt, action="mkdir", path="missing/dir", parents=False)
    with pytest.raises(StepError) as clash:
        await perform(rt, action="mkdir", path="file")

    parent_error = os.strerror(errno.ENOENT)
    assert missing.value.message == f"cannot mkdir {project / 'missing' / 'dir'}: {parent_error}"
    assert clash.value.message == f"cannot mkdir {project / 'file'}: {os.strerror(errno.EEXIST)}"


async def test_paths_outside_the_file_roots_are_refused(
    rt: StepRuntime, project: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(PathNotAllowedError) as caught:
        await perform(rt, action="write", path=str(outside / "x.txt"))

    assert caught.value.message == (
        f"{outside / 'x.txt'} is outside the file roots; to allow it, add a directory that "
        "contains it to [security] file_roots in treadle.toml"
    )
    assert caught.value.retryable is False
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="write", path="../outside/x.txt")
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="delete", path="sub/../..", recursive=True)
    assert entries(outside) == []


async def test_symlinks_cant_escape_the_file_roots(
    rt: StepRuntime, project: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (project / "door").symlink_to(outside)
    (project / "secret-link").symlink_to(outside / "secret.txt")

    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="write", path="door/new.txt")
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="write", path="secret-link", content="x", overwrite=True)
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="copy", source="secret-link", path="stolen.txt")
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="delete", path="door/secret.txt")

    assert (outside / "secret.txt").read_text() == "secret"
    assert entries(outside) == ["secret.txt"]


async def test_tilde_expands_to_the_home_directory(
    rt: StepRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    rt = dataclasses.replace(rt, file_roots=(*rt.file_roots, home))

    result = await perform(rt, action="write", path="~/notes.txt", content="hi")

    assert result.outputs["path"] == str(home / "notes.txt")
    assert (home / "notes.txt").read_text() == "hi"


async def test_the_state_directory_is_never_modified(rt: StepRuntime, project: Path) -> None:
    (project / "a.txt").write_text("x")

    with pytest.raises(PathNotAllowedError, match="it is in the state directory"):
        await perform(rt, action="write", path=".treadle/evil.txt")
    with pytest.raises(PathNotAllowedError, match="state directory"):
        await perform(rt, action="move", source="a.txt", path=".treadle/a.txt")
    with pytest.raises(PathNotAllowedError, match="state directory"):
        await perform(rt, action="delete", path=".treadle/tmp", recursive=True)

    assert (rt.state_dir / "tmp").is_dir()


async def test_protected_paths_and_their_parents_are_never_deleted_or_moved(
    rt: StepRuntime, tmp_path: Path
) -> None:
    project = tmp_path / "outer" / "project"
    workflows = project / "workflows"
    workflows.mkdir(parents=True)
    rt = dataclasses.replace(
        rt,
        project_dir=project,
        file_roots=(tmp_path,),
        protected_paths=(rt.state_dir, workflows, project, tmp_path),
    )

    with pytest.raises(PathNotAllowedError) as deleted:
        await perform(rt, action="delete", path=str(tmp_path / "outer"), recursive=True)
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="delete", path=".", recursive=True)
    with pytest.raises(PathNotAllowedError) as moved:
        await perform(rt, action="move", source="workflows", path="old-workflows")
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="delete", path=str(tmp_path), recursive=True)
    with pytest.raises(PathNotAllowedError):
        await perform(rt, action="delete", path="ghost-parent/..", missing_ok=True)

    assert deleted.value.message == (
        f"refusing to delete {tmp_path / 'outer'}: it is or contains the protected path {workflows}"
    )
    assert moved.value.message == (
        f"refusing to move {workflows}: it is or contains the protected path {workflows}"
    )
    assert workflows.is_dir()


async def test_protected_directories_can_still_receive_files(
    rt: StepRuntime, project: Path
) -> None:
    created = await perform(rt, action="mkdir", path=".")
    written = await perform(rt, action="write", path="notes.txt", content="x")
    deleted = await perform(rt, action="delete", path="notes.txt")

    assert created.outputs == {"path": str(project), "bytes": 0, "changed": False}
    assert (written.outputs["changed"], deleted.outputs["changed"]) == (True, True)


async def test_a_directory_copy_never_merges_into_the_state_directory(
    rt: StepRuntime, project: Path
) -> None:
    (project / "snapshot" / ".treadle").mkdir(parents=True)
    (project / "snapshot" / ".treadle" / "treadle.db").write_text("stale")

    with pytest.raises(PathNotAllowedError) as caught:
        await perform(
            rt, action="copy", source="snapshot", path=".", recursive=True, overwrite=True
        )

    assert caught.value.message == (
        f"refusing to copy into {project}: it contains the state directory"
    )
    assert not (rt.state_dir / "treadle.db").exists()


def test_resolve_path_follows_the_final_symlink_only_when_asked(
    rt: StepRuntime, project: Path
) -> None:
    (project / "real.txt").write_text("x")
    (project / "link").symlink_to("real.txt")

    assert resolve_path("link", rt, modify=True) == project / "real.txt"
    assert resolve_path("link", rt, modify=True, follow_final=False) == project / "link"
    assert resolve_path(".treadle/x", rt, modify=False) == rt.state_dir / "x"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"action": "write", "path": "x", "content": "y"}, [], id="valid"),
        pytest.param(
            {"action": "delete", "path": "x", "content": "y", "mode": "0644"},
            [
                ("'content' doesn't apply to action 'delete'", ("with", "content")),
                ("'mode' doesn't apply to action 'delete'", ("with", "mode")),
            ],
            id="inapplicable-keys",
        ),
        pytest.param(
            {"action": "delete", "path": "x", "parents": True},
            [("'parents' doesn't apply to action 'delete'", ("with", "parents"))],
            id="parents-on-delete",
        ),
        pytest.param(
            {"action": "copy", "path": "x"},
            [("action 'copy' needs 'source'", ("with",))],
            id="missing-source",
        ),
        pytest.param(
            {"action": "write", "path": "x", "source": "y", "recursive": True},
            [
                ("'source' doesn't apply to action 'write'", ("with", "source")),
                ("'recursive' doesn't apply to action 'write'", ("with", "recursive")),
            ],
            id="copy-keys-on-write",
        ),
        pytest.param({"action": static_template("a"), "content": "y"}, [], id="templated-action"),
        pytest.param({"action": "remove", "content": "y"}, [], id="invalid-action"),
    ],
)
def test_check_reports_keys_that_dont_apply(
    params: dict[str, object], expected: list[tuple[str, tuple[str, ...]]]
) -> None:
    issues = FileStep().check(params, ("with",))

    assert issues == [Issue(Severity.ERROR, "E407", message, path) for message, path in expected]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"action": "delet", "path": "x"},
            ("E104", "must be one of: write, append, copy, move, delete, mkdir"),
            id="action",
        ),
        pytest.param(
            {"action": "write", "path": ""}, ("E104", "must not be empty"), id="empty-path"
        ),
        pytest.param(
            {"action": "write"}, ("E102", "missing required key 'path'"), id="missing-path"
        ),
        pytest.param(
            {"action": "write", "path": "x", "mode": "999"},
            ("E104", "must match the pattern [0-7]{3,4}"),
            id="mode",
        ),
        pytest.param(
            {"action": "write", "path": "x", "mode": 644},
            ("E103", "expected a string, got a number"),
            id="mode-type",
        ),
    ],
)
def test_the_schema_rejects_bad_parameters(
    raw: dict[str, object], expected: tuple[str, str]
) -> None:
    _, issues = validate(FileStep().schema, raw)

    assert [(issue.code, issue.message) for issue in issues] == [expected]


def test_the_step_type_attributes() -> None:
    step = FileStep()

    assert step.name == "file"
    assert step.output_names == step.reserved_outputs == {"path", "bytes", "changed"}
    assert step.taint_sinks == {"path": "path", "source": "path"}
