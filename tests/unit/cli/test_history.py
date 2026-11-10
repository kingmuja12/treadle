"""`treadle runs`, `show`, and `logs` over a project with three finished runs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import FLAKY, GREET, Cli, store_runs
from tests.unit.store.helpers import T0, Runs

from treadle.cli.main import main
from treadle.models import LogStream, RunStatus
from treadle.store.repo import NewLogLine
from treadle.types import ExitCode

_TIME = r"\d\d:\d\d:\d\d"


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> ProjectFactory:
    """Run 1 and run 3 of `greet` succeed; run 2 of `flaky` fails after two attempts."""
    project = ProjectFactory(tmp_path_factory.mktemp("history") / "project")
    project.write_workflow("greet", GREET)
    project.write_workflow("flaky", FLAKY)
    for args in (("greet", "-i", "name=Ada"), ("flaky",), ("greet",)):
        main(["-C", str(project.root), "run", *args])
    return project


@pytest.fixture
def cli(history: ProjectFactory, capsys: pytest.CaptureFixture[str]) -> Cli:
    return Cli(history, capsys)


def test_runs_lists_the_newest_first(cli: Cli) -> None:
    result = cli("runs")

    assert result.code == ExitCode.OK
    header, *rows = result.out.splitlines()
    assert header.split() == ["ID", "WORKFLOW", "STATUS", "TRIGGER", "STARTED", "DURATION"]
    assert [row.split()[:4] for row in rows] == [
        ["3", "greet", "✓", "succeeded"],
        ["2", "flaky", "✗", "failed"],
        ["1", "greet", "✓", "succeeded"],
    ]
    assert all(re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", row) for row in rows)


@pytest.mark.parametrize(
    ("args", "ids"),
    [
        (("--workflow", "greet"), ["3", "1"]),
        (("--status", "failed"), ["2"]),
        (("--status", "failed", "--status", "succeeded"), ["3", "2", "1"]),
        (("--trigger", "manual"), ["3", "2", "1"]),
        (("--grep", "MISSING.TXT"), ["2"]),
        (("--before", "3"), ["2", "1"]),
    ],
)
def test_runs_filters(cli: Cli, args: tuple[str, ...], ids: list[str]) -> None:
    result = cli("runs", *args)

    assert [row.split()[0] for row in result.out.splitlines()[1:]] == ids


def test_runs_with_no_match(cli: Cli) -> None:
    result = cli("runs", "--trigger", "webhook")

    assert result.code == ExitCode.OK
    assert result.out == "No runs match.\n"


@pytest.mark.parametrize(
    ("args", "hint"),
    [
        (("--workflow", "greet", "--limit", "1"), "--workflow greet --limit 1 --before 3"),
        (
            ("--trigger", "manual", "--grep", "Hello, ", "--limit", "1"),
            "--trigger manual --grep 'Hello, ' --limit 1 --before 3",
        ),
    ],
)
def test_runs_pages_keep_their_filters(cli: Cli, args: tuple[str, ...], hint: str) -> None:
    result = cli("runs", *args)

    assert result.out.splitlines()[-1] == f"Older runs: treadle runs {hint}"


def test_runs_json(cli: Cli) -> None:
    result = cli("runs", "--json", "--limit", "2")

    document = result.json()
    assert document["next_before_id"] == 2
    assert [run["id"] for run in document["runs"]] == [3, 2]
    failed = document["runs"][1]
    assert failed["status"] == "failed"
    assert failed["error"].startswith("step 'fetch' failed after 2 attempts: source ")
    assert failed["trigger"] == {"kind": "manual", "origin": "cli"}
    assert set(failed) == {
        "id",
        "workflow",
        "workflow_name",
        "digest",
        "status",
        "reason",
        "error",
        "trigger_kind",
        "trigger",
        "inputs",
        "attempt",
        "rerun_of",
        "replaced_by",
        "scheduled_for",
        "created_at",
        "started_at",
        "finished_at",
        "duration_ms",
    }


def test_runs_before_any_run(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    empty = Cli(ProjectFactory(tmp_path / "empty"), capsys)

    result = empty("runs")

    assert result.code == ExitCode.OK
    assert result.out == "No runs yet.\n"


def test_show_summarizes_a_run(cli: Cli) -> None:
    result = cli("show", "1")

    assert result.code == ExitCode.OK
    lines = result.out.splitlines()
    assert lines[0] == "Run #1 · greet · succeeded · manual (cli)"
    assert re.fullmatch(
        r"Started \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC · took \S+ · attempt 1", lines[1]
    )
    assert lines[3].split() == ["STEP", "STATUS", "ATTEMPTS", "DURATION", "NOTE"]
    assert lines[4].split()[:4] == ["greet", "✓", "ok", "1"]
    assert len(lines) == 6


def test_show_leads_with_the_failure(cli: Cli) -> None:
    result = cli("show", "#2")

    lines = result.out.splitlines()
    assert lines[0] == "Run #2 · flaky · failed · manual (cli)"
    assert lines[2].startswith("Error: step 'fetch' failed after 2 attempts: source ")
    assert lines[5].split()[:4] == ["fetch", "✗", "failed", "2"]
    assert lines[6].split() == ["report", "⊘", "skipped", "0", "-", "upstream_failed"]
    assert lines[9] == "--- fetch, attempt 2: last 20 lines ---"
    assert lines[-1] == "Hint: treadle logs 2 --step fetch"


@pytest.mark.parametrize(("ref", "run_id"), [("last", 3), ("last:flaky", 2), (" 1 ", 1)])
def test_show_resolves_run_references(cli: Cli, ref: str, run_id: int) -> None:
    assert cli("show", ref, "--json").json()["id"] == run_id


def test_show_json_is_the_run_detail(cli: Cli) -> None:
    detail = cli("show", "2", "--json").json()

    assert [step["id"] for step in detail["steps"]] == ["fetch", "report", "alert"]
    fetch = detail["steps"][0]
    assert [attempt["status"] for attempt in fetch["attempts"]] == ["failed", "failed"]
    assert detail["steps"][1]["skip_reason"] == "upstream_failed"


@pytest.mark.parametrize(
    ("ref", "code", "message"),
    [
        ("99", ExitCode.NOT_FOUND, "error: run 99 doesn't exist\n"),
        ("last:ghost", ExitCode.NOT_FOUND, "error: workflow 'ghost' has no runs\n"),
        (
            "latest",
            ExitCode.USAGE,
            "error: invalid run reference 'latest'; use 42, #42, last, or last:<workflow>\n",
        ),
        (
            "99999999999999999999",
            ExitCode.NOT_FOUND,
            "error: run 99999999999999999999 doesn't exist\n",
        ),
    ],
)
def test_show_refuses_bad_references(cli: Cli, ref: str, code: int, message: str) -> None:
    result = cli("show", ref)

    assert (result.code, result.err) == (code, message)


def test_logs_print_each_line_with_its_step_and_attempt(cli: Cli) -> None:
    result = cli("logs", "1")

    assert result.code == ExitCode.OK
    lines = result.out.splitlines()
    assert re.fullmatch(rf"{_TIME} greet#1 system \| \[info\] Hello, Ada!", lines[0])
    assert re.fullmatch(rf"{_TIME} done#1 system \| \[info\] greeted in run 1", lines[1])


def test_logs_of_one_attempt(cli: Cli) -> None:
    result = cli("logs", "2", "--step", "fetch", "--attempt", "1")

    texts = [line.split(" | ", 1)[1] for line in result.out.splitlines()]
    assert texts[0].startswith("attempt 1 failed: source ")
    assert texts[1] == "retrying in 0s (attempt 2 of 2)"
    assert all(" fetch#1 " in line for line in result.out.splitlines())


def test_logs_json_lines(cli: Cli) -> None:
    lines = cli("logs", "1", "--json").json_lines()

    assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)
    assert set(lines[0]) == {"seq", "ts", "step", "attempt", "stream", "text"}
    assert lines[0]["text"] == "[info] Hello, Ada!"


def test_following_a_finished_run_prints_its_lines_and_returns(cli: Cli) -> None:
    followed, plain = cli("logs", "1", "-f"), cli("logs", "1")

    assert followed.code == ExitCode.OK
    assert followed.out == plain.out


def test_logs_of_an_unknown_step_exit_4(cli: Cli) -> None:
    result = cli("logs", "1", "--step", "ghost")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == "error: run 1 has no step 'ghost'\n"


def test_logs_page_through_long_runs(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    project = ProjectFactory(tmp_path / "long")
    lines = [
        NewLogLine(seq, T0, "build", 1, LogStream.STDOUT, f"line {seq}") for seq in range(1, 1206)
    ]

    def chatty(runs: Runs) -> None:
        run = runs.finished(RunStatus.SUCCEEDED, steps=("build",))
        runs.repo.append_logs(run.id, lines)

    store_runs(project.settings(), "wf", chatty)

    result = Cli(project, capsys)("logs", "1")

    printed = result.out.splitlines()
    assert len(printed) == 1205
    assert printed[-1].endswith("build#1 stdout | line 1205")
