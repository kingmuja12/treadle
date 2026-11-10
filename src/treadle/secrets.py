"""Secrets from `.env` and `TREADLE_SECRET_*` variables, and the `.env` parser."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from treadle.errors import ConfigError

SECRET_PREFIX: Final = "TREADLE_SECRET_"  # noqa: S105 - a variable-name prefix, not a secret
# One `.env` entry: a blank or comment line, or KEY=VALUE where the value is double-quoted
# (escapes, may span lines), single-quoted (literal), or plain (trimmed; ` #` ends it).
# Design note: section 4 lets only double-quoted values span lines, so single-quoted ones
# end at the line; an escape other than \n, \t, \", and \\ stays as written.
_ENTRY: Final = re.compile(
    r"""[ \t]*(?:\#[^\n]*)?\n
    | [ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=
      (?: [ \t]*"(?P<double>(?:[^"\\]|\\.)*)"[ \t]*(?:\#[^\n]*)?
        | [ \t]*'(?P<single>[^'\n]*)'[ \t]*(?:\#[^\n]*)?
        | (?![ \t]*["'])(?P<plain>[^\n]*) )\n""",
    re.VERBOSE | re.DOTALL,
)
_ASSIGNMENT: Final = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")
_ESCAPE: Final = re.compile(r"\\(.)", re.DOTALL)
_ESCAPES: Final = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
_COMMENT: Final = re.compile(r"[ \t]#")
_log = logging.getLogger(__name__)


def parse_dotenv(text: str, *, source: str) -> dict[str, str]:
    """Parse `.env` text: `KEY=VALUE` and `export KEY=VALUE` lines, comments, and blank lines.

    Unquoted values are trimmed and ` #` starts a comment. Single quotes are literal; double
    quotes may span lines and support `\\n`, `\\t`, `\\"`, and `\\\\`, while other escapes
    stay as written. A repeated key keeps the last value. Raises ConfigError naming `source`
    and the line, never the value.
    """
    text = text.replace("\r\n", "\n")
    text += "" if text.endswith("\n") else "\n"
    values: dict[str, str] = {}
    position = 0
    while position < len(text):
        entry = _ENTRY.match(text, position)
        if entry is None:
            raise ConfigError(_syntax_error(text, position, source))
        position = entry.end()
        if entry["key"] is not None:
            values[entry["key"]] = _value(entry)
    return values


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    """An immutable view of the secrets; implements `SecretLookup`.

    `secrets` holds the `.env` entries plus `TREADLE_SECRET_*` variables with the prefix
    removed, the variables winning.
    """

    secrets: Mapping[str, str]
    dotenv: Mapping[str, str]
    environ: Mapping[str, str]

    def get(self, name: str) -> str | None:
        """Return `secrets.NAME`: `TREADLE_SECRET_<name>` first, then `.env`."""
        return self.secrets.get(name)

    def lookup_env(self, name: str) -> str | None:
        """Return a variable from the process environment, then from `.env`."""
        value = self.environ.get(name)
        return self.dotenv.get(name) if value is None else value

    def mask_values(self, hidden_env: Iterable[str] = ()) -> frozenset[str]:
        """Return every secret value plus `lookup_env` of each hidden variable name."""
        hidden = (self.lookup_env(name) for name in hidden_env)
        return frozenset(self.secrets.values()) | {value for value in hidden if value is not None}


class SecretStore:
    """Loads secrets and reloads them when `.env` changes.

    Until `load()` runs, the snapshot holds only the `TREADLE_SECRET_*` variables.
    """

    def __init__(self, env_file: Path | None, environ: Mapping[str, str]) -> None:
        self._env_file = env_file
        self._environ = dict(environ)
        self._stamp: tuple[int, int, int] | None = None  # of the `.env` version loaded
        self._snapshot = self._build({})

    def load(self) -> SecretSnapshot:
        """Read `.env` and return a new snapshot. Raises ConfigError on a parse error.

        A missing `.env` file, or no `env_file` at all, means no `.env` entries.
        """
        stamp, dotenv = self._file_stamp(), self._read()
        self._stamp, self._snapshot = stamp, self._build(dotenv)
        return self._snapshot

    def reload_if_changed(self) -> bool:
        """Reload when `.env` changed, and return True when it did.

        A parse error keeps the old snapshot and logs a warning, once per change.
        """
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return False
        try:
            self.load()
        except ConfigError as error:
            self._stamp = stamp
            _log.warning("%s; keeping the previous secrets", error)
            return False
        return True

    @property
    def snapshot(self) -> SecretSnapshot:
        """The current snapshot."""
        return self._snapshot

    def permission_warning(self) -> str | None:
        """Return a warning when `.env` is readable by group or others, else None."""
        path = self._env_file
        try:
            mode = 0 if path is None else path.stat().st_mode
        except OSError:
            return None
        if mode & 0o044:
            return f"{path} is readable by other users; run `chmod 600 {path}`"
        return None

    def _file_stamp(self) -> tuple[int, int, int] | None:
        """Identify the current `.env` file version, or None when there is none."""
        try:
            info = self._env_file.stat() if self._env_file is not None else None
        except OSError:
            return None
        return None if info is None else (info.st_mtime_ns, info.st_size, info.st_ino)

    def _read(self) -> dict[str, str]:
        path = self._env_file
        if path is None:
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError:
            raise ConfigError(f"{path} isn't valid UTF-8") from None
        except OSError as error:
            raise ConfigError(f"cannot read {path}: {error.strerror}") from None
        return parse_dotenv(text, source=str(path))

    def _build(self, dotenv: dict[str, str]) -> SecretSnapshot:
        prefixed = {
            name.removeprefix(SECRET_PREFIX): value
            for name, value in self._environ.items()
            if name.startswith(SECRET_PREFIX) and name != SECRET_PREFIX
        }
        return SecretSnapshot({**dotenv, **prefixed}, dotenv, self._environ)


def _value(entry: re.Match[str]) -> str:
    """Return the value of a matched `.env` assignment."""
    if entry["double"] is not None:
        return _ESCAPE.sub(lambda escape: _ESCAPES.get(escape[1], escape[0]), entry["double"])
    if entry["single"] is not None:
        return entry["single"]
    return _COMMENT.split(entry["plain"], maxsplit=1)[0].strip()


def _syntax_error(text: str, position: int, source: str) -> str:
    """Describe the entry at `position` without quoting any of it except a valid key."""
    line = text.count("\n", 0, position) + 1
    where = f"{source}:{line}"
    assignment = _ASSIGNMENT.match(text, position)
    if assignment is not None:
        return f"{where}: the value of {assignment[1]} has no closing quote, or text after it"
    return f"{where}: expected KEY=VALUE, a comment, or a blank line"
