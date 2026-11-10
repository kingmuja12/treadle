"""YAML loading with YAML 1.2 core-schema rules, exact positions, and rejected YAML features."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import yaml
from yaml.composer import ComposerError
from yaml.reader import ReaderError

from treadle.types import Issue, Position, Severity, YamlPath

MAX_FILE_BYTES: Final = 1 << 20

# Design note: the design sets no nesting limit, but the composer, the schema, and the
# expression compiler all recurse, so a deeply nested document is rejected as E050 before any
# of them can hit Python's recursion limit. 64 matches the JSON nesting limit elsewhere.
_MAX_DEPTH: Final = 64
_NULL: Final = "tag:yaml.org,2002:null"
_BOOL: Final = "tag:yaml.org,2002:bool"
_INT: Final = "tag:yaml.org,2002:int"
_FLOAT: Final = "tag:yaml.org,2002:float"
_TRUE: Final = frozenset({"true", "True", "TRUE"})
_SIGNS_AND_DIGITS: Final = "-+0123456789"
# The YAML 1.2 core schema, in priority order: (tag, pattern, possible first characters).
_CORE_SCHEMA: Final = (
    (_NULL, r"(?:~|null|Null|NULL|)\Z", ["~", "n", "N", ""]),
    (_BOOL, r"(?:true|True|TRUE|false|False|FALSE)\Z", list("tTfF")),
    (_INT, r"(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)\Z", list(_SIGNS_AND_DIGITS)),
    (
        _FLOAT,
        r"(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))\Z",
        list(_SIGNS_AND_DIGITS + "."),
    ),
)
_REPEAT_HINT: Final = "repeat the value where you need it"


@dataclass(frozen=True, slots=True)
class YamlDocument:
    """A loaded document with the position of every key and value.

    `data` holds plain values: dict, list, str, int, float, bool, or None. `styles` records
    each string scalar's style: "plain", "'", '"', "|", or ">". Paths name mapping entries by
    `str(key)` and list items by index.
    """

    data: object
    source: str
    value_positions: Mapping[YamlPath, Position]
    key_positions: Mapping[YamlPath, Position]
    styles: Mapping[YamlPath, str]
    # The source split into lines once, for position lookups; not a constructor argument.
    _lines: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lines", tuple(self.source.splitlines()))

    def position(self, path: YamlPath, *, key: bool = False) -> Position | None:
        """Return the position of a value (or its key), falling back to the nearest ancestor.

        A path through a shorthand resolves to the value as written: `("steps", 0, "needs",
        0)` finds `needs: build`, and a list index into a single mapping is skipped.
        """
        resolved = self._resolve(path)
        if key and resolved in self.key_positions:
            return self.key_positions[resolved]
        for end in range(len(resolved), -1, -1):
            found = self.value_positions.get(resolved[:end])
            if found is not None:
                return found
        return None

    def offset_position(self, path: YamlPath, offset: int) -> Position | None:
        """Map a character offset inside the string at `path` to a file position.

        Exact for single-line plain scalars, single-line single-quoted scalars without `''`
        before the offset, and literal block scalars; otherwise the scalar's start.
        """
        # Design note: a plain or single-quoted scalar is also exact on its first line when it
        # spans several, because the check below compares the value with the source text.
        resolved = self._resolve(path)
        start = self.value_positions.get(resolved)
        value = self._value(resolved)
        if start is None or not isinstance(value, str):
            return self.position(path)
        offset = min(max(offset, 0), len(value))
        style = self.styles.get(resolved)
        if style == "|":
            return self._literal_position(start, value, offset) or start
        if style == "plain":
            column = start.column
        elif style == "'" and "'" not in value[:offset]:
            column = start.column + 1
        else:
            return start
        # The scalar is exact up to the offset only when the source repeats it verbatim.
        line = self._line(start.line)
        if line is not None and line[column - 1 :].startswith(value[: offset + 1]):
            return Position(start.line, column + offset)
        return start

    def _resolve(self, path: YamlPath) -> YamlPath:
        """Return the longest prefix of `path` that exists, skipping shorthand levels."""
        if path in self.value_positions:
            return path
        node, resolved = self.data, []
        for part in path:
            if isinstance(node, list):
                if not (isinstance(part, int) and 0 <= part < len(node)):
                    break
                node = node[part]
            elif isinstance(node, dict):
                if isinstance(part, int):
                    continue  # one mapping that stands for a one-item list
                if part not in node:
                    break
                node = node[part]
            else:
                continue  # a scalar that stands for a list or a mapping
            resolved.append(part)
        return tuple(resolved)

    def _value(self, path: YamlPath) -> object:
        node = self.data
        for part in path:
            if isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
                node = node[part]
                continue
            if not (isinstance(node, dict) and part in node):
                return None
            node = node[part]
        return node

    def _line(self, number: int) -> str | None:
        return self._lines[number - 1] if 0 < number <= len(self._lines) else None

    def _literal_position(self, start: Position, value: str, offset: int) -> Position | None:
        """Map an offset in a `|` block: each value line is one source line after the header."""
        row = value.count("\n", 0, offset)
        begin = value.rfind("\n", 0, offset) + 1
        end = value.find("\n", offset)
        content = value[begin : end if end >= 0 else len(value)]
        number = start.line + 1 + row
        line = self._line(number)
        if line is None or not line.endswith(content):
            return None
        return Position(number, len(line) - len(content) + offset - begin + 1)


def load_yaml(text: str) -> tuple[YamlDocument | None, list[Issue]]:
    """Load one YAML document, reporting E050 to E054.

    Only YAML 1.2 booleans and nulls resolve, integers are decimal, `0o`, or `0x`, and dates
    stay strings. Duplicate keys (with both locations), merge keys, anchors, aliases, and tags
    are errors. The document is None whenever there is an issue.
    """
    issues: list[Issue] = []
    try:
        loader = _Loader(text, issues)
        try:
            root = loader.get_single_node()
        finally:
            loader.dispose()
    except ReaderError as exc:
        return None, [*issues, _reader_issue(text, exc)]
    except yaml.MarkedYAMLError as exc:
        return None, [*issues, _syntax_issue(exc)]
    builder = _Builder(issues)
    data = builder.build(root, ()) if root is not None else None
    if issues:
        return None, issues
    return YamlDocument(data, text, builder.values, builder.keys, builder.styles), []


class _Loader(yaml.SafeLoader):
    """A SafeLoader with YAML 1.2 core-schema resolvers.

    It reports anchors, aliases, and tags as it composes, and stops at `_MAX_DEPTH` levels.
    """

    def __init__(self, text: str, issues: list[Issue]) -> None:
        super().__init__(text)
        self._issues = issues
        self._depth = 0

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        # The PyYAML stubs leave peek_event and get_event unannotated.
        event: Any = self.peek_event()  # type: ignore[no-untyped-call]
        if self.check_event(yaml.AliasEvent):
            self.get_event()  # type: ignore[no-untyped-call]
            _report(self._issues, "E053", "aliases (`*name`) aren't supported", event, _REPEAT_HINT)
            return yaml.ScalarNode(_NULL, "", event.start_mark, event.end_mark)
        if event.anchor is not None:
            _report(self._issues, "E053", "anchors (`&name`) aren't supported", event, _REPEAT_HINT)
            event.anchor = None
        if getattr(event, "tag", None) is not None:
            hint = "remove the tag; quote a value to make it a string"
            _report(self._issues, "E054", "tags aren't supported", event, hint)
            event.tag = None
        self._depth += 1
        try:
            if self._depth > _MAX_DEPTH:
                problem = f"the document is nested more than {_MAX_DEPTH} levels deep"
                raise ComposerError(problem=problem, problem_mark=event.start_mark)
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


_Loader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first in _CORE_SCHEMA:
    _Loader.add_implicit_resolver(_tag, re.compile(_pattern), _first)


class _Builder:
    """Builds plain values from composed nodes, recording positions and styles by path."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        self.values: dict[YamlPath, Position] = {}
        self.keys: dict[YamlPath, Position] = {}
        self.styles: dict[YamlPath, str] = {}

    def build(self, node: yaml.Node, path: YamlPath) -> object:
        self.values.setdefault(path, _position(node.start_mark))
        if isinstance(node, yaml.ScalarNode):
            value = _scalar(node)
            if isinstance(value, str):
                self.styles.setdefault(path, node.style or "plain")
            return value
        if isinstance(node, yaml.SequenceNode):
            return [self.build(item, (*path, index)) for index, item in enumerate(node.value)]
        return self._mapping(node, path)

    def _mapping(self, node: yaml.Node, path: YamlPath) -> dict[object, object]:
        result: dict[object, object] = {}
        seen: dict[tuple[type, object], Position] = {}
        for key_node, value_node in node.value:
            where = _position(key_node.start_mark)
            if not isinstance(key_node, yaml.ScalarNode):
                # Design note: no code covers a list or mapping used as a key, and the loader
                # builds only string-keyed mappings, so a complex key is a YAML error (E050).
                self._issue("E050", "complex mapping keys aren't supported", where)
                continue
            if key_node.style is None and key_node.value == "<<":
                hint = "repeat the keys you need instead of merging them"
                self._issue("E052", "merge keys (`<<`) aren't supported", where, hint)
                continue
            key = _scalar(key_node)
            first = seen.setdefault((type(key), key), where)
            if first is not where:
                message = (
                    f"duplicate key '{key}': first defined at line {first.line}, "
                    f"column {first.column}"
                )
                self._issue("E051", message, where, "remove one of the two entries")
            child = (*path, str(key))
            self.keys.setdefault(child, where)
            result.setdefault(key, self.build(value_node, child))
        return result

    def _issue(self, code: str, message: str, where: Position, hint: str | None = None) -> None:
        self.issues.append(Issue(Severity.ERROR, code, message, position=where, hint=hint))


