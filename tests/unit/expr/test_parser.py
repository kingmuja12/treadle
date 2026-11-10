from __future__ import annotations

from collections.abc import Callable

import pytest

from treadle.errors import ExpressionSyntaxError
from treadle.expr.ast import Binary, BinaryOp, Call, Const, Index, Member, Name, Node, Not
from treadle.expr.parser import MAX_DEPTH, MAX_SOURCE, parse_expression

CHAINED = "comparisons can't be chained"
TOO_DEEP = "the expression is nested more than 32 levels deep"
END = "the end of the expression"


def tree(node: Node) -> str:
    """Write a syntax tree as a compact prefix expression, for readable tables."""
    if isinstance(node, Const):
        return repr(node.value)
    if isinstance(node, Name):
        return node.name
    if isinstance(node, Member):
        return f"{tree(node.target)}.{node.name}"
    if isinstance(node, Index):
        return f"{tree(node.target)}[{tree(node.index)}]"
    if isinstance(node, Not):
        return f"(! {tree(node.operand)})"
    if isinstance(node, Binary):
        return f"({node.op.value} {tree(node.left)} {tree(node.right)})"
    return f"{node.name}({', '.join(tree(arg) for arg in node.args)})"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a || b && c", "(|| a (&& b c))"),
        ("a && b || c", "(|| (&& a b) c)"),
        ("a || b || c", "(|| (|| a b) c)"),
        ("a && b && c", "(&& (&& a b) c)"),
        ("a == b && c != d", "(&& (== a b) (!= c d))"),
        ("a < b == c >= d", "(== (< a b) (>= c d))"),
        ("a <= b != c > d", "(!= (<= a b) (> c d))"),
        ("!a == b", "(== (! a) b)"),
        ("!a < b", "(< (! a) b)"),
        ("!a.b[0]", "(! a.b[0])"),
        ("!!a", "(! (! a))"),
        ("(a || b) && c", "(&& (|| a b) c)"),
        ("!(a < b)", "(! (< a b))"),
        ("(a == b) == c", "(== (== a b) c)"),
        ("a.b.c", "a.b.c"),
        ("a['b'].c[1]", "a['b'].c[1]"),
        ("a[b[c]]", "a[b[c]]"),
        ("a[b || c]", "a[(|| b c)]"),
        ("f()", "f()"),
        ("f (a)", "f(a)"),
        ("f(a, b.c, 'd')", "f(a, b.c, 'd')"),
        ("format('{0}', a == b)", "format('{0}', (== a b))"),
        ("fromJSON(x).y[0]", "fromJSON(x).y[0]"),
        ("(a).b", "a.b"),
        ("'text'.length", "'text'.length"),
        ("a.true", "a.true"),
        ("a.false.null", "a.false.null"),
        ("steps.build-image.outputs.tag", "steps.build-image.outputs.tag"),
        ("true", "True"),
        ("null", "None"),
        ("-1.5", "-1.5"),
        ("a <= -1", "(<= a -1)"),
        ("'it''s'", '"it\'s"'),
    ],
)
def test_parse_expression_applies_the_grammar_and_precedence(source: str, expected: str) -> None:
    assert tree(parse_expression(source).root) == expected


def test_nodes_carry_offsets_into_the_expression_source() -> None:
    expression = parse_expression(" !a.b == f(1, x['k']) ", offset=7)

    assert (expression.source, expression.offset) == (" !a.b == f(1, x['k']) ", 7)
    assert expression.root == Binary(
        BinaryOp.EQ,
        Not(Member(Name("a", 2, 3), "b", 2, 5), 1, 5),
        Call("f", (Const(1, 11, 12), Index(Name("x", 14, 15), Const("k", 16, 19), 14, 20)), 9, 21),
        1,
        21,
    )


