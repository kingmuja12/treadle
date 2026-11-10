"""Hand-written declarative validation for workflow files, step parameters, and treadle.toml.

A spec checks one plain value (from YAML or TOML), appends an `Issue` for each problem, and
returns the normalized value. Specs know YAML paths, never file positions; the loader maps
paths to positions. With `allow_templates`, a `Template` passes any spec unchanged.
"""

from __future__ import annotations

import copy
import difflib
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from treadle.expr.ast import Template
from treadle.types import Issue, Severity, YamlPath
from treadle.units import format_duration, format_size, parse_duration, parse_size

MISSING: Final[object] = object()
_KINDS: Final = (
    (bool, "a boolean"),
    (int, "a number"),
    (float, "a number"),
    (str, "a string"),
    (list, "a list"),
    (dict, "a mapping"),
    (Template, "a template"),
)


@dataclass(slots=True)
class CheckContext:
    """Collects issues during one validation. `allow_templates` lets `Template` values pass.

    `key_issues` also holds each issue that concerns a mapping key rather than its value: an
    unknown key, keys that can't be used together, a key that isn't a string, and a key that
    doesn't match `MapOf`'s pattern.
    """

    issues: list[Issue]
    allow_templates: bool = False
    key_issues: list[Issue] = field(default_factory=list)


class Spec(Protocol):
    """A validator for one value."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        """Validate `value` at `path`, append issues to `ctx`, and return the normalized value."""

    def describe(self) -> str:
        """Describe the accepted values for messages, such as "a duration such as 30s"."""


@dataclass(frozen=True, slots=True)
class Field:
    """One key of an `Obj`. A missing optional key takes `default` unless it's `MISSING`.

    `help` becomes the hint of the "missing required key" issue.
    """

    spec: Spec
    required: bool = False
    default: object = MISSING
    help: str | None = None


class _Base(ABC):
    """Passes templates through when allowed, and reports wrong types as E103."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        """Validate `value` at `path`, append issues to `ctx`, and return the normalized value."""
        if ctx.allow_templates and isinstance(value, Template):
            return value
        return self._check(value, path, ctx)

    @abstractmethod
    def describe(self) -> str:
        """Describe the accepted values for messages."""

    @abstractmethod
    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        """Check a value that isn't a passing template."""

    def _wrong_type(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        _report(ctx, "E103", f"expected {self.describe()}, got {kind_of(value)}", path)
        return value


class _Ranged(_Base):
    """A number, duration, or size with optional inclusive bounds (E104 when out of range)."""

    _accepts: tuple[type, ...] = (int,)

    def __init__(self, low: float | None, high: float | None) -> None:
        self._low = low
        self._high = high

    @abstractmethod
    def _convert(self, value: Any) -> float:
        """Convert an accepted value, raising ValueError with the E104 message."""

    def _show(self, bound: float) -> str:
        return f"{bound:g}"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, bool) or not isinstance(value, self._accepts):
            return self._wrong_type(value, path, ctx)
        try:
            number = self._convert(value)
        except ValueError as exc:
            _report(ctx, "E104", str(exc), path)
            return value
        too_low = self._low is not None and number < self._low
        too_high = self._high is not None and number > self._high
        if too_low or too_high:
            _report(ctx, "E104", self._range_message(), path)
        return number

    def _range_message(self) -> str:
        low = None if self._low is None else self._show(self._low)
        high = None if self._high is None else self._show(self._high)
        if low is None:
            return f"must be at most {high}"
        return f"must be at least {low}" if high is None else f"must be between {low} and {high}"


class Str(_Base):
    """A string, optionally limited to a pattern (full match), choices, or a length range."""

    def __init__(
        self,
        *,
        pattern: str | None = None,
        choices: Sequence[str] | None = None,
        min_len: int = 0,
        max_len: int | None = None,
    ) -> None:
        self._pattern = re.compile(pattern) if pattern is not None else None
        self._choices = tuple(choices) if choices is not None else None
        self._min_len = min_len
        self._max_len = max_len

    def describe(self) -> str:
        """Return "a string", or the choices."""
        return f"one of: {', '.join(self._choices)}" if self._choices is not None else "a string"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if not isinstance(value, str):
            return self._wrong_type(value, path, ctx)
        if self._choices is not None and value not in self._choices:
            close = suggest(value, self._choices)
            hint = f"did you mean '{close}'?" if close else None
            _report(ctx, "E104", f"must be {self.describe()}", path, hint)
        elif self._pattern is not None and not self._pattern.fullmatch(value):
            _report(ctx, "E104", f"must match the pattern {self._pattern.pattern}", path)
        elif len(value) < self._min_len:
            short = "must not be empty" if self._min_len == 1 else None
            _report(ctx, "E104", short or f"must be at least {self._min_len} characters", path)
        elif self._max_len is not None and len(value) > self._max_len:
            _report(ctx, "E104", f"must be at most {self._max_len} characters", path)
        return value


