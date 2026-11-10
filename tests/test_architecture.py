"""Enforce section 4's layer map, third-party import rules, and package `__init__` rules.

Every import counts, including imports under `if TYPE_CHECKING:` and inside functions. A
module may import from any lower layer and from modules listed earlier in its own layer.
"""

from __future__ import annotations

import ast
import importlib
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "treadle"

LAYERS: tuple[tuple[str, ...], ...] = (
    ("types", "errors", "units", "clock", "expr.ast"),
    ("models", "schema"),
    ("config", "protocols", "steps.base"),
    (
        "expr.lexer",
        "expr.parser",
        "expr.analysis",
        "expr.template",
        "expr.functions",
        "expr.evaluator",
        "cron",
        "yamlio",
        "globs",
        "fswatch",
        "secrets",
        "lock",
    ),
    (
        "store.db",
        "store.migrations",
        "store.repo",
        "store.async_store",
        "steps.procs",
        "steps.shell",
        "steps.http",
        "steps.file",
        "steps.log",
        "steps.sleep",
        "steps.builtin",
    ),
    (
        "workflow.inputs",
        "workflow.graph",
        "workflow.lint",
        "workflow.loader",
        "serialize",
        "retention",
    ),
    ("engine.masking", "engine.context", "engine.logsink", "engine.executor", "engine.manager"),
    (
        "registry",
        "triggers.scheduler",
        "triggers.watch",
        "triggers.webhook",
        "triggers.manager",
        "control",
    ),
    ("web.security", "web.api", "web.dashboard", "web.hooks", "web.app", "runner", "daemon"),
    (
        "cli.output",
        "cli.scaffold",
        "cli.follow",
        "cli.commands",
        "cli.parser",
        "cli.main",
        "__main__",
    ),
)
PACKAGES = (
    "treadle",
    "treadle.cli",
    "treadle.engine",
    "treadle.expr",
    "treadle.steps",
    "treadle.store",
    "treadle.triggers",
    "treadle.web",
    "treadle.workflow",
)
THIRD_PARTY = {
    "yaml": ("yamlio",),
    "httpx": ("steps.base", "steps.http", "engine.executor", "runner", "daemon"),
    "starlette": ("web.*",),
    "jinja2": ("web.*",),
    "uvicorn": ("daemon",),
}

# Every package __init__ is in layer 0, before every module.
RANKS = dict.fromkeys(PACKAGES, (0, -1))
RANKS.update(
    {
        f"treadle.{name}": (layer, position)
        for layer, names in enumerate(LAYERS)
        for position, name in enumerate(names)
    }
)
MODULES = sorted(set(RANKS) - set(PACKAGES))


def module_name(path: Path) -> str:
    """Return the dotted name of a source file, such as `treadle.expr.ast`."""
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def source_files() -> dict[str, Path]:
    """Return every Python file under `src/treadle`, by module name."""
    return {module_name(path): path for path in sorted(SRC.rglob("*.py"))}


def imports(path: Path) -> list[tuple[str, int, int]]:
    """Return (module, relative level, line) for every import in a file.

    `from package import name` resolves to `package.name` when that's a Treadle module.
    """
    found: list[tuple[str, int, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, 0, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                found.append((candidate if candidate in RANKS else base, node.level, node.lineno))
    return found


def allowed_third_party(top_level: str, module: str) -> bool:
    """True when the third-party package `top_level` may be imported by `module`."""
    short = module.removeprefix("treadle.")
    return any(fnmatch(short, pattern) for pattern in THIRD_PARTY.get(top_level, ()))


def test_every_listed_module_exists() -> None:
    missing = sorted(set(RANKS) - set(source_files()))
    assert not missing, f"modules in the layer map without a file: {missing}"


def test_no_unlisted_modules() -> None:
    extra = sorted(set(source_files()) - set(RANKS))
    assert not extra, f"modules that the layer map doesn't list: {extra}"


@pytest.mark.parametrize("module", sorted(RANKS))
def test_imports_follow_the_layer_and_third_party_rules(module: str) -> None:
    path = source_files()[module]
    problems = []
    for imported, level, line in imports(path):
        top_level = imported.partition(".")[0]
        if level:
            problems.append(f"line {line}: relative import")
        elif top_level == "treadle":
            if imported not in RANKS:
                problems.append(f"line {line}: unknown module {imported}")
            elif RANKS[imported] >= RANKS[module]:
                problems.append(f"line {line}: {imported} is not in a lower layer or earlier")
        elif top_level not in sys.stdlib_module_names and not allowed_third_party(
            top_level, module
        ):
            problems.append(f"line {line}: third-party import {imported}")
    assert not problems, f"{module}:\n" + "\n".join(problems)


@pytest.mark.parametrize("package", PACKAGES)
def test_package_init_holds_only_a_docstring(package: str) -> None:
    tree = ast.parse(source_files()[package].read_text(encoding="utf-8"))
    body = list(tree.body)
    assert ast.get_docstring(tree), f"{package}/__init__.py needs a docstring"
    body.pop(0)
    if package == "treadle":
        assert [ast.unparse(node) for node in body] == ["__version__ = '0.1.0'"]
    else:
        assert body == []


@pytest.mark.parametrize("module", MODULES)
def test_module_starts_with_future_annotations(module: str) -> None:
    tree = ast.parse(source_files()[module].read_text(encoding="utf-8"))
    statements = tree.body[1:] if ast.get_docstring(tree) else tree.body
    first = statements[0] if statements else None
    assert isinstance(first, ast.ImportFrom), f"{module} must start with the future import"
    assert first.module == "__future__"
    assert [alias.name for alias in first.names] == ["annotations"]


@pytest.mark.parametrize("module", [name for name in sorted(RANKS) if name != "treadle.__main__"])
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module).__name__ == module


def test_import_scanner_sees_every_kind_of_import(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "from typing import TYPE_CHECKING\n"
        "from treadle import models\n"
        "from treadle.types import Issue\n"
        "from . import sibling\n"
        "if TYPE_CHECKING:\n"
        "    import httpx\n"
        "def later():\n"
        "    import yaml\n",
        encoding="utf-8",
    )
    assert imports(source) == [
        ("typing", 0, 1),
        ("treadle.models", 0, 2),
        ("treadle.types", 0, 3),
        ("", 1, 4),
        ("httpx", 0, 6),
        ("yaml", 0, 8),
    ]


def test_third_party_allowlist_matches_module_patterns() -> None:
    assert allowed_third_party("starlette", "treadle.web.api")
    assert allowed_third_party("httpx", "treadle.steps.base")
    assert not allowed_third_party("starlette", "treadle.daemon")
    assert not allowed_third_party("requests", "treadle.steps.http")
