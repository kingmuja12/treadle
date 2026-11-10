from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from treadle.errors import EvaluationError
from treadle.expr.ast import BinaryOp
from treadle.expr.evaluator import (
    EvalContext,
    compare,
    equals,
    evaluate,
    evaluate_condition,
    render,
    to_display,
    truthy,
)
from treadle.expr.parser import parse_expression
from treadle.expr.template import parse_condition, parse_template
from treadle.types import JSONValue

INPUTS: dict[str, JSONValue] = {
    "null": None,
    "zero": 0,
    "int": 3,
    "float": 1.5,
    "empty": "",
    "str": "abc",
    "arr": [1, "two", None],
    "obj": {"k": "v", "1": "one", "n": {"m": 5}},
}
CTX = EvalContext({"inputs": INPUTS, "secrets": {"TOKEN": "s3cr3t-value"}, "env": {}})


def run(source: str, ctx: EvalContext = CTX) -> JSONValue:
    return evaluate(parse_expression(source), ctx)


def run_error(source: str, ctx: EvalContext = CTX) -> EvaluationError:
    with pytest.raises(EvaluationError) as caught:
        run(source, ctx)
    return caught.value


@dataclass
class FakeStatus:
    """A StatusView with fixed answers that records which functions were called."""

    succeeded: bool = True
    failed: bool = False
    cancelling: bool = False
    calls: list[str] = field(default_factory=list)

    def success(self) -> bool:
        self.calls.append("success")
        return self.succeeded

    def failure(self) -> bool:
        self.calls.append("failure")
        return self.failed

    def cancelled(self) -> bool:
        self.calls.append("cancelled")
        return self.cancelling


# One or two values of every type: null, boolean, number, numeric string, other string,
# array, and object. Each row is (left, right, left == right).
EQUALITY = [
    (None, None, True),
    (None, False, False),
    (None, 0, False),
    (None, "", False),
    (None, "null", False),
    (None, "0", False),
    (None, [], False),
    (None, {}, False),
    (True, True, True),
    (False, False, True),
    (True, False, False),
    (True, 1, False),
    (False, 0, False),
    (True, 1.0, False),
    (True, "true", False),
    (False, "", False),
    (True, "1", False),
    (True, [True], False),
    (True, {}, False),
    (3, 3, True),
    (3, 4, False),
    (1, 1.0, True),
    (0.1, 0.1, True),
    (0, -0.0, True),
    (3, "3", True),
    (3, "3.0", True),
    (3.0, "3", True),
    (300, "3e2", True),
    (-2, "-2", True),
    (3, " 3", False),
    (3, "3 ", False),
    (3, "+3", False),
    (3, "03", False),
    (3, "0x3", False),
    (0, "", False),
    (3, "abc", False),
    (3, [3], False),
    (3, {"3": 3}, False),
    ("abc", "abc", True),
    ("abc", "ABC", False),
    ("3", "3.0", False),
    ("3", "abc", False),
    ("3", [3], False),
    ("3", {"3": 3}, False),
    ("", "", True),
    ("a", ["a"], False),
    ("{}", {}, False),
    ([], [], True),
    ([1, "2", None], [1.0, 2, None], True),
    ([1, 2], [2, 1], False),
    ([1], [1, 1], False),
    ([[1], {"a": True}], [[1], {"a": True}], True),
    ([True], [1], False),
    ([], {}, False),
    ({}, {}, True),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
    ({"a": 1}, {"a": 1, "b": 2}, False),
    ({"a": 1}, {"b": 1}, False),
    ({"a": [1]}, {"a": ["1"]}, True),
    ({"a": None}, {"a": False}, False),
]


@pytest.mark.parametrize(("left", "right", "expected"), EQUALITY)
def test_equals_follows_the_coercion_rules_for_every_type_pair(
    left: JSONValue, right: JSONValue, expected: bool
) -> None:
    assert equals(left, right) is expected
    assert equals(right, left) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("inputs.int == '3'", True),
        ("inputs.int != '3'", False),
        ("'true' == true", False),
        ("null == 0", False),
        ("true == 1", False),
        ("inputs.null == null", True),
        ("inputs.missing != null", False),
        ("inputs.str == 'ABC'", False),
    ],
)
def test_equality_operators_never_raise(source: str, expected: bool) -> None:
    assert run(source) is expected


