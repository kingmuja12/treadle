#!/usr/bin/env python3
"""Fail when the repository holds secret files or secret-looking content.

The script checks every file that `git ls-files --cached --others --exclude-standard` lists,
so files that aren't committed yet are checked too. It flags `.env` files other than
`.env.example`, `*.pem` and `*.key` files, private-key headers, GitHub and Slack tokens, and
AWS access key IDs. Each pattern is built from pieces, so this file never matches itself.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

SECRET_PATTERNS = {
    "a private key header": re.compile("-----BEGIN " + r"(?:[A-Z0-9]+ )*" + "PRIVATE KEY-----"),
    "a GitHub token": re.compile(
        r"\b(?:" + "gh" + r"p_[A-Za-z0-9]{20,}|" + "github" + r"_pat_[A-Za-z0-9_]{20,})"
    ),
    "a Slack bot token": re.compile(r"\b" + "xox" + r"b-[A-Za-z0-9-]{10,}"),
    "an AWS access key ID": re.compile(r"\b(?:" + "AK" + "IA|AS" + r"IA)[0-9A-Z]{16}\b"),
}
_SECRET_FILES = ("*.pem", "*.key")


def is_secret_file(path: str) -> bool:
    """True for `.env`, `.env.*` except `.env.example`, `*.pem`, and `*.key`."""
    name = PurePosixPath(path).name
    if name == ".env.example":
        return False
    secret_type = any(fnmatch(name, pattern) for pattern in _SECRET_FILES)
    return name == ".env" or name.startswith(".env.") or secret_type


def secrets_in(text: str) -> list[str]:
    """Return the kinds of secrets that `text` appears to contain."""
    return [kind for kind, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def listed_files(root: Path) -> list[str]:
    """Return the files git tracks or would track, relative to `root`."""
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    output = subprocess.run(command, cwd=root, check=True, capture_output=True).stdout
    return sorted({name for name in output.decode("utf-8").split("\0") if name})


def problems(root: Path, files: Iterable[str]) -> list[str]:
    """Return one message per secret file or secret-looking content among `files`."""
    found: list[str] = []
    for name in files:
        if is_secret_file(name):
            found.append(f"{name}: secret files must never be committed")
            continue
        path = root / name
        if path.is_file():
            text = path.read_bytes().decode("utf-8", errors="replace")
            found.extend(f"{name}: contains what looks like {kind}" for kind in secrets_in(text))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Print every problem and return 1 when there is any."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    files = listed_files(args.root)
    found = problems(args.root, files)
    for message in found:
        print(f"error: {message}")
    if found:
        return 1
    print(f"Repository hygiene checks passed ({len(files)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
