from __future__ import annotations

import math
import textwrap
import time

import pytest

from treadle.types import Issue, Position, Severity
from treadle.yamlio import YamlDocument, load_yaml


def load(text: str) -> YamlDocument:
    document, issues = load_yaml(textwrap.dedent(text))
    assert issues == []
    assert document is not None
    return document


def problems(text: str) -> list[tuple[str, str, tuple[int, int] | None]]:
    document, issues = load_yaml(textwrap.dedent(text))
    assert document is None
    return [
        (issue.code, issue.message, (issue.position.line, issue.position.column))
        if issue.position
        else (issue.code, issue.message, None)
        for issue in issues
    ]


@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        pytest.param("push", "push", id="plain-string"),
        pytest.param("yes", "yes", id="yes-stays-a-string"),
        pytest.param("no", "no", id="no-stays-a-string"),
        pytest.param("on", "on", id="on-stays-a-string"),
        pytest.param("Off", "Off", id="off-stays-a-string"),
        pytest.param("y", "y", id="y-stays-a-string"),
        pytest.param("true", True, id="true"),
        pytest.param("True", True, id="title-true"),
        pytest.param("TRUE", True, id="upper-true"),
        pytest.param("false", False, id="false"),
        pytest.param("False", False, id="title-false"),
        pytest.param("FALSE", False, id="upper-false"),
        pytest.param("tRUE", "tRUE", id="mixed-case-isnt-a-boolean"),
        pytest.param("null", None, id="null"),
        pytest.param("Null", None, id="title-null"),
        pytest.param("NULL", None, id="upper-null"),
        pytest.param("~", None, id="tilde"),
        pytest.param("", None, id="empty"),
        pytest.param("0755", 755, id="leading-zero-is-decimal"),
        pytest.param("0o17", 15, id="octal"),
        pytest.param("0x1F", 31, id="hex"),
        pytest.param("-12", -12, id="negative"),
        pytest.param("+7", 7, id="explicit-plus"),
        pytest.param("0o8", "0o8", id="bad-octal-is-a-string"),
        pytest.param("0b101", "0b101", id="binary-is-a-string"),
        pytest.param("1_000", "1_000", id="underscores-are-a-string"),
        pytest.param("1.5", 1.5, id="float"),
        pytest.param(".5", 0.5, id="leading-dot"),
        pytest.param("1.", 1.0, id="trailing-dot"),
        pytest.param("1e3", 1000.0, id="exponent"),
        pytest.param("-2.5E-1", -0.25, id="signed-exponent"),
        pytest.param(".inf", math.inf, id="infinity"),
        pytest.param("-.Inf", -math.inf, id="negative-infinity"),
        pytest.param("+.INF", math.inf, id="positive-infinity"),
        pytest.param("2026-11-09", "2026-11-09", id="date-stays-a-string"),
        pytest.param("2026-11-09T10:00:00Z", "2026-11-09T10:00:00Z", id="timestamp"),
        pytest.param("1:30", "1:30", id="sexagesimal-stays-a-string"),
        pytest.param("=", "=", id="value-key"),
        pytest.param("'true'", "true", id="quoted-true"),
        pytest.param('"5"', "5", id="quoted-number"),
        pytest.param("'~'", "~", id="quoted-tilde"),
    ],
)
def test_scalars_resolve_with_the_yaml_1_2_core_schema(scalar: str, expected: object) -> None:
    assert load(f"value: {scalar}\n").data == {"value": expected}


def test_on_is_a_string_key() -> None:
    assert load("on: push\n").data == {"on": "push"}


@pytest.mark.parametrize("text", [".nan", ".NaN", ".NAN"])
def test_nan_resolves_to_a_float(text: str) -> None:
    value = load(f"value: {text}\n").data

    assert isinstance(value, dict)
    assert math.isnan(value["value"])


def test_an_integer_too_long_for_python_becomes_a_float() -> None:
    assert load(f"value: {'9' * 5000}\n").data == {"value": math.inf}