@pytest.mark.parametrize(
    ("source", "message", "offset", "hint"),
    [
        ("a < b < c", CHAINED, 6, "combine comparisons with &&"),
        ("a == b != c", CHAINED, 7, "combine comparisons with &&"),
        ("a >= b > c", CHAINED, 7, "combine comparisons with &&"),
        ("a == b == c || d", CHAINED, 7, "combine comparisons with &&"),
        ("", f"expected an expression, got {END}", 0, None),
        ("   ", f"expected an expression, got {END}", 3, None),
        ("a b", "unexpected 'b'", 2, None),
        ("a 'b'", "unexpected 'b'", 2, None),
        ("a -1", "unexpected '-1'", 2, None),
        ("a &&", f"expected an expression, got {END}", 4, None),
        ("== a", "expected an expression, got '=='", 0, None),
        ("!", f"expected an expression, got {END}", 1, None),
        (",", "expected an expression, got ','", 0, None),
        ("(a", f"expected ')', got {END}", 2, None),
        ("(a]", "expected ')', got ']'", 2, None),
        ("a)", "unexpected ')'", 1, None),
        ("()", "expected an expression, got ')'", 1, None),
        ("a[1", f"expected ']', got {END}", 3, None),
        ("a[]", "expected an expression, got ']'", 2, None),
        ("a.", f"expected a property name, got {END}", 2, None),
        ("a.1", "expected a property name, got '1'", 2, None),
        ("a.'b'", "expected a property name, got 'b'", 2, None),
        ("a..b", "expected a property name, got '.'", 2, None),
        ("f(a b)", "expected ',' or ')', got 'b'", 4, None),
        ("f(a", f"expected ',' or ')', got {END}", 3, None),
        ("f(a,)", "expected an expression, got ')'", 4, None),
        ("f(,)", "expected an expression, got ','", 2, None),
        ("a.b()", "unexpected '('", 3, None),
        ("f(1)(2)", "unexpected '('", 4, None),
        ('a == "b"', "unexpected character '\"'", 5, "use single quotes"),
    ],
)
def test_invalid_expressions_raise_with_exact_offsets_and_hints(
    source: str, message: str, offset: int, hint: str | None
) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_expression(source)

    error = caught.value
    assert (error.message, error.offset, error.hint) == (message, offset, hint)


def test_error_offsets_are_shifted_into_the_yaml_string() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_expression("a < b < c", offset=12)

    assert caught.value.offset == 18


def test_an_expression_may_be_max_source_characters_long() -> None:
    source = "a" + " " * (MAX_SOURCE - 1)

    assert parse_expression(source).root == Name("a", 0, 1)


def test_a_longer_expression_is_rejected_at_its_start() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_expression("a" + " " * MAX_SOURCE, offset=5)

    assert caught.value.message == "the expression is longer than 4096 characters"
    assert caught.value.offset == 5


NESTINGS: dict[str, Callable[[int], str]] = {
    "parentheses": lambda depth: "(" * depth + "a" + ")" * depth,
    "indexes": lambda depth: "a[" * depth + "0" + "]" * depth,
    "calls": lambda depth: "f(" * depth + "a" + ")" * depth,
    "negations": lambda depth: "!" * depth + "a",
    "members": lambda depth: "a" + ".b" * depth,
    "operators": lambda depth: " || ".join(["a"] * (depth + 1)),
}


@pytest.mark.parametrize("make", NESTINGS.values(), ids=NESTINGS.keys())
def test_nesting_up_to_max_depth_parses(make: Callable[[int], str]) -> None:
    parse_expression(make(MAX_DEPTH))


@pytest.mark.parametrize(
    ("kind", "offset"),
    [
        ("parentheses", 32),
        ("indexes", 65),
        ("calls", 65),
        ("negations", 0),
        ("members", 65),
        ("operators", 162),
    ],
)
def test_nesting_beyond_max_depth_is_rejected_where_it_overflows(kind: str, offset: int) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        parse_expression(NESTINGS[kind](MAX_DEPTH + 1))

    assert (caught.value.message, caught.value.offset) == (TOO_DEEP, offset)
