"""The step-type contract and the registry that maps `uses:` names to step types."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from treadle.config import Limits
from treadle.errors import StepConfigError
from treadle.models import LogStream, ShellSpec
from treadle.protocols import ProcessTracker
from treadle.schema import Obj
from treadle.types import Issue, JSONValue, YamlPath

if TYPE_CHECKING:
    import httpx


class StepLog(Protocol):
    """Where a running step writes its log lines. The engine masks and caps them."""

    def write(self, stream: LogStream, text: str) -> None:
        """Write one line, without its newline."""

    def system(self, text: str) -> None:
        """Write one line from Treadle itself. System lines don't count against log caps."""

    def add_mask(self, value: str) -> None:
        """Mask `value` in every later line of the run."""


@dataclass(frozen=True, slots=True)
class StepRuntime:
    """Everything one step attempt may use.

    `env` is the complete process environment for the attempt. `attempt_dir` exists with mode
    0700, and `output_path` (inside it) exists, is empty, and has mode 0600.
    """

    run_id: int
    workflow_id: str
    step_id: str
    attempt: int
    env: Mapping[str, str]
    project_dir: Path
    attempt_dir: Path
    output_path: Path
    default_shell: ShellSpec
    default_cwd: Path
    file_roots: tuple[Path, ...]
    protected_paths: tuple[Path, ...]
    state_dir: Path
    kill_grace: float
    limits: Limits
    log: StepLog
    processes: ProcessTracker
    http: httpx.AsyncClient


@dataclass(frozen=True, slots=True)
class StepResult:
    """A successful attempt: its raw outputs and, for processes, the exit code."""

    outputs: dict[str, JSONValue] = field(default_factory=dict)
    exit_code: int | None = None


class StepType(Protocol):
    """A kind of step, named by `uses:`.

    A step type whose work a cancellation can't stop, such as a file operation in a thread,
    sets the class attribute `interruptible = False`. Its cancelled attempt waits for the work
    to end, so a timeout is final: a retry would repeat work that may have finished. Without
    the attribute, a step type is interruptible.
    """

    @property
    def name(self) -> str:
        """The `uses:` name."""

    @property
    def schema(self) -> Obj:
        """The schema of the `with` parameters. Unknown keys are E101."""

    @property
    def output_names(self) -> frozenset[str] | None:
        """The fixed output names, or None when outputs are dynamic (`shell`)."""

    @property
    def reserved_outputs(self) -> frozenset[str]:
        """Output names that a step's declared `outputs` may not reuse (E406)."""

    @property
    def taint_sinks(self) -> Mapping[str, Literal["code", "path", "url"]]:
        """Parameters where untrusted data is dangerous, by sink kind, for lint warnings."""

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return E407 issues for cross-field problems that the schema can't express.

        `params` are schema-normalized and may still hold `Template` values.
        """

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Run one attempt with rendered, validated parameters.

        Raises StepError on failure, and re-raises `asyncio.CancelledError` after cleaning up.
        """


class StepRegistry:
    """The step types available to the loader and the executor, by name."""

    def __init__(self, types: Iterable[StepType] = ()) -> None:
        self._types: dict[str, StepType] = {}
        for step_type in types:
            self.register(step_type)

    def register(self, step_type: StepType) -> None:
        """Add a step type. Raises ValueError when its name is already registered."""
        if step_type.name in self._types:
            raise ValueError(f"step type '{step_type.name}' is already registered")
        self._types[step_type.name] = step_type

    def get(self, name: str) -> StepType | None:
        """Return the step type with this name, or None."""
        return self._types.get(name)

    def names(self) -> list[str]:
        """Return the registered names, sorted."""
        return sorted(self._types)


def check_os_text(text: str, what: str) -> None:
    """Raise StepConfigError when `text` can't be a path, an argument, or an environment value.

    The operating system refuses text with a NUL character, or with a lone surrogate that the
    file system encoding can't encode, and untrusted data, such as a webhook field, can hold
    either. The message names `what`, never the text.
    """
    if "\0" in text:
        problem = "a NUL character"
    else:
        try:
            os.fsencode(text)
        except UnicodeEncodeError:
            problem = "text that isn't valid Unicode"
        else:
            return
    raise StepConfigError(f"{what} holds {problem}, which the operating system doesn't accept")
