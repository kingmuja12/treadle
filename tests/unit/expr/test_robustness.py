"""Seeded random inputs: parsing raises only syntax errors, and evaluation only its own errors."""

from __future__ import annotations

import random
from collections.abc import Callable

from treadle.errors import EvaluationError, ExpressionSyntaxError
from treadle.expr.evaluator import EvalContext, evaluate, render
from treadle.expr.functions import FUNCTIONS
from treadle.expr.parser import parse_expression
from treadle.expr.template import parse_condition, parse_template

TOKENS = (
    "inputs",
    "steps",
    "a-b",
    "_x",
    "true",
    "false",
    "null",
    "0",
    "42",
    "-3",
    "1.5",
    "2e9",
    "'s'",
    "''",
    "'it''s'",
    "==",
    "!=",
    "<",
    "<=",
    ">",
    ">=",
    "&&",
    "||",
    "!",
    ".",
    "[",
    "]",
    "(",
    ")",
    ",",
    " ",
)
JUNK = ("1e999", "01", "1.", "'open", '"dq"', "-", "=", "&", "|", "$", "${{", "}}", "@", "é", "\t")
LEAVES = (
    "v.n",
    "v.s",
    "v.num",
    "v.arr",
    "v.obj",
    "v.none",
    "v.arr[0]",
    "v.obj['k']",
    "v.missing",
    "1",
    "-2.5",
    "0",
    "'x'",
    "''",
    "'3'",
    "true",
    "false",
    "null",
)
OPERATORS = ("||", "&&", "==", "!=", "<", "<=", ">", ">=")
NAMES = tuple(name for name, spec in FUNCTIONS.items() if not spec.status)
CTX = EvalContext(
    {
        "v": {
            "n": 3,
            "s": "abc",
            "num": "7",
            "arr": [1, "two", None, [3]],
            "obj": {"k": "v", "1": [True]},
            "none": None,
        }
    }
)


def token_soup(rng: random.Random) -> str:
    """Join random tokens, mostly grammatical ones, so most strings reach the parser."""
    pieces = (
        rng.choice(JUNK if rng.random() < 0.05 else TOKENS) for _ in range(rng.randint(0, 24))
    )
    return "".join(pieces)


def mutated_expression(rng: random.Random) -> str:
    """Return a valid expression with a short slice deleted, repeated, or replaced by a token."""
    source = random_expression(rng)
    position = rng.randrange(len(source) + 1)
    width = rng.randint(1, 3)
    edits = (
        source[:position] + source[position + width :],
        source[:position] + source[position : position + width] * 2 + source[position:],
        source[:position] + rng.choice(TOKENS + JUNK) + source[position + width :],
    )
    return rng.choice(edits)


def random_expression(rng: random.Random, depth: int = 0) -> str:
    """Build a grammatical expression; comparisons are parenthesized so they never chain."""
    if depth > 3 or rng.random() < 0.3:
        return rng.choice(LEAVES)
    shape = rng.randrange(5)
    if shape == 0:
        left, right = random_expression(rng, depth + 1), random_expression(rng, depth + 1)
        return f"({left} {rng.choice(OPERATORS)} {right})"
    if shape == 1:
        return f"!{random_expression(rng, depth + 1)}"
    if shape == 2:
        name = rng.choice(NAMES)
        spec = FUNCTIONS[name]
        count = rng.randint(spec.min_args, spec.max_args or spec.min_args + 2)
        args = ", ".join(random_expression(rng, depth + 1) for _ in range(count))
        return f"{name}({args})"
    if shape == 3:
        return f"{random_expression(rng, depth + 1)}[{random_expression(rng, depth + 1)}]"
    return f"({random_expression(rng, depth + 1)}).{rng.choice(('k', 'n', 'true', 'x-y'))}"


def is_json(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(map(is_json, value))
    return isinstance(value, dict) and all(
        isinstance(key, str) and is_json(item) for key, item in value.items()
    )


def syntax_error(parse: Callable[[str], object], text: str) -> ExpressionSyntaxError | None:
    """Return the syntax error that parsing `text` raises, or None when it parses.

    Any other exception escapes and fails the test.
    """
    try:
        parse(text)
    except ExpressionSyntaxError as error:
        return error
    return None


def test_random_token_strings_raise_only_syntax_errors() -> None:
    rng = random.Random(1234)
    sources = [token_soup(rng) for _ in range(1000)] + [
        mutated_expression(rng) for _ in range(1000)
    ]
    errors = [syntax_error(parse_expression, source) for source in sources]

    assert all(0 <= error.offset <= len(error.source) for error in errors if error is not None)
    assert None in errors
    assert any(error is not None for error in errors)


def test_random_templates_and_conditions_raise_only_syntax_errors() -> None:
    rng = random.Random(1234)
    texts = [token_soup(rng) for _ in range(1000)] + [
        f"a ${{{{ {mutated_expression(rng)} }}}} b" for _ in range(1000)
    ]

    for parse in (parse_template, parse_condition):
        for text in texts:
            error = syntax_error(parse, text)
            assert error is None or 0 <= error.offset <= len(text), text


def test_random_expressions_evaluate_to_json_or_raise_evaluation_errors() -> None:
    rng = random.Random(1234)
    evaluated = 0

    for _ in range(2000):
        source = random_expression(rng)
        try:
            value = evaluate(parse_expression(source), CTX)
            rendered = render(parse_template(f"<${{{{ {source} }}}}>"), CTX)
        except EvaluationError:
            continue
        assert is_json(value), source
        assert isinstance(rendered, str), source
        evaluated += 1

    assert evaluated > 500
