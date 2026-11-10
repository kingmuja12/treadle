"""Templates (`${{ }}` inside strings) and step conditions (section 3, "Rendering templates")."""

from __future__ import annotations

from typing import Final, TypeAlias

from treadle.errors import ExpressionSyntaxError
from treadle.expr.analysis import uses_status
from treadle.expr.ast import Condition, Const, Expression, Template, TemplateTree
from treadle.expr.lexer import scan_template
from treadle.expr.parser import parse_expression
from treadle.types import YamlPath

_OPEN: Final = "${{"
_CLOSE: Final = "}}"

_TreeErrors: TypeAlias = list[tuple[YamlPath, ExpressionSyntaxError]]


def has_template(text: str) -> bool:
    """True when `text` contains `${{`."""
    return _OPEN in text


def parse_template(text: str) -> Template:
    """Split `text` into literal parts and parsed expressions.

    Raises ExpressionSyntaxError with offsets relative to `text`, and with `text` as the
    error's `source`, so `caret()` points into the YAML string.
    """
    parts: list[str | Expression] = []
    position = 0
    for start, end in scan_template(text):
        if start > position:
            parts.append(text[position:start])
        parts.append(_parse_slice(text, start + len(_OPEN), end - len(_CLOSE)))
        position = end
    if position < len(text):
        parts.append(text[position:])
    return Template(text, tuple(parts))


def compile_tree(
    value: object, *, errors: list[tuple[YamlPath, ExpressionSyntaxError]] | None = None
) -> TemplateTree:
    """Turn every string that contains `${{` into a Template.

    It recurses through lists and mapping values, never keys. Raises TypeError for values
    that aren't JSON, including mapping keys that aren't strings. A template that doesn't
    parse raises ExpressionSyntaxError, unless `errors` is a list: then each failure is
    appended with its path inside `value`, the string stays as it is, and compiling goes on.
    """
    return _compile(value, (), errors)


def parse_condition(value: str | bool) -> Condition:
    """Parse a step `if`: a bare expression, a string that is exactly one `${{ }}`, or a bool.

    Whitespace is stripped. Any other string containing `${{` raises ExpressionSyntaxError
    ("an `if` must be a single expression") at its first `${{`. `Condition.source` is `value`
    as written, and the expression's offset indexes it.
    """
    if isinstance(value, bool):
        text = "true" if value else "false"
        return Condition(text, Expression(text, Const(value, 0, len(text))), uses_status=False)
    stripped = value.strip()
    expression: Expression | None = None
    if not has_template(value):
        lead = len(value) - len(value.lstrip())
        expression = _parse_slice(value, lead, lead + len(stripped))
    elif stripped.startswith(_OPEN) and stripped.endswith(_CLOSE):
        expression = parse_template(value).single
    if expression is None:
        raise ExpressionSyntaxError(
            "an `if` must be a single expression",
            source=value,
            offset=value.find(_OPEN),
            hint="write the whole condition as one expression, without ${{ }}",
        )
    return Condition(value, expression, uses_status(expression))


def _parse_slice(text: str, start: int, end: int) -> Expression:
    """Parse `text[start:end]` as an expression, reporting errors against all of `text`."""
    # Design note: section 3 calls an error's `source` the expression text, but its `offset`
    # indexes the YAML string, and `ExpressionError.caret()` indexes `source` with `offset`.
    # Errors therefore carry the YAML string whenever it is known, so the two agree.
    try:
        return parse_expression(text[start:end], offset=start)
    except ExpressionSyntaxError as error:
        raise ExpressionSyntaxError(
            error.message, source=text, offset=error.offset, hint=error.hint
        ) from None


def _compile(value: object, path: YamlPath, errors: _TreeErrors | None) -> TemplateTree:
    if isinstance(value, str):
        return _compile_string(value, path, errors)
    # Design note: non-finite floats pass through, because schema validation reports them
    # (E104) after the loader compiles a tree.
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_compile(item, (*path, index), errors) for index, item in enumerate(value)]
    if isinstance(value, dict):
        compiled: dict[str, TemplateTree] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"mapping keys must be strings, got {type(key).__name__}")
            compiled[key] = _compile(item, (*path, key), errors)
        return compiled
    raise TypeError(f"{type(value).__name__} isn't a JSON value")


def _compile_string(text: str, path: YamlPath, errors: _TreeErrors | None) -> TemplateTree:
    if not has_template(text):
        return text
    try:
        return parse_template(text)
    except ExpressionSyntaxError as error:
        if errors is None:
            raise
        errors.append((path, error))
        return text