# Each row is (left, right, [<, <=, >, >=]).
ORDERED = [
    (1, 2, [True, True, False, False]),
    (2, 2, [False, True, False, True]),
    (2.5, 2, [False, False, True, True]),
    (-1, -0.5, [True, True, False, False]),
    (10, "9", [False, False, True, True]),
    ("10", 9, [False, False, True, True]),
    ("1e1", 10, [False, True, False, True]),
    ("2.5", "10", [False, False, True, True]),
    ("10", "9", [True, True, False, False]),
    ("abc", "abd", [True, True, False, False]),
    ("a", "B", [False, False, True, True]),
    ("é", "z", [False, False, True, True]),
    ("", "a", [True, True, False, False]),
    ("x", "x", [False, True, False, True]),
]
RELATIONS = [BinaryOp.LT, BinaryOp.LE, BinaryOp.GT, BinaryOp.GE]


@pytest.mark.parametrize(("left", "right", "results"), ORDERED)
def test_compare_orders_numbers_strings_and_numeric_strings(
    left: JSONValue, right: JSONValue, results: list[bool]
) -> None:
    assert [compare(op, left, right) for op in RELATIONS] == results


@pytest.mark.parametrize(
    ("left", "right", "message"),
    [
        (None, 1, "cannot compare null with number"),
        (1, None, "cannot compare number with null"),
        (None, None, "cannot compare null with null"),
        (None, True, "cannot compare null with boolean"),
        (None, [], "cannot compare null with array"),
        ("3", None, "cannot compare string with null"),
        ("a", None, "cannot compare string with null"),
        (True, 1, "cannot compare boolean with number"),
        (1, False, "cannot compare number with boolean"),
        (True, False, "cannot compare boolean with boolean"),
        ("true", True, "cannot compare string with boolean"),
        ("abc", 1, "cannot compare string with number"),
        (1, " 1", "cannot compare number with string"),
        ([1], [2], "cannot compare array with array"),
        ("1", [1], "cannot compare string with array"),
        ({}, 1, "cannot compare object with number"),
        ({}, {}, "cannot compare object with object"),
        (1, [1], "cannot compare number with array"),
        (True, {}, "cannot compare boolean with object"),
        ([], {}, "cannot compare array with object"),
    ],
)
def test_compare_raises_for_every_other_pair(
    left: JSONValue, right: JSONValue, message: str
) -> None:
    for op in RELATIONS:
        with pytest.raises(EvaluationError) as caught:
            compare(op, left, right, source="a < b", offset=2)
        assert (caught.value.message, caught.value.source, caught.value.offset) == (
            message,
            "a < b",
            2,
        )


def test_a_failed_comparison_points_at_the_comparison() -> None:
    error = run_error("inputs.int > 1 && inputs.null <= 1")

    assert (error.message, error.offset) == ("cannot compare null with number", 18)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (False, False),
        (None, False),
        (0, False),
        (0.0, False),
        (-0.0, False),
        ("", False),
        (True, True),
        (1, True),
        (-1, True),
        (0.5, True),
        ("0", True),
        ("false", True),
        (" ", True),
        ([], True),
        ({}, True),
        ([0], True),
    ],
)
def test_truthiness(value: JSONValue, expected: bool) -> None:
    ctx = EvalContext({"inputs": {"value": value}}, FakeStatus())

    assert truthy(value) is expected
    assert evaluate_condition(parse_condition("always() && inputs.value"), ctx) is expected
    assert run("!inputs.value", ctx) is not expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("inputs.null || 'world'", "world"),
        ("inputs.str || 'world'", "abc"),
        ("inputs.zero || inputs.empty", ""),
        ("inputs.empty || inputs.zero", 0),
        ("inputs.int && inputs.str", "abc"),
        ("inputs.zero && inputs.str", 0),
        ("inputs.arr && inputs.obj.k", "v"),
        ("false || null", None),
        ("!inputs.str", False),
        ("!inputs.empty", True),
        ("!!inputs.arr", True),
        ("!inputs.obj == false", True),
    ],
)
def test_logical_operators_return_operands_and_not_returns_a_boolean(
    source: str, expected: JSONValue
) -> None:
    assert run(source) == expected
    assert type(run(source)) is type(expected)