@pytest.mark.parametrize(
    "literal",
    [
        pytest.param("0x" + "f" * 4000, id="hex"),
        pytest.param("0o" + "7" * 5000, id="octal"),
    ],
)
def test_an_octal_or_hex_integer_too_long_to_print_becomes_infinite(literal: str) -> None:
    assert load(f"value: {literal}\n").data == {"value": math.inf}


def test_a_long_hex_integer_that_prints_stays_an_integer() -> None:
    assert load(f"value: 0x{'f' * 20}\n").data == {"value": 16**20 - 1}


def test_non_string_keys_keep_their_types_and_positions() -> None:
    document = load("2: two\ntrue: yes\nnull: none\n")

    assert document.data == {2: "two", True: "yes", None: "none"}
    assert document.position(("True",), key=True) == Position(2, 1)
    assert document.position(("None",)) == Position(3, 7)


def test_an_empty_document_has_no_data() -> None:
    document = load("# only a comment\n")

    assert document.data is None
    assert document.value_positions == {}


def test_duplicate_keys_report_both_positions() -> None:
    text = """\
    name: first
    steps:
      - id: a
        id: b
    name: second
    """
    assert problems(text) == [
        ("E051", "duplicate key 'id': first defined at line 3, column 5", (4, 5)),
        ("E051", "duplicate key 'name': first defined at line 1, column 1", (5, 1)),
    ]


def test_keys_of_different_types_are_not_duplicates() -> None:
    assert load("'1': a\n1: b\n").data == {"1": "a", 1: "b"}


def test_a_duplicate_hint_says_what_to_do() -> None:
    _, issues = load_yaml("a: 1\na: 2\n")

    assert issues == [
        Issue(
            Severity.ERROR,
            "E051",
            "duplicate key 'a': first defined at line 1, column 1",
            position=Position(2, 1),
            hint="remove one of the two entries",
        )
    ]


def test_merge_keys_are_rejected_but_a_quoted_key_is_a_string() -> None:
    assert problems("base: {a: 1}\nother:\n  <<: {a: 1}\n") == [
        ("E052", "merge keys (`<<`) aren't supported", (3, 3))
    ]
    assert load("'<<': value\n").data == {"<<": "value"}


def test_anchors_and_aliases_are_rejected() -> None:
    text = "a: &x 1\nb: *x\nc: &y [1]\nd: *y\n"

    assert problems(text) == [
        ("E053", "anchors (`&name`) aren't supported", (1, 4)),
        ("E053", "aliases (`*name`) aren't supported", (2, 4)),
        ("E053", "anchors (`&name`) aren't supported", (3, 4)),
        ("E053", "aliases (`*name`) aren't supported", (4, 4)),
    ]


def test_an_undefined_or_repeated_anchor_is_still_only_reported() -> None:
    assert problems("a: *nowhere\nb: &x 1\nc: &x 2\n") == [
        ("E053", "aliases (`*name`) aren't supported", (1, 4)),
        ("E053", "anchors (`&name`) aren't supported", (2, 4)),
        ("E053", "anchors (`&name`) aren't supported", (3, 4)),
    ]


@pytest.mark.parametrize(
    "text",
    ["a: !!str 5\n", "a: !!int x\n", "a: ! x\n", "a: !custom x\n", "a: !!map {b: 1}\n"],
)
def test_tags_are_rejected(text: str) -> None:
    assert problems(text) == [("E054", "tags aren't supported", (1, 4))]


def test_a_tagged_value_never_breaks_loading_of_the_rest() -> None:
    assert problems("a: !!float x\nb: !!seq [1]\n") == [
        ("E054", "tags aren't supported", (1, 4)),
        ("E054", "tags aren't supported", (2, 4)),
    ]


def test_complex_keys_are_rejected() -> None:
    assert problems("? [a, b]\n: value\n") == [
        ("E050", "complex mapping keys aren't supported", (1, 3))
    ]