def _report(issues: list[Issue], code: str, message: str, event: Any, hint: str) -> None:
    position = _position(event.start_mark)
    issues.append(Issue(Severity.ERROR, code, message, position=position, hint=hint))


def _position(mark: Any) -> Position:
    return Position(mark.line + 1, mark.column + 1)


def _scalar(node: yaml.ScalarNode) -> object:
    text: str = node.value
    if node.tag == _NULL:
        return None
    if node.tag == _BOOL:
        return text in _TRUE
    if node.tag == _INT:
        return _integer(text)
    if node.tag == _FLOAT:
        return _float(text)
    return text


def _integer(text: str) -> int | float:
    if text.startswith("0o"):
        return int(text[2:], 8)
    if text.startswith("0x"):
        return int(text[2:], 16)
    try:
        return int(text)
    except ValueError:  # more digits than Python converts; the schema reports it out of range
        return float(text)


def _float(text: str) -> float:
    lowered = text.lower()
    if lowered.endswith(".inf"):
        return -math.inf if text.startswith("-") else math.inf
    if lowered == ".nan":
        return math.nan
    return float(text)


def _syntax_issue(exc: yaml.MarkedYAMLError) -> Issue:
    mark = exc.problem_mark or exc.context_mark
    message = exc.problem or exc.context or "invalid YAML"
    hint: str | None = None
    if message == "but found another document":
        message = "the file holds more than one YAML document"
        hint = "a workflow file holds one document; remove the extra `---`"
    elif "'\\t'" in message:
        hint = "indent with spaces; YAML doesn't allow tabs here"
    elif exc.problem and exc.context:
        context_mark = exc.context_mark
        where = (
            f", which starts at line {context_mark.line + 1}, column {context_mark.column + 1}"
            if context_mark is not None
            else ""
        )
        hint = f"{exc.context}{where}"
    position = _position(mark) if mark is not None else None
    return Issue(Severity.ERROR, "E050", message, position=position, hint=hint)


def _reader_issue(text: str, exc: ReaderError) -> Issue:
    offset: int = exc.position
    line_start = max(text.rfind(char, 0, offset) for char in "\n\r\x85\u2028\u2029") + 1
    line = len(text[:line_start].splitlines()) + 1
    message = f"unacceptable character U+{exc.character:04X}: {exc.reason}"
    position = Position(line, offset - line_start + 1)
    hint = "remove the control character; YAML files hold printable text only"
    return Issue(Severity.ERROR, "E050", message, position=position, hint=hint)
