"""`treadle init`: the files of a new project."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Final

from treadle.config import CONFIG_FILENAME, default_config_text

# Each project file, relative to the project directory, and the template it comes from.
_TEMPLATES: Final = (
    ("workflows/hello.yml", "hello.yml"),
    ("workflows/site-check.yml", "site-check.yml"),
    (".env.example", "env.example"),
)
_GITIGNORE: Final = ".gitignore"
_GITIGNORE_HEADER: Final = "# Treadle: secrets and local state"
_GITIGNORE_LINES: Final = (".env", ".env.*", "!.env.example", ".treadle/")


def scaffold(target: Path, *, force: bool, timezone: str) -> list[tuple[Path, str]]:
    """Create the project files in `target` and report what happened to each one.

    Returns each path with "created", "skipped", or "updated". Existing files are kept
    unless `force` is set. `.gitignore` is never replaced: the lines Treadle needs are
    appended when they're missing, with or without `force`.
    """
    files = [(Path(CONFIG_FILENAME), default_config_text(timezone))]
    files.extend((Path(relative), _template(name)) for relative, name in _TEMPLATES)
    target.mkdir(parents=True, exist_ok=True)
    results = [
        (target / relative, _place(target / relative, text, force)) for relative, text in files
    ]
    gitignore = target / _GITIGNORE
    results.append((gitignore, _extend_gitignore(gitignore)))
    return results


def _template(name: str) -> str:
    return (resources.files("treadle.cli") / "templates" / name).read_text(encoding="utf-8")


def _place(path: Path, text: str, force: bool) -> str:
    """Write one file unless it exists; with `force`, replace it when its content differs."""
    if path.exists() or path.is_symlink():
        if not force or _same_text(path, text):
            return "skipped"
        path.write_text(text, encoding="utf-8")
        return "updated"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        file.write(text)
    return "created"


def _same_text(path: Path, text: str) -> bool:
    try:
        return path.read_text(encoding="utf-8") == text
    except (OSError, UnicodeDecodeError):
        return False


def _extend_gitignore(path: Path) -> str:
    """Append the missing ignore lines under a comment, creating the file when needed."""
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        path.write_text("\n".join([_GITIGNORE_HEADER, *_GITIGNORE_LINES]) + "\n", encoding="utf-8")
        return "created"
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _GITIGNORE_LINES if line not in present]
    if not missing:
        return "skipped"
    # The block starts on a new line, separated from earlier content by a blank line.
    lead = "" if not existing or existing.endswith("\n") else "\n"
    lead += "\n" if existing.strip() else ""
    with path.open("a", encoding="utf-8") as file:
        file.write(lead + "\n".join([_GITIGNORE_HEADER, *missing]) + "\n")
    return "updated"
