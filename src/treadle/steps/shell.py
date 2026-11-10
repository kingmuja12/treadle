"""The `shell` step type: a `run` script through a shell, or an `argv` program."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Final, Literal, cast

from treadle.errors import StepConfigError, StepError
from treadle.models import ShellSpec
from treadle.schema import AnyOf, Bool, CheckContext, Field, Int, ListOf, Obj, Str
from treadle.steps.base import StepResult, StepRuntime, check_os_text
from treadle.steps.procs import run_process
from treadle.types import Issue, JSONValue, Severity, YamlPath
from treadle.units import format_size

_SHELL_COMMANDS: Final[Mapping[str, tuple[str, ...]]] = {
    "bash": ("bash", "--noprofile", "--norc", "-e", "-o", "pipefail"),
    "sh": ("sh", "-e"),
    "python": ("python3", "-u"),
}
_RESERVED: Final = frozenset({"exit_code", "stdout"})
_ENTRY: Final = re.compile(r"(.*?)(?:=(.*)|<<(.+))")
_OUTPUT_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_SHELL_NAME: Final = Str(choices=tuple(_SHELL_COMMANDS))
_SHELL_TEMPLATE: Final = ListOf(Str(), min_len=1)
_SHELL_EITHER: Final = AnyOf(_SHELL_NAME, _SHELL_TEMPLATE)


class _Shell:
    """A shell name or an argv template, checked by the form it takes for exact messages."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, str):
            return _SHELL_NAME.check(value, path, ctx)
        if isinstance(value, list):
            return _SHELL_TEMPLATE.check(value, path, ctx)
        return _SHELL_EITHER.check(value, path, ctx)

    def describe(self) -> str:
        return _SHELL_EITHER.describe()


_SCHEMA: Final = Obj(
    "shell step",
    {
        "run": Field(Str()),
        "argv": Field(ListOf(Str(), min_len=1)),
        "shell": Field(_Shell()),
        "cwd": Field(Str()),
        "capture_stdout": Field(Bool(), default=False),
        # Exit codes are 0-255, and an empty list could never succeed.
        "success_codes": Field(ListOf(Int(min=0, max=255), min_len=1), default=[0]),
    },
    exclusive=[("run", "argv")],
    require_one=[("run", "argv")],
)


class ShellStep:
    """Runs a `run` script through a shell, or an `argv` program directly.

    Outputs are `exit_code`, every entry of the `$TREADLE_OUTPUT` file, and `stdout` when
    `capture_stdout` is true.
    """

    name = "shell"
    output_names: ClassVar[frozenset[str] | None] = None
    reserved_outputs: ClassVar[frozenset[str]] = _RESERVED
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {
        "run": "code",
        "cwd": "path",
    }

    @property
    def schema(self) -> Obj:
        """The `with` keys: run, argv, shell, cwd, capture_stdout, and success_codes."""
        return _SCHEMA

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return an E407 issue when `shell` comes with `argv`, which runs without a shell."""
        if "shell" in params and "argv" in params:
            message = "'shell' applies only to 'run' scripts, not to 'argv'"
            hint = "remove 'shell', or move the command into 'run'"
            return [Issue(Severity.ERROR, "E407", message, (*path, "shell"), hint=hint)]
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Run one attempt and return `exit_code`, the output-file entries, and `stdout`.

        A `run` script is first written to `<attempt dir>/script` with mode 0600. An exit
        code outside `success_codes` raises a retryable StepError ("exit code N") that
        carries the outputs. After a successful exit, a malformed or oversized output file
        raises StepConfigError; after a failed exit, the file is ignored with a system line.
        """
        script_path = _write_script(params, rt)
        argv = build_argv(params, script_path=script_path, default_shell=rt.default_shell)
        capture = rt.limits.stdout_capture_bytes if params.get("capture_stdout") is True else 0
        result = await run_process(argv, cwd=_workdir(params, rt), rt=rt, capture=capture)
        code = result.exit_code
        succeeded = code in cast("list[int]", params.get("success_codes", [0]))
        outputs: dict[str, JSONValue] = {"exit_code": code}
        if capture:
            outputs["stdout"] = result.stdout_tail.removesuffix("\n")
        try:
            outputs.update(parse_output_file(_read_output(rt)))
        except StepConfigError as exc:
            if succeeded:
                raise StepConfigError(exc.message, outputs=outputs, exit_code=code) from None
            # The failed exit already fails the attempt and keeps it retryable;
            # a broken output file is usually a symptom of that same failure.
            rt.log.system(f"ignored the output file: {exc.message}")
        if not succeeded:
            raise StepError(f"exit code {code}", outputs=outputs, exit_code=code)
        return StepResult(outputs, exit_code=code)


