"""The expression functions.

This module also holds the value rules that the evaluator reuses, because the layer order puts
it first: display strings, `==`, numeric strings, and the type names in messages.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from treadle.errors import EvaluationError
from treadle.expr.lexer import _NUMBER
from treadle.types import JSONValue

# The longest string that rendering or a function may build: 1 MiB of characters.
_MAX_CHARS: Final = 1 << 20
_MAX_JSON_DEPTH: Final = 64
_FORMAT_TOKEN: Final = re.compile(r"\{\{|\}\}|\{([0-9]{1,4})\}|[{}]")


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    """A function's name and arity. `max_args` is None for a variadic function (`format`)."""

    name: str
    min_args: int
    max_args: int | None
    status: bool = False

    def arity_problem(self, count: int) -> str | None:
        """Return the message for a call with `count` arguments, or None when it's allowed.

        The loader reports the message as E206, and a call at run time raises it.
        """
        low, high = self.min_args, self.max_args
        if low <= count and (high is None or count <= high):
            return None
        expected = (
            f"at least {low}" if high is None else str(low) if low == high else f"{low} or {high}"
        )
        return f"wrong number of arguments for {self.name}(): expected {expected}, got {count}"


# Every function, the four status functions included.
FUNCTIONS: Final[Mapping[str, FunctionSpec]] = {
    spec.name: spec
    for spec in (
        FunctionSpec("contains", 2, 2),
        FunctionSpec("startsWith", 2, 2),
        FunctionSpec("endsWith", 2, 2),
        FunctionSpec("format", 1, None),
        FunctionSpec("join", 1, 2),
        FunctionSpec("toJSON", 1, 1),
        FunctionSpec("fromJSON", 1, 1),
        FunctionSpec("lower", 1, 1),
        FunctionSpec("upper", 1, 1),
        FunctionSpec("trim", 1, 1),
        FunctionSpec("length", 1, 1),
        FunctionSpec("default", 2, 2),
        FunctionSpec("success", 0, 0, status=True),
        FunctionSpec("failure", 0, 0, status=True),
        FunctionSpec("always", 0, 0, status=True),
        FunctionSpec("cancelled", 0, 0, status=True),
    )
}


def call_function(name: str, args: Sequence[JSONValue], *, source: str, offset: int) -> JSONValue:
    """Call a non-status function with evaluated arguments.

    Raises EvaluationError, with `source` and `offset`, for an unknown function, a wrong
    number of arguments, or invalid argument values.
    """
    spec = FUNCTIONS.get(name)
    if spec is None or spec.status:
        raise EvaluationError(f"unknown function '{name}'", source=source, offset=offset)
    problem = spec.arity_problem(len(args))
    if problem is not None:
        raise EvaluationError(problem, source=source, offset=offset)
    try:
        return _IMPLEMENTATIONS[name](*args)
    except _ArgumentError as error:
        raise EvaluationError(f"{name}() {error}", source=source, offset=offset) from None


class _ArgumentError(Exception):
    """An invalid argument value. `call_function` adds the function name and location."""


def _contains(haystack: JSONValue, needle: JSONValue) -> bool:
    if isinstance(haystack, str):
        return needle is not None and _display(needle) in haystack
    if isinstance(haystack, list):
        # A null needle never matches in a string, but array membership uses ==, so an
        # array that holds null contains null.
        return any(_equals(item, needle) for item in haystack)
    if haystack is None:
        return False
    raise _ArgumentError(f"expects a string or an array, got {_kind(haystack)}")


def _format(fmt: JSONValue, *args: JSONValue) -> str:
    text, values = _text(fmt), [_display(arg) for arg in args]
    pieces: list[str] = []
    position = 0
    for match in _FORMAT_TOKEN.finditer(text):
        pieces.extend((text[position : match.start()], _placeholder(match, values)))
        position = match.end()
    pieces.append(text[position:])
    return _joined(pieces)


def _placeholder(match: re.Match[str], values: Sequence[str]) -> str:
    """Return the text for one `{N}`, `{{`, or `}}`, or raise for a stray brace."""
    token = match.group()
    if token in ("{{", "}}"):
        return token[0]
    if match.group(1) is None:
        raise _ArgumentError(f"has a stray '{token}' at index {match.start()}")
    index = int(match.group(1))
    if index >= len(values):
        raise _ArgumentError(f"has no argument for {{{index}}}")
    return values[index]