class Int(_Ranged):
    """An integer, never a boolean."""

    def __init__(self, *, min: int | None = None, max: int | None = None) -> None:
        super().__init__(min, max)

    def describe(self) -> str:
        """Return "an integer"."""
        return "an integer"

    def _convert(self, value: Any) -> float:
        return int(value)

    def _show(self, bound: float) -> str:
        return str(int(bound))


class Num(_Ranged):
    """A finite integer or float, never a boolean. Returns a float."""

    _accepts = (int, float)

    def __init__(self, *, min: float | None = None, max: float | None = None) -> None:
        super().__init__(min, max)

    def describe(self) -> str:
        """Return "a number"."""
        return "a number"

    def _convert(self, value: Any) -> float:
        try:
            number = float(value)
        except OverflowError:  # an integer beyond the float range
            number = math.inf
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        return number


class Bool(_Base):
    """A boolean. Only YAML 1.2 booleans load as booleans, so "yes" is a string."""

    def describe(self) -> str:
        """Return "a boolean (true or false)"."""
        return "a boolean (true or false)"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        return value if isinstance(value, bool) else self._wrong_type(value, path, ctx)


class Duration(_Ranged):
    """A duration such as `30s` or `1h30m`, or a number of seconds. Returns float seconds."""

    _accepts = (int, float, str)

    def __init__(self, *, min: float = 0.0, max: float | None = None) -> None:
        super().__init__(min, max)

    def describe(self) -> str:
        """Return "a duration such as 30s"."""
        return "a duration such as 30s"

    def _convert(self, value: Any) -> float:
        return parse_duration(value)

    def _show(self, bound: float) -> str:
        return format_duration(bound)


class Size(_Ranged):
    """A size such as `64KiB`, or a number of bytes. Returns int bytes."""

    _accepts = (int, str)

    def __init__(self, *, min: int = 0, max: int | None = None) -> None:
        super().__init__(min, max)

    def describe(self) -> str:
        """Return "a size such as 64KiB"."""
        return "a size such as 64KiB"

    def _convert(self, value: Any) -> float:
        return parse_size(value)

    def _show(self, bound: float) -> str:
        return format_size(int(bound))


class ListOf(_Base):
    """A list whose items match `item`.

    With `scalar_ok`, any single non-list value other than null becomes a one-element list, so
    `needs: build` and `schedule: {cron: ...}` both work. With `unique`, an item equal to an
    earlier one is E104 at the repeat; items that failed their own check aren't compared.
    """

    def __init__(
        self,
        item: Spec,
        *,
        min_len: int = 0,
        max_len: int | None = None,
        scalar_ok: bool = False,
        unique: bool = False,
    ) -> None:
        self._item = item
        self._min_len = min_len
        self._max_len = max_len
        self._scalar_ok = scalar_ok
        self._unique = unique

    def describe(self) -> str:
        """Return "a list", or "<item> or a list of them" with `scalar_ok`."""
        return f"{self._item.describe()} or a list of them" if self._scalar_ok else "a list"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, list):
            written = [(item, (*path, index)) for index, item in enumerate(value)]
        elif self._scalar_ok and value is not None:
            written = [(value, path)]
        else:
            return self._wrong_type(value, path, ctx)
        items: list[object] = []
        valid: list[bool] = []
        for item, item_path in written:
            before = len(ctx.issues)
            items.append(self._item.check(item, item_path, ctx))
            valid.append(len(ctx.issues) == before)
        if len(items) < self._min_len:
            _report(ctx, "E104", f"must have at least {_count(self._min_len, 'item')}", path)
        if self._max_len is not None and len(items) > self._max_len:
            _report(ctx, "E104", f"must have at most {_count(self._max_len, 'item')}", path)
        for index in _repeats(items, valid) if self._unique else ():
            _report(ctx, "E104", f"duplicate value {items[index]!r}", (*path, index))
        return items


