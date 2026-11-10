"""`treadle cron`, `treadle graph`, and `treadle version`."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import BROKEN, GREET, Cli

from treadle import __version__
from treadle.store.db import sqlite_version
from treadle.types import ExitCode

_DAG = """\
steps:
  - id: build
    uses: log
    with:
      message: build
  - id: test
    uses: log
    with:
      message: test
  - id: notify
    needs: [build]
    if: always()
    uses: log
    with:
      message: done
"""


def test_cron_shows_fires_with_dst_notes(cli: Cli) -> None:
    result = cli(
        "cron", "30 2 * * *", "--tz", "America/New_York", "--after", "2026-03-07", "-n", "3"
    )

    assert result.code == ExitCode.OK
    assert result.out.splitlines() == [
        "LOCAL                 UTC                   NOTE",
        "2026-03-07 02:30 EST  2026-03-07T07:30:00Z",
        "2026-03-08 03:00 EDT  2026-03-08T07:00:00Z  02:30 doesn't exist (DST gap); runs at 03:00",
        "2026-03-09 02:30 EDT  2026-03-09T06:30:00Z",
    ]


def test_cron_json(cli: Cli) -> None:
    result = cli(
        "cron", "30 1 * * *", "--tz", "Europe/London", "--after", "2026-10-24T12:00Z", "-n", "2"
    )
    as_json = cli(
        "cron",
        "30 1 * * *",
        "--tz",
        "Europe/London",
        "--after",
        "2026-10-24T12:00Z",
        "-n",
        "2",
        "--json",
    )

    assert result.code == as_json.code == ExitCode.OK
    assert as_json.json() == {
        "expression": "30 1 * * *",
        "timezone": "Europe/London",
        "fires": [
            {
                "local": "2026-10-25T01:30:00+01:00",
                "utc": "2026-10-25T00:30:00.000Z",
                "note": "01:30 happens twice (DST overlap); runs only the first time",
            },
            {"local": "2026-10-26T01:30:00+00:00", "utc": "2026-10-26T01:30:00.000Z", "note": None},
        ],
    }


def test_cron_defaults_to_the_project_time_zone(cli: Cli, project: ProjectFactory) -> None:
    project.write_config('timezone = "Asia/Tokyo"\n')

    result = cli("cron", "@daily", "--after", "2026-11-09T00:00", "-n", "1")

    assert result.out.splitlines()[1].startswith("2026-11-10 00:00 JST  2026-11-09T15:00:00Z")


def test_cron_outside_a_project_uses_the_local_zone(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Australia/Lord_Howe")

    result = cli.anywhere("-C", str(tmp_path), "cron", "@hourly", "-n", "1", "--json")

    assert result.code == ExitCode.OK
    assert result.json()["timezone"] == "Australia/Lord_Howe"


def test_an_invalid_expression_points_at_the_bad_field(cli: Cli) -> None:
    result = cli("cron", "0 25 * * *")

    assert result.code == ExitCode.USAGE
    assert result.err == "error: hour 25 isn't between 0 and 23\n  0 25 * * *\n    ^\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--tz", "Mars/Olympus"), "unknown time zone 'Mars/Olympus'"),
        (("--after", "yesterday"), "--after: invalid date or time 'yesterday'"),
    ],
)
def test_bad_cron_options_are_usage_errors(cli: Cli, args: tuple[str, ...], message: str) -> None:
    result = cli("cron", "@daily", *args)

    assert result.code == ExitCode.USAGE
    assert message in result.err


def test_graph_text_by_workflow_id(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("dag", _DAG)

    result = cli("graph", "dag")

    assert result.code == ExitCode.OK
    assert result.out.splitlines() == [
        "[0] build  log  if: success()",
        "[1] test  log  needs: build (implicit)  if: success()",
        "[1] notify  log  needs: build  if: always()",
    ]


def test_graph_mermaid_and_json(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("dag", _DAG)

    mermaid, as_json = (
        cli("graph", "dag", "--format", "mermaid"),
        cli("graph", "dag", "--format", "json"),
    )

    assert mermaid.out.splitlines()[0] == "flowchart TD"
    assert "    s0 -.-> s1" in mermaid.out.splitlines()
    assert "    s0 --> s2" in mermaid.out.splitlines()
    graph = as_json.json()
    assert graph["workflow"] == "dag"
    assert [node["id"] for node in graph["nodes"]] == ["build", "test", "notify"]
    assert {"from": "build", "to": "test", "implicit": True} in graph["edges"]


def test_graph_of_a_file_outside_a_project(cli: Cli, tmp_path: Path) -> None:
    (tmp_path / "greet.yml").write_text(GREET)

    result = cli.anywhere("-C", str(tmp_path), "graph", "greet.yml")

    assert result.code == ExitCode.OK
    assert result.out.startswith("[0] greet  log")


def test_graph_of_an_invalid_workflow_exits_3_with_its_issues(
    cli: Cli, project: ProjectFactory
) -> None:
    project.write_workflow("broken", BROKEN)

    result = cli("graph", "broken")

    assert result.code == ExitCode.CONFIG
    assert result.out == ""
    assert result.err.startswith("error: workflow 'broken' is invalid\n")
    assert "workflows/broken.yml:2:1: error[E101]: unknown key 'stpes' in workflow" in result.err


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("ghost", "error: workflow 'ghost' doesn't exist in workflows\n"),
        ("ghost.yml", "error: ghost.yml doesn't exist\n"),
    ],
)
def test_graph_of_a_missing_workflow_exits_4(cli: Cli, ref: str, message: str) -> None:
    result = cli("graph", ref)

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == message


def test_version_text_and_json(cli: Cli) -> None:
    sqlite = ".".join(str(part) for part in sqlite_version())

    text, as_json = cli("version"), cli("version", "--json")

    assert text.out == (
        f"treadle {__version__} (Python {platform.python_version()}, {sys.platform}, "
        f"SQLite {sqlite}, schema 1)\n"
    )
    assert as_json.json() == {
        "treadle": __version__,
        "python": platform.python_version(),
        "platform": sys.platform,
        "sqlite": sqlite,
        "schema_version": 1,
    }
