from __future__ import annotations

import pytest

from treadle.types import ExitCode, Issue, LockHolder, Position, Severity, format_path

SOURCE = "name: deploy\nsteps:\n  - id: check\n    if: startswith(trigger.body.ref, 'refs/tags/')\n"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ((), ""),
        (("steps",), "steps"),
        (("steps", 2, "with", "url"), "steps[2].with.url"),
        ((0, "id"), "[0].id"),
        (("matrix", 1, 2), "matrix[1][2]"),
    ],
)
def test_format_path(path: tuple[str | int, ...], expected: str) -> None:
    assert format_path(path) == expected


def test_render_with_position_and_source_shows_the_line_and_a_caret() -> None:
    issue = Issue(
        Severity.ERROR,
        "E204",
        "unknown function 'startswith'",
        ("steps", 0, "if"),
        Position(4, 9),
        "did you mean 'startsWith'? Function names are case-sensitive.",
    )

    assert issue.render("workflows/deploy.yml", SOURCE) == (
        "workflows/deploy.yml:4:9: error[E204]: unknown function 'startswith'\n"
        "   |\n"
        " 4 |     if: startswith(trigger.body.ref, 'refs/tags/')\n"
        "   |         ^\n"
        "   = help: did you mean 'startsWith'? Function names are case-sensitive."
    )


def test_render_widens_the_gutter_for_long_line_numbers() -> None:
    source = "\n" * 119 + "key: value\n"
    issue = Issue(Severity.WARNING, "W101", "careful", position=Position(120, 6), hint="quote it")

    assert issue.render("f.yml", source).splitlines() == [
        "f.yml:120:6: warning[W101]: careful",
        "    |",
        "120 | key: value",
        "    |      ^",
        "    = help: quote it",
    ]


def test_render_keeps_tabs_so_the_caret_lines_up() -> None:
    issue = Issue(Severity.ERROR, "E104", "bad", position=Position(1, 3))

    assert issue.render("f.yml", "\tx: 1\n").splitlines()[-1] == "   | \t ^"


def test_render_with_a_position_but_no_source_line_omits_the_excerpt() -> None:
    issue = Issue(Severity.ERROR, "E102", "missing required key 'steps'", position=Position(9, 1))

    assert issue.render("f.yml") == "f.yml:9:1: error[E102]: missing required key 'steps'"
    assert issue.render("f.yml", "only: one line\n") == issue.render("f.yml")


def test_render_without_a_position_names_the_yaml_path() -> None:
    issue = Issue(Severity.ERROR, "E104", "must be between 0 and 65535", ("server", "port"))

    assert issue.render("treadle.toml") == (
        "treadle.toml: error[E104]: must be between 0 and 65535 (at server.port)"
    )


def test_render_without_position_or_path_shows_only_the_message_and_hint() -> None:
    issue = Issue(Severity.ERROR, "E055", "file is larger than 1 MiB", hint="split it")

    assert issue.render("big.yml") == (
        "big.yml: error[E055]: file is larger than 1 MiB\n   = help: split it"
    )


@pytest.mark.parametrize(
    ("severity", "escape"), [(Severity.ERROR, "\x1b[31m"), (Severity.WARNING, "\x1b[33m")]
)
def test_render_with_color_colors_only_the_severity_word(severity: Severity, escape: str) -> None:
    issue = Issue(severity, "X001", "message", position=Position(1, 1))

    assert issue.render("f.yml", color=True) == (
        f"f.yml:1:1: {escape}{severity.value}\x1b[0m[X001]: message"
    )


def test_to_json_with_position() -> None:
    issue = Issue(
        Severity.WARNING, "W201", "secret isn't defined", ("env",), Position(3, 7), "set it"
    )

    assert issue.to_json("f.yml") == {
        "file": "f.yml",
        "severity": "warning",
        "code": "W201",
        "message": "secret isn't defined",
        "path": "env",
        "line": 3,
        "column": 7,
        "hint": "set it",
    }


def test_to_json_uses_null_for_unknown_values() -> None:
    issue = Issue(Severity.ERROR, "E056", "file isn't valid UTF-8")

    assert issue.to_json("f.yml") == {
        "file": "f.yml",
        "severity": "error",
        "code": "E056",
        "message": "file isn't valid UTF-8",
        "path": None,
        "line": None,
        "column": None,
        "hint": None,
    }


def test_lock_holder_round_trips_through_json() -> None:
    holder = LockHolder(4242, "serve", "2026-11-09T02:30:00.000Z", "0.1.0", "127.0.0.1", 8765)

    assert holder.to_json() == {
        "pid": 4242,
        "kind": "serve",
        "started_at": "2026-11-09T02:30:00.000Z",
        "version": "0.1.0",
        "host": "127.0.0.1",
        "port": 8765,
    }
    assert LockHolder.from_json(holder.to_json()) == holder


def test_lock_holder_from_json_defaults_host_and_port_and_ignores_extra_keys() -> None:
    data = {"pid": 7, "kind": "run", "started_at": "now", "version": "0.1.0", "extra": True}

    assert LockHolder.from_json(data) == LockHolder(7, "run", "now", "0.1.0")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "holder",
        {"kind": "run", "started_at": "now", "version": "0.1.0"},
        {"pid": True, "kind": "run", "started_at": "now", "version": "0.1.0"},
        {"pid": "7", "kind": "run", "started_at": "now", "version": "0.1.0"},
        {"pid": 7, "kind": "worker", "started_at": "now", "version": "0.1.0"},
        {"pid": 7, "kind": "run", "started_at": 0, "version": "0.1.0"},
        {"pid": 7, "kind": "run", "started_at": "now", "version": 1},
        {"pid": 7, "kind": "run", "started_at": "now", "version": "0.1.0", "host": 1},
        {"pid": 7, "kind": "run", "started_at": "now", "version": "0.1.0", "port": "80"},
        {"pid": 7, "kind": "run", "started_at": "now", "version": "0.1.0", "port": False},
    ],
)
def test_lock_holder_from_json_returns_none_when_malformed(data: object) -> None:
    assert LockHolder.from_json(data) is None


def test_exit_codes_are_stable() -> None:
    assert [(code.name, code.value) for code in ExitCode] == [
        ("OK", 0),
        ("FAILURE", 1),
        ("USAGE", 2),
        ("CONFIG", 3),
        ("NOT_FOUND", 4),
        ("CANCELLED", 5),
        ("TIMED_OUT", 6),
        ("SKIPPED", 7),
        ("BUSY", 8),
        ("INTERRUPTED", 130),
    ]
