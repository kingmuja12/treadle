from __future__ import annotations

import math

import pytest

from treadle.errors import EvaluationError, ExpressionSyntaxError
from treadle.expr.ast import Call, Condition, Const, Expression, Name, Node, Template
from treadle.expr.evaluator import MAX_RESULT_CHARS, EvalContext, render, render_str, render_tree
from treadle.expr.template import compile_tree, has_template, parse_condition, parse_template
from treadle.types import JSONValue, YamlPath

CTX = EvalContext(
    {
        "inputs": {
            "null": None,
            "int": 3,
            "whole": 3.0,
            "float": 0.1,
            "str": "abc",
            "arr": [1, "two", None],
            "obj": {"n": {"m": 5}},
            "payload": '{"a": [1, true]}',
            "big": "a" * MAX_RESULT_CHARS,
        }
    }
)
TOO_LONG = "the rendered string is longer than 1048576 characters"
SINGLE = "an `if` must be a single expression"


def test_parse_template_splits_text_and_expressions_with_offsets() -> None:
    template = parse_template("a ${{ b }} c ${{d}}")

    assert template == Template(
        "a ${{ b }} c ${{d}}",
        (
            "a ",
            Expression(" b ", Name("b", 1, 2), offset=5),
            " c ",
            Expression("d", Name("d", 0, 1), offset=16),
        ),
    )


def test_a_string_without_expressions_is_a_static_template() -> None:
    assert parse_template("plain }} text") == Template("plain }} text", ("plain }} text",))
    assert parse_template("").parts == ()


def test_parse_template_reports_syntax_errors_against_the_whole_text() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_template("url: ${{ inputs.host == }}")

    error = caught.value
    assert (error.source, error.offset) == ("url: ${{ inputs.host == }}", 24)
    assert error.caret() == "url: ${{ inputs.host == }}\n                        ^"


def test_parse_template_rejects_an_unterminated_expression() -> None:
    with pytest.raises(ExpressionSyntaxError, match="unterminated"):
        parse_template("${{ 'open }}")


@pytest.mark.parametrize(
    ("text", "expected"), [("${{", True), ("a ${{ b }}", True), ("$ {{", False), ("}}", False)]
)
def test_has_template(text: str, expected: bool) -> None:
    assert has_template(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${{ inputs.int }}", 3),
        ("${{ inputs.whole }}", 3.0),
        ("${{ inputs.float }}", 0.1),
        ("${{ inputs.null }}", None),
        ("${{ true }}", True),
        ("${{ inputs.str }}", "abc"),
        ("  ${{ inputs.arr }}\n", [1, "two", None]),
        ("${{ fromJSON(inputs.payload) }}", {"a": [1, True]}),
        ("${{ inputs.obj.n }}", {"m": 5}),
    ],
)
def test_a_single_expression_renders_to_its_raw_value(text: str, expected: JSONValue) -> None:
    value = render(parse_template(text), CTX)

    assert value == expected
    assert type(value) is type(expected)


def test_mixed_templates_concatenate_display_strings() -> None:
    template = parse_template(
        "n=${{ inputs.null }}|${{ true }}|${{ 3 }}|${{ inputs.whole }}|${{ inputs.float }}"
        "|${{ 'x' }}|${{ inputs.arr }}|${{ inputs.obj }}"
    )

    assert render(template, CTX) == 'n=|true|3|3|0.1|x|[1,"two",null]|{"n":{"m":5}}'


def test_a_static_template_renders_its_text() -> None:
    assert render(parse_template("just text"), CTX) == "just text"
    assert render(parse_template(""), CTX) == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${{ inputs.int }}", "3"),
        ("${{ inputs.whole }}", "3"),
        ("${{ inputs.null }}", ""),
        ("${{ false }}", "false"),
        (" ${{ inputs.arr }} ", '[1,"two",null]'),
        ("v${{ inputs.int }}", "v3"),
    ],
)
def test_render_str_always_returns_the_display_string(text: str, expected: str) -> None:
    assert render_str(parse_template(text), CTX) == expected


def test_the_quoted_opener_escape_renders_a_literal_opener() -> None:
    assert render(parse_template("${{ '${{' }}"), CTX) == "${{"
    assert render(parse_template("a ${{ '${{' }} b }}"), CTX) == "a ${{ b }}"


def test_render_tree_renders_templates_through_lists_and_mapping_values() -> None:
    tree = compile_tree(
        {
            "count": "${{ inputs.int }}",
            "${{ key }}": "static",
            "items": ["${{ inputs.str }}!", "plain", 2, None, True, 1.5, ["${{ inputs.null }}"]],
            "nested": {"deeper": {"value": "${{ inputs.obj.n.m }}"}},
        }
    )

    assert render_tree(tree, CTX) == {
        "count": 3,
        "${{ key }}": "static",
        "items": ["abc!", "plain", 2, None, True, 1.5, [None]],
        "nested": {"deeper": {"value": 5}},
    }


def test_render_tree_passes_scalars_through() -> None:
    assert render_tree("text", CTX) == "text"
    assert render_tree(7, CTX) == 7
    assert render_tree(None, CTX) is None


def test_compile_tree_turns_template_strings_into_templates() -> None:
    tree = compile_tree({"url": "https://${{ inputs.host }}/x", "list": ["${{ 1 }}", "plain"]})

    assert isinstance(tree, dict)
    assert tree["url"] == parse_template("https://${{ inputs.host }}/x")
    assert tree["list"] == [parse_template("${{ 1 }}"), "plain"]


