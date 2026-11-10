"""Secret masking for logs, outputs, errors, and payloads (section 5, "Secret masking")."""

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
    line of a multi-line value, and the JSON-escaped and URL-encoded forms.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._forms: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self._longest = 0
        for value in values:
            self._remember(value)
        self._rebuild()

    def add(self, value: str) -> None:
        """Mask `value` from now on. Shorter values are ignored."""
        if self._remember(value):
            self._rebuild()

    def mask(self, text: str) -> str:
        """Return `text` with every known value replaced."""
        if self._pattern is None:
            return text
        return self._pattern.sub(MASK, text)

    def mask_json(self, value: JSONValue) -> JSONValue:
        """Return a copy of a JSON value with keys and string values masked."""
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list):
            return [self.mask_json(item) for item in value]
        if isinstance(value, dict):
            return {self.mask(key): self.mask_json(item) for key, item in value.items()}
        return value

    def copy(self) -> Masker:
        """Return an independent masker with the same values."""
        clone = Masker()
        clone._forms = set(self._forms)
        clone._pattern = self._pattern
        clone._longest = self._longest
        return clone

    @property
    def longest(self) -> int:
        """The length of the longest masked form, or 0."""
        return self._longest

    def _remember(self, value: str) -> bool:
        """Add the masked forms of `value`, and return True when any of them is new."""
        # Design note: a value or line made only of whitespace is ignored. Masking it would
        # hide indentation in every log line, and it can't reveal anything.
        if len(value) < MIN_SECRET_LENGTH or not value.strip():
            return False
        forms = {value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}
        forms.update(
            line for line in value.splitlines() if len(line) >= MIN_SECRET_LENGTH and line.strip()
        )
        new = forms - self._forms
        self._forms |= new
        return bool(new)

    def _rebuild(self) -> None:
        """Compile one alternation of every form, longest first, so the longest match wins."""
        ordered = sorted(self._forms, key=lambda form: (-len(form), form))
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None
        self._longest = len(ordered[0]) if ordered else 0
