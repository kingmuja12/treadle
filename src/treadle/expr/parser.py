"""The recursive-descent expression parser (section 3, "Grammar")."""

from __future__ import annotations

from typing import Final

from treadle.errors import ExpressionSyntaxError
from treadle.expr.ast import (
    Binary,
    BinaryOp,
    Call,
    Const,
    Expression,
    Index,
    Member,
    Name,
    Node,
    Not,
)
from treadle.expr.lexer import Token, TokenKind, tokenize

MAX_SOURCE: Final = 4096
MAX_DEPTH: Final = 32

# Binary operators by precedence, lowest first. The levels from _COMPARISONS on don't chain.
_LEVELS: Final = (("||",), ("&&",), ("==", "!="), ("<", "<=", ">", ">="))
_COMPARISONS: Final = 2
_LITERALS: Final = frozenset(
    {TokenKind.NUMBER, TokenKind.STRING, TokenKind.TRUE, TokenKind.FALSE, TokenKind.NULL}
)
_PROPERTY_NAMES: Final = frozenset(
    {TokenKind.NAME, TokenKind.TRUE, TokenKind.FALSE, TokenKind.NULL}
)


def parse_expression(source: str, *, offset: int = 0) -> Expression:
    """Parse one expression. `offset` is where `source` starts inside its YAML string.

    Enforces `MAX_SOURCE` characters and `MAX_DEPTH` nesting levels, and rejects chained
    comparisons with the hint "combine comparisons with &&". Raises ExpressionSyntaxError with
    the offset of the problem inside the YAML string.
    """
    # Design note: nesting counts both open parentheses, brackets, and call argument lists,
    # and the height of the syntax tree, so long `||` chains count too. Bounding the tree
    # keeps evaluation and analysis far from Python's recursion limit.
    if len(source) > MAX_SOURCE:
        raise ExpressionSyntaxError(
            f"the expression is longer than {MAX_SOURCE} characters", source=source, offset=offset
        )
    return Expression(source, _Parser(source, offset).parse(), offset)


class _Parser:
    """Parses one expression from its tokens. `depth` counts the open nesting levels."""

    def __init__(self, source: str, offset: int) -> None:
        self.source = source
        self.offset = offset
        self.tokens = tokenize(source, offset=offset)
        self.position = 0
        self.depth = 0

    def parse(self) -> Node:
        node = self.binary(0)
        token = self.peek()
        if token.kind is not TokenKind.END:
            raise self.error(f"unexpected {_describe(token)}", token)
        return node

    def binary(self, level: int) -> Node:
        """Parse one precedence level of binary operators, left-associative."""
        if level == len(_LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.at_operator(_LEVELS[level]):
            operator = self.take()
            right = self.binary(level + 1)
            binary = Binary(BinaryOp(operator.text), left, right, left.start, right.end)
            left = self.checked(binary, operator)
            if level >= _COMPARISONS and self.at_operator(_LEVELS[level]):
                raise self.error(
                    "comparisons can't be chained",
                    self.peek(),
                    hint="combine comparisons with &&",
                )
        return left

    def unary(self) -> Node:
        bangs: list[Token] = []
        while self.at_operator(("!",)):
            bangs.append(self.take())
        node = self.postfix()
        for bang in reversed(bangs):
            node = self.checked(Not(node, bang.start, node.end), bang)
        return node

    def postfix(self) -> Node:
        node = self.primary()
        while True:
            token = self.peek()
            if token.kind is TokenKind.DOT:
                self.take()
                name = self.peek()
                if name.kind not in _PROPERTY_NAMES:
                    raise self.error(f"expected a property name, got {_describe(name)}", name)
                self.take()
                node = self.checked(Member(node, name.text, node.start, name.end), token)
            elif token.kind is TokenKind.LBRACKET:
                self.take()
                index = self.nested(token)
                close = self.expect(TokenKind.RBRACKET)
                node = self.checked(Index(node, index, node.start, close.end), token)
            else:
                return node

    def primary(self) -> Node:
        token = self.peek()
        if token.kind in _LITERALS:
            self.take()
            return Const(token.value, token.start, token.end)
        if token.kind is TokenKind.NAME:
            self.take()
            if self.peek().kind is TokenKind.LPAREN:
                return self.call(token)
            return Name(token.text, token.start, token.end)
        if token.kind is TokenKind.LPAREN:
            self.take()
            node = self.nested(token)
            self.expect(TokenKind.RPAREN)
            return node
        raise self.error(f"expected an expression, got {_describe(token)}", token)

    def call(self, name: Token) -> Node:
        opener = self.take()
        args: list[Node] = []
        if self.peek().kind is not TokenKind.RPAREN:
            args.append(self.nested(opener))
            while self.peek().kind is TokenKind.COMMA:
                self.take()
                args.append(self.nested(opener))
        close = self.peek()
        if close.kind is not TokenKind.RPAREN:
            raise self.error(f"expected ',' or ')', got {_describe(close)}", close)
        self.take()
        return self.checked(Call(name.text, tuple(args), name.start, close.end), name)

    def nested(self, opener: Token) -> Node:
        """Parse an expression inside parentheses, brackets, or an argument list."""
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error(_TOO_DEEP, opener)
        node = self.binary(0)
        self.depth -= 1
        return node

    def checked(self, node: Node, token: Token) -> Node:
        """Return `node` unless the tree below it is more than `MAX_DEPTH` levels high."""
        if _height(node) > MAX_DEPTH:
            raise self.error(_TOO_DEEP, token)
        return node

    def peek(self) -> Token:
        return self.tokens[self.position]

    def take(self) -> Token:
        token = self.tokens[self.position]
        self.position += 1
        return token

    def at_operator(self, operators: tuple[str, ...]) -> bool:
        token = self.peek()
        return token.kind is TokenKind.OP and token.text in operators

    def expect(self, kind: TokenKind) -> Token:
        token = self.peek()
        if token.kind is not kind:
            raise self.error(f"expected '{kind.value}', got {_describe(token)}", token)
        return self.take()

    def error(self, message: str, token: Token, hint: str | None = None) -> ExpressionSyntaxError:
        return ExpressionSyntaxError(
            message, source=self.source, offset=self.offset + token.start, hint=hint
        )


_TOO_DEEP: Final = f"the expression is nested more than {MAX_DEPTH} levels deep"


def _height(node: Node) -> int:
    """Return how many levels of nodes lie below `node`. Callers check children first."""
    match node:
        case Member(target=child) | Not(operand=child):
            return 1 + _height(child)
        case Index(target=left, index=right) | Binary(left=left, right=right):
            return 1 + max(_height(left), _height(right))
        case Call(args=args):
            return 1 + max(map(_height, args), default=0)
    return 0


def _describe(token: Token) -> str:
    """Describe a token for messages: `'name'`, `'abc'` for a string, or the end."""
    if token.kind is TokenKind.END:
        return "the end of the expression"
    return token.text if token.kind is TokenKind.STRING else f"'{token.text}'"
