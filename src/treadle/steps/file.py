"""The `file` step type: write, append, copy, move, delete, or mkdir inside `file_roots`."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Final, Literal, TypeAlias, cast

from treadle.errors import PathNotAllowedError, StepConfigError, StepError
from treadle.schema import Bool, Field, Obj, Str
from treadle.steps.base import StepResult, StepRuntime, check_os_text
from treadle.steps.shell import expand_home
from treadle.types import Issue, JSONValue, Severity, YamlPath

_Params: TypeAlias = Mapping[str, JSONValue]
_ACTIONS: Final = ("write", "append", "copy", "move", "delete", "mkdir")
_APPLIES: Final[Mapping[str, tuple[str, ...]]] = {
    "source": ("copy", "move"),
    "content": ("write", "append"),
    "overwrite": ("write", "copy", "move"),
    "parents": ("write", "append", "copy", "move", "mkdir"),
    "recursive": ("copy", "delete"),
    "missing_ok": ("delete",),
    "mode": ("write", "mkdir"),
}
_OUTPUTS: Final = frozenset({"path", "bytes", "changed"})
# The optional keys have no schema defaults, so `check` can tell which keys a workflow set;
# each action applies the documented defaults itself.
_SCHEMA: Final = Obj(
    "file step",
    {
        "action": Field(Str(choices=_ACTIONS), required=True),
        "path": Field(Str(min_len=1), required=True),
        "source": Field(Str(min_len=1)),
        "content": Field(Str()),
        "overwrite": Field(Bool()),
        "parents": Field(Bool()),
        "recursive": Field(Bool()),
        "missing_ok": Field(Bool()),
        "mode": Field(Str(pattern="[0-7]{3,4}")),
    },
)


class FileStep:
    """Writes, appends, copies, moves, deletes, or creates paths inside the file roots.

    Outputs are `path` (the resolved target), `bytes`, and `changed`.
    """

    name = "file"
    # A thread can't be interrupted, so a timed-out attempt isn't retried; see StepType.
    interruptible: ClassVar[bool] = False
    output_names: ClassVar[frozenset[str] | None] = _OUTPUTS
    reserved_outputs: ClassVar[frozenset[str]] = _OUTPUTS
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {
        "path": "path",
        "source": "path",
    }

    @property
    def schema(self) -> Obj:
        """The `with` keys of a `file` step, such as `action`, `path`, and `source`."""
        return _SCHEMA

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return E407 issues for keys that don't apply to the action, or a missing `source`.

        A templated or invalid `action` yields no issues: the executor checks the rendered
        parameters again, and the schema reports an invalid action.
        """
        action = params.get("action")
        if action not in _ACTIONS:
            return []
        issues = [
            _invalid(f"'{key}' doesn't apply to action '{action}'", (*path, key))
            for key, actions in _APPLIES.items()
            if key in params and action not in actions
        ]
        if action in ("copy", "move") and "source" not in params:
            issues.append(_invalid(f"action '{action}' needs 'source'", path))
        return issues

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Perform the action in a worker thread and return `path`, `bytes`, and `changed`.

        A thread can't be interrupted, so a cancelled step waits for the operation to end,
        logs whether it finished, and re-raises; a retry never overlaps it. A second
        cancellation stops the wait, and the operation's outcome is logged when it ends.
        Path violations raise PathNotAllowedError. An existing destination without
        `overwrite`, or a directory without `recursive`, raises StepConfigError. Other OS
        errors raise a retryable StepError.
        """
        work = asyncio.create_task(asyncio.to_thread(_perform, params, rt))
        # asyncio.wait never cancels `work`. From Python 3.14 on, asyncio.shield would also
        # report an error that `work` raises after a cancellation, even one logged here.
        try:
            await asyncio.wait([work])
        except asyncio.CancelledError:
            try:
                await asyncio.wait([work])
            except asyncio.CancelledError:
                # Logging the outcome also retrieves the error, so the event loop never
                # reports it as unretrieved.
                work.add_done_callback(functools.partial(_log_late_outcome, rt))
                raise
            _log_late_outcome(rt, work)
            raise
        return work.result()


def _log_late_outcome(rt: StepRuntime, work: asyncio.Task[StepResult]) -> None:
    """Log how a file operation that outlived its step's cancellation ended.

    A task that a closing event loop cancelled has no outcome to log.
    """
    if not work.cancelled():
        rt.log.system(_late_outcome(work))


def _late_outcome(work: asyncio.Task[StepResult]) -> str:
    """Describe how a file operation that outlived its step's cancellation ended."""
    error = work.exception()
    if error is None:
        return "the file operation finished after the step was cancelled"
    message = error.message if isinstance(error, StepError) else str(error)
    return f"the file operation failed after the step was cancelled: {message}"


