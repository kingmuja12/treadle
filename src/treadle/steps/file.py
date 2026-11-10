"""The `file` step type: write, append, copy, move, delete, or mkdir inside `file_roots`."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Final, Literal, TypeAlias, cast

from treadle.errors import PathNotAllowedError, StepConfigError, StepError
from treadle.schema import Bool, Field, Obj, Str
from treadle.steps.base import StepResult, StepRuntime
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
# Design note: the optional keys have no schema defaults, so `check` can tell which keys a
# workflow set; the actions apply section 2's defaults themselves.
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
    output_names: ClassVar[frozenset[str] | None] = _OUTPUTS
    reserved_outputs: ClassVar[frozenset[str]] = _OUTPUTS
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {
        "path": "path",
        "source": "path",
    }

    @property
    def schema(self) -> Obj:
        """The `with` keys of the `file` table in section 2."""
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

        A thread can't be interrupted, so a cancelled step waits for the operation to end
        before it re-raises, and a retry never overlaps it; a second cancellation stops the
        wait. Path violations raise PathNotAllowedError. An existing destination without
        `overwrite`, or a directory without `recursive`, raises StepConfigError. Other OS
        errors raise a retryable StepError.
        """
        work = asyncio.create_task(asyncio.to_thread(_perform, params, rt))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await work
            raise


def resolve_path(raw: str, rt: StepRuntime, *, modify: bool, follow_final: bool = True) -> Path:
    """Expand and resolve a `file` step path, and check it against the file roots.

    `~` expands, and a relative path resolves against the project directory. Symlinks
    resolve, except a final one when `follow_final` is False. The result must be inside a
    file root and, with `modify`, outside the state directory. Raises PathNotAllowedError.
    """
    path = rt.project_dir / Path(raw).expanduser()
    if follow_final or path.name in ("", ".."):
        resolved = _real(path)
    else:
        resolved = _real(path.parent) / path.name
    if not any(resolved.is_relative_to(_real(root)) for root in rt.file_roots):
        raise PathNotAllowedError(
            f"{resolved} is outside the file roots; to allow it, add a directory that "
            "contains it to [security] file_roots in treadle.toml"
        )
    state = _real(rt.state_dir)
    if modify and resolved.is_relative_to(state):
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
    data = cast(str, params.get("content", "")).encode()
    _refuse_existing(target, params)
    _make_parents(target, params)
    _atomic_write(target, data, _mode(params))
    return len(data), True


def _append(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    data = cast(str, params.get("content", "")).encode()
    _make_parents(target, params)
    with target.open("ab") as file:
        file.write(data)
    return len(data), True


def _copy(target: Path, params: _Params, rt: StepRuntime) -> tuple[int, bool]:
    source = _source(params, rt, move=False)
    if source.is_dir():
        if params.get("recursive") is not True:
            raise StepConfigError(f"{source} is a directory; set recursive: true to copy it")
        if target.is_relative_to(source):
            raise StepConfigError(f"can't copy {source} into itself")
        if _real(rt.state_dir).is_relative_to(target):
            message = f"refusing to copy into {target}: it contains the state directory"
            raise PathNotAllowedError(message)
    _refuse_existing(target, params)
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
    source = resolve_path(cast(str, params["source"]), rt, modify=move, follow_final=not move)
    if move:
        _refuse_removal(source, rt, "move")
    if not os.path.lexists(source):
        raise StepError(f"source {source} doesn't exist")
    return source


def _refuse_removal(path: Path, rt: StepRuntime, action: str) -> None:
    """Refuse to delete or move a protected path, or a directory that holds one.

    Protected paths are the state, workflows, and project directories and each file root.
    """
    # Design note: section 2 protects these paths from delete and move. Deleting or moving a
    # parent takes them along, so a parent of a protected path is refused as well.
    for protected in map(_real, rt.protected_paths):
        if protected.is_relative_to(path):
            message = (
                f"refusing to {action} {path}: it is or contains the protected path {protected}"
            )
            raise PathNotAllowedError(message)


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
    # Design note: section 2 doesn't define `overwrite` for directories; merging into the
    # destination keeps every file there that the source doesn't replace.
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


def _invalid(message: str, path: YamlPath) -> Issue:
    return Issue(Severity.ERROR, "E407", message, path)
