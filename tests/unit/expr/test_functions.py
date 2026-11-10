from __future__ import annotations

import pytest

from treadle.errors import EvaluationError
from treadle.expr.evaluator import EvalContext, evaluate
from treadle.expr.functions import FUNCTIONS, call_function
from treadle.expr.parser import parse_expression
from treadle.types import JSONValue

MAX_CHARS = 1 << 20
VALUES: dict[str, JSONValue] = {
    "null": None,
    "true": True,
    "false": False,
    "int": 3,
    "float": 1.5,
    "whole": 2.0,
    "str": "AbC",
    "spaces": "\t x y \r\n",
    "arr": [1, "b", None, 2.0],
    "obj": {"k": "v"},
    "empty_arr": [],
    "empty_obj": {},
    "nested": [[1], {"a": True}],
    "quotes": '"' * 2000,
    "almost": "a" * (MAX_CHARS - 2),
    "halves": ["a" * (MAX_CHARS // 2), "a" * (MAX_CHARS // 2 - 1)],
    "big": "a" * MAX_CHARS,
    "deep": "[" * 64 + "]" * 64,
    "deeper": "[" * 65 + "]" * 65,
    "deepest": "[" * 100_000,
    "huge_json": " " * MAX_CHARS + "1",
}
CTX = EvalContext({"v": VALUES})


def run(source: str) -> JSONValue:
    return evaluate(parse_expression(source), CTX)


def test_functions_lists_every_function_with_its_arity() -> None:
    assert {
        name: (spec.min_args, spec.max_args, spec.status) for name, spec in FUNCTIONS.items()
    } == {
        "contains": (2, 2, False),
        "startsWith": (2, 2, False),
        "endsWith": (2, 2, False),
        "format": (1, None, False),
        "join": (1, 2, False),
        "toJSON": (1, 1, False),
        "fromJSON": (1, 1, False),
        "lower": (1, 1, False),
        "upper": (1, 1, False),
        "trim": (1, 1, False),
        "length": (1, 1, False),
        "default": (2, 2, False),
        "success": (0, 0, True),
        "failure": (0, 0, True),
        "always": (0, 0, True),
        "cancelled": (0, 0, True),
    }
    assert all(spec.name == name for name, spec in FUNCTIONS.items())


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("contains('Hello', 'ell')", True),
        ("contains('Hello', 'ELL')", False),
        ("contains('a1b', 1)", True),
        ("contains('x1.5x', v.float)", True),
        ("contains('is true', true)", True),
        ('contains(\'x{"k":"v"}\', v.obj)', True),
        ("contains('abc', null)", False),
        ("contains('', '')", True),
        ("contains(v.arr, 1)", True),
        ("contains(v.arr, '1')", True),
        ("contains(v.arr, 'B')", False),
        ("contains(v.arr, null)", True),
        ("contains(v.arr, 2)", True),
        ("contains(v.arr, true)", False),
        ("contains(v.empty_arr, null)", False),
        ("contains(v.nested, fromJSON('{\"a\": true}'))", True),
        ("contains(v.nested, v.arr)", False),
        ("contains('[1,\"b\",null,2.0]', v.arr)", True),
        ("contains(null, 'x')", False),
        ("contains(null, null)", False),
    ],
)
def test_contains(source: str, expected: bool) -> None:
    assert run(source) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("startsWith('refs/tags/v1', 'refs/tags/')", True),
        ("startsWith('Refs/tags', 'refs')", False),
        ("startsWith(123, 12)", True),
        ("startsWith(v.whole, '2')", True),
        ("startsWith(v.float, '1.5')", True),
        ("startsWith(true, 'tr')", True),
        ("startsWith(null, '')", True),
        ("startsWith(null, 'n')", False),
        ("startsWith('abc', null)", True),
        ("endsWith('report.pdf', '.pdf')", True),
        ("endsWith('report.PDF', '.pdf')", False),
        ("endsWith(1.5, 5)", True),
        ("endsWith(false, 'se')", True),
        ("endsWith('x', 'longer x')", False),
        ("endsWith(null, '')", True),
        ("endsWith('x', null)", True),
    ],
)
def test_starts_with_and_ends_with(source: str, expected: bool) -> None:
    assert run(source) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("format('{0} and {1}', 'a', 'b')", "a and b"),
        ("format('{1}{0}{1}', 'a', 'b')", "bab"),
        (
            "format('{0}|{1}|{2}|{3}|{4}|{5}', null, true, 3, v.whole, 0.5, v.arr)",
            '|true|3|2|0.5|[1,"b",null,2.0]',
        ),
        ("format('{{0}} is {0}', 'x')", "{0} is x"),
        ("format('}}{{')", "}{"),
        ("format('{00}', 'x')", "x"),
        ("format('no placeholders', 'unused')", "no placeholders"),
        ("format(42)", "42"),
        ("format(null)", ""),
        ("format(true)", "true"),
        ("format('{0}', v.obj)", '{"k":"v"}'),
        ("format(v.obj.missing)", ""),
    ],
)
def test_format(source: str, expected: str) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("join(v.arr)", "1,b,,2"),
        ("join(v.arr, ' | ')", "1 | b |  | 2"),
        ("join(v.arr, null)", "1b2"),
        ("join(v.arr, 0)", "10b002"),
        ("join(v.arr, true)", "1truebtruetrue2"),
        ("join(v.nested)", '[1],{"a":true}'),
        ("join(v.empty_arr)", ""),
        ("join('already joined')", "already joined"),
        ("join('x', '-')", "x"),
        ("join(null)", ""),
    ],
)
def test_join(source: str, expected: str) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("toJSON(null)", "null"),
        ("toJSON(true)", "true"),
        ("toJSON(3)", "3"),
        ("toJSON(v.whole)", "2.0"),
        ("toJSON('a\"b')", '"a\\"b"'),
        ("toJSON('é')", '"é"'),
        ("toJSON(v.obj)", '{"k":"v"}'),
        ("toJSON(v.arr)", '[1,"b",null,2.0]'),
        ('toJSON(fromJSON(\'{"b": 1, "a": 2}\'))', '{"b":1,"a":2}'),
    ],
)
def test_to_json_is_compact_single_line_json(source: str, expected: str) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("fromJSON('{\"a\": [1, 2.5, null, true]}')", {"a": [1, 2.5, None, True]}),
        ("fromJSON('\"text\"')", "text"),
        ("fromJSON('null')", None),
        ("fromJSON(' 7 ')", 7),
        ("fromJSON('-0.25e1')", -2.5),
        ("length(fromJSON(v.deep))", 1),
        ("fromJSON(toJSON(v.arr))", [1, "b", None, 2.0]),
    ],
)
def test_from_json_parses_strict_json(source: str, expected: JSONValue) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("lower('AbC')", "abc"),
        ("lower(v.str)", "abc"),
        ("lower(null)", ""),
        ("lower(true)", "true"),
        ("lower(1.5)", "1.5"),
        ("upper('AbC')", "ABC"),
        ("upper(false)", "FALSE"),
        ("upper(3)", "3"),
        ("upper(null)", ""),
        ("trim(v.spaces)", "x y"),
        ("trim('  x  ')", "x"),
        ("trim(null)", ""),
        ("trim(v.whole)", "2"),
        ("trim(true)", "true"),
    ],
)
def test_lower_upper_and_trim_convert_scalars(source: str, expected: str) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("length('héllo')", 5),
        ("length('')", 0),
        ("length(v.arr)", 4),
        ("length(v.obj)", 1),
        ("length(v.empty_obj)", 0),
        ("length(null)", 0),
    ],
)
def test_length(source: str, expected: int) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("default(null, 'x')", "x"),
        ("default(v.obj.missing, v.arr)", [1, "b", None, 2.0]),
        ("default(false, 'x')", False),
        ("default(0, 1)", 0),
        ("default('', 'x')", ""),
        ("default(v.obj, null)", {"k": "v"}),
        ("default(null, null)", None),
    ],
)
def test_default_replaces_only_null(source: str, expected: JSONValue) -> None:
    assert run(source) == expected


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("contains(1, 1)", "contains() expects a string or an array, got number"),
        ("contains(true, 't')", "contains() expects a string or an array, got boolean"),
        ("contains(v.obj, 'k')", "contains() expects a string or an array, got object"),
        ("startsWith(v.arr, 1)", "startsWith() expects a string, got array"),
        ("startsWith('a', v.obj)", "startsWith() expects a string, got object"),
        ("startsWith(v.obj, 'k')", "startsWith() expects a string, got object"),
        ("endsWith(v.obj, 'x')", "endsWith() expects a string, got object"),
        ("endsWith('x', v.arr)", "endsWith() expects a string, got array"),
        ("format(v.arr)", "format() expects a string, got array"),
        ("format(v.obj)", "format() expects a string, got object"),
        ("format('{0}')", "format() has no argument for {0}"),
        ("format('{1}', 'a')", "format() has no argument for {1}"),
        ("format('a { b', 1)", "format() has a stray '{' at index 2"),
        ("format('a } b')", "format() has a stray '}' at index 2"),
        ("format('{x}')", "format() has a stray '{' at index 0"),
        ("format('{0}}', 1)", "format() has a stray '}' at index 3"),
        ("join(1)", "join() expects an array, got number"),
        ("join(true)", "join() expects an array, got boolean"),
        ("join(v.obj)", "join() expects an array, got object"),
        ("join(v.arr, v.arr)", "join() expects a string, got array"),
        ("join(v.arr, v.obj)", "join() expects a string, got object"),
        ("fromJSON(1)", "fromJSON() expects a string, got number"),
        ("fromJSON(null)", "fromJSON() expects a string, got null"),
        ("fromJSON(true)", "fromJSON() expects a string, got boolean"),
        ("fromJSON(v.arr)", "fromJSON() expects a string, got array"),
        ("fromJSON(v.obj)", "fromJSON() expects a string, got object"),
        ("fromJSON('NaN')", "fromJSON() can't parse the JSON: NaN isn't valid JSON"),
        ("fromJSON('[Infinity]')", "fromJSON() can't parse the JSON: Infinity isn't valid JSON"),
        ("fromJSON('-Infinity')", "fromJSON() can't parse the JSON: -Infinity isn't valid JSON"),
        ("fromJSON('1e400')", "fromJSON() can't parse the JSON: number 1e400 is out of range"),
        (
            "fromJSON('{')",
            "fromJSON() can't parse the JSON: Expecting property name enclosed in double "
            "quotes: line 1 column 2 (char 1)",
        ),
        (
            "fromJSON('')",
            "fromJSON() can't parse the JSON: Expecting value: line 1 column 1 (char 0)",
        ),
        (
            "fromJSON('[1] x')",
            "fromJSON() can't parse the JSON: Extra data: line 1 column 5 (char 4)",
        ),
        ("fromJSON(v.deeper)", "fromJSON() input nests deeper than 64 levels"),
        ("fromJSON(v.deepest)", "fromJSON() input nests deeper than 64 levels"),
        ("fromJSON(v.huge_json)", "fromJSON() input is longer than 1048576 characters"),
        ("lower(v.arr)", "lower() expects a string, got array"),
        ("upper(v.obj)", "upper() expects a string, got object"),
        ("trim(v.arr)", "trim() expects a string, got array"),
        ("lower(v.obj)", "lower() expects a string, got object"),
        ("upper(v.arr)", "upper() expects a string, got array"),
        ("trim(v.obj)", "trim() expects a string, got object"),
        ("length(1)", "length() expects a string, an array, or an object, got number"),
        ("length(true)", "length() expects a string, an array, or an object, got boolean"),
    ],
)
def test_invalid_arguments_raise_with_the_function_name(source: str, message: str) -> None:
    with pytest.raises(EvaluationError) as caught:
        run(source)

    assert caught.value.message == message


