from __future__ import annotations

import re
import time

import pytest

from treadle.globs import GlobMatcher, compile_glob


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.pdf", "report.pdf", True),
        ("*.pdf", "report.pdf.part", False),
        ("*.pdf", "docs/report.pdf", False),
        ("*", "", True),
        ("report?.pdf", "report1.pdf", True),
        ("report?.pdf", "report12.pdf", False),
        ("a?b", "a/b", False),
        ("a*b", "a/b", False),
        ("docs/*.pdf", "docs/report.pdf", True),
        ("docs/*.pdf", "docs/2026/report.pdf", False),
        ("docs/**/*.pdf", "docs/report.pdf", True),
        ("docs/**/*.pdf", "docs/2026/11/report.pdf", True),
        ("docs/**/*.pdf", "other/report.pdf", False),
        ("**/report.pdf", "report.pdf", True),
        ("**/report.pdf", "a/b/report.pdf", True),
        ("docs/**", "docs/a/b.txt", True),
        ("docs/**", "docs", False),
        ("docs/**/**", "docs/a", True),
        ("**/**/x", "a/x", True),
        ("**", "a/b/c", True),
        ("a**b", "axxb", True),
        ("a**b", "a/b", False),
        ("[abc].txt", "b.txt", True),
        ("[abc].txt", "d.txt", False),
        ("[a-c].txt", "c.txt", True),
        ("[!a-c].txt", "d.txt", True),
        ("[!a-c].txt", "a.txt", False),
        ("x[!a]y", "x/y", False),
        ("[]]", "]", True),
        ("[!]]", "a", True),
        ("[^]", "^", True),
        ("[\\]", "\\", True),
        ("a+b(c).txt", "a+b(c).txt", True),
        ("*.PDF", "report.pdf", False),
        ("*.tar.*", "a.b.tar.gz", True),
        ("*.tar.*", "a.tar", False),
        ("*-*", "a-b-c", True),
        ("*x*x*", "xx", True),
        ("*x*x*", "x", False),
        ("a*b*c", "abcbc", True),
        ("a*b*c", "abcbd", False),
        ("*[0-9]*.log", "app-2026.log", True),
        ("*?*", "", False),
        ("*b/*.pdf", "abxb/z.pdf", True),
        ("a*b*c/d*e", "abxbc/dxe", True),
        ("a*b*c/d*e", "abxbcx/dxe", False),
        ("**/x*-*-*.pdf", "deep/dir/x1-2-3.pdf", True),
    ],
)
def test_the_glob_table(pattern: str, path: str, expected: bool) -> None:
    assert bool(compile_glob(pattern).fullmatch(path)) is expected


def test_a_pattern_with_many_stars_rejects_a_long_near_miss_quickly() -> None:
    matcher = GlobMatcher(["*-*-*-*-*-*.pdf"], [])
    near_miss = "-".join(["a"] * 121) + ".txt"

    started = time.perf_counter()
    matched = matcher.matches(near_miss)
    elapsed = time.perf_counter() - started

    assert (len(near_miss), matched) == (245, False)
    # A backtracking translation takes over 8 seconds on this name.
    assert elapsed < 1.0


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("", "the pattern is empty"),
        ("/abs", "can't start or end with '/' or contain '//'"),
        ("dir/", "can't start or end with '/' or contain '//'"),
        ("a//b", "can't start or end with '/' or contain '//'"),
        ("*.[ch", "the '[' at position 3 has no closing ']'"),
        ("[!]", "the '[' at position 1 has no closing ']'"),
        ("[z-a]", "invalid character class: bad character range z-a"),
    ],
)
def test_bad_patterns_raise_value_error(pattern: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        compile_glob(pattern)


def test_a_pattern_without_a_slash_matches_the_basename_at_any_depth() -> None:
    matcher = GlobMatcher(["*.pdf"], [])

    assert matcher.matches("report.pdf")
    assert matcher.matches("2026/11/report.pdf")
    assert not matcher.matches("2026/report.txt")


def test_a_pattern_with_a_slash_matches_the_whole_relative_path() -> None:
    matcher = GlobMatcher(["inbox/*.csv"], [])

    assert matcher.matches("inbox/a.csv")
    assert not matcher.matches("other/inbox/a.csv")
    assert not matcher.matches("a.csv")


def test_a_file_must_match_an_include_and_no_exclude() -> None:
    matcher = GlobMatcher(["*"], [".*", "*~", "*.tmp", "build/**"])

    assert matcher.matches("notes.txt")
    assert not matcher.matches(".hidden")
    assert not matcher.matches("docs/.hidden")
    assert not matcher.matches("notes.txt~")
    assert not matcher.matches("build/out.o")
    assert matcher.matches("src/build/out.o")


def test_excludes_prune_directories_by_basename_or_relative_path() -> None:
    matcher = GlobMatcher(["*"], [".*", "node_modules", "cache/tmp"])

    assert matcher.prune(".git")
    assert matcher.prune("web/node_modules")
    assert matcher.prune("cache/tmp")
    assert not matcher.prune("other/cache/tmp")
    assert not matcher.prune("src")


def test_includes_never_prune() -> None:
    assert not GlobMatcher(["docs"], []).prune("docs")


def test_a_matcher_rejects_a_bad_pattern() -> None:
    with pytest.raises(ValueError, match="no closing"):
        GlobMatcher(["[a"], [])