@pytest.mark.parametrize(
    "source",
    [
        "false && (null < 1)",
        "true || (null < 1)",
        "inputs.null && missing.root",
        "inputs.str || secrets.MISSING",
    ],
)
def test_logical_operators_short_circuit(source: str) -> None:
    run(source)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("inputs.obj.k", "v"),
        ("inputs.obj.missing", None),
        ("inputs.obj.n.m", 5),
        ("inputs.missing", None),
        ("inputs.str.length", None),
        ("inputs.null.x", None),
        ("inputs.arr.x", None),
        ("inputs.int.x.y", None),
        ("inputs.arr[0]", 1),
        ("inputs.arr[1.0]", "two"),
        ("inputs.arr[2]", None),
        ("inputs.arr[3]", None),
        ("inputs.arr[-1]", None),
        ("inputs.obj['k']", "v"),
        ("inputs.obj[1]", "one"),
        ("inputs.obj[1.0]", "one"),
        ("inputs.obj['missing']", None),
        ("inputs['obj']['n']['m']", 5),
        ("inputs.null[0]", None),
        ("inputs.null['k']", None),
        ("inputs.str[0]", None),
        ("inputs.int['k']", None),
        ("(inputs.arr)[inputs.zero]", 1),
    ],
)
def test_member_and_index_access(source: str, expected: JSONValue) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("inputs.arr['0']", "an array index must be an integer, got string"),
        ("inputs.arr[1.5]", "an array index must be an integer, got 1.5"),
        ("inputs.arr[true]", "an array index must be an integer, got boolean"),
        ("inputs.arr[null]", "an array index must be an integer, got null"),
        ("inputs.arr[inputs.arr]", "an array index must be an integer, got array"),
        ("inputs.arr[inputs.obj]", "an array index must be an integer, got object"),
        ("inputs.obj[false]", "an object key must be a string or a number, got boolean"),
        ("inputs.obj[null]", "an object key must be a string or a number, got null"),
        ("inputs.obj[inputs.arr]", "an object key must be a string or a number, got array"),
        ("inputs.obj[inputs.obj]", "an object key must be a string or a number, got object"),
    ],
)
def test_bad_index_types_raise_at_the_index(source: str, message: str) -> None:
    error = run_error(source)

    assert (error.message, error.offset) == (message, 11)


def test_a_missing_secret_raises_instead_of_rendering_as_empty() -> None:
    assert run("secrets.TOKEN") == "s3cr3t-value"
    assert run("secrets['TOKEN']") == "s3cr3t-value"
    for source in ("secrets.MISSING", "secrets['MISSING']", "'x' == secrets.MISSING"):
        assert run_error(source).message == "secret MISSING is not defined"


def test_a_root_that_is_not_in_scope_raises() -> None:
    error = run_error("inputs.int == 3 && result.status")

    assert (error.message, error.offset) == ("'result' isn't available here", 19)


def test_evaluation_errors_carry_the_expression_and_its_offset() -> None:
    expression = parse_template("x: ${{ inputs.null < 1 }}").parts[1]
    assert not isinstance(expression, str)

    with pytest.raises(EvaluationError) as caught:
        evaluate(expression, CTX)

    assert (caught.value.source, caught.value.offset) == (" inputs.null < 1 ", 7)


def test_render_reports_errors_against_the_whole_template() -> None:
    template = parse_template("x: ${{ inputs.null < 1 }}")

    with pytest.raises(EvaluationError) as caught:
        render(template, CTX)

    assert caught.value.caret() == "x: ${{ inputs.null < 1 }}\n       ^"