def test_a_syntax_error_names_the_problem_and_its_context() -> None:
    _, issues = load_yaml("a: [1, 2\n")

    assert issues == [
        Issue(
            Severity.ERROR,
            "E050",
            "expected ',' or ']', but got '<stream end>'",
            position=Position(2, 1),
            hint="while parsing a flow sequence, which starts at line 1, column 4",
        )
    ]


def test_a_syntax_error_without_context_has_no_hint() -> None:
    _, issues = load_yaml("a: b: c\n")

    assert [(issue.message, issue.hint) for issue in issues] == [
        ("mapping values are not allowed here", None)
    ]


def test_tabs_get_a_hint() -> None:
    _, issues = load_yaml("a:\n\tb: 1\n")

    assert issues[0].hint == "indent with spaces; YAML doesn't allow tabs here"
    assert issues[0].position == Position(2, 1)


def test_a_second_document_is_rejected() -> None:
    _, issues = load_yaml("a: 1\n---\nb: 2\n")

    assert [(issue.message, issue.position) for issue in issues] == [
        ("the file holds more than one YAML document", Position(2, 1))
    ]


def test_a_control_character_is_located() -> None:
    _, issues = load_yaml("a: 1\r\nb: x\x07y\n")

    assert issues == [
        Issue(
            Severity.ERROR,
            "E050",
            "unacceptable character U+0007: special characters are not allowed",
            position=Position(2, 5),
            hint="remove the control character; YAML files hold printable text only",
        )
    ]


@pytest.mark.parametrize(
    "escape",
    [
        pytest.param("\\U00110000", id="above-the-unicode-range"),
        pytest.param("\\UFFFFFFFF", id="above-a-c-int"),
    ],
)
def test_an_escape_above_the_unicode_range_is_located(escape: str) -> None:
    _, issues = load_yaml(f'a: 1\nname: "ab{escape}cd"\n')

    assert issues == [
        Issue(
            Severity.ERROR,
            "E050",
            f"invalid escape {escape}: the code point is above U+10FFFF",
            position=Position(2, 10),
            hint="the largest code point is \\U0010FFFF",
        )
    ]


def test_the_largest_code_point_escape_loads() -> None:
    assert load('name: "\\U0010FFFF"\n').data == {"name": "\U0010ffff"}


def test_surrogate_pair_escapes_become_one_character() -> None:
    document = load('name: "Deploy \\ud83d\\uDE80"\n"\\ud83d\\ude80": key\n')

    assert document.data == {"name": "Deploy \U0001f680", "\U0001f680": "key"}


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        pytest.param('a: "\\ud800"\n', "D800", id="lone-high-surrogate"),
        pytest.param('a: "x\\uDC00y"\n', "DC00", id="lone-low-surrogate"),
        pytest.param('a: "\\udc00\\ud800"\n', "DC00", id="reversed-pair"),
    ],
)
def test_a_lone_surrogate_escape_is_rejected(text: str, unit: str) -> None:
    _, issues = load_yaml(text)

    assert issues == [
        Issue(
            Severity.ERROR,
            "E050",
            f"invalid escape: U+{unit} is a lone surrogate, not a character",
            position=Position(1, 4),
            hint="write the character itself, or one \\U escape such as \\U0001F680",
        )
    ]


def test_lone_surrogates_in_values_and_keys_are_all_reported() -> None:
    assert problems('a: "\\ud83d"\n"\\ud800": b\n') == [
        ("E050", "invalid escape: U+D83D is a lone surrogate, not a character", (1, 4)),
        ("E050", "invalid escape: U+D800 is a lone surrogate, not a character", (2, 1)),
    ]


@pytest.mark.parametrize("escape", ["\\0", "\\x00", "\\u0000", "\\U00000000"])
def test_a_nul_escape_is_rejected(escape: str) -> None:
    _, issues = load_yaml(f'env:\n  TAG: "v1{escape}"\n')

    assert issues == [
        Issue(
            Severity.ERROR,
            "E050",
            "invalid escape: a string can't hold U+0000 (NUL)",
            position=Position(2, 8),
            hint="remove the escape; processes, file paths, and environment variables can't "
            "take NUL",
        )
    ]


