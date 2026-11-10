"""CLI output: lines, tables, JSON, and status symbols, with optional color."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Final, TextIO

from treadle.types import Issue, JSONValue

# Each status shows a symbol before its name. A stream that can't encode the symbols gets the
# bracketed ASCII form instead. "ok" is how step tables label a succeeded step.
_SYMBOLS: Final[Mapping[str, tuple[str, str]]] = {
    "succeeded": ("✓", "[ok]"),
    "ok": ("✓", "[ok]"),
    "failed": ("✗", "[fail]"),
    "skipped": ("⊘", "[skip]"),
    "timed_out": ("⧖", "[time]"),
    "cancelled": ("⊗", "[cancel]"),
    "interrupted": ("↯", "[stop]"),
    "running": ("▸", "[run]"),
    "retrying": ("↻", "[retry]"),
    "queued": ("…", "[wait]"),
    "pending": ("…", "[wait]"),
}
_GREEN: Final = "32"
_RED: Final = "31"
_YELLOW: Final = "33"
_COLORS: Final[Mapping[str, str]] = {
    "succeeded": _GREEN,
    "ok": _GREEN,
    "failed": _RED,
    "timed_out": _RED,
    "skipped": _YELLOW,
    "cancelled": _YELLOW,
    "interrupted": _YELLOW,
}
# Every non-ASCII character the CLI prints outside user data: the symbols, the log gutter,
# and the separator in run summaries.
_PROBE: Final = "".join(symbol for symbol, _ in _SYMBOLS.values()) + "│·"
_ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")
_GAP: Final = "  "


class Output:
    """Writes command output to `out` and messages to `err`."""

    def __init__(self, out: TextIO, err: TextIO, *, color: bool) -> None:
        self._out = out
        self._err = err
        self._color = color
        self._unicode = _can_encode(out, _PROBE)

    def line(self, text: str = "", *, err: bool = False) -> None:
        """Write one line to stdout, or to stderr with `err`."""
        _write(self._err if err else self._out, text)

    def error(self, text: str) -> None:
        """Write one line to stderr with the "error: " prefix."""
        _write(self._err, f"{self._paint('error:', _RED)} {text}")

    def warn(self, text: str) -> None:
        """Write one line to stderr with the "warning: " prefix."""
        _write(self._err, f"{self._paint('warning:', _YELLOW)} {text}")

    def table(
        self, headers: Sequence[str], rows: Sequence[Sequence[str]], *, indent: int = 0
    ) -> None:
        """Write an aligned table: columns two spaces apart, each row indented by `indent`."""
        grid = [list(headers), *(list(row) for row in rows)]
        widths = [max(_width(row[column]) for row in grid) for column in range(len(headers))]
        for row in grid:
            cells = [
                cell + " " * (width - _width(cell)) for cell, width in zip(row, widths, strict=True)
            ]
            self.line((" " * indent + _GAP.join(cells)).rstrip())

    def json(self, value: JSONValue) -> None:
        """Write one indented JSON document."""
        _write(self._out, json.dumps(value, indent=2, ensure_ascii=not self._unicode))

    def json_line(self, value: JSONValue) -> None:
        """Write one compact JSON line (JSON Lines)."""
        text = json.dumps(value, ensure_ascii=not self._unicode, separators=(",", ":"))
        _write(self._out, text)

    def status(self, status: str, *, symbol_only: bool = False) -> str:
        """Return a status with its symbol: "✓ succeeded", or "[ok] succeeded" without UTF-8.

        With `symbol_only`, return the symbol alone. An unknown status comes back unchanged.
        """
        symbols = _SYMBOLS.get(status)
        if symbols is None:
            return status
        symbol = symbols[0] if self._unicode else symbols[1]
        return self._paint(symbol if symbol_only else f"{symbol} {status}", _COLORS.get(status))

    @property
    def unicode(self) -> bool:
        """True when stdout can encode the status symbols."""
        return self._unicode

    def _paint(self, text: str, color: str | None) -> str:
        if not self._color or color is None:
            return text
        return f"\x1b[{color}m{text}\x1b[0m"


def use_color(stream: TextIO, environ: Mapping[str, str], *, disabled: bool) -> bool:
    """True when `stream` is a TTY, color isn't disabled, and `NO_COLOR` isn't set.

    A `TERM` of "dumb" also turns color off.
    """
    if disabled or environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # no isatty, or a closed stream
        return False


def _render_issue(out: Output, issue: Issue, file: str, source: str | None) -> str:
    """Render an issue in the diagnostic format, in color when `out` uses color."""
    return issue.render(file, source, color=out._color)


def _discard(stream: TextIO) -> None:
    """Point a stream's file descriptor at /dev/null, because nothing reads it any more.

    Buffered output and the interpreter's final flush then go nowhere instead of failing
    again. A stream without a descriptor, such as a test's capture, stays as it is.
    """
    try:
        fileno = stream.fileno()
    except (OSError, ValueError):
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fileno)
    finally:
        os.close(devnull)


def _can_encode(stream: TextIO, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _write(stream: TextIO, text: str) -> None:
    """Write a line and flush it, replacing characters that the stream can't encode."""
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding) + "\n")
    stream.flush()


def _width(cell: str) -> int:
    """Return the printed width of a table cell, ignoring color codes."""
    return len(_ANSI.sub("", cell))
