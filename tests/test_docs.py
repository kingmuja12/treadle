"""Check the documentation against the code.

The example workflows and every `yaml` block in `docs/` whose first line is
`# treadle-example` must validate under `--strict` rules. The README must keep the sections
that readers look for, and every relative link and heading anchor in the Markdown files must
resolve.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from treadle.cli.main import main
from treadle.config import Limits, find_config
from treadle.steps.builtin import builtin_registry
from treadle.types import ExitCode
from treadle.workflow.loader import LoadResult, WorkflowLoader, cross_check

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
EXAMPLES = ROOT / "examples"
README = ROOT / "README.md"
MARKER = "# treadle-example"

DOC_PAGES = (
    "workflow-syntax",
    "expressions",
    "triggers",
    "cli",
    "architecture",
    "security-model",
    "operations",
    "coming-from-github-actions",
)
EXAMPLE_NAMES = ("nightly-backup", "deploy-on-push", "sort-downloads", "site-uptime")
README_SECTIONS = (
    "Who Treadle is for",
    "Features",
    "Platform support",
    "Install",
    "Quickstart",
    "Examples",
    "CLI overview",
    "Security notes",
    "Documentation",
    "Project status",
    "Roadmap",
    "Contributing",
    "License",
)
# Pages that must each show at least one complete, validated workflow.
PAGES_WITH_EXAMPLES = ("workflow-syntax", "expressions", "triggers", "coming-from-github-actions")

_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`]*)$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")
_LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
_EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@dataclass(frozen=True, slots=True)
class Block:
    """A fenced code block: its file, the line of its opening fence, its language, its text."""

    path: Path
    line: int
    info: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.path.relative_to(ROOT).as_posix()}:{self.line}"

    @property
    def is_example(self) -> bool:
        return self.text.split("\n", 1)[0].strip() == MARKER


def fenced_blocks(path: Path) -> list[Block]:
    """Return the fenced code blocks of a Markdown file, with list indentation removed."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            index += 1
            continue
        indent, fence = opening["indent"], opening["fence"]
        body: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip() != fence:
            line = lines[index]
            body.append(line[len(indent) :] if line.startswith(indent) else line.lstrip())
            index += 1
        line_number = index - len(body)
        blocks.append(Block(path, line_number, opening["info"].strip(), "\n".join(body) + "\n"))
        index += 1
    return blocks


def prose(path: Path) -> str:
    """Return a Markdown file's text without its fenced code blocks."""
    kept: list[str] = []
    fence: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        opening = _FENCE.match(line)
        if fence is None and opening is not None:
            fence = opening["fence"]
        elif fence is not None and line.strip() == fence:
            fence = None
        elif fence is None:
            kept.append(line)
    return "\n".join(kept)


