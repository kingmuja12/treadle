from __future__ import annotations

import pytest

from treadle.expr.analysis import Reference, calls, references, tree_expressions, uses_status, walk
from treadle.expr.ast import Expression
from treadle.expr.parser import parse_expression
from treadle.expr.template import compile_tree, parse_template


def test_walk_visits_parents_before_children_in_source_order() -> None:
    root = parse_expression("f(a.b, !c) == d[e]").root

    assert [type(node).__name__ for node in walk(root)] == [
        "Binary",
        "Call",
        "Member",
        "Name",
        "Not",
        "Name",
        "Index",
        "Name",
        "Name",
    ]


@pytest.mark.parametrize(
    ("source", "paths"),
    [
        ("inputs.name", [("inputs", "name")]),
        ("env", [("env",)]),
        ("steps.build.outputs.tag", [("steps", "build", "outputs", "tag")]),
        ("steps['build'].outputs[0]", [("steps", "build", "outputs", "0")]),
        ("a[-1]", [("a", "-1")]),
        ("trigger.body[inputs.key]", [("trigger", "body"), ("inputs", "key")]),
        ("trigger.body[inputs.key].x", [("trigger", "body"), ("inputs", "key")]),
        ("a[1.5]", [("a",)]),
        ("a[true].b", [("a",)]),
        ("a[null]", [("a",)]),
        ("x.true.null", [("x", "true", "null")]),
        ("fromJSON(env.DATA).x", [("env", "DATA")]),
        ("success() && steps.a.status == 'failed'", [("steps", "a", "status")]),
        ("!inputs.flag || result.status", [("inputs", "flag"), ("result", "status")]),
        ("'literal' == 3", []),
    ],
)
def test_references_follow_static_chains_from_a_root_name(
    source: str, paths: list[tuple[str, ...]]
) -> None:
    assert [reference.path for reference in references(parse_expression(source))] == paths


def test_reference_offsets_index_the_expression_source() -> None:
    expression = parse_template("x ${{ steps.a.outputs.b || inputs.c }}").parts[1]
    assert isinstance(expression, Expression)

    assert references(expression) == [
        Reference(("steps", "a", "outputs", "b"), 1, 18),
        Reference(("inputs", "c"), 22, 30),
    ]
    assert expression.source[1:18] == "steps.a.outputs.b"


def test_calls_lists_every_call_outermost_first() -> None:
    expression = parse_expression("format('{0}', join(x, lower(y))) || f()")

    assert [call.name for call in calls(expression)] == ["format", "join", "lower", "f"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("success()", True),
        ("always() || x", True),
        ("!cancelled()", True),
        ("contains(x, failure())", True),
        ("x == 'success'", False),
        ("success", False),
        ("format('{0}', x)", False),
    ],
)
def test_uses_status_finds_status_function_calls(source: str, expected: bool) -> None:
    assert uses_status(parse_expression(source)) is expected


def test_tree_expressions_yields_the_path_of_every_expression() -> None:
    tree = compile_tree(
        {
            "url": "${{ a }}/${{ b }}",
            "list": ["plain", "${{ c }}", 3, None],
            "nested": {"key": ["${{ d }}"]},
        }
    )

    found = [(path, expression.source.strip()) for path, expression in tree_expressions(tree)]

    assert found == [
        (("url",), "a"),
        (("url",), "b"),
        (("list", 1), "c"),
        (("nested", "key", 0), "d"),
    ]


def test_tree_expressions_prefixes_the_given_path() -> None:
    found = [path for path, _ in tree_expressions(parse_template("${{ x }}"), ("steps", 0, "if"))]

    assert found == [("steps", 0, "if")]
    assert list(tree_expressions("static text", ("env", "A"))) == []
