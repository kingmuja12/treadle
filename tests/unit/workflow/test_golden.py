"""Golden diagnostics: each case renders exactly the issues in its `.txt` file.

A case is `cases/<name>.yml`, a directory `cases/<name>/` of workflow files that load
together with `cross_check`, a generated file in GENERATED, or a generated directory with a
symbolic link in LINKED. Every documented E and W code has at least one case. After an
intended message change, run the suite with TREADLE_UPDATE_GOLDEN=1 to rewrite the expected
files, then review the diff.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from treadle.config import Limits
from treadle.secrets import SecretSnapshot
from treadle.steps.builtin import builtin_registry
from treadle.workflow.loader import LoadResult, WorkflowLoader, cross_check, discover

CASES = Path(__file__).parent / "cases"
PROJECT = Path("/golden/project")
SECRETS = SecretSnapshot(
    secrets={"API_TOKEN": "a-long-api-token-value", "SHORT": "abc"},
    dotenv={},
    environ={"HOOK_SECRET": "0123456789abcdef0123", "WEAK_SECRET": "short"},
)
GENERATED = {
    "e055-too-large": b"steps: []\n" + b"#" * (1 << 20),
    "e056-not-utf8": b"name: Caf\xe9\nsteps:\n  - id: greet\n    run: echo hi\n",
}
# Directories that hold a symbolic link, which a source tree shouldn't: (link, its target).
LINKED = {"w060-symlinked-file": ("linked.yml", "real.yml")}
CODES = [
    "E050",
    "E051",
    "E052",
    "E053",
    "E054",
    "E055",
    "E056",
    "E057",
    "E058",
    "E059",
    "E101",
    "E102",
    "E103",
    "E104",
    "E105",
    "E106",
    "E201",
    "E202",
    "E203",
    "E204",
    "E205",
    "E206",
    "E207",
    "E208",
    "E209",
    "E210",
    "E211",
    "E212",
    "E301",
    "E302",
    "E303",
    "E304",
    "E305",
    "E306",
    "E401",
    "E402",
    "E403",
    "E404",
    "E405",
    "E406",
    "E407",
    "W060",
    "W101",
    "W102",
    "W103",
    "W104",
    "W105",
    "W106",
    "W201",
    "W202",
    "W301",
    "W302",
    "W311",
    "W312",
]
NAMES = sorted(
    [path.stem for path in CASES.glob("*.yml")]
    + [path.name for path in CASES.iterdir() if path.is_dir()]
    + list(GENERATED)
    + list(LINKED)
)


def make_loader() -> WorkflowLoader:
    return WorkflowLoader(
        builtin_registry(),
        project_dir=PROJECT,
        default_timezone="UTC",
        limits=Limits(),
        secrets=SECRETS,
        state_dir=PROJECT / ".treadle",
        workflows_dir=PROJECT / "workflows",
    )


def load_case(name: str, tmp_path: Path) -> list[LoadResult]:
    loader = make_loader()
    if name in GENERATED:
        path = tmp_path / f"{name}.yml"
        path.write_bytes(GENERATED[name])
        return [loader.load_file(path)]
    if name in LINKED:
        link, target = LINKED[name]
        directory = tmp_path / name
        directory.mkdir()
        (directory / target).write_text(
            "steps:\n  - id: greet\n    run: echo hi\n", encoding="utf-8"
        )
        (directory / link).symlink_to(directory / target)
        return load_directory(loader, directory)
    directory = CASES / name
    if directory.is_dir():
        return load_directory(loader, directory)
    return [loader.load_file(CASES / f"{name}.yml")]


def load_directory(loader: WorkflowLoader, directory: Path) -> list[LoadResult]:
    """Load a directory's workflow files together, then add the files that discovery skips."""
    skipped: list[LoadResult] = []
    loaded = cross_check([loader.load_file(path) for path in discover(directory, skipped=skipped)])
    return sorted([*loaded, *skipped], key=lambda result: result.path)


def render(results: Sequence[LoadResult]) -> str:
    return "".join(
        issue.render(f"workflows/{result.path.name}", result.source) + "\n"
        for result in results
        for issue in result.issues
    )


@pytest.mark.parametrize("name", NAMES)
def test_the_case_renders_its_expected_issues(name: str, tmp_path: Path) -> None:
    actual = render(load_case(name, tmp_path))
    expected = CASES / f"{name}.txt"
    if os.environ.get("TREADLE_UPDATE_GOLDEN") == "1":
        expected.write_text(actual, encoding="utf-8")

    assert actual == expected.read_text(encoding="utf-8")


def test_every_diagnostic_code_has_a_case() -> None:
    rendered = "".join(path.read_text(encoding="utf-8") for path in CASES.glob("*.txt"))

    assert [code for code in CODES if f"[{code}]" not in rendered] == []


def test_every_expected_file_belongs_to_a_case() -> None:
    assert sorted(path.stem for path in CASES.glob("*.txt")) == NAMES