@pytest.mark.parametrize(
    "source",
    [
        "format('{0}{0}', v.big)",
        "format('{0}x', v.big)",
        "join(fromJSON('[1, 2]'), v.big)",
        "toJSON(v.big)",
        "toJSON(toJSON(toJSON(toJSON(toJSON(toJSON(toJSON(toJSON(toJSON(toJSON(v.quotes))))))))))",
    ],
)
def test_functions_refuse_to_build_strings_over_one_mebibyte(source: str) -> None:
    with pytest.raises(EvaluationError, match="would return more than 1048576 characters"):
        run(source)


def test_function_results_may_reach_one_mebibyte() -> None:
    assert run("format('{0}', v.big)") == VALUES["big"]
    assert len(str(run("join(v.halves)"))) == MAX_CHARS
    assert len(str(run("toJSON(v.almost)"))) == MAX_CHARS


def test_call_function_reports_its_source_and_offset() -> None:
    with pytest.raises(EvaluationError) as caught:
        call_function("length", [5], source="x: ${{ length(5) }}", offset=7)

    assert (caught.value.source, caught.value.offset) == ("x: ${{ length(5) }}", 7)


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("nope", [], "unknown function 'nope'"),
        ("success", [], "unknown function 'success'"),
        ("contains", ["a"], "wrong number of arguments for contains(): expected 2, got 1"),
        ("lower", ["a", "b"], "wrong number of arguments for lower(): expected 1, got 2"),
        ("join", [[], ",", "x"], "wrong number of arguments for join(): expected 1 or 2, got 3"),
        ("format", [], "wrong number of arguments for format(): expected at least 1, got 0"),
    ],
)
def test_call_function_rejects_unknown_names_and_wrong_arity(
    name: str, args: list[JSONValue], message: str
) -> None:
    with pytest.raises(EvaluationError) as caught:
        call_function(name, args, source="", offset=0)

    assert caught.value.message == message
