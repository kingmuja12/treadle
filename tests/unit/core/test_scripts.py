from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"script_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loc = load_script("loc")
check_coverage = load_script("check_coverage")
check_hygiene = load_script("check_hygiene")

SAMPLE = '''"""Module docstring
over two lines."""

from __future__ import annotations

# A comment line.

TEMPLATE = """
a multi-line string
is code
"""


class Example:
    """Class docstring."""

    value = 1  # a trailing comment

    def method(self) -> int:
        """Method docstring."""
        return (
            self.value
        )


async def run() -> None:
    """Async docstring only."""
'''


def test_loc_counts_code_but_not_blank_lines_comments_or_docstrings() -> None:
    assert loc.count_code_lines(SAMPLE) == 12
    assert loc.count_code_lines("") == 0


def test_loc_main_totals_a_directory_and_enforces_the_maximum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text('"""Doc."""\nz = 3\n', encoding="utf-8")

    assert loc.main([str(tmp_path), "--max", "3"]) == 0
    assert capsys.readouterr().out == "3 code lines in 2 files\n"
    assert loc.main([str(tmp_path), "--max", "2", "--files"]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == "3 code lines in 2 files"
    assert "     1  " in captured.out
    assert "exceed the limit of 2" in captured.err


def test_loc_python_files_expands_directories_in_sorted_order(tmp_path: Path) -> None:
    for name in ("b.py", "a.py", "notes.txt"):
        (tmp_path / name).write_text("", encoding="utf-8")

    assert loc.python_files([tmp_path]) == [tmp_path / "a.py", tmp_path / "b.py"]
    assert loc.python_files([tmp_path / "b.py"]) == [tmp_path / "b.py"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("src/treadle/expr/lexer.py", "expr/lexer.py"),
        ("/home/ada/treadle/src/treadle/cron.py", "cron.py"),
        ("src/treadle/__init__.py", "__init__.py"),
        ("tests/test_architecture.py", None),
        ("treadle.py", None),
    ],
)
def test_coverage_package_path(filename: str, expected: str | None) -> None:
    assert check_coverage.package_path(filename) == expected


REPORT = {
    "files": {
        "src/treadle/expr/lexer.py": {"summary": {"percent_covered": 94.9}},
        "src/treadle/cron.py": {"summary": {"percent_covered": 95.0}},
        "src/treadle/models.py": {"summary": {"percent_covered": 10.0}},
        "src/treadle/web/security.py": {"summary": {"percent_covered": 99.0}},
        "src/treadle/web/app.py": {"summary": {"percent_covered": 50.0}},
        "src/treadle/store/repo.py": {"summary": {"percent_covered": 80.0}},
    }
}


def test_coverage_failures_apply_floors_only_to_floored_files() -> None:
    assert check_coverage.failures(REPORT) == [("expr/lexer.py", 94.9), ("store/repo.py", 80.0)]
    assert check_coverage.failures(REPORT, 50.0) == []
    assert check_coverage.failures(REPORT, 95.0, ["web/*"]) == [("web/app.py", 50.0)]


def test_coverage_main_reports_files_below_their_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")

    assert check_coverage.main([str(report)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "error: src/treadle/expr/lexer.py has 94.9% coverage; the floor is 95%",
        "error: src/treadle/store/repo.py has 80.0% coverage; the floor is 95%",
    ]
    assert check_coverage.main([str(report), "--min", "80"]) == 0
    assert capsys.readouterr().out.startswith("Per-file coverage floors met (80%")


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        ("config/.env", True),
        (".env.production", True),
        (".env.example", False),
        ("deploy/server.pem", True),
        ("id_ed25519.key", True),
        ("keys.py", False),
        ("environment.md", False),
    ],
)
def test_hygiene_secret_files(path: str, secret: bool) -> None:
    assert check_hygiene.is_secret_file(path) is secret


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("-----BEGIN " + "RSA PRIVATE KEY-----", "a private key header"),
        ("-----BEGIN " + "PRIVATE KEY-----", "a private key header"),
        ("token: " + "gh" + "p_" + "a1" * 18, "a GitHub token"),
        ("github" + "_pat_" + "11AB" * 6, "a GitHub token"),
        ("SLACK=" + "xox" + "b-1234567890-abcdef", "a Slack bot token"),
        ("aws " + "AK" + "IA" + "ABCDEFGHIJKLMNOP", "an AWS access key ID"),
    ],
)
def test_hygiene_detects_secret_content(text: str, kind: str) -> None:
    assert check_hygiene.secrets_in(text) == [kind]


def test_hygiene_ignores_prose_hashes_and_its_own_source() -> None:
    assert check_hygiene.secrets_in("GitHub tokens start with gh" + "p_ or github" + "_pat_.") == []
    assert check_hygiene.secrets_in("sha256 = 3f9a1c0be2d4" * 4) == []
    own_source = (SCRIPTS / "check_hygiene.py").read_text(encoding="utf-8")
    assert check_hygiene.secrets_in(own_source) == []


def test_hygiene_problems_flags_secret_files_and_content(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text(
        "key " + "AK" + "IA" + "0123456789ABCDEF\n", encoding="utf-8"
    )
    (tmp_path / "clean.txt").write_text("nothing here\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("# NTFY_URL=\n", encoding="utf-8")
    files = [".env", ".env.example", "clean.txt", "deleted.txt", "notes.txt"]

    assert check_hygiene.problems(tmp_path, files) == [
        ".env: secret files must never be committed",
        "notes.txt: contains what looks like an AWS access key ID",
    ]