@pytest.mark.parametrize("value", [None, True, 0, 2.5, "text", [], {}, math.inf])
def test_compile_tree_keeps_json_values_without_templates(value: JSONValue) -> None:
    assert compile_tree(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({1, 2}, "set isn't a JSON value"),
        ((1, 2), "tuple isn't a JSON value"),
        ([b"bytes"], "bytes isn't a JSON value"),
        ({"a": {1: "x"}}, "mapping keys must be strings, got int"),
    ],
)
def test_compile_tree_rejects_values_that_are_not_json(value: object, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        compile_tree(value)


def test_compile_tree_raises_the_first_syntax_error() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        compile_tree({"a": "ok", "b": ["${{ x == }}"]})

    assert caught.value.source == "${{ x == }}"


def test_compile_tree_can_collect_every_syntax_error_with_its_path() -> None:
    errors: list[tuple[YamlPath, ExpressionSyntaxError]] = []

    tree = compile_tree({"a": "${{ ( }}", "b": ["fine ${{ x }}", "${{ y z }}"]}, errors=errors)

    assert [(path, error.message) for path, error in errors] == [
        (("a",), "expected an expression, got the end of the expression"),
        (("b", 1), "unexpected 'z'"),
    ]
    assert tree == {"a": "${{ ( }}", "b": [parse_template("fine ${{ x }}"), "${{ y z }}"]}


@pytest.mark.parametrize(
    ("value", "source", "root", "offset", "uses_status"),
    [
        (True, "true", Const(True, 0, 4), 0, False),
        (False, "false", Const(False, 0, 5), 0, False),
        ("success()", "success()", Call("success", (), 0, 9), 0, True),
        ("  failure()  ", "failure()", Call("failure", (), 0, 9), 2, True),
        ("${{ always() }}", " always() ", Call("always", (), 1, 9), 3, True),
        ("  ${{ inputs }}\n", " inputs ", Name("inputs", 1, 7), 5, False),
        ("inputs", "inputs", Name("inputs", 0, 6), 0, False),
    ],
)
def test_parse_condition_accepts_bare_wrapped_and_boolean_forms(
    value: str | bool, source: str, root: Node, offset: int, uses_status: bool
) -> None:
    condition = parse_condition(value)

    assert condition == Condition(
        value if isinstance(value, str) else source,
        Expression(source, root, offset),
        uses_status,
    )


@pytest.mark.parametrize(
    ("value", "uses_status"),
    [
        ("steps.build.status == 'failed'", False),
        ("always() && steps.build.status == 'failed'", True),
        ("${{ !cancelled() }}", True),
    ],
)
def test_parse_condition_records_whether_status_functions_appear(
    value: str, uses_status: bool
) -> None:
    assert parse_condition(value).uses_status is uses_status


@pytest.mark.parametrize(
    ("value", "message", "offset"),
    [
        ("a == ${{ b }}", SINGLE, 5),
        ("${{ a }} && ${{ b }}", SINGLE, 0),
        ("  ${{ a }} == 'x'", SINGLE, 2),
        ("${{ a }} ${{ b }}", SINGLE, 0),
        ("${{ a }} }}", SINGLE, 0),
        ("", "expected an expression, got the end of the expression", 0),
        ("${{ }}", "expected an expression, got the end of the expression", 4),
        ("  a ==", "expected an expression, got the end of the expression", 6),
        ("${{ a == }}", "expected an expression, got the end of the expression", 9),
    ],
)
def test_parse_condition_rejects_anything_but_one_expression(
    value: str, message: str, offset: int
) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_condition(value)

    assert (caught.value.message, caught.value.offset, caught.value.source) == (
        message,
        offset,
        value,
    )


def test_a_mixed_condition_hints_at_removing_the_template() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_condition("a == ${{ b }}")

    assert caught.value.hint == "write the whole condition as one expression, without ${{ }}"


def test_a_rendered_string_may_be_max_result_chars_long() -> None:
    assert render(parse_template("${{ inputs.big }}"), CTX) == "a" * MAX_RESULT_CHARS
    assert render_str(parse_template("${{ inputs.big }}"), CTX) == "a" * MAX_RESULT_CHARS


@pytest.mark.parametrize(
    "text", ["x${{ inputs.big }}", "${{ inputs.big }}${{ inputs.big }}", "${{ inputs.big }}y"]
)
def test_a_longer_mixed_template_raises(text: str) -> None:
    with pytest.raises(EvaluationError) as caught:
        render(parse_template(text), CTX)

    assert (caught.value.message, caught.value.source, caught.value.offset) == (TOO_LONG, text, 0)


def test_a_longer_single_expression_string_raises() -> None:
    ctx = EvalContext({"big": "a" * (MAX_RESULT_CHARS + 1), "list": ["a" * MAX_RESULT_CHARS]})

    with pytest.raises(EvaluationError, match=TOO_LONG):
        render(parse_template("${{ big }}"), ctx)
    assert render(parse_template("${{ list }}"), ctx) == ["a" * MAX_RESULT_CHARS]
    with pytest.raises(EvaluationError, match=TOO_LONG):
        render_str(parse_template("${{ list }}"), ctx)
