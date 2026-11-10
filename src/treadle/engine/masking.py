"""Secret masking for logs, outputs, errors, and payloads."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import quote, quote_plus

from treadle.types import JSONValue

MASK: Final = "***"
MIN_SECRET_LENGTH: Final = 4


class Masker:
    """Replaces secret values with `***` using one alternation regex, longest value first.

    For each value of at least `MIN_SECRET_LENGTH` characters it masks the raw value, each
    line of a multi-line value, and the JSON-escaped and URL-encoded forms. The regex is
    compiled when text is next masked after a change, so adding many values in a row, as a
    burst of `::add-mask::` lines does, costs one compile.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._forms: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self._stale = False  # the forms changed since the pattern was compiled
        self._longest = 0
        for value in values:
            self.add(value)

    def add(self, value: str) -> bool:
        """Mask `value` from now on, and return True when that needed anything new.

        Values shorter than `MIN_SECRET_LENGTH`, and blank ones, are ignored.
        """
        new = _forms(value) - self._forms
        if not new:
            return False
        self._forms |= new
        self._longest = max(self._longest, *map(len, new))
        self._stale = True
        return True

    def knows(self, value: str) -> bool:
        """True when masking `value` needs nothing new: every form of it is masked already."""
        return _forms(value) <= self._forms

    def mask(self, text: str) -> str:
        """Return `text` with every known value replaced."""
        pattern = self._compiled()
        return text if pattern is None else pattern.sub(MASK, text)

    def mask_json(self, value: JSONValue) -> JSONValue:
        """Return a copy of a JSON value with keys and strings masked, at any depth.

        A number whose JSON text holds a known value, such as a numeric chat ID, becomes the
        string `***`. Booleans and null stay as they are.
        """
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.mask_json(item) for item in value]
        if isinstance(value, dict):
            return {self.mask(key): self.mask_json(item) for key, item in value.items()}
        pattern = self._compiled()
        if isinstance(value, bool) or value is None or pattern is None:
            return value
        return MASK if pattern.search(json.dumps(value)) else value

    def copy(self) -> Masker:
        """Return an independent masker with the same values."""
        clone = Masker()
        clone._forms = set(self._forms)
        clone._pattern = self._pattern
        clone._stale = self._stale
        clone._longest = self._longest
        return clone

    @property
    def longest(self) -> int:
        """The length of the longest masked form, or 0."""
        return self._longest

    def _compiled(self) -> re.Pattern[str] | None:
        """Return the alternation of every form, longest first, so the longest match wins."""
        if self._stale:
            ordered = sorted(self._forms, key=lambda form: (-len(form), form))
            self._pattern = re.compile("|".join(map(re.escape, ordered)))
            self._stale = False
        return self._pattern


def _forms(value: str) -> set[str]:
    """Return the masked forms of a value, or none for a short or blank value."""
    # A value or line made only of whitespace is ignored. Masking it would hide indentation
    # in every log line, and it can't reveal anything.
    if len(value) < MIN_SECRET_LENGTH or not value.strip():
        return set()
    forms = {value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}
    forms.update(
        line for line in value.splitlines() if len(line) >= MIN_SECRET_LENGTH and line.strip()
    )
    return forms
