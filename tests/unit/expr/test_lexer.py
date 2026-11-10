from __future__ import annotations

import pytest

from treadle.errors import ExpressionSyntaxError
from treadle.expr.lexer import OPERATORS, Token, TokenKind, scan_template, tokenize
from treadle.types import JSONScalar

NO_SUBTRACTION = "there is no subtraction; a '-' can only start a number or continue a name"


def texts(source: str) -> list[str]:
    """Return the text of every token except END."""
    return [token.text for token in tokenize(source)[:-1]]


def test_tokenize_reports_kinds_values_and_offsets() -> None:
    assert tokenize("steps.build-image.outputs['tag'] == 3") == [
        Token(TokenKind.NAME, "steps", None, 0, 5),
        Token(TokenKind.DOT, ".", None, 5, 6),
        Token(TokenKind.NAME, "build-image", None, 6, 17),
        Token(TokenKind.DOT, ".", None, 17, 18),
        Token(TokenKind.NAME, "outputs", None, 18, 25),
        Token(TokenKind.LBRACKET, "[", None, 25, 26),
        Token(TokenKind.STRING, "'tag'", "tag", 26, 31),
        Token(TokenKind.RBRACKET, "]", None, 31, 32),
        Token(TokenKind.OP, "==", None, 33, 35),
        Token(TokenKind.NUMBER, "3", 3, 36, 37),
        Token(TokenKind.END, "", None, 37, 37),
    ]


def test_punctuation_has_its_own_kinds() -> None:
    kinds = [token.kind for token in tokenize("f(a, b)[0].c")]
    assert kinds == [
        TokenKind.NAME,
        TokenKind.LPAREN,
        TokenKind.NAME,
        TokenKind.COMMA,
        TokenKind.NAME,
        TokenKind.RPAREN,
        TokenKind.LBRACKET,
        TokenKind.NUMBER,
        TokenKind.RBRACKET,
        TokenKind.DOT,
        TokenKind.NAME,
        TokenKind.END,
    ]


def test_an_empty_source_is_just_the_end_token() -> None:
    assert tokenize(" \t\r\n") == [Token(TokenKind.END, "", None, 4, 4)]


@pytest.mark.parametrize(
    ("source", "kind", "value"),
    [
        ("true", TokenKind.TRUE, True),
        ("false", TokenKind.FALSE, False),
        ("null", TokenKind.NULL, None),
        ("0", TokenKind.NUMBER, 0),
        ("42", TokenKind.NUMBER, 42),
        ("-7", TokenKind.NUMBER, -7),
        ("-0", TokenKind.NUMBER, 0),
        ("3.25", TokenKind.NUMBER, 3.25),
        ("-0.5", TokenKind.NUMBER, -0.5),
        ("1e3", TokenKind.NUMBER, 1000.0),
        ("2E-2", TokenKind.NUMBER, 0.02),
        ("1.5e+2", TokenKind.NUMBER, 150.0),
        ("''", TokenKind.STRING, ""),
        ("'it''s'", TokenKind.STRING, "it's"),
        ("''''", TokenKind.STRING, "'"),
        ("'a\\nb'", TokenKind.STRING, "a\\nb"),
        ("'say \"hi\"'", TokenKind.STRING, 'say "hi"'),
        ("'${{ x }}'", TokenKind.STRING, "${{ x }}"),
    ],
)
def test_literals_carry_their_values(source: str, kind: TokenKind, value: JSONScalar) -> None:
    token = tokenize(source)[0]

    assert (token.kind, token.text, token.value) == (kind, source, value)
    assert type(token.value) is type(value)


@pytest.mark.parametrize(
    "source", ["build-image", "_private", "a-1", "a--b-", "trueish", "true-x", "nulls", "B2"]
)
def test_names_may_contain_dashes_and_digits(source: str) -> None:
    assert tokenize(source)[0] == Token(TokenKind.NAME, source, None, 0, len(source))


@pytest.mark.parametrize(
    ("source", "tokens"),
    [
        ("a -1", ["a", "-1"]),
        ("a-1", ["a-1"]),
        ("1-2", ["1", "-2"]),
        ("(-1)", ["(", "-1", ")"]),
        ("[-0.5]", ["[", "-0.5", "]"]),
    ],
)
def test_a_dash_starts_a_number_only_at_a_token_start_before_a_digit(
    source: str, tokens: list[str]
) -> None:
    assert texts(source) == tokens


