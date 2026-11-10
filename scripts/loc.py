#!/usr/bin/env python3
"""Count code lines: non-blank lines that aren't comments and aren't part of a docstring.

By default the script counts `src/treadle`. Pass files or directories to count something else,
`--files` to print one count per file, and `--max N` to fail when the total exceeds N.
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "src" / "treadle"
_NON_CODE_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)
_DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def docstring_lines(tree: ast.AST) -> set[int]:
    """Return the line numbers that module, class, and function docstrings occupy."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCUMENTED) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def count_code_lines(source: str) -> int:
    """Return the number of code lines in Python source text."""
    code: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in _NON_CODE_TOKENS:
            code.update(range(token.start[0], token.end[0] + 1))
    return len(code - docstring_lines(ast.parse(source)))


def python_files(paths: Iterable[Path]) -> list[Path]:
    """Expand directories into their `*.py` files, recursively and sorted."""
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


def main(argv: Sequence[str] | None = None) -> int:
    """Print the code-line total and return 1 when it exceeds `--max`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories to count")
    parser.add_argument("--max", type=int, help="fail when the total exceeds this many lines")
    parser.add_argument("--files", action="store_true", help="print a count for each file")
    args = parser.parse_args(argv)
    files = python_files(args.paths or [DEFAULT_ROOT])
    total = 0
    for path in files:
        count = count_code_lines(path.read_text(encoding="utf-8"))
        total += count
        if args.files:
            print(f"{count:6d}  {path}")
    print(f"{total} code lines in {len(files)} files")
    if args.max is not None and total > args.max:
        print(f"error: {total} code lines exceed the limit of {args.max}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
