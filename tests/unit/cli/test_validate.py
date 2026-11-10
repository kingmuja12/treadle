"""`treadle validate`: rendered issues, summaries, `--strict`, `--json`, and exit codes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import BROKEN, GREET, Cli

from treadle.types import ExitCode

# A shell script that templates an input: W102.
_WARNS = """\
inputs:
  name:
    default: world
steps:
  - id: greet
    run: echo "Hello, ${{ inputs.name }}"
"""


def test_valid_workflows_print_a_summary(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)

    result = cli("validate")

    assert result.code == ExitCode.OK
    assert result.out == "1 valid, 0 invalid\n"


def test_errors_are_rendered_with_excerpts_and_fail(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)
    project.write_workflow("broken", BROKEN)

    result = cli("validate")

    assert result.code == ExitCode.FAILURE
    assert result.out.splitlines() == [
        "workflows/broken.yml:1:1: error[E102]: missing required key 'steps'",
        "   |",
        " 1 | name: Broken",
        "   | ^",
        "   = help: add a `steps` list with at least one step",
        "",
        "workflows/broken.yml:2:1: error[E101]: unknown key 'stpes' in workflow",
        "   |",
        " 2 | stpes:",
        "   | ^",
        "   = help: did you mean 'steps'?",
        "",
        "1 valid, 1 invalid (2 errors)",
    ]


def test_warnings_pass_unless_strict(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("warns", _WARNS)

    lenient, strict = cli("validate"), cli("validate", "--strict")

    assert lenient.code == ExitCode.OK
    assert "warning[W102]" in lenient.out
    assert lenient.out.endswith("1 valid, 0 invalid (1 warning)\n")
    assert strict.code == ExitCode.FAILURE
    assert strict.out == lenient.out


def test_json_lists_every_file_with_its_issues(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("broken", BROKEN)
    project.write_workflow("warns", _WARNS)

    result = cli("validate", "--json")

    assert result.code == ExitCode.FAILURE
    document = result.json()
    assert {key: document[key] for key in ("valid", "errors", "warnings")} == {
        "valid": False,
        "errors": 2,
        "warnings": 1,
    }
    broken, warns = document["files"]
    assert {key: broken[key] for key in ("path", "workflow", "valid")} == {
        "path": "workflows/broken.yml",
        "workflow": "broken",
        "valid": False,
    }
    assert broken["issues"][1] == {
        "file": "workflows/broken.yml",
        "severity": "error",
        "code": "E101",
        "message": "unknown key 'stpes' in workflow",
        "path": "stpes",
        "line": 2,
        "column": 1,
        "hint": "did you mean 'steps'?",
    }
    assert warns["valid"] is True
    assert [issue["code"] for issue in warns["issues"]] == ["W102"]


def test_paths_outside_a_project_skip_the_machine_checks(cli: Cli, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "uses-secret.yml").write_text(
        "steps:\n  - id: call\n    uses: http\n    with:\n      url: ${{ secrets.HOOK_URL }}\n"
    )

    result = cli.anywhere("-C", str(loose), "validate", "uses-secret.yml")

    assert result.code == ExitCode.OK
    assert result.out == "1 valid, 0 invalid; machine-specific checks skipped (no treadle.toml)\n"


def test_inside_a_project_an_undefined_secret_warns(cli: Cli, project: ProjectFactory) -> None:
    path = project.write_workflow(
        "call", "steps:\n  - id: call\n    uses: http\n    with:\n      url: ${{ secrets.X }}\n"
    )

    result = cli("validate", str(path))

    assert result.code == ExitCode.OK
    assert "warning[W201]" in result.out


def test_a_directory_path_validates_its_workflow_files(cli: Cli, project: ProjectFactory) -> None:
    project.write_workflow("greet", GREET)
    project.write_workflow("broken", BROKEN)

    result = cli("validate", "workflows", "workflows/greet.yml")

    assert result.code == ExitCode.FAILURE
    assert result.out.endswith("1 valid, 1 invalid (2 errors)\n")


def test_a_missing_path_exits_4(cli: Cli) -> None:
    result = cli("validate", "nope.yml")

    assert result.code == ExitCode.NOT_FOUND
    assert result.err == "error: nope.yml doesn't exist\n"


def test_an_empty_workflows_directory(cli: Cli) -> None:
    result = cli("validate")

    assert result.code == ExitCode.OK
    assert result.out == "No workflow files in workflows.\n"


def test_without_paths_a_project_is_required(cli: Cli, tmp_path: Path) -> None:
    result = cli.anywhere("-C", str(tmp_path), "validate")

    assert result.code == ExitCode.CONFIG
    assert "run `treadle init`" in result.err


def test_a_file_outside_the_directory_is_shown_by_its_full_path(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = project.write_workflow("broken", BROKEN)

    result = cli.anywhere("-C", str(elsewhere), "validate", str(path))

    assert result.code == ExitCode.FAILURE
    assert result.out.startswith(f"{path}:1:1: error[E102]")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_an_unreadable_file_is_a_config_error(cli: Cli, project: ProjectFactory) -> None:
    path = project.write_workflow("secret", GREET)
    path.chmod(0o000)
    try:
        result = cli("validate")
    finally:
        path.chmod(0o600)

    assert result.code == ExitCode.CONFIG
    assert result.err.startswith("error: can't read workflows/secret.yml: ")


def _link(project: ProjectFactory, tmp_path: Path, name: str = "linked.yml") -> Path:
    """Put a symbolic link in the workflows directory, to a valid workflow elsewhere."""
    target = tmp_path / "shared.yml"
    target.write_text(GREET)
    link = project.workflows_dir / name
    link.symlink_to(target)
    return link


_W060 = [
    "workflows/linked.yml: warning[W060]: the file is a symbolic link, so Treadle skips it",
    "   = help: copy the file into the workflows directory instead",
    "",
]


def test_a_symbolic_link_gets_w060_and_counts_only_as_a_warning(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    project.write_workflow("greet", GREET)
    _link(project, tmp_path)

    lenient, strict, directory = (
        cli("validate"),
        cli("validate", "--strict"),
        cli("validate", "workflows"),
    )

    assert lenient.code == directory.code == ExitCode.OK
    assert lenient.out.splitlines() == [*_W060, "1 valid, 0 invalid (1 warning)"]
    assert directory.out == lenient.out
    assert (strict.code, strict.out) == (ExitCode.FAILURE, lenient.out)


def test_json_lists_a_symbolic_link_without_a_workflow(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    project.write_workflow("greet", GREET)
    _link(project, tmp_path)

    document = cli("validate", "--json").json()

    assert (document["valid"], document["errors"], document["warnings"]) == (True, 0, 1)
    greet, linked = document["files"]
    assert (greet["path"], greet["valid"]) == ("workflows/greet.yml", True)
    assert {key: linked[key] for key in ("path", "workflow", "valid")} == {
        "path": "workflows/linked.yml",
        "workflow": None,
        "valid": False,
    }
    assert [issue["code"] for issue in linked["issues"]] == ["W060"]


def test_a_symbolic_link_given_by_its_path_is_checked_as_it_is(
    cli: Cli, project: ProjectFactory, tmp_path: Path
) -> None:
    _link(project, tmp_path)

    result = cli("validate", "workflows/linked.yml")

    assert (result.code, result.out) == (ExitCode.OK, "1 valid, 0 invalid\n")
