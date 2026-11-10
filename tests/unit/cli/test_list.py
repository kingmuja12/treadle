"""`treadle list`: the workflows table and its JSON form, without a daemon."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import BROKEN, GREET, Cli, store_runs

from treadle.models import RunStatus
from treadle.types import ExitCode

_NIGHTLY = """\
name: Nightly
triggers:
  schedule: "30 2 * * *"
  webhook:
    name: nightly-hook
    secret_env: NIGHTLY_SECRET
steps:
  - id: work
    uses: log
    with:
      message: working
"""


def test_the_table_lists_each_workflow(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)
    project.write_workflow("nightly", _NIGHTLY)
    store_runs(project.settings(), "greet", lambda runs: runs.finished(RunStatus.FAILED))

    result = cli("list")

    assert result.code == ExitCode.OK
    header, greet, nightly = result.out.splitlines()
    assert header.split() == ["ID", "NAME", "TRIGGERS", "NEXT", "FIRE", "LAST", "RUN", "STATE"]
    assert greet.split() == ["greet", "Greeting", "-", "-", "#1", "✗", "failed", "ok"]
    assert nightly.startswith("nightly ")
    assert "cron 30 2 * * * (UTC), webhook /hooks/nightly-hook" in nightly
    assert re.search(r"\d{4}-\d\d-\d\d 02:30 UTC", nightly)
    assert nightly.split()[-2:] == ["-", "ok"]


def test_invalid_workflows_show_their_state_and_a_hint(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("broken", BROKEN)

    result = cli("list")

    assert result.code == ExitCode.OK
    lines = result.out.splitlines()
    assert lines[1].split() == ["broken", "-", "-", "-", "-", "invalid"]
    assert lines[-1] == "1 workflow is invalid; `treadle validate` shows the issues."


def test_json_has_the_workflow_shape(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("nightly", _NIGHTLY)
    project.write_workflow("broken", BROKEN)

    result = cli("list", "--json")

    assert result.code == ExitCode.OK
    broken, nightly = result.json()["workflows"]
    assert set(nightly) == {
        "id",
        "name",
        "description",
        "path",
        "valid",
        "stale",
        "digest",
        "issues",
        "concurrency",
        "triggers",
        "last_run",
    }
    assert nightly["path"] == "workflows/nightly.yml"
    assert nightly["concurrency"] == {"policy": "queue", "max_queued": 10}
    assert [trigger["kind"] for trigger in nightly["triggers"]] == ["schedule", "webhook"]
    assert nightly["triggers"][0]["next_fire"].endswith("T02:30:00.000Z")
    assert nightly["last_run"] is None
    assert [issue["code"] for issue in nightly["issues"]] == ["W311"]
    assert broken["valid"] is False
    assert broken["name"] is None


def test_an_empty_project(cli: Cli) -> None:
    result = cli("list")

    assert result.code == ExitCode.OK
    assert result.out == "No workflows in workflows.\n"


def test_a_symbolic_link_is_skipped_with_a_warning(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    project.write_workflow("greet", GREET)
    target = tmp_path / "shared.yml"
    target.write_text(GREET)
    (project.workflows_dir / "linked.yml").symlink_to(target)

    result, as_json = cli("list"), cli("list", "--json")

    warning = "warning: workflows/linked.yml: the file is a symbolic link, so Treadle skips it\n"
    assert (result.code, result.err, as_json.err) == (ExitCode.OK, warning, warning)
    assert [line.split()[0] for line in result.out.splitlines()[1:]] == ["greet"]
    assert [workflow["id"] for workflow in as_json.json()["workflows"]] == ["greet"]
