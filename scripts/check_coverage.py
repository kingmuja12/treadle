#!/usr/bin/env python3
"""Enforce per-file coverage floors from a coverage.py JSON report.

Run `pytest --cov --cov-report=json` first, then `python3 scripts/check_coverage.py coverage.json`.
Each file that matches a floored pattern must reach the minimum (95% by default), counting
statements and branches together.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

FLOORED_PATTERNS = (
    "expr/*",
    "cron.py",
    "store/*",
    "engine/*",
    "triggers/*",
    "workflow/loader.py",
    "web/security.py",
)
DEFAULT_MINIMUM = 95.0


def package_path(filename: str) -> str | None:
    """Return a report filename relative to the `treadle` package, or None outside it."""
    parts = Path(filename).as_posix().split("/")
    directories = parts[:-1]
    if "treadle" not in directories:
        return None
    package = len(directories) - 1 - directories[::-1].index("treadle")
    return "/".join(parts[package + 1 :])


def failures(
    report: Mapping[str, Any],
    minimum: float = DEFAULT_MINIMUM,
    patterns: Iterable[str] = FLOORED_PATTERNS,
) -> list[tuple[str, float]]:
    """Return each floored file below `minimum`, with its coverage percentage."""
    floored = tuple(patterns)
    below: list[tuple[str, float]] = []
    for filename, data in sorted(report["files"].items()):
        relative = package_path(filename)
        if relative is None or not any(fnmatch(relative, pattern) for pattern in floored):
            continue
        percent = float(data["summary"]["percent_covered"])
        if percent < minimum:
            below.append((relative, percent))
    return below


def main(argv: Sequence[str] | None = None) -> int:
    """Print the files below their floor and return 1 when there is any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", type=Path, help="the coverage.json file")
    parser.add_argument("--min", type=float, default=DEFAULT_MINIMUM, help="minimum percent")
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    below = failures(report, args.min)
    for relative, percent in below:
        print(
            f"error: src/treadle/{relative} has {percent:.1f}% coverage; the floor is {args.min:g}%"
        )
    if below:
        return 1
    print(f"Per-file coverage floors met ({args.min:g}% for {', '.join(FLOORED_PATTERNS)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
