"""The tree-walking evaluator and template rendering.

Section 3 ("Types, operators, and coercion") defines the semantics. Values are plain JSON
values; nothing reaches `eval` or Python attributes.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from treadle.errors import EvaluationError
from treadle.expr.ast import (
    STATUS_FUNCTIONS,
    Binary,
    BinaryOp,
    Call,
    Condition,
    Const,
    Expression,
    Index,
    Member,
    Name,
    Node,
    Not,
    Template,
    TemplateTree,
)
from treadle.expr.functions import _MAX_CHARS, _display, _equals, _kind, _numeric, call_function
from treadle.types import JSONValue


class StatusView(Protocol):
    """Answers the status functions for one step, from its ancestors' statuses."""

    def success(self) -> bool:
        """True when the run isn't cancelling and no ancestor failed without being tolerated."""

    def failure(self) -> bool:
        """True when some ancestor failed or timed out without being tolerated."""

    def cancelled(self) -> bool:
        """True when the run is cancelling, for any reason."""


@dataclass(frozen=True, slots=True)
class EvalContext:
    """The context roots in scope.

    With `status` None, calling a status function raises EvaluationError.
    """

    values: Mapping[str, JSONValue]
    status: StatusView | None = None


MAX_RESULT_CHARS: Final = _MAX_CHARS

_RELATIONS: Final[Mapping[BinaryOp, Callable[[Any, Any], bool]]] = {
    BinaryOp.LT: operator.lt,
    BinaryOp.LE: operator.le,
    BinaryOp.GT: operator.gt,
    BinaryOp.GE: operator.ge,
}


def evaluate(expr: Expression, ctx: EvalContext) -> JSONValue:
    """Evaluate an expression to a JSON value.

    A root name missing from `ctx.values` raises EvaluationError, and so does
    `secrets.NAME` for a secret that isn't defined. Errors carry `expr.source`, with offsets
    shifted by `expr.offset`; `render` and `evaluate_condition` report the whole YAML string.
    """
    return _Evaluator(ctx, expr.source, expr.offset).value(expr.root)


def evaluate_condition(cond: Condition, ctx: EvalContext) -> bool:
    """Evaluate a step `if`, as `success() && (expr)` when it uses no status function.

    When `success()` is false, the expression isn't evaluated.
    """
    evaluator = _Evaluator(ctx, cond.source, cond.expression.offset)
    root = cond.expression.root
    if not cond.uses_status and not evaluator.status(Call("success", (), root.start, root.end)):
        return False
    return truthy(evaluator.value(root))


def render(template: Template, ctx: EvalContext) -> JSONValue:
    """Render a template.

    A single expression yields its raw value, and anything else yields the concatenated
    display strings. Raises EvaluationError past `MAX_RESULT_CHARS`.
    """
    single = template.single
    if single is not None:
        value = _Evaluator(ctx, template.source, single.offset).value(single.root)
        return _bounded(value, template) if isinstance(value, str) else value
    pieces: list[str] = []
    size = 0
    for part in template.parts:
        if isinstance(part, str):
            piece = part
        else:
            piece = _display(_Evaluator(ctx, template.source, part.offset).value(part.root))
        size += len(piece)
        if size > MAX_RESULT_CHARS:
            raise _too_long(template)
        pieces.append(piece)
    return "".join(pieces)


def render_str(template: Template, ctx: EvalContext) -> str:
    """Render a template to a string, even when it is a single expression (env values)."""
    return _bounded(_display(render(template, ctx)), template)


def render_tree(tree: TemplateTree, ctx: EvalContext) -> JSONValue:
    """Render every template in a tree, recursing through lists and mapping values."""
    if isinstance(tree, Template):
        return render(tree, ctx)
    if isinstance(tree, list):
        return [render_tree(item, ctx) for item in tree]
    if isinstance(tree, dict):
        return {key: render_tree(item, ctx) for key, item in tree.items()}
    return tree