def resolve_path(
    raw: str, rt: StepRuntime, *, modify: bool, follow_final: bool = True, key: str = "path"
) -> Path:
    """Expand and resolve a `file` step path, and check it against the file roots.

    `~` expands, and a relative path resolves against the project directory. Symlinks
    resolve, except a final one when `follow_final` is False. The result must be inside a
    file root and, with `modify`, outside the state directory. Raises PathNotAllowedError,
    or StepConfigError naming `key` for a path that the operating system can't take or
    whose "~user" has no known home directory.
    """
    check_os_text(raw, key)
    path = rt.project_dir / expand_home(raw, key)
    if follow_final or path.name in ("", ".."):
        resolved = _real(path)
    else:
        resolved = _real(path.parent) / path.name
    if not any(_within(resolved, _real(root)) for root in rt.file_roots):
        raise PathNotAllowedError(
            f"{resolved} is outside the file roots; to allow it, add a directory that "
            "contains it to [security] file_roots in treadle.toml"
        )
    state = _real(rt.state_dir)
    if modify and _within(resolved, state):
        message = f"refusing to modify {resolved}: it is in the state directory {state}"
        raise PathNotAllowedError(message)
    return resolved


def _perform(params: _Params, rt: StepRuntime) -> StepResult:
    action = cast(str, params["action"])
    raw = cast(str, params["path"])
    target = resolve_path(raw, rt, modify=True, follow_final=action != "delete")
    try:
        written, changed = _HANDLERS[action](target, params, rt)
    except OSError as exc:
        raise StepError(f"cannot {action} {target}: {exc.strerror or exc}") from exc
    return StepResult({"path": str(target), "bytes": written, "changed": changed})


