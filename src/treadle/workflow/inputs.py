"""Run inputs: parsing CLI `key=value` pairs, and coercing values to each input's type."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Final

from treadle.errors import InputError
from treadle.models import InputSpec, InputType
from treadle.schema import suggest
from treadle.types import JSONValue

# An integer is an optional sign and ASCII digits, matched in full: Python's \d would also
# match non-ASCII digits, and $ would allow a trailing newline.
_INTEGER: Final = re.compile(r"[+-]?[0-9]+")
_JSON_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def parse_input_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """Parse CLI `-i key=value` pairs, splitting at the first `=`. Raises InputError.

    A repeated key keeps its last value, and `name=` supplies an empty string.
    """
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    for pair in pairs:
        name, equals, value = pair.partition("=")
        if not equals:
            errors[pair] = f"has no value (write {pair}=VALUE)"
        elif not name:
            errors[""] = f"'{pair}' has no input name (write NAME=VALUE)"
        else:
            values[name] = value
    if errors:
        raise InputError(errors)
    return values


def coerce_inputs(
    specs: Sequence[InputSpec], raw: Mapping[str, object], *, from_strings: bool
) -> dict[str, JSONValue]:
    """Coerce raw inputs to their declared types and fill defaults.

    `from_strings` selects the string rules (CLI, dashboard) instead of the JSON rules (API).
    Unknown names, missing required inputs, and bad values are collected into one InputError.
    A null value counts as not supplied, so stored inputs of an earlier run coerce again.
    The result holds every declared input in declaration order.
    """
    declared = [spec.name for spec in specs]
    errors: dict[str, str] = {}
    for name in raw:
        if name not in declared:
            close = suggest(name, declared)
            errors[name] = "isn't declared" + (f" (did you mean '{close}'?)" if close else "")
    values: dict[str, JSONValue] = {}
    for spec in specs:
        value = raw.get(spec.name)
        if value is None:
            if spec.required:
                errors[spec.name] = "is required"
            values[spec.name] = spec.default
            continue
        try:
            values[spec.name] = _from_text(spec, value) if from_strings else _from_json(spec, value)
        except ValueError as exc:
            errors[spec.name] = str(exc)
    if errors:
        raise InputError(errors)
    return values


def _from_text(spec: InputSpec, value: object) -> JSONValue:
    """Apply the string rules of the CLI and the dashboard."""
    if not isinstance(value, str):
        raise ValueError("must be text")
    if spec.type is InputType.STRING:
        return value
    if spec.type is InputType.CHOICE:
        return _choice(spec, value)
    if spec.type is InputType.BOOLEAN:
        if value.lower() not in ("true", "false"):
            raise ValueError("must be true or false")
        return value.lower() == "true"
    if spec.type is InputType.INTEGER:
        if not _INTEGER.fullmatch(value):
            raise ValueError("must be an integer")
        return _number(value, integer=True)
    if not _JSON_NUMBER.fullmatch(value):
        raise ValueError("must be a number")
    return _number(value, integer=False)


def _from_json(spec: InputSpec, value: object) -> JSONValue:
    """Apply the JSON rules of the API. Booleans never count as numbers."""
    if spec.type in (InputType.STRING, InputType.CHOICE):
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return _choice(spec, value) if spec.type is InputType.CHOICE else value
    if spec.type is InputType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    wanted = "an integer" if spec.type is InputType.INTEGER else "a number"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"must be {wanted}")
    if spec.type is InputType.INTEGER and isinstance(value, float):
        if not value.is_integer():
            raise ValueError("must be an integer")
        return int(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


def _choice(spec: InputSpec, value: str) -> str:
    if value not in spec.options:
        raise ValueError(f"must be one of: {', '.join(spec.options)}")
    return value


def _number(text: str, *, integer: bool) -> int | float:
    """Convert text that matched a number pattern; only its size can still fail."""
    try:
        number: int | float = int(text) if integer else json.loads(text)
    except ValueError:  # more digits than Python converts to an integer
        raise ValueError("is too large") from None
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number