class MapOf(_Base):
    """A mapping with string keys whose values match `value`."""

    def __init__(
        self, value: Spec, *, key_pattern: str | None = None, max_len: int | None = None
    ) -> None:
        self._value = value
        self._key_pattern = re.compile(key_pattern) if key_pattern is not None else None
        self._max_len = max_len

    def describe(self) -> str:
        """Return "a mapping"."""
        return "a mapping"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if not isinstance(value, dict):
            return self._wrong_type(value, path, ctx)
        if self._max_len is not None and len(value) > self._max_len:
            _report(ctx, "E104", f"must have at most {self._max_len} entries", path)
        result: dict[str, object] = {}
        for key, item in value.items():
            name = _key_name(key, path, ctx)
            if self._key_pattern is not None and not self._key_pattern.fullmatch(name):
                message = (
                    f"invalid key '{name}': must match the pattern {self._key_pattern.pattern}"
                )
                _report(ctx, "E104", message, (*path, name), key=True)
            result[name] = self._value.check(item, (*path, name), ctx)
        return result


class AnyOf(_Base):
    """The first alternative that validates without issues; otherwise E103 "expected X or Y"."""

    def __init__(self, *alternatives: Spec) -> None:
        self._alternatives = alternatives

    def describe(self) -> str:
        """Join the alternatives' descriptions with "or"."""
        return " or ".join(alternative.describe() for alternative in self._alternatives)

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        for alternative in self._alternatives:
            trial = CheckContext([], ctx.allow_templates)
            result = alternative.check(value, path, trial)
            if not trial.issues:
                return result
        return self._wrong_type(value, path, ctx)


class AnyJSON(_Base):
    """Any JSON value: null, a boolean, a finite number, a string, a list, or a mapping."""

    def describe(self) -> str:
        """Return "a JSON value"."""
        return "a JSON value"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, list):
            return [self.check(item, (*path, index), ctx) for index, item in enumerate(value)]
        if isinstance(value, dict):
            names = [(_key_name(key, path, ctx), item) for key, item in value.items()]
            return {name: self.check(item, (*path, name), ctx) for name, item in names}
        if isinstance(value, float) and not math.isfinite(value):
            _report(ctx, "E104", "must be a finite number", path)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            return self._wrong_type(value, path, ctx)
        return value


class Obj(_Base):
    """A mapping with known keys.

    Unknown keys are E101 with a hint from `hints`, a kebab-case hint, or a suggestion. Missing
    required keys are E102. Missing optional keys take their default, or stay absent without
    one. With `shorthand`, a scalar value `v` means `{shorthand: v}`. `exclusive` groups are
    E105, and `require_one` groups are E106. `check` returns a `dict[str, object]`.
    """

    # An explicit null for a key whose default is null passes unchecked, so a written
    # `timestamp_header: null` or an input's `default: null` means the same as leaving it out.

    def __init__(
        self,
        name: str,
        fields: Mapping[str, Field],
        *,
        shorthand: str | None = None,
        hints: Mapping[str, str] | None = None,
        exclusive: Sequence[tuple[str, ...]] = (),
        require_one: Sequence[tuple[str, ...]] = (),
    ) -> None:
        self._name = name
        self._fields = dict(fields)
        self._shorthand = shorthand
        self._hints = dict(hints or {})
        self._exclusive = tuple(exclusive)
        self._require_one = tuple(require_one)

    @property
    def fields(self) -> Mapping[str, Field]:
        """The known keys, in declaration order."""
        return self._fields

    def describe(self) -> str:
        """Return "a <name> mapping", preceded by the shorthand field's description."""
        mapping = f"a {self._name} mapping"
        if self._shorthand is None:
            return mapping
        return f"{self._fields[self._shorthand].spec.describe()} or {mapping}"

    def _check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if (
            self._shorthand is not None
            and value is not None
            and not isinstance(value, (list, dict))
        ):
            return self._check_mapping({self._shorthand: value}, path, ctx, wrapped=True)
        if not isinstance(value, dict):
            return self._wrong_type(value, path, ctx)
        return self._check_mapping(value, path, ctx, wrapped=False)

    def _check_mapping(
        self, value: dict[object, object], path: YamlPath, ctx: CheckContext, *, wrapped: bool
    ) -> dict[str, object]:
        for key in value:
            if key not in self._fields:
                self._unknown(str(key), path, ctx)
        result: dict[str, object] = {}
        for key, spec_field in self._fields.items():
            if key in value:
                raw = value[key]
                key_path = path if wrapped else (*path, key)
                passes = raw is None and spec_field.default is None
                result[key] = raw if passes else spec_field.spec.check(raw, key_path, ctx)
            elif spec_field.required:
                _report(ctx, "E102", f"missing required key '{key}'", path, spec_field.help)
            elif spec_field.default is not MISSING:
                result[key] = copy.deepcopy(spec_field.default)
        for group in self._exclusive:
            present = [key for key in group if key in value]
            if len(present) > 1:
                message = f"{_join(present, 'and')} can't be used together"
                _report(ctx, "E105", message, (*path, present[1]), key=True)
        for group in self._require_one:
            if not any(key in value for key in group):
                _report(ctx, "E106", f"one of {_join(group, 'or')} is required", path)
        return result

    def _unknown(self, key: str, path: YamlPath, ctx: CheckContext) -> None:
        hint = self._hints.get(key)
        snake = key.replace("-", "_")
        if hint is None and snake in self._fields:
            hint = f"use `{snake}`"
        close = suggest(key, self._fields) if hint is None else None
        if close is not None:
            hint = f"did you mean '{close}'?"
        _report(ctx, "E101", f"unknown key '{key}' in {self._name}", (*path, key), hint, key=True)


