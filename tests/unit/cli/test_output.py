"""`Output`: lines, tables, JSON, status symbols, and when to use color."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from treadle.cli.output import Output, _discard, use_color


def _streams(encoding: str = "utf-8") -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
    return (
        io.TextIOWrapper(io.BytesIO(), encoding=encoding),
        io.TextIOWrapper(io.BytesIO(), encoding=encoding),
    )


def _text(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode(stream.encoding)


def test_lines_go_to_stdout_and_messages_to_stderr() -> None:
    out, err = _streams()
    output = Output(out, err, color=False)

    output.line("result")
    output.line("progress", err=True)
    output.error("it broke")
    output.warn("watch out")

    assert _text(out) == "result\n"
    assert _text(err) == "progress\nerror: it broke\nwarning: watch out\n"


def test_color_paints_the_message_prefixes() -> None:
    out, err = _streams()
    output = Output(out, err, color=True)

    output.error("it broke")
    output.warn("watch out")

    assert _text(err) == "\x1b[31merror:\x1b[0m it broke\n\x1b[33mwarning:\x1b[0m watch out\n"


def test_a_table_aligns_columns_two_spaces_apart_ignoring_color_codes() -> None:
    out, err = _streams()
    output = Output(out, err, color=True)

    output.table(
        ["ID", "STATUS", "NOTE"], [["1", output.status("succeeded"), ""], ["12", "x", "y"]]
    )

    assert _text(out).splitlines() == [
        "ID  STATUS       NOTE",
        "1   \x1b[32m✓ succeeded\x1b[0m",
        "12  x            y",
    ]


def test_a_table_can_be_indented() -> None:
    out, err = _streams()

    Output(out, err, color=False).table(["A", "B"], [["one", "two"]], indent=2)

    assert _text(out).splitlines() == ["  A    B", "  one  two"]


@pytest.mark.parametrize(
    ("status", "unicode", "ascii"),
    [
        ("succeeded", "✓ succeeded", "[ok] succeeded"),
        ("failed", "✗ failed", "[fail] failed"),
        ("skipped", "⊘ skipped", "[skip] skipped"),
        ("timed_out", "⧖ timed_out", "[time] timed_out"),
        ("cancelled", "⊗ cancelled", "[cancel] cancelled"),
        ("interrupted", "↯ interrupted", "[stop] interrupted"),
        ("running", "▸ running", "[run] running"),
        ("queued", "… queued", "[wait] queued"),
    ],
)
def test_statuses_get_symbols_or_their_ascii_form(status: str, unicode: str, ascii: str) -> None:
    utf8 = Output(*_streams(), color=False)
    plain = Output(*_streams("ascii"), color=False)

    assert utf8.unicode
    assert not plain.unicode
    assert utf8.status(status) == unicode
    assert plain.status(status) == ascii


def test_an_unknown_status_comes_back_unchanged() -> None:
    assert Output(*_streams(), color=True).status("mystery") == "mystery"


def test_the_symbol_alone() -> None:
    assert Output(*_streams(), color=False).status("failed", symbol_only=True) == "✗"


def test_json_is_indented_and_json_lines_are_compact() -> None:
    out, err = _streams()
    output = Output(out, err, color=False)

    output.json({"name": "Ada", "steps": [1, 2]})
    output.json_line({"event": "log", "text": "café"})

    text = _text(out)
    document, line = text[: text.index("}\n") + 1], text.splitlines()[-1]
    assert json.loads(document) == {"name": "Ada", "steps": [1, 2]}
    assert "\n  " in document
    assert line == '{"event":"log","text":"café"}'


def test_json_escapes_what_an_ascii_stream_cant_print() -> None:
    out, err = _streams("ascii")

    Output(out, err, color=False).json_line({"text": "café"})

    assert _text(out) == '{"text":"caf\\u00e9"}\n'


def test_a_line_that_the_stream_cant_encode_is_replaced_instead_of_failing() -> None:
    out, err = _streams("ascii")

    Output(out, err, color=False).line("naïve")

    assert _text(out) == "na?ve\n"


class _Terminal(io.StringIO):
    """A text stream that says it's a terminal, and fails once closed, like a real one."""

    def isatty(self) -> bool:
        super().isatty()
        return True


@pytest.mark.parametrize(
    ("environ", "disabled", "expected"),
    [
        ({}, False, True),
        ({}, True, False),
        ({"NO_COLOR": "1"}, False, False),
        ({"NO_COLOR": ""}, False, True),
        ({"TERM": "dumb"}, False, False),
    ],
)
def test_color_needs_a_terminal_and_no_opt_out(
    environ: dict[str, str], disabled: bool, expected: bool
) -> None:
    assert use_color(_Terminal(), environ, disabled=disabled) is expected


def test_no_color_without_a_terminal() -> None:
    assert not use_color(io.StringIO(), {}, disabled=False)


def test_no_color_on_a_closed_stream() -> None:
    stream = _Terminal()
    stream.close()

    assert not use_color(stream, {}, disabled=False)


def test_a_discarded_stream_writes_to_dev_null(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"

    with path.open("w", encoding="utf-8") as stream:
        stream.write("buffered\n")
        _discard(stream)
        stream.write("after\n")

    assert path.read_text(encoding="utf-8") == ""


def test_a_stream_without_a_descriptor_stays_as_it_is() -> None:
    stream = io.StringIO()

    _discard(stream)
    stream.write("kept")

    assert stream.getvalue() == "kept"