def truthy(value: JSONValue) -> bool:
    """False for false, null, 0, 0.0, and ''; True for everything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return value is not None and value != ""


def equals(left: JSONValue, right: JSONValue) -> bool:
    """Compare two values with `==` semantics, which never raise.

    Strings compare case-sensitively, and a number equals a string that is a valid JSON
    number with the same value. Arrays and objects compare element by element with `==`.
    """
    return _equals(left, right)


def compare(
    op: BinaryOp, left: JSONValue, right: JSONValue, *, source: str = "", offset: int = 0
) -> bool:
    """Apply a relational operator to numbers, strings, or a number and a numeric string.

    `op` is `<`, `<=`, `>`, or `>=`. Strings compare by code point. Any other pair of values
    raises EvaluationError at `offset` in `source`, such as "cannot compare null with number".
    """
    relation = _RELATIONS[op]
    if isinstance(left, str) and isinstance(right, str):
        return relation(left, right)
    first, second = _numeric(left), _numeric(right)
    if first is None or second is None:
        raise EvaluationError(
            f"cannot compare {_kind(left)} with {_kind(right)}", source=source, offset=offset
        )
    return relation(first, second)


def to_display(value: JSONValue) -> str:
    """Convert a value to its display string.

    Null is '', booleans are true or false, whole floats drop the decimal point, and arrays
    and objects become compact JSON.
    """
    return _display(value)


class _Evaluator:
    """Evaluates the nodes of one expression, reporting errors in `source` at `offset`."""

    def __init__(self, ctx: EvalContext, source: str, offset: int) -> None:
        self.ctx = ctx
        self.source = source
        self.offset = offset

    def value(self, node: Node) -> JSONValue:
        if isinstance(node, Const):
            return node.value
        if isinstance(node, Name):
            if node.name not in self.ctx.values:
                raise self.error(f"'{node.name}' isn't available here", node)
            return self.ctx.values[node.name]
        if isinstance(node, Member):
            return self.lookup(node, self.value(node.target), node.name)
        if isinstance(node, Index):
            return self.index(node)
        if isinstance(node, Not):
            return not truthy(self.value(node.operand))
        if isinstance(node, Binary):
            return self.binary(node)
        return self.call(node)

    def lookup(self, node: Member | Index, container: JSONValue, key: str) -> JSONValue:
        """Return a member of an object, or null; a missing secret raises instead."""
        if not isinstance(container, dict):
            return None
        if key not in container and isinstance(node.target, Name) and node.target.name == "secrets":
            raise self.error(f"secret {key} is not defined", node)
        return container.get(key)

    def index(self, node: Index) -> JSONValue:
        container, key = self.value(node.target), self.value(node.index)
        if isinstance(container, list):
            position = _array_index(key)
            if position is None:
                shown = _display(key) if isinstance(key, float) else _kind(key)
                raise self.error(f"an array index must be an integer, got {shown}", node.index)
            return container[position] if 0 <= position < len(container) else None
        if isinstance(container, dict):
            # Design note: section 3 gives object keys as strings or numbers only, so any
            # other key type raises, like a bad array index.
            if isinstance(key, bool) or not isinstance(key, (str, int, float)):
                raise self.error(
                    f"an object key must be a string or a number, got {_kind(key)}", node.index
                )
            return self.lookup(node, container, _display(key))
        return None

    def binary(self, node: Binary) -> JSONValue:
        left = self.value(node.left)
        if node.op is BinaryOp.OR:
            return left if truthy(left) else self.value(node.right)
        if node.op is BinaryOp.AND:
            return self.value(node.right) if truthy(left) else left
        right = self.value(node.right)
        if node.op is BinaryOp.EQ:
            return equals(left, right)
        if node.op is BinaryOp.NE:
            return not equals(left, right)
        return compare(node.op, left, right, source=self.source, offset=self.offset + node.start)

    def call(self, node: Call) -> JSONValue:
        if node.name in STATUS_FUNCTIONS:
            return self.status(node)
        args = [self.value(arg) for arg in node.args]
        return call_function(node.name, args, source=self.source, offset=self.offset + node.start)

    def status(self, node: Call) -> bool:
        """Answer a status function from the context's StatusView."""
        view = self.ctx.status
        if view is None:
            raise self.error(f"{node.name}() is only available in a step `if`", node)
        if node.args:
            raise self.error(f"{node.name}() takes no arguments", node)
        if node.name == "always":
            return True
        answers = {"success": view.success, "failure": view.failure, "cancelled": view.cancelled}
        return answers[node.name]()

    def error(self, message: str, node: Node) -> EvaluationError:
        return EvaluationError(message, source=self.source, offset=self.offset + node.start)


def _array_index(key: JSONValue) -> int | None:
    """Return the integer value of an array index, or None for any other value."""
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key
    if isinstance(key, float) and key.is_integer():
        return int(key)
    return None


def _bounded(text: str, template: Template) -> str:
    if len(text) > MAX_RESULT_CHARS:
        raise _too_long(template)
    return text


def _too_long(template: Template) -> EvaluationError:
    return EvaluationError(
        f"the rendered string is longer than {MAX_RESULT_CHARS} characters",
        source=template.source,
        offset=0,
    )
