"""Value types that every layer shares: JSON aliases, exit codes, diagnostics, and lock holders."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import IntEnum, StrEnum
from typing import Final, Literal, TypeAlias, TypeGuard

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]
PathPart: TypeAlias = str | int
YamlPath: TypeAlias = tuple[PathPart, ...]

_LOCK_KINDS: Final = ("serve", "run", "maintenance")
_RESET: Final = "\x1b[0m"


class ExitCode(IntEnum):
    """Exit codes of the `treadle` CLI. Section 9 defines what each one means."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    CONFIG = 3
    NOT_FOUND = 4
    CANCELLED = 5
    TIMED_OUT = 6
    SKIPPED = 7
    BUSY = 8
    INTERRUPTED = 130


class Severity(StrEnum):
    """How serious an `Issue` is. `treadle validate --strict` treats warnings as failures."""

    ERROR = "error"
    WARNING = "warning"


_COLORS: Final = {Severity.ERROR: "\x1b[31m", Severity.WARNING: "\x1b[33m"}


@dataclass(frozen=True, slots=True)
class Position:
    """A location in a source file. Both numbers are 1-based."""

    line: int
    column: int


@dataclass(frozen=True, slots=True)
class Issue:
    """One load-time diagnostic with a stable code, such as `E201` or `W101`.

    `path` locates the value inside the YAML or TOML document. `position` is the file location,
    when the loader could map the path to one.
    """

    severity: Severity
    code: str
    message: str
    path: YamlPath = ()
    position: Position | None = None
    hint: str | None = None

    def render(self, file: str, source: str | None = None, *, color: bool = False) -> str:
        """Render the issue in the rustc-style diagnostic format that section 3 shows.

        With a position, the first line is `file:line:column: severity[code]: message`, followed
        by the source line and a caret when `source` holds that line. Without a position, the
        first line names the YAML path instead. A hint adds a final `= help:` line.
        """
        severity = self.severity.value
        if color:
            severity = f"{_COLORS[self.severity]}{severity}{_RESET}"
        summary = f"{severity}[{self.code}]: {self.message}"
        # Design note: the gutter is at least two digits wide, so `= help:` lines up with the
        # `|` gutter and matches the design's literal "   = help: {hint}" for most files. An
        # empty path drops the "(at ...)" suffix instead of printing "(at )".
        gutter = "   "
        if self.position is None:
            where = f" (at {format_path(self.path)})" if self.path else ""
            lines = [f"{file}: {summary}{where}"]
        else:
            line, column = self.position.line, self.position.column
            lines = [f"{file}:{line}:{column}: {summary}"]
            excerpt = _excerpt(source, self.position)
            if excerpt:
                gutter = " " * (max(len(str(line)), 2) + 1)
                lines.extend(excerpt)
        if self.hint:
            lines.append(f"{gutter}= help: {self.hint}")
        return "\n".join(lines)

    def to_json(self, file: str) -> JSONObject:
        """Return the JSON shape the API and `validate --json` share, with null for unknowns.

        `path` is the formatted YAML path, or null for an issue about the whole file.
        """
        # Design note: section 4 lists seven keys but the signature takes `file`, and a
        # signature beats prose, so the shape also carries "file".
        position = self.position
        return {
            "file": file,
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "path": format_path(self.path) if self.path else None,
            "line": position.line if position else None,
            "column": position.column if position else None,
            "hint": self.hint,
        }


@dataclass(frozen=True, slots=True)
class LockHolder:
    """The process that holds the state-directory lock, as recorded in `holder.json`."""

    pid: int
    kind: Literal["serve", "run", "maintenance"]
    started_at: str
    version: str
    host: str | None = None
    port: int | None = None

    def to_json(self) -> JSONObject:
        """Return the `holder.json` document."""
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> LockHolder | None:
        """Parse a `holder.json` document. Return None when it's malformed, never raise."""
        if not isinstance(data, dict):
            return None
        pid, kind, host, port = (
            data.get("pid"),
            data.get("kind"),
            data.get("host"),
            data.get("port"),
        )
        started_at, version = data.get("started_at"), data.get("version")
        if (
            _is_int(pid)
            and kind in _LOCK_KINDS
            and isinstance(started_at, str)
            and isinstance(version, str)
            and (host is None or isinstance(host, str))
            and (port is None or _is_int(port))
        ):
            return cls(pid, kind, started_at, version, host, port)
        return None


def format_path(path: YamlPath) -> str:
    """Format a YAML path for messages: `("steps", 2, "with", "url")` -> `steps[2].with.url`."""
    text = ""
    for part in path:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text


def _excerpt(source: str | None, position: Position) -> list[str]:
    """Return the gutter, source, and caret lines for `position`, or [] without that line."""
    lines = source.splitlines() if source is not None else []
    if not 0 < position.line <= len(lines):
        return []
    text = lines[position.line - 1]
    number = str(position.line).rjust(2)
    pad = " " * len(number)
    lead = "".join(char if char == "\t" else " " for char in text[: position.column - 1])
    return [f"{pad} |", f"{number} | {text}", f"{pad} | {lead}^"]


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
