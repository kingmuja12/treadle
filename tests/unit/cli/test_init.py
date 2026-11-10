"""`treadle init` and the project files it scaffolds."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.unit.cli.helpers import Cli

from treadle.cli.scaffold import scaffold
from treadle.config import default_config_text
from treadle.types import ExitCode

_FILES = ["treadle.toml", "workflows/hello.yml", "workflows/site-check.yml", ".env.example"]


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "new-project"


def _statuses(results: list[tuple[Path, str]], root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): status for path, status in results}


def test_scaffold_creates_every_project_file(target: Path) -> None:
    results = scaffold(target, force=False, timezone="Europe/London")

    assert _statuses(results, target) == dict.fromkeys([*_FILES, ".gitignore"], "created")
    assert (target / "treadle.toml").read_text() == default_config_text("Europe/London")
    assert 'timezone = "Europe/London"' in (target / "treadle.toml").read_text()
    assert "name: Hello" in (target / "workflows/hello.yml").read_text()
    assert "# triggers:" in (target / "workflows/site-check.yml").read_text()
    example = (target / ".env.example").read_text()
    assert all(line.startswith("#") for line in example.splitlines())
    assert (target / ".gitignore").read_text().splitlines() == [
        "# Treadle: secrets and local state",
        ".env",
        ".env.*",
        "!.env.example",
        ".treadle/",
    ]


def test_scaffold_keeps_existing_files_unless_forced(target: Path) -> None:
    scaffold(target, force=False, timezone="UTC")
    hello = target / "workflows/hello.yml"
    hello.write_text("name: Mine\n")

    kept = _statuses(scaffold(target, force=False, timezone="UTC"), target)
    assert hello.read_text() == "name: Mine\n"
    forced = _statuses(scaffold(target, force=True, timezone="UTC"), target)

    assert set(kept.values()) == {"skipped"}
    assert forced["workflows/hello.yml"] == "updated"
    assert forced["treadle.toml"] == "skipped"  # unchanged files are left alone
    assert "name: Hello" in hello.read_text()


def test_scaffold_appends_only_the_missing_ignore_lines(target: Path) -> None:
    target.mkdir()
    gitignore = target / ".gitignore"
    gitignore.write_text("node_modules/\n.env")

    status = _statuses(scaffold(target, force=False, timezone="UTC"), target)[".gitignore"]

    assert status == "updated"
    assert gitignore.read_text() == (
        "node_modules/\n.env\n\n# Treadle: secrets and local state\n.env.*\n!.env.example\n"
        ".treadle/\n"
    )
    assert _statuses(scaffold(target, force=True, timezone="UTC"), target)[".gitignore"] == (
        "skipped"
    )


def test_init_reports_each_file_and_the_next_steps(
    cli: Cli, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "America/New_York")

    result = cli.anywhere("-C", str(target.parent), "init", target.name)

    assert result.code == ExitCode.OK
    assert result.out.splitlines() == [
        "Treadle project in new-project:",
        "  created  treadle.toml",
        "  created  workflows/hello.yml",
        "  created  workflows/site-check.yml",
        "  created  .env.example",
        "  created  .gitignore",
        "",
        "Next steps:",
        "  cd new-project",
        "  treadle run hello -i name=Ada",
        "  treadle serve",
        "",
        "On a machine that other people use, set TREADLE_TOKEN in .env before you serve.",
        "See .env.example.",
    ]
    assert 'timezone = "America/New_York"' in (target / "treadle.toml").read_text()


def test_init_in_the_current_directory_needs_no_cd(cli: Cli, target: Path) -> None:
    target.mkdir()

    result = cli.anywhere("-C", str(target), "init")

    assert result.code == ExitCode.OK
    assert result.out.startswith("Treadle project in .:\n")
    assert "  cd " not in result.out


def test_init_twice_skips_every_file(cli: Cli, target: Path) -> None:
    cli.anywhere("-C", str(target.parent), "init", target.name)

    again = cli.anywhere("-C", str(target.parent), "init", target.name)

    assert again.code == ExitCode.OK
    assert again.out.count("skipped") == 5


def test_init_falls_back_to_utc_when_the_zone_is_unknown(
    cli: Cli, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Nowhere/Special")
    monkeypatch.setattr("treadle.cli.commands.detect_timezone", lambda environ: None)

    result = cli.anywhere("-C", str(target.parent), "init", target.name)

    assert result.code == ExitCode.OK
    assert result.err == (
        "warning: couldn't detect the local time zone, so treadle.toml uses UTC\n"
    )
    assert 'timezone = "UTC"' in (target / "treadle.toml").read_text()


def test_init_refuses_a_file_as_the_project_directory(cli: Cli, target: Path) -> None:
    target.write_text("not a directory")

    result = cli.anywhere("-C", str(target.parent), "init", target.name)

    assert result.code == ExitCode.USAGE
    assert result.err == "error: new-project isn't a directory\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_init_reports_a_directory_it_cant_write(cli: Cli, target: Path) -> None:
    target.mkdir()
    target.chmod(0o500)
    try:
        result = cli.anywhere("-C", str(target), "init")
    finally:
        target.chmod(0o700)

    assert result.code == ExitCode.FAILURE
    assert result.err.startswith("error: can't create the project in .: ")


def test_the_scaffold_validates_strictly(cli: Cli, target: Path) -> None:
    cli.anywhere("-C", str(target.parent), "init", target.name)

    result = cli.anywhere("-C", str(target), "validate", "--strict")

    assert result.code == ExitCode.OK
    assert result.out == "2 valid, 0 invalid\n"


def test_force_replaces_a_file_that_isnt_text(target: Path) -> None:
    target.mkdir()
    (target / "treadle.toml").write_bytes(b"\xff\xfe not text")

    results = _statuses(scaffold(target, force=True, timezone="UTC"), target)

    assert results["treadle.toml"] == "updated"
    assert (target / "treadle.toml").read_text() == default_config_text("UTC")
