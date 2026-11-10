"""Every stub is implemented: no `NotImplementedError` remains in the package."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "treadle"


def _mentions(path: Path) -> list[int]:
    """Return the lines that name NotImplementedError in code, not in comments or strings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "NotImplementedError"
    ]


def test_no_module_raises_not_implemented_error() -> None:
    sources = sorted(SRC.rglob("*.py"))
    leftovers = {
        str(path.relative_to(SRC)): lines for path in sources if (lines := _mentions(path))
    }

    assert len(sources) > 60
    assert leftovers == {}
