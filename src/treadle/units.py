"""Durations and sizes, as section 2 ("Common value formats") defines them."""

from __future__ import annotations

import math
import re
from typing import Final

_NUMBER: Final = r"\d+(?:\.\d+)?"
_NUMBER_RE: Final = re.compile(_NUMBER)
_DURATION_UNITS: Final = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
_DURATION_RE: Final = re.compile(
    "".join(rf"(?:(?P<{unit}>{_NUMBER}){unit})?" for unit in _DURATION_UNITS)
)
_SIZE_UNITS: Final = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
}
_SIZE_RE: Final = re.compile(rf"({_NUMBER})\s*({'|'.join(_SIZE_UNITS)})?")


def parse_duration(value: object) -> float:
    """Return a duration in seconds, or raise ValueError with a reason.

    Accepts a non-negative number of seconds, a bare numeric string such as "30", or unit parts
    in the order d, h, m, s, ms, each at most once: "500ms", "1h30m", "2d". Booleans are
    rejected, because Python treats them as integers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a duration such as 30s, got {type(value).__name__}")
    if isinstance(value, str):
        seconds = _parse_duration_text(value)
    else:
        try:
            seconds = float(value)
        except OverflowError:  # an integer beyond the float range
            seconds = math.inf
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"invalid duration {value!r}: must be a non-negative, finite number")
    return seconds


def format_duration(seconds: float) -> str:
    """Format seconds compactly: "850ms", "12.3s", "3m04s", "1h02m", "2d03h".

    Negative values format as zero, and a zero second unit is dropped ("10m", "2h").
    """
    # Design note: dropping a zero second unit yields the "10m" and "2h" that the design's
    # messages use, such as "timed out after 10m".
    seconds = max(seconds, 0.0)
    if seconds == 0:
        return "0s"
    if round(seconds * 1000) < 1000:
        return f"{round(seconds * 1000)}ms"
    if round(seconds * 10) < 600:
        return f"{round(seconds, 1):g}s"
    total = round(seconds)
    if total < 3600:
        return _two_units(total // 60, total % 60, "m", "s")
    if total < 86400:
        return _two_units(total // 3600, total % 3600 // 60, "h", "m")
    return _two_units(total // 86400, total % 86400 // 3600, "d", "h")


def parse_size(value: object) -> int:
    """Return a size in bytes, or raise ValueError with a reason.

    Accepts a non-negative integer, or a number with a unit: B, KB, MB, GB (powers of 1,000)
    or KiB, MiB, GiB (powers of 1,024), such as "64KiB" or "1.5 MiB". Booleans are rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"expected a size such as 64KiB, got {type(value).__name__}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"invalid size {value}: must not be negative")
        return value
    match = _SIZE_RE.fullmatch(value.strip())
    size = float(match[1]) * _SIZE_UNITS[match[2] or "B"] if match else math.nan
    if not math.isfinite(size):
        raise ValueError(f"invalid size {value!r}: use bytes or a unit, such as 64KiB or 1MiB")
    return round(size)


def format_size(n: int) -> str:
    """Format bytes for people: "512 B", "64.0 KiB", "1.5 MiB"."""
    if n < 1024:
        return f"{n} B"
    value, unit = n / 1024, "KiB"
    for larger in ("MiB", "GiB"):
        if round(value, 1) < 1024:
            break
        value, unit = value / 1024, larger
    return f"{value:.1f} {unit}"


def _parse_duration_text(text: str) -> float:
    stripped = text.strip()
    if _NUMBER_RE.fullmatch(stripped):
        return float(stripped)
    match = _DURATION_RE.fullmatch(stripped)
    if not stripped or match is None:
        raise ValueError(
            f"invalid duration {text!r}: use seconds or units such as 500ms, 30s, 5m, 1h30m, or 2d"
        )
    parts = match.groupdict().items()
    return sum(float(amount) * _DURATION_UNITS[unit] for unit, amount in parts if amount)


def _two_units(large: int, small: int, large_unit: str, small_unit: str) -> str:
    return f"{large}{large_unit}{small:02d}{small_unit}" if small else f"{large}{large_unit}"