@pytest.mark.parametrize(
    ("source", "view", "expected", "calls"),
    [
        ("success()", FakeStatus(succeeded=True), True, ["success"]),
        ("success()", FakeStatus(succeeded=False), False, ["success"]),
        ("failure()", FakeStatus(failed=True), True, ["failure"]),
        ("cancelled()", FakeStatus(cancelling=True), True, ["cancelled"]),
        ("always()", FakeStatus(succeeded=False), True, []),
        ("failure() || cancelled()", FakeStatus(failed=True), True, ["failure"]),
        ("failure() || cancelled()", FakeStatus(), False, ["failure", "cancelled"]),
        ("success() && failure()", FakeStatus(succeeded=False), False, ["success"]),
        ("!cancelled()", FakeStatus(), True, ["cancelled"]),
    ],
)
def test_status_functions_ask_the_status_view(
    source: str, view: FakeStatus, expected: bool, calls: list[str]
) -> None:
    assert run(source, EvalContext(CTX.values, view)) is expected
    assert view.calls == calls


@pytest.mark.parametrize("name", ["success", "failure", "always", "cancelled"])
def test_status_functions_need_a_status_view(name: str) -> None:
    assert run_error(f"{name}()").message == f"{name}() is only available in a step `if`"


def test_status_functions_take_no_arguments() -> None:
    error = run_error("x || success(1)", EvalContext({"x": False}, FakeStatus()))

    assert (error.message, error.offset) == ("success() takes no arguments", 5)


def test_a_condition_without_status_functions_is_success_and_the_expression() -> None:
    passing, failing = FakeStatus(succeeded=True), FakeStatus(succeeded=False)
    condition = parse_condition("inputs.str == 'abc'")

    assert evaluate_condition(condition, EvalContext(CTX.values, passing)) is True
    assert evaluate_condition(condition, EvalContext(CTX.values, failing)) is False
    assert passing.calls == failing.calls == ["success"]


def test_the_implicit_success_short_circuits_the_expression() -> None:
    condition = parse_condition("inputs.null < 1")

    assert evaluate_condition(condition, EvalContext(CTX.values, FakeStatus(False))) is False
    with pytest.raises(EvaluationError, match="cannot compare null with number"):
        evaluate_condition(condition, EvalContext(CTX.values, FakeStatus(True)))


@pytest.mark.parametrize(
    ("condition", "view", "expected", "calls"),
    [
        ("failure()", FakeStatus(succeeded=False, failed=True), True, ["failure"]),
        ("always()", FakeStatus(succeeded=False), True, []),
        ("cancelled() || inputs.int", FakeStatus(), True, ["cancelled"]),
        (True, FakeStatus(succeeded=False), False, ["success"]),
        (False, FakeStatus(), False, ["success"]),
        ("${{ inputs.empty }}", FakeStatus(), False, ["success"]),
    ],
)
def test_conditions_apply_the_implicit_success_only_without_status_functions(
    condition: str | bool, view: FakeStatus, expected: bool, calls: list[str]
) -> None:
    assert evaluate_condition(parse_condition(condition), EvalContext(CTX.values, view)) is expected
    assert view.calls == calls


def test_condition_errors_point_into_the_condition_as_written() -> None:
    condition = parse_condition("  ${{ inputs.null < 1 }}")

    with pytest.raises(EvaluationError) as caught:
        evaluate_condition(condition, EvalContext(CTX.values, FakeStatus()))

    assert caught.value.caret() == "  ${{ inputs.null < 1 }}\n      ^"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (-12, "-12"),
        (3.0, "3"),
        (-0.0, "0"),
        (1e20, "100000000000000000000"),
        (0.1, "0.1"),
        (1.5, "1.5"),
        (1e-7, "1e-07"),
        ("text", "text"),
        ("", ""),
        ([1, "a", None, True, 2.0], '[1,"a",null,true,2.0]'),
        ({"b": 1, "a": [2.5]}, '{"b":1,"a":[2.5]}'),
        ({"é": "ü"}, '{"é":"ü"}'),
        ([], "[]"),
        ({}, "{}"),
    ],
)
def test_to_display(value: JSONValue, text: str) -> None:
    assert to_display(value) == text
