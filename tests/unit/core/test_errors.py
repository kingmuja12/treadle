from __future__ import annotations

import pytest

from treadle.errors import (
    BusyError,
    ConfigError,
    ConflictError,
    CronError,
    EvaluationError,
    ExpressionError,
    ExpressionSyntaxError,
    InputError,
    LockHeldError,
    NoDatabaseError,
    NotFoundError,
    PathNotAllowedError,
    SchemaTooNewError,
    ShuttingDownError,
    StepConfigError,
    StepError,
    StoreError,
    TreadleError,
    UsageError,
    WorkflowInvalidError,
)
from treadle.types import ExitCode, Issue, LockHolder, Severity

ISSUE = Issue(Severity.ERROR, "E102", "missing required key 'steps'")


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (TreadleError("x"), ExitCode.FAILURE),
        (ConfigError("x"), ExitCode.CONFIG),
        (UsageError("x"), ExitCode.USAGE),
        (NotFoundError("x"), ExitCode.NOT_FOUND),
        (ConflictError("x"), ExitCode.FAILURE),
        (BusyError("x"), ExitCode.BUSY),
        (LockHeldError(None), ExitCode.BUSY),
        (ShuttingDownError("x"), ExitCode.BUSY),
        (InputError({"n": "must be an integer"}), ExitCode.USAGE),
        (WorkflowInvalidError("wf", [ISSUE]), ExitCode.CONFIG),
        (ExpressionError("x", source="a", offset=0), ExitCode.FAILURE),
        (ExpressionSyntaxError("x", source="a", offset=0), ExitCode.FAILURE),
        (EvaluationError("x", source="a", offset=0), ExitCode.FAILURE),
        (CronError("x", source="* *"), ExitCode.USAGE),
        (StoreError("x"), ExitCode.FAILURE),
        (NoDatabaseError("x"), ExitCode.FAILURE),
        (SchemaTooNewError("x"), ExitCode.CONFIG),
    ],
)
def test_each_error_maps_to_its_cli_exit_code(error: TreadleError, exit_code: ExitCode) -> None:
    assert type(error).exit_code is exit_code
    assert isinstance(error, TreadleError)


def test_step_errors_carry_the_class_exit_code_and_a_process_exit_code() -> None:
    error = StepError("exit code 23", exit_code=23)

    assert StepError.exit_code is ExitCode.FAILURE
    assert error.exit_code == 23


def test_config_error_keeps_its_issues() -> None:
    error = ConfigError("invalid configuration", issues=[ISSUE])

    assert str(error) == "invalid configuration"
    assert error.issues == (ISSUE,)
    assert ConfigError("no project").issues == ()


def test_lock_held_error_describes_the_holder() -> None:
    holder = LockHolder(42, "serve", "now", "0.1.0", "127.0.0.1", 8765)

    assert str(LockHeldError(holder)) == (
        "another Treadle process holds the state directory lock (serve, pid 42, port 8765)"
    )
    assert LockHeldError(holder).holder == holder
    assert str(LockHeldError(LockHolder(7, "run", "now", "0.1.0"))).endswith("(run, pid 7)")
    assert str(LockHeldError(None)) == "another Treadle process holds the state directory lock"
    assert isinstance(LockHeldError(None), BusyError)


def test_input_error_names_each_input() -> None:
    error = InputError({"count": "must be an integer", "": "unknown input 'x'"})

    assert error.errors == {"count": "must be an integer", "": "unknown input 'x'"}
    assert str(error) == "input 'count' must be an integer; unknown input 'x'"
    assert str(InputError({})) == "invalid inputs"


def test_workflow_invalid_error_keeps_the_workflow_and_issues() -> None:
    error = WorkflowInvalidError("deploy", [ISSUE])

    assert str(error) == "workflow 'deploy' is invalid"
    assert (error.workflow_id, error.issues) == ("deploy", (ISSUE,))


def test_expression_error_keeps_its_location() -> None:
    error = EvaluationError("cannot compare null with number", source="a < 1", offset=2, hint="h")

    assert (error.message, error.source, error.offset, error.hint) == (
        "cannot compare null with number",
        "a < 1",
        2,
        "h",
    )
    assert str(error) == "cannot compare null with number"


@pytest.mark.parametrize(
    ("source", "offset", "expected"),
    [
        ("startswith(x)", 0, "startswith(x)\n^"),
        ("a && b ||", 9, "a && b ||\n         ^"),
        ("first line\nsecond(", 17, "second(\n      ^"),
        ("one\ntwo", 3, "one\n   ^"),
        ("\tx(", 2, "\tx(\n\t ^"),
        ("abc", -4, "abc\n^"),
        ("abc", 99, "abc\n   ^"),
    ],
)
def test_expression_error_caret_points_into_its_line(
    source: str, offset: int, expected: str
) -> None:
    assert ExpressionSyntaxError("bad", source=source, offset=offset).caret() == expected


def test_cron_error_keeps_the_source_and_optional_offset() -> None:
    error = CronError("unknown month 'FOO'", source="0 0 1 FOO *", offset=6)

    assert (str(error), error.source, error.offset) == ("unknown month 'FOO'", "0 0 1 FOO *", 6)
    assert CronError("never fires", source="0 0 30 2 *").offset is None


def test_step_error_defaults_and_copies_outputs() -> None:
    outputs = {"status": 500}
    error = StepError("status 500", outputs=outputs)
    outputs["status"] = 200

    assert (error.message, error.retryable, error.outputs, error.exit_code) == (
        "status 500",
        True,
        {"status": 500},
        None,
    )
    assert StepError("x").outputs == {}


def test_step_config_errors_are_never_retryable() -> None:
    error = StepConfigError("malformed output file", outputs={"a": 1}, exit_code=0)

    assert (error.retryable, error.outputs, error.exit_code) == (False, {"a": 1}, 0)
    assert isinstance(PathNotAllowedError("outside file_roots"), StepConfigError)
    assert PathNotAllowedError("outside file_roots").retryable is False
