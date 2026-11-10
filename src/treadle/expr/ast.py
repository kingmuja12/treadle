"""Syntax-tree data for expressions, templates, and conditions.

This module holds data only: `expr.parser` builds these nodes and `expr.evaluator` evaluates
them. Node offsets (`start`, `end`) index the source of the `Expression` that holds them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from treadle.types import JSONScalar


class BinaryOp(StrEnum):
    """The binary operators, by their source spelling."""

    OR = "||"
    AND = "&&"
    EQ = "=="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="


@dataclass(frozen=True, slots=True)
class Const:
    """A literal: null, a boolean, a number, or a string."""

    value: JSONScalar
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Name:
    """A bare name, which is a context root such as `inputs` or `steps`."""

    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Member:
    """Property access, `target.name`."""

    target: Node
    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Index:
    """Index access, `target[index]`."""

    target: Node
    index: Node
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Not:
    """Logical negation, `!operand`."""

    operand: Node
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Binary:
    """A binary operation, such as `left == right`."""

    op: BinaryOp
    left: Node
    right: Node
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Call:
    """A function call, `name(args...)`."""

    name: str
    args: tuple[Node, ...]
    start: int
    end: int


Node: TypeAlias = Const | Name | Member | Index | Not | Binary | Call

STATUS_FUNCTIONS: Final[frozenset[str]] = frozenset({"success", "failure", "always", "cancelled"})


@dataclass(frozen=True, slots=True)
class Expression:
    """A parsed expression.

    `source` is the expression text without `${{ }}`, and `offset` is where `source` starts
    inside the YAML string that holds it.
    """

    source: str
    root: Node
    offset: int = 0


@dataclass(frozen=True, slots=True)
class Template:
    """A string split into literal text and `${{ }}` expressions, in source order."""

    source: str
    parts: tuple[str | Expression, ...]

    @property
    def is_static(self) -> bool:
        """True when the template holds no expression."""
        return all(isinstance(part, str) for part in self.parts)

    @property
    def single(self) -> Expression | None:
        """The expression when the template is exactly one, with only whitespace around it.

        Such a template renders to the expression's raw value instead of a string.
        """
        expressions = [part for part in self.parts if isinstance(part, Expression)]
        texts = [part for part in self.parts if isinstance(part, str)]
        if len(expressions) == 1 and not any(text.strip() for text in texts):
            return expressions[0]
        return None


@dataclass(frozen=True, slots=True)
class Condition:
    """A step `if`. When `uses_status` is False, it evaluates as `success() && (expression)`."""

    source: str
    expression: Expression
    uses_status: bool


TemplateTree: TypeAlias = Template | JSONScalar | list["TemplateTree"] | dict[str, "TemplateTree"]