def test_a_nul_escape_in_a_key_is_rejected() -> None:
    assert problems('"a\\0b": 1\n') == [
        ("E050", "invalid escape: a string can't hold U+0000 (NUL)", (1, 1))
    ]


def test_other_escapes_and_quoted_backslashes_still_load() -> None:
    document = load("a: \"tab\\there \\\\u0000\"\nb: 'single \\0'\nc: plain \\0\n")

    assert document.data == {"a": "tab\there \\u0000", "b": "single \\0", "c": "plain \\0"}


def test_deep_nesting_is_rejected_before_it_can_exhaust_the_stack() -> None:
    assert problems("[" * 65 + "]" * 65) == [
        ("E050", "the document is nested more than 64 levels deep", (1, 65))
    ]
    assert load("[" * 64 + "]" * 64).data is not None


def test_issues_found_before_a_syntax_error_are_kept() -> None:
    assert problems("a: &x 1\nb: [\n") == [
        ("E053", "anchors (`&name`) aren't supported", (1, 4)),
        ("E050", "expected the node content, but found '<stream end>'", (3, 1)),
    ]


SAMPLE = """\
name: Sample
steps:
  - id: first
    run: echo one
  - id: second
    needs: first
    with:
      list: [a, b]
"""


def test_value_and_key_positions_are_recorded_for_every_node() -> None:
    document = load(SAMPLE)

    assert document.position(()) == Position(1, 1)
    assert document.position(("name",)) == Position(1, 7)
    assert document.position(("name",), key=True) == Position(1, 1)
    assert document.position(("steps", 1)) == Position(5, 5)
    assert document.position(("steps", 1, "with", "list", 1)) == Position(8, 17)
    assert document.position(("steps", 1, "with", "list"), key=True) == Position(8, 7)


def test_a_missing_path_falls_back_to_the_nearest_ancestor() -> None:
    document = load(SAMPLE)

    assert document.position(("steps", 0, "timeout")) == Position(3, 5)
    assert document.position(("steps", 0, "timeout"), key=True) == Position(3, 5)
    assert document.position(("steps", 9)) == Position(3, 3)
    assert document.position(("name", "inner")) == Position(1, 7)


def test_paths_through_shorthands_resolve_to_the_written_value() -> None:
    document = load(
        """\
        needs: build
        hook: {name: a, secret_env: B}
        """
    )

    assert document.position(("needs", 0)) == Position(1, 8)
    assert document.position(("hook", 0, "secret_env")) == Position(2, 29)


def test_an_empty_document_has_no_positions() -> None:
    assert load("").position(("steps",)) is None


def test_styles_are_recorded_for_strings_only() -> None:
    document = load(
        """\
        plain: a
        single: 'b'
        double: "c"
        literal: |
          d
        folded: >
          e
        number: 5
        """
    )

    assert document.styles == {
        ("plain",): "plain",
        ("single",): "'",
        ("double",): '"',
        ("literal",): "|",
        ("folded",): ">",
    }


OFFSETS = """\
plain: echo ${{ inputs.a }}
single: 'echo ${{ inputs.b }}'
escaped: 'it''s ${{ inputs.c }}'
double: "echo ${{ inputs.d }}"
literal: |
  first line

    indented ${{ inputs.e }}
folded: >
  echo ${{ inputs.f }}
multi: one
  two ${{ inputs.g }}
number: 5
"""


