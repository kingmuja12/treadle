"""Glob patterns for watch triggers: which files a watch reports, and which folders it skips."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

# Zero or more whole segments, each followed by "/": what a non-final "**" segment matches.
_ANY_DIRECTORIES: Final = "(?:[^/]+/)*"
_STAR: Final = "[^/]*"


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a glob to a regular expression.

    `*` and `?` never match `/`, `[...]` is a class (`[!...]` negates), and `**` as a whole
    segment matches zero or more segments. Raises ValueError for a bad pattern. Match the
    result with `fullmatch` against a path relative to the watched root. The `*` wildcards
    of a segment match in time linear in the name's length, however many there are.
    """
    # Relative paths never start or end with "/" or hold an empty segment, so a pattern that
    # does could never match; it is rejected instead of ignored silently.
    if not pattern:
        raise ValueError("the pattern is empty")
    if pattern.startswith("/") or pattern.endswith("/") or "//" in pattern:
        raise ValueError(
            "a pattern matches paths relative to the watched directory, so it can't start "
            "or end with '/' or contain '//'"
        )
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment != "**":
            parts.append(_segment(segment) + ("" if last else "/"))
            continue
        if parts and parts[-1] == _ANY_DIRECTORIES:
            parts.pop()  # "**/**" means the same as "**"
        parts.append(".*" if last else _ANY_DIRECTORIES)
    try:
        return re.compile("".join(parts))
    except re.error as exc:
        raise ValueError(f"invalid character class: {exc.msg}") from None


class GlobMatcher:
    """Decides which files a watch includes and which directories it prunes.

    A pattern without `/` matches the basename at any depth; one with `/` matches the whole
    relative path. Matching is case-sensitive.
    """

    def __init__(self, include: Sequence[str], exclude: Sequence[str]) -> None:
        self._include = [_compiled(pattern) for pattern in include]
        self._exclude = [_compiled(pattern) for pattern in exclude]

    def matches(self, rel_path: str) -> bool:
        """True when a file matches an include pattern and no exclude pattern."""
        return _any(self._include, rel_path) and not _any(self._exclude, rel_path)

    def prune(self, rel_dir: str) -> bool:
        """True when an exclude pattern matches a directory, so the scan doesn't enter it."""
        return _any(self._exclude, rel_dir)


def _compiled(pattern: str) -> tuple[re.Pattern[str], bool]:
    """Return the regex and whether it matches the whole path (True) or the basename."""
    return compile_glob(pattern), "/" in pattern


def _any(patterns: Sequence[tuple[re.Pattern[str], bool]], rel_path: str) -> bool:
    basename = rel_path.rpartition("/")[2]
    return any(regex.fullmatch(rel_path if whole else basename) for regex, whole in patterns)


def _segment(text: str) -> str:
    """Translate one path segment: wildcards, classes, and literal characters.

    A greedy `*` before more fixed text would let a near-miss name make the regex try every
    placement of the fixed parts, which takes polynomial time in the name's length. So each
    `*` but the segment's last matches lazily up to the next fixed part, inside an atomic
    group that never gives the match back. Fixed parts match a fixed number of characters,
    so their earliest placement is one that can succeed whenever any can. The last `*` stays
    greedy, so the rest of the segment can still end where the segment ends.
    """
    parts = _fixed_parts(text)
    if len(parts) == 1:
        return parts[0]
    first, *middle, last = parts
    lazy = "".join(f"(?>{_STAR}?{part})" for part in middle)
    return f"{first}{lazy}{_STAR}{last}"


def _fixed_parts(text: str) -> list[str]:
    """Split a segment at each run of `*` and translate the fixed parts between them.

    A segment with n runs of `*` has n + 1 parts. Only the first and the last can be empty.
    """
    parts: list[list[str]] = [[]]
    after_star = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "*":
            if not after_star:
                parts.append([])
            after_star = True
            index += 1
            continue
        after_star = False
        if char == "?":
            parts[-1].append("[^/]")
            index += 1
        elif char == "[":
            translated, index = _char_class(text, index)
            parts[-1].append(translated)
        else:
            parts[-1].append(re.escape(char))
            index += 1
    return ["".join(part) for part in parts]


def _char_class(text: str, start: int) -> tuple[str, int]:
    """Translate the class that starts at `text[start]` and return the index after it.

    A `]` right after `[` or `[!` is a literal member. A class never matches `/`.
    """
    index = start + 1
    negate = text.startswith("!", index)
    if negate:
        index += 1
    first = index
    if text.startswith("]", index):
        index += 1
    close = text.find("]", index)
    if close < 0:
        raise ValueError(f"the '[' at position {start + 1} has no closing ']'")
    members = "".join(f"\\{char}" if char in "\\^[]" else char for char in text[first:close])
    return f"(?!/)[{'^' if negate else ''}{members}]", close + 1
