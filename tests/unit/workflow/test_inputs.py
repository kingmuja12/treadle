from __future__ import annotations

import math

import pytest
from tests.support.builders import make_input

from treadle.errors import InputError
from treadle.models import InputSpec, InputType
from treadle.types import JSONValue
from treadle.workflow.inputs import coerce_inputs, parse_input_pairs

STRING = make_input("value", InputType.STRING)
NUMBER = make_input("value", InputType.NUMBER)
INTEGER = make_input("value", InputType.INTEGER)
BOOLEAN = make_input("value", InputType.BOOLEAN)
CHOICE = make_input("value", InputType.CHOICE, options=["low", "high"])


def coerce_one(spec: InputSpec, value: object, *, from_strings: bool) -> JSONValue:
    return coerce_inputs([spec], {"value": value}, from_strings=from_strings)["value"]


def problem(spec: InputSpec, value: object, *, from_strings: bool) -> str:
    with pytest.raises(InputError) as raised:
        coerce_inputs([spec], {"value": value}, from_strings=from_strings)
    return raised.value.errors["value"]


def test_pairs_split_at_the_first_equals_sign() -> None:
    pairs = ["name=Ada", "empty=", "query=a=b", "name=Grace"]

    assert parse_input_pairs(pairs) == {"name": "Grace", "empty": "", "query": "a=b"}


def test_bad_pairs_are_collected_into_one_error() -> None:
    with pytest.raises(InputError) as raised:
        parse_input_pairs(["flag", "=value", "ok=1"])

    assert raised.value.errors == {
        "flag": "has no value (write flag=VALUE)",
        "": "'=value' has no input name (write NAME=VALUE)",
    }
    assert str(raised.value) == (
        "input 'flag' has no value (write flag=VALUE); "
        "'=value' has no input name (write NAME=VALUE)"
    )


@pytest.mark.parametrize(
    ("spec", "text", "expected"),
    [
        pytest.param(STRING, "hello", "hello", id="string"),
        pytest.param(STRING, "", "", id="empty-string"),
        pytest.param(STRING, "42", "42", id="string-of-digits"),
        pytest.param(NUMBER, "42", 42, id="number-integer"),
        pytest.param(NUMBER, "-2.5", -2.5, id="number-float"),
        pytest.param(NUMBER, "1e3", 1000.0, id="number-exponent"),
        pytest.param(NUMBER, "0", 0, id="number-zero"),
        pytest.param(INTEGER, "42", 42, id="integer"),
        pytest.param(INTEGER, "+7", 7, id="integer-plus"),
        pytest.param(INTEGER, "-007", -7, id="integer-leading-zeros"),
        pytest.param(BOOLEAN, "true", True, id="boolean-true"),
        pytest.param(BOOLEAN, "FALSE", False, id="boolean-upper"),
        pytest.param(BOOLEAN, "True", True, id="boolean-title"),
        pytest.param(CHOICE, "high", "high", id="choice"),
    ],
)
def test_string_sources_coerce_valid_text(spec: InputSpec, text: str, expected: object) -> None:
    value = coerce_one(spec, text, from_strings=True)

    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    ("spec", "text", "message"),
    [
        pytest.param(NUMBER, "NaN", "must be a number", id="nan"),
        pytest.param(NUMBER, "Infinity", "must be a number", id="infinity"),
        pytest.param(NUMBER, "1e999", "must be a finite number", id="overflow"),
        pytest.param(NUMBER, "+1", "must be a number", id="json-has-no-plus"),
        pytest.param(NUMBER, "01", "must be a number", id="json-has-no-leading-zero"),
        pytest.param(NUMBER, "1.", "must be a number", id="json-needs-fraction-digits"),
        pytest.param(NUMBER, " 1", "must be a number", id="number-whitespace"),
        pytest.param(NUMBER, "", "must be a number", id="number-empty"),
        pytest.param(NUMBER, "1" * 5000, "is too large", id="number-too-long"),
        pytest.param(INTEGER, "1.0", "must be an integer", id="integer-fraction"),
        pytest.param(INTEGER, "1e3", "must be an integer", id="integer-exponent"),
        pytest.param(INTEGER, "١٢", "must be an integer", id="integer-non-ascii-digits"),
        pytest.param(INTEGER, "5\n", "must be an integer", id="integer-newline"),
        pytest.param(INTEGER, "9" * 5000, "is too large", id="integer-too-long"),
        pytest.param(BOOLEAN, "yes", "must be true or false", id="boolean-yes"),
        pytest.param(BOOLEAN, "1", "must be true or false", id="boolean-digit"),
        pytest.param(CHOICE, "High", "must be one of: low, high", id="choice-case"),
        pytest.param(STRING, 5, "must be text", id="not-text"),
    ],
)
def test_string_sources_reject_bad_text(spec: InputSpec, text: object, message: str) -> None:
    assert problem(spec, text, from_strings=True) == message