def validate(
    spec: Spec,
    value: object,
    *,
    path: YamlPath = (),
    allow_templates: bool = False,
    key_issues: list[Issue] | None = None,
) -> tuple[object, list[Issue]]:
    """Check `value` against `spec` and return the normalized value and every issue found.

    When `key_issues` is given, each issue that concerns a mapping key rather than its value
    is also appended to it, so that a caller can point that issue at the key.
    """
    ctx = CheckContext([], allow_templates)
    result = spec.check(value, path, ctx)
    if key_issues is not None:
        key_issues.extend(ctx.key_issues)
    return result, ctx.issues


def suggest(word: str, candidates: Iterable[str]) -> str | None:
    """Return the closest candidate by `difflib` similarity (cutoff 0.6), or None."""
    matches = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def kind_of(value: object) -> str:
    """Name a value's type for messages: "null", "a boolean", "a number", "a string", "a list",
    "a mapping", "a template", or, for anything else, the name of its class."""
    if value is None:
        return "null"
    return next((name for cls, name in _KINDS if isinstance(value, cls)), type(value).__name__)


def _report(
    ctx: CheckContext,
    code: str,
    message: str,
    path: YamlPath,
    hint: str | None = None,
    *,
    key: bool = False,
) -> None:
    issue = Issue(Severity.ERROR, code, message, path, hint=hint)
    ctx.issues.append(issue)
    if key:
        ctx.key_issues.append(issue)


def _repeats(items: Sequence[object], valid: Sequence[bool]) -> Iterator[int]:
    """Yield the index of each valid item that equals an earlier valid item.

    Hashable items are looked up in a set. Lists and mappings, which aren't hashable, are
    compared with the earlier lists and mappings only.
    """
    seen: set[object] = set()
    unhashable: list[object] = []
    for index, (item, ok) in enumerate(zip(items, valid, strict=True)):
        if not ok:
            continue
        try:
            repeated = item in seen
            seen.add(item)
        except TypeError:
            repeated = item in unhashable
            if not repeated:
                unhashable.append(item)
        if repeated:
            yield index


def _key_name(key: object, path: YamlPath, ctx: CheckContext) -> str:
    """Return `key` as a string, reporting E103 when the mapping key isn't a string."""
    if not isinstance(key, str):
        message = f"expected a string key, got {kind_of(key)}"
        _report(ctx, "E103", message, (*path, str(key)), key=True)
    return str(key)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _join(words: Sequence[str], conjunction: str) -> str:
    quoted = [f"'{word}'" for word in words]
    if len(quoted) <= 2:
        return f" {conjunction} ".join(quoted)
    return f"{', '.join(quoted[:-1])}, {conjunction} {quoted[-1]}"
