"""The expression tokenizer and the template scanner."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from treadle.errors import ExpressionSyntaxError
from treadle.types import JSONScalar


class TokenKind(StrEnum):
    """The kinds of expression tokens."""

    NAME = "name"
    NUMBER = "number"
    STRING = "string"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"
    OP = "op"
    DOT = "."
    LBRACKET = "["
    RBRACKET = "]"
    LPAREN = "("
    RPAREN = ")"
    COMMA = ","
    END = "end"


@dataclass(frozen=True, slots=True)
class Token:
    """One token, with its offsets in the source.

    `value` holds the literal value for NUMBER, STRING, TRUE, FALSE, and NULL tokens, and
    None otherwise.
    """

    kind: TokenKind
    text: str
    value: JSONScalar
    start: int
    end: int


OPERATORS: Final[tuple[str, ...]] = ("==", "!=", "<=", ">=", "&&", "||", "<", ">", "!")

# Names use ASCII letters only, as step IDs and input names do.
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# JSON number syntax. A "-" starts a number only when a digit follows it.
_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
# The possessive quantifier treats every "''" as an escape, so "'a''" stays unterminated.
_STRING: Final = re.compile(r"'(?:[^']|'')*+'")
# A "${{ ... }}" span. The body holds anything but quotes and "}}", and whole string literals,
# so the closing group is missing when the span is unterminated or a quote never closes.
_SPAN: Final = re.compile(r"\$\{\{(?:[^'}]|'[^']*'|\}(?!\}))*(\}\})?")
_WHITESPACE: Final = frozenset(" \t\r\n")
_KEYWORDS: Final[dict[str, tuple[TokenKind, JSONScalar]]] = {
    "true": (TokenKind.TRUE, True),
    "false": (TokenKind.FALSE, False),
    "null": (TokenKind.NULL, None),
}
_PUNCTUATION: Final = {kind.value: kind for kind in TokenKind if len(kind.value) == 1}
_HINTS: Final = {
    '"': "use single quotes",
    "=": "use == to compare values",
    "-": "there is no subtraction; a '-' can only start a number or continue a name",
}


def tokenize(source: str, *, offset: int = 0) -> list[Token]:
    """Split an expression into tokens, ending with an END token.

    Names may contain `-`; a `-` starts a number only at the start of a token before a digit.
    Strings use single quotes with `''` as the escape. Raises ExpressionSyntaxError, with the
    hint "use single quotes" for a double quote. Token offsets index `source`, and error
    offsets are shifted by `offset`, the position of `source` inside its YAML string.
    """
    tokens: list[Token] = []
    position = 0
    while position < len(source):
        if source[position] in _WHITESPACE:
            position += 1
            continue
        token = _token_at(source, position, offset)
        tokens.append(token)
        position = token.end
    tokens.append(Token(TokenKind.END, "", None, len(source), len(source)))
    return tokens


def scan_template(text: str) -> list[tuple[int, int]]:
    """Return the [start, end) span of each `${{ ... }}` in `text`, delimiters included.

    The closing `}}` is the first one outside a string literal. Raises ExpressionSyntaxError
    for an unterminated `${{`.
    """
    spans: list[tuple[int, int]] = []
    for match in _SPAN.finditer(text):
        if match.group(1) is not None:
            spans.append(match.span())
        elif match.end() < len(text):  # the body stopped at a quote that never closes
            raise ExpressionSyntaxError("unterminated string", source=text, offset=match.end())
        else:
            raise ExpressionSyntaxError(
                "unterminated ${{",
                source=text,
                offset=match.start(),
                hint="close it with }}, or write a literal ${{ as ${{ '${{' }}",
            )
    return spans


def _token_at(source: str, start: int, offset: int) -> Token:
    """Read the token that starts at `start`."""
    if match := _NAME.match(source, start):
        text = match.group()
        kind, value = _KEYWORDS.get(text, (TokenKind.NAME, None))
        return Token(kind, text, value, start, match.end())
    if match := _NUMBER.match(source, start):
        return _number(source, match, offset)
    if source[start] == "'":
        if match := _STRING.match(source, start):
            text = match.group()
            return Token(TokenKind.STRING, text, text[1:-1].replace("''", "'"), start, match.end())
        raise ExpressionSyntaxError("unterminated string", source=source, offset=offset + start)
    for operator in OPERATORS:
        if source.startswith(operator, start):
            return Token(TokenKind.OP, operator, None, start, start + len(operator))
    char = source[start]
    if char in _PUNCTUATION:
        return Token(_PUNCTUATION[char], char, None, start, start + 1)
    raise ExpressionSyntaxError(
        f"unexpected character '{char}'",
        source=source,
        offset=offset + start,
        hint=_HINTS.get(char),
    )


def _number(source: str, match: re.Match[str], offset: int) -> Token:
    """Build a NUMBER token, rejecting runs such as `01`, `1.`, and `1e` and infinite values."""
    start, end = match.span()
    if end < len(source) and (source[end].isalnum() or source[end] in "_."):
        raise ExpressionSyntaxError("invalid number", source=source, offset=offset + start)
    text = match.group()
    number = float(text)
    if not math.isfinite(number):
        raise ExpressionSyntaxError("number out of range", source=source, offset=offset + start)
    integral = match.group(1) is None and match.group(2) is None
    return Token(TokenKind.NUMBER, text, int(text) if integral else number, start, end)