def _write(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    data = _content(params)
    _refuse_existing(target, params)
    _make_parents(target, params)
    _atomic_write(target, data, _mode(params))
    return len(data), True


def _append(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    data = _content(params)
    _make_parents(target, params)
    with target.open("ab") as file:
        file.write(data)
    return len(data), True


def _copy(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    source = _source(params, rt, move=False)
    if source.is_dir():
        if params.get("recursive") is not True:
            raise StepConfigError(f"{source} is a directory; set recursive: true to copy it")
        if _within(target, source):
            raise StepConfigError(f"can't copy {source} into itself")
        if _within(_real(rt.state_dir), target):
            message = f"refusing to copy into {target}: it contains the state directory"
            raise PathNotAllowedError(message)
    _refuse_existing(target, params)
    if source.is_dir() and not source.is_symlink() and target.is_dir():
        _refuse_escaping_links(source, target, rt)
    _make_parents(target, params)
    _duplicate(source, target)
    return 0, True


def _move(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    source = _source(params, rt, move=True)
    _refuse_existing(target, params)
    _make_parents(target, params)
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _move_across(source, target)
    return 0, True


def _delete(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    _refuse_removal(target, rt, "delete")
    if params.get("missing_ok") is True and not os.path.lexists(target):
        return 0, False
    if target.is_dir() and not target.is_symlink() and params.get("recursive") is not True:
        raise StepConfigError(f"{target} is a directory; set recursive: true to delete it")
    _remove(target)
    return 0, True


def _mkdir(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    if target.is_dir():
        return 0, False
    target.mkdir(parents=params.get("parents", True) is True)
    mode = _mode(params)
    if mode is not None:
        target.chmod(mode)
    return 0, True


_HANDLERS: Final[Mapping[str, Callable[[Path, _Params, StepRuntime], tuple[int, bool]]]] = {
    "write": _write,
    "append": _append,
    "copy": _copy,
    "move": _move,
    "delete": _delete,
    "mkdir": _mkdir,
}


def _source(params: _Params, rt: StepRuntime, *, move: bool) -> Path:
    """Resolve `source`. A move modifies its source, and a moved symlink isn't followed."""
    raw = cast(str, params["source"])
    source = resolve_path(raw, rt, modify=move, follow_final=not move, key="source")
    if move:
        _refuse_removal(source, rt, "move")
    if not os.path.lexists(source):
        raise StepError(f"source {source} doesn't exist")
    return source


def _refuse_removal(path: Path, rt: StepRuntime, action: str) -> None:
    """Refuse to delete or move a protected path, or a directory that holds one.

    Protected paths are the state, workflows, and project directories and each file root.
    `path`'s final component isn't followed, so a symlink to a protected path may go.
    """
    # Deleting or moving a parent takes the protected paths along, so a parent of a protected
    # path is refused as well.
    for protected in map(_real, rt.protected_paths):
        if _within(protected, path, follow_ancestor=False):
            message = (
                f"refusing to {action} {path}: it is or contains the protected path {protected}"
            )
            raise PathNotAllowedError(message)


def _content(params: _Params) -> bytes:
    """Return `content` as UTF-8. Raises StepConfigError for text that isn't valid Unicode."""
    try:
        return cast(str, params.get("content", "")).encode()
    except UnicodeEncodeError:
        message = "content holds text that isn't valid Unicode, which can't be written as UTF-8"
        raise StepConfigError(message) from None


def _refuse_escaping_links(source: Path, target: Path, rt: StepRuntime) -> None:
    """Refuse a merging copy that would write through a link out of the allowed paths.

    Where the source has a file or a directory, the copy follows a symlink that the
    destination holds at the same place. Each such link must lead inside a file root and
    outside the state directory, so it's checked before anything is copied.
    """
    roots = [_real(root) for root in rt.file_roots]
    state = _real(rt.state_dir)
    pending = [Path()]
    while pending:
        relative = pending.pop()
        with os.scandir(source / relative) as children:
            for child in children:
                if child.is_symlink():
                    continue  # copied as a link, never followed
                destination = target / relative / child.name
                if destination.is_symlink():
                    _check_link(destination, roots, state)
                if child.is_dir(follow_symlinks=False):
                    pending.append(relative / child.name)


def _check_link(link: Path, roots: list[Path], state: Path) -> None:
    """Raise PathNotAllowedError when `link` leads outside the file roots or into `state`."""
    real = _real(link)
    if not any(_within(real, root) for root in roots):
        problem = f"it links to {real}, outside the file roots"
    elif _within(real, state):
        problem = f"it links into the state directory {state}"
    else:
        return
    raise PathNotAllowedError(f"refusing to copy through {link}: {problem}")


def _refuse_existing(target: Path, params: _Params) -> None:
    if params.get("overwrite") is not True and os.path.lexists(target):
        raise StepConfigError(f"{target} already exists; set overwrite: true to replace it")


def _make_parents(target: Path, params: _Params) -> None:
    if params.get("parents", True) is True:
        target.parent.mkdir(parents=True, exist_ok=True)


def _mode(params: _Params) -> int | None:
    mode = params.get("mode")
    return int(mode, 8) if isinstance(mode, str) else None


def _atomic_write(target: Path, data: bytes, mode: int | None) -> None:
    """Write a temporary file beside `target`, fsync it, and rename it over `target`.

    The temporary file gets its final permissions before any data reaches it: `mode`, else
    the permission bits of the file it replaces, else what the umask allows for a new file.
    """
    if mode is None and target.is_file():
        mode = stat.S_IMODE(target.stat().st_mode)
    temp = _temp_path(target)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temp, flags, 0o666 if mode is None else 0o600)
    try:
        with open(descriptor, "wb") as file:
            if mode is not None:
                os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _move_across(source: Path, target: Path) -> None:
    """Move between filesystems: copy beside `target`, rename the copy over it, then delete.

    The rename gives the move the same replace rules as `os.replace` on one filesystem.
    """
    temp = _temp_path(target)
    try:
        _duplicate(source, temp)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            _remove(temp)
        raise
    _remove(source)


def _duplicate(source: Path, target: Path) -> None:
    """Copy a file or a symlink with its metadata, or copy a tree into `target`.

    Symlinks inside a tree are copied as links, never followed.
    """
    # A directory copy with `overwrite` merges into the destination, which keeps every file
    # there that the source doesn't replace.
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copyfile(source, target, follow_symlinks=False)
        shutil.copystat(source, target, follow_symlinks=False)


def _remove(path: Path) -> None:
    """Delete a file or a symlink, or a directory tree without following links inside it."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _temp_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.urandom(4).hex()}.tmp")


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _within(path: Path, ancestor: Path, *, follow_ancestor: bool = True) -> bool:
    """True when `path` is `ancestor` or inside it.

    Both paths are resolved, except that a final symlink may be left unfollowed. They're
    compared as text first, then by file identity (device and inode): `path` and each of its
    parents that exists against `ancestor`, which counts as the link itself when
    `follow_ancestor` is False. Text alone isn't enough, because a file system that ignores
    letter case or Unicode normalization, as APFS does, gives one directory many spellings,
    and `os.path.realpath` keeps the one it was given.
    """
    if path.is_relative_to(ancestor):
        return True
    try:
        info = os.stat(ancestor) if follow_ancestor else os.lstat(ancestor)
    except OSError:
        return False
    identity = (info.st_dev, info.st_ino)
    for candidate in (path, *path.parents):
        try:
            candidate_info = os.lstat(candidate)
        except OSError:
            continue  # not created yet, or not reachable
        if (candidate_info.st_dev, candidate_info.st_ino) == identity:
            return True
    return False


def _invalid(message: str, path: YamlPath) -> Issue:
    return Issue(Severity.ERROR, "E407", message, path)