@pytest.mark.parametrize(
    ("spec", "value", "expected"),
    [
        pytest.param(STRING, "hello", "hello", id="string"),
        pytest.param(CHOICE, "low", "low", id="choice"),
        pytest.param(BOOLEAN, False, False, id="boolean"),
        pytest.param(NUMBER, 3, 3, id="number-int"),
        pytest.param(NUMBER, 2.5, 2.5, id="number-float"),
        pytest.param(INTEGER, 4, 4, id="integer"),
        pytest.param(INTEGER, 4.0, 4, id="integer-whole-float"),
    ],
)
def test_json_sources_accept_matching_types(
    spec: InputSpec, value: object, expected: object
) -> None:
    coerced = coerce_one(spec, value, from_strings=False)

    assert coerced == expected
    assert type(coerced) is type(expected)


@pytest.mark.parametrize(
    ("spec", "value", "message"),
    [
        pytest.param(STRING, 5, "must be a string", id="string-number"),
        pytest.param(STRING, True, "must be a string", id="string-bool"),
        pytest.param(CHOICE, "medium", "must be one of: low, high", id="choice-unknown"),
        pytest.param(CHOICE, 1, "must be a string", id="choice-number"),
        pytest.param(BOOLEAN, "true", "must be true or false", id="boolean-string"),
        pytest.param(BOOLEAN, 1, "must be true or false", id="boolean-int"),
        pytest.param(NUMBER, True, "must be a number", id="number-bool"),
        pytest.param(NUMBER, "3", "must be a number", id="number-string"),
        pytest.param(NUMBER, math.inf, "must be a finite number", id="number-infinity"),
        pytest.param(NUMBER, math.nan, "must be a finite number", id="number-nan"),
        pytest.param(INTEGER, False, "must be an integer", id="integer-bool"),
        pytest.param(INTEGER, 4.5, "must be an integer", id="integer-fraction"),
        pytest.param(INTEGER, math.inf, "must be an integer", id="integer-infinity"),
        pytest.param(INTEGER, [4], "must be an integer", id="integer-list"),
    ],
)
def test_json_sources_reject_other_types(spec: InputSpec, value: object, message: str) -> None:
    assert problem(spec, value, from_strings=False) == message


@pytest.mark.parametrize("from_strings", [True, False])
def test_defaults_fill_inputs_that_arent_supplied(from_strings: bool) -> None:
    specs = [
        make_input("name", default="world"),
        make_input("count", InputType.INTEGER, default=3),
        make_input("note"),
    ]

    assert coerce_inputs(specs, {}, from_strings=from_strings) == {
        "name": "world",
        "count": 3,
        "note": None,
    }


def test_a_null_value_counts_as_not_supplied() -> None:
    specs = [make_input("name", default="world"), make_input("note")]

    assert coerce_inputs(specs, {"name": None, "note": None}, from_strings=False) == {
        "name": "world",
        "note": None,
    }


def test_the_result_follows_declaration_order() -> None:
    specs = [make_input("b"), make_input("a")]

    assert list(coerce_inputs(specs, {"a": "1", "b": "2"}, from_strings=True)) == ["b", "a"]


def test_every_problem_is_reported_at_once() -> None:
    specs = [
        make_input("name", required=True),
        make_input("count", InputType.INTEGER),
        make_input("level", InputType.CHOICE, options=["low"]),
    ]

    with pytest.raises(InputError) as raised:
        coerce_inputs(specs, {"nmae": "x", "count": "many", "extra": "1"}, from_strings=True)

    assert raised.value.errors == {
        "nmae": "isn't declared (did you mean 'name'?)",
        "extra": "isn't declared",
        "name": "is required",
        "count": "must be an integer",
    }


def test_a_required_input_with_a_null_value_is_missing() -> None:
    with pytest.raises(InputError) as raised:
        coerce_inputs([make_input("name", required=True)], {"name": None}, from_strings=False)

    assert raised.value.errors == {"name": "is required"}