def _join(items: JSONValue, separator: JSONValue = ",") -> str:
    if items is None:
        return ""
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        raise _ArgumentError(f"expects an array, got {_kind(items)}")
    return _joined([_display(item) for item in items], _text(separator))


def _from_json(text: JSONValue) -> JSONValue:
    # A non-string raises instead of being converted, because any conversion would be a
    # guess.
    if not isinstance(text, str):
        raise _ArgumentError(f"expects a string, got {_kind(text)}")
    if len(text) > _MAX_CHARS:
        raise _ArgumentError(f"input is longer than {_MAX_CHARS} characters")
    # The depth is checked before parsing, because how deep json.loads can recurse, and so
    # which error it raises for deep input, differs between Python versions.
    if _nests_deeper_than(text, _MAX_JSON_DEPTH):
        raise _ArgumentError(f"input nests deeper than {_MAX_JSON_DEPTH} levels")
    try:
        value: JSONValue = json.loads(text, parse_constant=_no_constant, parse_float=_finite)
    except ValueError as error:
        raise _ArgumentError(f"can't parse the JSON: {error}") from None
    return value


def _no_constant(name: str) -> float:
    raise ValueError(f"{name} isn't valid JSON")


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"number {text} is out of range")
    return number


def _nests_deeper_than(text: str, levels: int) -> bool:
    """True when the arrays and objects in JSON `text` nest more than `levels` deep.

    One pass counts the brackets and braces outside strings, and stops at the first one
    that is too deep. The count is exact for valid JSON. For other text, it only decides
    whether this error or the parser's error comes first.
    """
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > levels:
                return True
        elif char in "]}":
            depth -= 1
    return False


def _length(value: JSONValue) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, list, dict)):
        return len(value)
    raise _ArgumentError(f"expects a string, an array, or an object, got {_kind(value)}")


def _text(value: JSONValue) -> str:
    """Convert a scalar argument with the display rules. Arrays and objects raise."""
    if isinstance(value, (list, dict)):
        raise _ArgumentError(f"expects a string, got {_kind(value)}")
    return _display(value)


def _to_json(value: JSONValue) -> str:
    """Return `toJSON(value)`. Nested calls double the escapes, so the size is bounded."""
    text = _json(value)
    _check_size(len(text))
    return text


def _joined(pieces: Sequence[str], separator: str = "") -> str:
    """Join `pieces`, refusing a result longer than `_MAX_CHARS` before building it."""
    _check_size(sum(map(len, pieces)) + len(separator) * max(len(pieces) - 1, 0))
    return separator.join(pieces)


def _check_size(size: int) -> None:
    """Refuse to return a string of more than `_MAX_CHARS` characters."""
    if size > _MAX_CHARS:
        raise _ArgumentError(f"would return more than {_MAX_CHARS} characters")


def _display(value: JSONValue) -> str:
    """Return the display string of a value, which templates render."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    return _json(value)


def _json(value: JSONValue) -> str:
    """Return compact, single-line JSON with keys in insertion order."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _equals(left: JSONValue, right: JSONValue) -> bool:
    """Apply `==`: same types by value, numbers with numeric strings, other pairs unequal."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, (int, float, str)) and isinstance(right, (int, float, str)):
        number = _numeric(left)
        return number is not None and number == _numeric(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_equals, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equals(left[key], right[key]) for key in left)
    return left is None and right is None


def _numeric(value: JSONValue) -> int | float | None:
    """Return a number, or the value of a string that is exactly a JSON number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str) or (match := _NUMBER.fullmatch(value)) is None:
        return None
    if match.group(1) is None and match.group(2) is None:
        with contextlib.suppress(ValueError):  # past Python's limit on integer digits
            return int(value)
    return float(value)


def _kind(value: JSONValue) -> str:
    """Name a value's type for messages: null, boolean, number, string, array, or object."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


_IMPLEMENTATIONS: Final[Mapping[str, Callable[..., JSONValue]]] = {
    "contains": _contains,
    "startsWith": lambda text, prefix: _text(text).startswith(_text(prefix)),
    "endsWith": lambda text, suffix: _text(text).endswith(_text(suffix)),
    "format": _format,
    "join": _join,
    "toJSON": _to_json,
    "fromJSON": _from_json,
    "lower": lambda text: _text(text).lower(),
    "upper": lambda text: _text(text).upper(),
    "trim": lambda text: _text(text).strip(),
    "length": _length,
    "default": lambda value, fallback: fallback if value is None else value,
}
