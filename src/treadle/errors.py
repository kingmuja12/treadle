"""The exception hierarchy. Each error class names the CLI exit code it maps to."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from treadle.types import ExitCode, Issue, JSONValue, LockHolder


class TreadleError(Exception):
    """Base class of every error that Treadle raises on purpose.

    `treadle.cli.main` catches it, prints the message, and exits with `exit_code`.
    """

    exit_code: ClassVar[ExitCode] = ExitCode.FAILURE


class ConfigError(TreadleError):
    """An invalid `treadle.toml`, a missing project, or another unusable setting."""

    exit_code = ExitCode.CONFIG

    def __init__(self, message: str, *, issues: Sequence[Issue] = ()) -> None:
        super().__init__(message)
        self.issues: tuple[Issue, ...] = tuple(issues)


class UsageError(TreadleError):
    """Bad command-line arguments."""

    exit_code = ExitCode.USAGE


class NotFoundError(TreadleError):
    """A workflow or run doesn't exist."""

    exit_code = ExitCode.NOT_FOUND


class ConflictError(TreadleError):
    """The request conflicts with the current state, such as re-running an active run."""


class BusyError(TreadleError):
    """Another process or the daemon can't take the request now."""

    exit_code = ExitCode.BUSY


class LockHeldError(BusyError):
    """Another process holds the state-directory lock. `holder` is None when unreadable."""

    def __init__(self, holder: LockHolder | None) -> None:
        detail = ""
        if holder is not None:
            port = f", port {holder.port}" if holder.port is not None else ""
            detail = f" ({holder.kind}, pid {holder.pid}{port})"
        super().__init__(f"another Treadle process holds the state directory lock{detail}")
        self.holder = holder


class ShuttingDownError(BusyError):
    """The daemon is draining and accepts no new runs."""


class InputError(TreadleError):
    """Run inputs failed validation.

    `errors` maps each input name, or "" for a general problem, to its message.
    """

    exit_code = ExitCode.USAGE

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors: dict[str, str] = dict(errors)
        parts = [
            f"input '{name}' {problem}" if name else problem for name, problem in errors.items()
        ]
        super().__init__("; ".join(parts) or "invalid inputs")


class WorkflowInvalidError(TreadleError):
    """The workflow has no valid current version, so it can't run."""

    exit_code = ExitCode.CONFIG

    def __init__(self, workflow_id: str, issues: Sequence[Issue]) -> None:
        super().__init__(f"workflow '{workflow_id}' is invalid")
        self.workflow_id = workflow_id
        self.issues: tuple[Issue, ...] = tuple(issues)


class ExpressionError(TreadleError):
    """An expression problem. `offset` is a 0-based character index into `source`."""

    def __init__(self, message: str, *, source: str, offset: int, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.offset = offset
        self.hint = hint

    def caret(self) -> str:
        """Return the line of `source` that holds `offset`, then a line with a caret under it."""
        offset = min(max(self.offset, 0), len(self.source))
        start = self.source.rfind("\n", 0, offset) + 1
        end = self.source.find("\n", offset)
        line = self.source[start : end if end >= 0 else len(self.source)]
        lead = "".join(char if char == "\t" else " " for char in self.source[start:offset])
        return f"{line}\n{lead}^"


class ExpressionSyntaxError(ExpressionError):
    """An expression or template that doesn't parse (E201 at load time)."""


class EvaluationError(ExpressionError):
    """An expression that fails at run time, such as comparing null with a number."""


class CronError(TreadleError):
    """An invalid cron expression. `offset` points at the bad field when it's known."""

    # An invalid cron expression is a usage error (exit code 2), so CronError carries
    # ExitCode.USAGE and `treadle cron` needs no special case.
    exit_code = ExitCode.USAGE

    def __init__(self, message: str, *, source: str, offset: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.offset = offset


class StepError(TreadleError):
    """A failed step attempt. `retryable` decides whether the retry policy applies.

    `outputs` carries partial outputs, and `exit_code` the process exit code, when known.
    """

    # StepError's instance `exit_code` is the step's process exit code, which shadows
    # TreadleError's CLI exit code; step errors never reach the CLI.
    exit_code: int | None  # type: ignore[assignment, misc]

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        outputs: Mapping[str, JSONValue] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.outputs: dict[str, JSONValue] = dict(outputs or {})
        self.exit_code = exit_code


class StepConfigError(StepError):
    """A step failure that retrying can't fix, such as malformed parameters. Never retried."""

    def __init__(
        self,
        message: str,
        *,
        outputs: Mapping[str, JSONValue] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message, retryable=False, outputs=outputs, exit_code=exit_code)


class PathNotAllowedError(StepConfigError):
    """A `file` step path outside `[security] file_roots`, or a protected path."""


class StoreError(TreadleError):
    """A database problem, such as an SQLite version that is too old."""


class NoDatabaseError(StoreError):
    """The state directory has no database yet."""


class SchemaTooNewError(StoreError):
    """The database was created by a newer Treadle. Downgrades are never attempted."""

    exit_code = ExitCode.CONFIG
