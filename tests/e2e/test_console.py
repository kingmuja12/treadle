"""The installed `treadle` script on a fresh project: the commands a new user runs first."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.e2e.conftest import Treadle

from treadle import __version__


@pytest.fixture(scope="module")
def initialized(tmp_path_factory: pytest.TempPathFactory, treadle: Treadle) -> Path:
    """A project that `treadle init` created and that ran `hello` once."""
    root = tmp_path_factory.mktemp("e2e") / "project"
    created = treadle("init", str(root), cwd=root.parent)
    assert created.returncode == 0, created.stderr
    ran = treadle("run", "hello", "-i", "name=Ada", cwd=root)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    return root


def test_init_scaffolds_a_project(initialized: Path) -> None:
    for name in ("treadle.toml", "workflows/hello.yml", "workflows/site-check.yml", ".gitignore"):
        assert (initialized / name).is_file()
    assert 'timezone = "UTC"' in (initialized / "treadle.toml").read_text()


def test_the_scaffold_validates_strictly(initialized: Path, treadle: Treadle) -> None:
    result = treadle("validate", "--strict", cwd=initialized)

    assert (result.returncode, result.stdout) == (0, "2 valid, 0 invalid\n")


def test_runs_lists_the_first_run_as_json(initialized: Path, treadle: Treadle) -> None:
    result = treadle("runs", "--json", cwd=initialized)

    assert result.returncode == 0
    document = json.loads(result.stdout)
    assert document["next_before_id"] is None
    (run,) = document["runs"]
    assert (run["id"], run["workflow"], run["status"]) == (1, "hello", "succeeded")
    assert run["inputs"] == {"name": "Ada"}


def test_show_summarizes_the_run(initialized: Path, treadle: Treadle) -> None:
    result = treadle("show", "last", cwd=initialized)

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0] == "Run #1 · hello · succeeded · manual (cli)"
    assert [line.split()[0] for line in lines[4:6]] == ["greet", "report"]


def test_logs_show_what_the_steps_printed(initialized: Path, treadle: Treadle) -> None:
    result = treadle("logs", "1", cwd=initialized)

    assert result.returncode == 0
    texts = [line.split(" | ", 1)[1] for line in result.stdout.splitlines()]
    assert texts == ["Hello, Ada!", "[info] Hello, Ada! (run 1)"]


def test_cron_previews_a_schedule(initialized: Path, treadle: Treadle) -> None:
    result = treadle(
        "cron", "*/15 * * * *", "--after", "2026-11-09T10:07", "-n", "2", cwd=initialized
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[1:] == [
        "2026-11-09 10:15 UTC  2026-11-09T10:15:00Z",
        "2026-11-09 10:30 UTC  2026-11-09T10:30:00Z",
    ]


def test_graph_prints_the_steps(initialized: Path, treadle: Treadle) -> None:
    result = treadle("graph", "hello", cwd=initialized)

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "[0] greet  shell  if: success()",
        "[1] report  log  needs: greet (implicit)  if: success()",
    ]


def test_version(initialized: Path, treadle: Treadle) -> None:
    result = treadle("version", cwd=initialized)

    assert result.returncode == 0
    assert re.fullmatch(
        rf"treadle {re.escape(__version__)} \(Python \S+, \S+, SQLite \S+, schema 1\)\n",
        result.stdout,
    )


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (("run", "ghost"), 4, "error: workflow 'ghost' doesn't exist\n"),
        (("show", "9"), 4, "error: run 9 doesn't exist\n"),
        (("run", "hello", "-i", "nope=1"), 2, "error: input 'nope' isn't declared"),
        (("cron", "* * *"), 2, "error: "),
        (("bogus",), 2, "invalid choice: 'bogus'"),
    ],
)
def test_failures_have_their_exit_codes(
    initialized: Path, treadle: Treadle, args: tuple[str, ...], code: int, message: str
) -> None:
    result = treadle(*args, cwd=initialized)

    assert result.returncode == code
    assert message in result.stderr


def test_outside_a_project_state_commands_exit_3(tmp_path: Path, treadle: Treadle) -> None:
    result = treadle("list", cwd=tmp_path)

    assert result.returncode == 3
    assert "run `treadle init`" in result.stderr