@pytest.mark.parametrize(
    ("key", "offset", "expected"),
    [
        pytest.param("plain", 5, Position(1, 13), id="plain-exact"),
        pytest.param("plain", 0, Position(1, 8), id="plain-start"),
        pytest.param("single", 5, Position(2, 15), id="single-quoted-exact"),
        pytest.param("escaped", 1, Position(3, 12), id="single-quoted-before-the-escape"),
        pytest.param("escaped", 5, Position(3, 10), id="single-quoted-after-the-escape"),
        pytest.param("double", 5, Position(4, 9), id="double-quoted-start"),
        pytest.param("literal", 0, Position(6, 3), id="literal-first-line"),
        pytest.param("literal", 23, Position(8, 14), id="literal-indented-line"),
        pytest.param("folded", 5, Position(9, 9), id="folded-start"),
        pytest.param("multi", 1, Position(11, 9), id="multi-line-plain-first-line"),
        pytest.param("multi", 8, Position(11, 8), id="multi-line-plain-later-line"),
        pytest.param("plain", -4, Position(1, 8), id="clamped-below"),
        pytest.param("plain", 999, Position(1, 28), id="clamped-to-the-end"),
        pytest.param("number", 3, Position(13, 9), id="not-a-string"),
        pytest.param("missing", 3, Position(1, 1), id="missing-path"),
    ],
)
def test_offsets_map_to_file_positions(key: str, offset: int, expected: Position) -> None:
    assert load(OFFSETS).offset_position((key,), offset) == expected


def test_a_literal_offset_points_at_the_expression() -> None:
    document = load(OFFSETS)
    value = document.data["literal"]  # type: ignore[index]
    offset = value.index("${{")

    assert document.offset_position(("literal",), offset) == Position(8, 14)


def test_an_offset_resolves_through_a_shorthand() -> None:
    document = load("schedule: 0 25 * * *\n")

    assert document.offset_position(("schedule", 0, "cron"), 2) == Position(1, 13)


def test_a_literal_line_that_doesnt_match_the_source_falls_back_to_the_start() -> None:
    document = load("run: |\n  echo\n")
    edited = YamlDocument(
        {"run": "other\n"},
        document.source,
        document.value_positions,
        document.key_positions,
        document.styles,
    )

    assert edited.offset_position(("run",), 2) == Position(1, 6)


def test_an_offset_in_a_value_under_a_non_string_key_falls_back_to_its_start() -> None:
    assert load("1: echo ${{ x }}\n").offset_position(("1",), 5) == Position(1, 4)


def _scalar_document(text: str, style: str) -> YamlDocument:
    """Return a document whose `run` key holds `text` as a scalar in `style`."""
    if style == "|":
        source, value = f"run: |\n  {text}\n", f"{text}\n"
    elif style == "'":
        source, value = f"run: '{text}'\n", text
    else:
        source, value = f"run: {text}\n", text
    return YamlDocument(
        {"run": value},
        source,
        {(): Position(1, 1), ("run",): Position(1, 6)},
        {("run",): Position(1, 1)},
        {("run",): style},
    )


def _fastest_placement(document: YamlDocument, offsets: range) -> float:
    """Return the best of three timings, in seconds, of placing every offset in `offsets`."""
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        for offset in offsets:
            document.offset_position(("run",), offset)
        timings.append(time.perf_counter() - started)
    return min(timings)


@pytest.mark.parametrize(
    ("style", "first"),
    [
        pytest.param("plain", Position(1, 6), id="plain"),
        pytest.param("'", Position(1, 7), id="single-quoted"),
        pytest.param("|", Position(2, 3), id="literal"),
    ],
)
def test_placing_an_offset_costs_the_same_in_a_short_and_a_long_scalar(
    style: str, first: Position
) -> None:
    short, long = 10_000, 4_000_000
    short_document = _scalar_document("x" * short, style)
    long_document = _scalar_document("x" * long, style)
    long_tail = range(long - 5_000, long)

    short_time = _fastest_placement(short_document, range(short - 5_000, short))
    long_time = _fastest_placement(long_document, long_tail)

    positions = [long_document.offset_position(("run",), offset) for offset in long_tail]
    assert positions == [Position(first.line, first.column + offset) for offset in long_tail]
    # A placement that rescans the source takes time in proportion to its length, which
    # makes the long scalar hundreds of times slower. Comparing the two, instead of checking
    # a fixed time, holds on any machine, with or without coverage.
    assert long_time < 4 * short_time + 0.01