def build_argv(
    params: Mapping[str, JSONValue], *, script_path: Path | None, default_shell: ShellSpec
) -> list[str]:
    """Return the command line for an `argv` list, or for a `run` script at `script_path`.

    A named shell runs with its flags, such as `sh -e`. A shell list replaces each element equal
    to `{0}` with the script path, or appends the path when no element is `{0}`. Raises
    ValueError for a script without `script_path`.
    """
    argv = params.get("argv")
    if isinstance(argv, list):
        return [str(part) for part in argv]
    if script_path is None:
        raise ValueError("a 'run' script needs a script path")
    script, shell = str(script_path), params.get("shell", default_shell)
    if isinstance(shell, str):
        return [*_SHELL_COMMANDS[shell], script]
    template = [str(part) for part in cast("Sequence[object]", shell)]
    if "{0}" not in template:
        return [*template, script]
    return [script if part == "{0}" else part for part in template]


def parse_output_file(text: str) -> dict[str, str]:
    """Parse the `$TREADLE_OUTPUT` file: `KEY=value` lines and `KEY<<DELIM` heredocs.

    A heredoc's value is every line up to the line that equals `DELIM`. Blank lines between
    entries are ignored, and a later entry replaces an earlier one with the same key. Raises
    StepConfigError for malformed content, an invalid output name, or a reserved name.
    """
    outputs: dict[str, str] = {}
    lines = enumerate(text.replace("\r\n", "\n").split("\n"), start=1)
    for number, line in lines:
        if not line.strip():
            continue
        where = f"line {number} of the output file"
        match = _ENTRY.fullmatch(line)
        if match is None:
            raise StepConfigError(f"{where} isn't KEY=value or KEY<<DELIMITER")
        key, value, delimiter = match.groups()
        if not _OUTPUT_NAME.fullmatch(key):
            raise StepConfigError(f"{where} has an invalid output name '{key}'")
        if key in _RESERVED:
            raise StepConfigError(f"{where} sets the reserved output '{key}'")
        if delimiter is not None:
            body = []
            for _, item in lines:
                if item == delimiter:
                    break
                body.append(item)
            else:
                raise StepConfigError(f"the heredoc on {where} never ends with '{delimiter}'")
            value = "\n".join(body)
        outputs[key] = value
    return outputs


def expand_home(raw: str | Path, key: str) -> Path:
    """Return `raw`, the value of the path parameter `key`, with a leading "~" expanded.

    The `file` step expands its paths the same way. Raises StepConfigError, naming `key` and
    the path, for a "~" or "~user" whose home directory isn't known, such as a user that
    doesn't exist on this machine.
    """
    path = Path(raw)
    try:
        return path.expanduser()
    except RuntimeError:
        message = (
            f"can't expand '{path.parts[0]}' in {key} '{raw}' to a home directory; "
            "check the user name after '~', or write the full path"
        )
        raise StepConfigError(message) from None


def _write_script(params: Mapping[str, JSONValue], rt: StepRuntime) -> Path | None:
    """Write a `run` script to a new 0600 file in the attempt directory and return its path.

    The file must not exist yet, so a planted file or symlink fails the attempt.
    """
    script = params.get("run")
    if not isinstance(script, str):
        return None
    path = rt.attempt_dir / "script"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(descriptor, "w", encoding="utf-8") as file:
            file.write(script)
    except OSError as exc:
        raise StepError(f"cannot write the script file {path}: {exc.strerror or exc}") from exc
    return path


def _workdir(params: Mapping[str, JSONValue], rt: StepRuntime) -> Path:
    """Return `cwd` with `~` expanded, relative to the project directory, or `defaults.cwd`.

    Raises StepConfigError for a `cwd` that the operating system can't take, and for a
    "~user" in `cwd` or `defaults.cwd` without a known home directory.
    """
    cwd = params.get("cwd")
    if not isinstance(cwd, str):
        # The executor leaves a `defaults.cwd` that it can't expand as written; any other
        # default is absolute, so this returns it unchanged.
        return rt.project_dir / expand_home(rt.default_cwd, "defaults.cwd")
    check_os_text(cwd, "cwd")
    return rt.project_dir / expand_home(cwd, "cwd")


def _read_output(rt: StepRuntime) -> str:
    """Return the output file's text. A file that the step deleted has no entries."""
    limit = rt.limits.output_file_bytes
    try:
        with rt.output_path.open("rb") as file:
            data = file.read(limit + 1)
    except FileNotFoundError:
        return ""
    if len(data) > limit:
        message = f"the output file is larger than {format_size(limit)} (limits.output_file_bytes)"
        raise StepConfigError(message)
    return data.decode("utf-8", "replace")