def markdown_files() -> list[Path]:
    """Return the repository's Markdown documentation."""
    top = [ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md")]
    return [*top, *sorted(DOCS.glob("*.md"))]


def doc_examples() -> list[Block]:
    """Return the blocks in `docs/` that are marked as complete workflows."""
    return [
        block
        for path in sorted(DOCS.glob("*.md"))
        for block in fenced_blocks(path)
        if block.is_example
    ]


def loader() -> WorkflowLoader:
    """A loader without machine-specific checks, as `treadle validate` uses outside a project."""
    return WorkflowLoader(
        builtin_registry(),
        project_dir=ROOT,
        default_timezone="UTC",
        limits=Limits(),
        environment_checks=False,
    )


@cache
def example_results() -> dict[str, LoadResult]:
    """Load every example file together, so cross-file checks such as E305 apply too."""
    shared = loader()
    results = cross_check([shared.load_file(path) for path in sorted(EXAMPLES.glob("*.yml"))])
    return {result.path.stem: result for result in results}


def problems(result: LoadResult, label: str) -> str:
    """Render a result's issues, for a failure message."""
    return "\n".join(issue.render(label, result.source) for issue in result.issues)


def assert_strictly_valid(result: LoadResult, label: str) -> None:
    """Fail unless the workflow loads with no errors and no warnings."""
    assert result.ok, f"{label} is invalid:\n{problems(result, label)}"
    assert not result.issues, f"{label} has warnings:\n{problems(result, label)}"


def slug(heading: str) -> str:
    """Return the anchor that GitHub gives a heading."""
    text = heading.strip().lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # a link keeps its text
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Return every heading anchor of a Markdown file, numbering repeats as GitHub does."""
    seen: Counter[str] = Counter()
    found: set[str] = set()
    for line in prose(path).splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        base = slug(match["text"])
        found.add(base if not seen[base] else f"{base}-{seen[base]}")
        seen[base] += 1
    return found


def relative_links(path: Path) -> Iterator[str]:
    """Yield the targets of the links in a Markdown file that point inside the repository."""
    for match in _LINK.finditer(prose(path)):
        target = match["target"]
        if not _EXTERNAL.match(target):
            yield target


def readme_sections() -> list[str]:
    return [
        match["text"]
        for line in prose(README).splitlines()
        if (match := _HEADING.match(line)) is not None and line.startswith("## ")
    ]


# The example workflows


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_every_example_exists(name: str) -> None:
    assert (EXAMPLES / f"{name}.yml").is_file()


@pytest.mark.parametrize("name", sorted(path.stem for path in EXAMPLES.glob("*.yml")))
def test_example_is_valid_under_strict_rules(name: str) -> None:
    result = example_results()[name]
    assert_strictly_valid(result, f"examples/{name}.yml")


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_example_explains_itself(name: str) -> None:
    result = example_results()[name]
    assert result.workflow is not None
    assert result.workflow.description, "give the example a description"
    assert (EXAMPLES / f"{name}.yml").read_text(encoding="utf-8").startswith("#"), (
        "start the example with a comment that explains its setup"
    )


def test_examples_pass_treadle_validate_strict(capsys: pytest.CaptureFixture[str]) -> None:
    if find_config(ROOT) is not None:
        pytest.skip("a treadle.toml above the repository turns on machine-specific checks")
    paths = [path.relative_to(ROOT).as_posix() for path in sorted(EXAMPLES.glob("*.yml"))]
    code = main(["-C", str(ROOT), "validate", "--strict", *paths])
    output = capsys.readouterr().out
    assert code == ExitCode.OK, output
    assert output.startswith(f"{len(paths)} valid, 0 invalid"), output


# Workflows in the documentation


def test_the_docs_show_complete_workflows() -> None:
    pages = {block.path.stem for block in doc_examples()}
    missing = [page for page in PAGES_WITH_EXAMPLES if page not in pages]
    assert not missing, f"pages without a `{MARKER}` block: {missing}"


def test_only_yaml_blocks_carry_the_example_marker() -> None:
    wrong = [
        block.label
        for path in markdown_files()
        for block in fenced_blocks(path)
        if block.is_example and block.info != "yaml"
    ]
    assert not wrong, f"`{MARKER}` blocks must be fenced as yaml: {wrong}"


@pytest.mark.parametrize("block", doc_examples(), ids=lambda block: block.label)
def test_docs_example_is_valid_under_strict_rules(block: Block) -> None:
    name = f"{block.path.stem}-{block.line}"
    result = loader().load_text(block.text, ROOT / f"{name}.yml")
    assert_strictly_valid(result, block.label)


def test_readme_workflows_are_valid_under_strict_rules() -> None:
    blocks = [block for block in fenced_blocks(README) if block.info == "yaml"]
    assert blocks, "the README should show a workflow"
    for block in blocks:
        result = loader().load_text(block.text, ROOT / f"readme-{block.line}.yml")
        assert_strictly_valid(result, block.label)


# The README


@pytest.mark.parametrize("section", README_SECTIONS)
def test_readme_has_section(section: str) -> None:
    assert section in readme_sections()


def test_readme_says_that_windows_isnt_supported() -> None:
    text = " ".join(prose(README).split())
    assert re.search(r"Windows (isn't|is not) supported", text), text[:200]
    assert "macOS and Linux" in text


def test_readme_installs_from_the_git_repository() -> None:
    text = README.read_text(encoding="utf-8")
    for command in ("uv tool install", "pipx install"):
        assert f"{command} git+https://github.com/kingmuja12/treadle" in text


def test_readme_shows_only_the_ci_badge() -> None:
    badges = re.findall(r"\[!\[[^\]]*\]\(([^)]+)\)\]", README.read_text(encoding="utf-8"))
    expected = "https://github.com/kingmuja12/treadle/actions/workflows/ci.yml/badge.svg"
    assert badges == [expected]


@pytest.mark.parametrize("page", DOC_PAGES)
def test_readme_links_each_doc_page(page: str) -> None:
    assert (DOCS / f"{page}.md").is_file()
    assert f"](docs/{page}.md)" in README.read_text(encoding="utf-8")


# The other project files


def test_changelog_has_the_unreleased_first_version() -> None:
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    assert "## [0.1.0] - Unreleased" in lines


def test_security_policy_uses_private_reporting_without_an_email_address() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "https://github.com/kingmuja12/treadle/security" in text
    assert "Report a vulnerability" in text
    assert not _EMAIL.search(text)


def test_contributing_explains_assistant_trailers() -> None:
    text = " ".join((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())
    assert "Co-Authored-By" in text
    assert "AI coding assistant" in text


# Links


@pytest.mark.parametrize("path", markdown_files(), ids=lambda path: path.name)
def test_relative_links_resolve(path: Path) -> None:
    broken: list[str] = []
    for target in relative_links(path):
        file_part, _, anchor = target.partition("#")
        linked = (path.parent / file_part).resolve() if file_part else path
        if not linked.exists():
            broken.append(f"{target}: no such file")
        elif anchor and linked.suffix == ".md" and anchor not in anchors(linked):
            broken.append(f"{target}: no heading with that anchor")
    assert not broken, f"{path.name}: " + "; ".join(broken)


def test_slug_matches_github_anchors() -> None:
    cases: Sequence[tuple[str, str]] = (
        ("Run Treadle with systemd", "run-treadle-with-systemd"),
        ("`treadle init`", "treadle-init"),
        ("Safe patterns for untrusted data", "safe-patterns-for-untrusted-data"),
        ("Coming from GitHub Actions", "coming-from-github-actions"),
    )
    for heading, expected in cases:
        assert slug(heading) == expected