def test_operators_take_the_longest_match() -> None:
    assert texts("<= < >= > == != && || !") == ["<=", "<", ">=", ">", "==", "!=", "&&", "||", "!"]
    assert texts("a<=b") == ["a", "<=", "b"]
    assert texts("!!a!=b") == ["!", "!", "a", "!=", "b"]
    assert OPERATORS == ("==", "!=", "<=", ">=", "&&", "||", "<", ">", "!")


def test_whitespace_separates_tokens() -> None:
    assert texts("a\t==\r\n'b'") == ["a", "==", "'b'"]


@pytest.mark.parametrize(
    ("source", "message", "offset", "hint"),
    [
        ('"main"', "unexpected character '\"'", 0, "use single quotes"),
        ('ref == "main"', "unexpected character '\"'", 7, "use single quotes"),
        ("a = b", "unexpected character '='", 2, "use == to compare values"),
        ("- 1", "unexpected character '-'", 0, NO_SUBTRACTION),
        ("-a", "unexpected character '-'", 0, NO_SUBTRACTION),
        ("a & b", "unexpected character '&'", 2, None),
        ("a | b", "unexpected character '|'", 2, None),
        ("$x", "unexpected character '$'", 0, None),
        ("a\u00a0b", "unexpected character '\u00a0'", 1, None),
        ("'abc", "unterminated string", 0, None),
        ("x == 'it''s", "unterminated string", 5, None),
        ("01", "invalid number", 0, None),
        ("1.", "invalid number", 0, None),
        ("1.5.2", "invalid number", 0, None),
        ("1e", "invalid number", 0, None),
        ("x < 2abc", "invalid number", 4, None),
        ("1_000", "invalid number", 0, None),
        ("x > 1e999", "number out of range", 4, None),
    ],
)
def test_bad_input_raises_with_an_offset_and_hint(
    source: str, message: str, offset: int, hint: str | None
) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        tokenize(source)

    error = caught.value
    assert (error.message, error.offset, error.hint, error.source) == (
        message,
        offset,
        hint,
        source,
    )


def test_offset_shifts_error_offsets_but_not_token_offsets() -> None:
    assert tokenize(" a", offset=10)[0].start == 1

    with pytest.raises(ExpressionSyntaxError) as caught:
        tokenize(" @", offset=10)

    assert caught.value.offset == 11


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        ("plain text", []),
        ("", []),
        ("${{ a }}", [(0, 8)]),
        ("x ${{ a }} y ${{ b }}", [(2, 10), (13, 21)]),
        ("${{a}}${{b}}", [(0, 6), (6, 12)]),
        ("${{ '}}' }}", [(0, 11)]),
        ("${{ 'it''s }}' }}", [(0, 17)]),
        ("${{ '${{' }}", [(0, 12)]),
        ("${{ a }}}", [(0, 8)]),
        ("${{ a } b }}", [(0, 12)]),
        ("$ {{ a }}", []),
        ("}} {{ ${{a}}", [(6, 12)]),
    ],
)
def test_scan_template_finds_each_span(text: str, spans: list[tuple[int, int]]) -> None:
    assert scan_template(text) == spans


@pytest.mark.parametrize(
    ("text", "message", "offset"),
    [
        ("x ${{ a", "unterminated ${{", 2),
        ("${{ a }} ${{", "unterminated ${{", 9),
        ("${{ a } b", "unterminated ${{", 0),
        ("${{ 'a }}", "unterminated string", 4),
        ("ok ${{ x == 'it''s }}", "unterminated string", 16),
    ],
)
def test_scan_template_rejects_unterminated_spans(text: str, message: str, offset: int) -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        scan_template(text)

    assert (caught.value.message, caught.value.offset, caught.value.source) == (
        message,
        offset,
        text,
    )


def test_an_unterminated_template_hints_at_the_escape() -> None:
    with pytest.raises(ExpressionSyntaxError) as caught:
        scan_template("cost: ${{")

    assert caught.value.hint == "close it with }}, or write a literal ${{ as ${{ '${{' }}"
