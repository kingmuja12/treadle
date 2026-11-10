from __future__ import annotations

import os
import signal

import pytest

from treadle.steps.procs import LineBuffer, ProcessRegistry


def feed_all(buffer: LineBuffer, *chunks: bytes) -> list[tuple[str, bool]]:
    lines = [line for chunk in chunks for line in buffer.feed(chunk)]
    return lines + buffer.flush()


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param((b"one\ntwo\n",), [("one", False), ("two", False)], id="whole-lines"),
        pytest.param((b"ab", b"c\nde", b"f"), [("abc", False), ("def", False)], id="split"),
        pytest.param((b"one\r\ntwo\r", b"\n"), [("one", False), ("two", False)], id="crlf"),
        pytest.param((b"\n\n",), [("", False), ("", False)], id="empty-lines"),
        pytest.param((b"tail\r",), [("tail\r", False)], id="final-cr-is-text"),
        pytest.param((b"mid\rline\n",), [("mid\rline", False)], id="inner-cr-is-text"),
        pytest.param((b"caf\xc3\n",), [("caf�", False)], id="invalid-utf8"),
        pytest.param((b"caf\xc3", b"\xa9\n"), [("café", False)], id="split-utf8"),
        pytest.param((), [], id="nothing"),
    ],
)
def test_lines_are_split_and_decoded(
    chunks: tuple[bytes, ...], expected: list[tuple[str, bool]]
) -> None:
    assert feed_all(LineBuffer(100), *chunks) == expected


def test_a_line_at_the_limit_isnt_truncated() -> None:
    assert feed_all(LineBuffer(4, slack=2), b"abcd\n") == [("abcd", False)]


def test_a_line_over_the_limit_keeps_the_slack_and_is_marked() -> None:
    assert feed_all(LineBuffer(4, slack=2), b"abcde\n") == [("abcde", True)]
    assert feed_all(LineBuffer(4, slack=2), b"abcdefghij\nok\n") == [
        ("abcdef", True),
        ("ok", False),
    ]


def test_the_rest_of_a_long_line_is_dropped_across_chunks() -> None:
    buffer = LineBuffer(4, slack=2)

    assert buffer.feed(b"abc") == []
    assert buffer.feed(b"defgh") == []
    assert buffer.feed(b"ij\nok\n") == [("abcdef", True), ("ok", False)]
    assert buffer.feed(b"x" * 50) == []
    assert buffer.flush() == [("xxxxxx", True)]
    assert buffer.flush() == []


def test_a_crlf_line_measures_its_length_without_the_terminator() -> None:
    assert feed_all(LineBuffer(4, slack=2), b"abcd\r\n") == [("abcd", False)]
    assert feed_all(LineBuffer(4, slack=2), b"abcdef\r\n") == [("abcdef", True)]
    assert feed_all(LineBuffer(4, slack=2), b"abcdefg\r\n") == [("abcdef", True)]


def test_a_cut_inside_a_character_decodes_as_a_replacement() -> None:
    assert feed_all(LineBuffer(3, slack=0), "abé\n".encode()) == [("ab�", True)]


def test_the_default_slack_is_1024_bytes() -> None:
    [(text, truncated)] = feed_all(LineBuffer(10), b"x" * 5000 + b"\n")

    assert (len(text), truncated) == (1034, True)


def test_the_registry_tracks_groups_like_a_set() -> None:
    registry = ProcessRegistry()
    registry.add(5)
    registry.add(5)
    registry.add(7)
    registry.discard(5)
    registry.discard(99)

    assert len(registry) == 1


def test_kill_all_counts_the_groups_it_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    signalled: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        signalled.append((pgid, sig))
        if pgid == 2:
            raise ProcessLookupError(pgid)
        if pgid == 3:
            raise PermissionError(pgid)

    monkeypatch.setattr(os, "killpg", killpg)
    registry = ProcessRegistry()
    for pgid in (1, 2, 3):
        registry.add(pgid)

    assert registry.kill_all() == 1
    assert sorted(signalled) == [(1, signal.SIGKILL), (2, signal.SIGKILL), (3, signal.SIGKILL)]
    assert registry.kill_all(signal.SIGTERM) == 1
    assert signalled[-1][1] == signal.SIGTERM
    assert ProcessRegistry().kill_all() == 0
