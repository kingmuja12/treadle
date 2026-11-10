"""Static analysis of parsed expressions, for the loader's checks and lint warnings."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypeGuard

from treadle.expr.ast import (
    STATUS_FUNCTIONS,
    Binary,
    Call,
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
from treadle.types import YamlPath


@dataclass(frozen=True, slots=True)
class Reference:
    """A static chain of member accesses from a root name.

    For example, `("steps", "build", "outputs", "tag")`. `start` and `end` index
    `Expression.source`.
    """

    path: tuple[str, ...]
    start: int
    end: int


def walk(node: Node) -> Iterator[Node]:
    """Yield `node` and every node below it, parents before children, in source order."""
    yield node
    for child in _children(node):
        yield from walk(child)


def references(expr: Expression) -> list[Reference]:
    """Return every context reference in `expr`.

    A reference follows `Member` nodes, and `Index` nodes whose index is a string or integer
    `Const`, from a `Name`. It stops at the first dynamic index, so `trigger.body[inputs.key]`
    yields `("trigger", "body")` and `("inputs", "key")`.
    """
    found: list[Reference] = []
    _collect(expr.root, found)
    return found


def calls(expr: Expression) -> list[Call]:
    """Return every function call in `expr`, outermost first."""
    return [node for node in walk(expr.root) if isinstance(node, Call)]


def uses_status(expr: Expression) -> bool:
    """True when `expr` calls a status function: success, failure, always, or cancelled."""
    return any(call.name in STATUS_FUNCTIONS for call in calls(expr))


def tree_expressions(
    tree: TemplateTree, path: YamlPath = ()
) -> Iterator[tuple[YamlPath, Expression]]:
    """Yield the YAML path and expression of every template inside a template tree."""
    if isinstance(tree, Template):
        for part in tree.parts:
            if isinstance(part, Expression):
                yield path, part
    elif isinstance(tree, list):
        for index, item in enumerate(tree):
            yield from tree_expressions(item, (*path, index))
    elif isinstance(tree, dict):
        for key, item in tree.items():
            yield from tree_expressions(item, (*path, key))


def _children(node: Node) -> tuple[Node, ...]:
    match node:
        case Member(target=child) | Not(operand=child):
            return (child,)
        case Index(target=left, index=right) | Binary(left=left, right=right):
            return (left, right)
        case Call(args=args):
            return args
    return ()


def _collect(node: Node, found: list[Reference]) -> None:
    """Append the references in `node`, taking each static chain whole."""
    path = _static_path(node)
    if path is not None:
        found.append(Reference(path, node.start, node.end))
        return
    for child in _children(node):
        _collect(child, found)


def _static_path(node: Node) -> tuple[str, ...] | None:
    """Return the path of a chain of static accesses that starts at a name, or None."""
    keys: list[str] = []
    while not isinstance(node, Name):
        if isinstance(node, Member):
            keys.append(node.name)
        elif isinstance(node, Index) and _is_static_key(node.index):
            keys.append(str(node.index.value))
        else:
            return None
        node = node.target
    return (node.name, *reversed(keys))


def _is_static_key(node: Node) -> TypeGuard[Const]:
    """True for a string or integer literal, which a reference can follow."""
    if not isinstance(node, Const):
        return False
    return isinstance(node.value, str) or (
        isinstance(node.value, int) and not isinstance(node.value, bool)
    )
