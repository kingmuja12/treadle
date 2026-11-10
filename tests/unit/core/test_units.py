from __future__ import annotations

import pytest

from treadle.units import format_duration, format_size, parse_duration, parse_size


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (0, 0.0),
        (30, 30.0),
        (1.5, 1.5),
        ("30", 30.0),
        ("2.5", 2.5),
        ("500ms", 0.5),
        ("30s", 30.0),
        ("5m", 300.0),
        ("1h30m", 5400.0),
        ("2d", 172800.0),
        ("1.5h", 5400.0),
        ("1d2h3m4s5ms", 93784.005),
        (" 10s ", 10.0),
    ],
)
def test_parse_duration_accepts_numbers_and_unit_strings(value: object, seconds: float) -> None:
    assert parse_duration(value) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (True, "got bool"),
        (False, "got bool"),
        (None, "got NoneType"),
        ([30], "got list"),
        (-1, "non-negative"),
        (float("nan"), "finite"),
        (float("inf"), "finite"),
        (10**400, "finite"),
        ("9" * 400, "finite"),
        ("", "use seconds or units"),
        ("abc", "use seconds or units"),
        ("5 minutes", "use seconds or units"),
        ("30m1h", "use seconds or units"),
        ("1h1h", "use seconds or units"),
        ("-5s", "use seconds or units"),
        ("1e3", "use seconds or units"),
    ],
)
def test_parse_duration_rejects_bad_values_with_a_reason(value: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_duration(value)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (-3, "0s"),
        (0.0004, "0ms"),
        (0.85, "850ms"),
        (0.9996, "1s"),
        (12.3, "12.3s"),
        (30, "30s"),
        (59.96, "1m"),
        (184, "3m04s"),
        (600, "10m"),
        (3599.6, "1h"),
        (3720, "1h02m"),
        (7200, "2h"),
        (86400, "1d"),
        (183600, "2d03h"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


@pytest.mark.parametrize(
    ("value", "size"),
    [
        (0, 0),
        (512, 512),
        ("512", 512),
        ("512B", 512),
        ("1KB", 1000),
        ("2.3KB", 2300),
        ("2GB", 2_000_000_000),
        ("64KiB", 65536),
        ("64 KiB", 65536),
        ("1MiB", 1 << 20),
        ("1.5 MiB", 1572864),
        ("1GiB", 1 << 30),
    ],
)
def test_parse_size_accepts_bytes_and_units(value: object, size: int) -> None:
    assert parse_size(value) == size


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (True, "got bool"),
        (1.5, "got float"),
        (None, "got NoneType"),
        (-1, "must not be negative"),
        ("", "use bytes or a unit"),
        ("64kib", "use bytes or a unit"),
        ("64 XB", "use bytes or a unit"),
        ("KiB", "use bytes or a unit"),
        ("9" * 400 + "GB", "use bytes or a unit"),
    ],
)
def test_parse_size_rejects_bad_values_with_a_reason(value: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_size(value)


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (65536, "64.0 KiB"),
        (1048575, "1.0 MiB"),
        (1572864, "1.5 MiB"),
        (1 << 30, "1.0 GiB"),
        (5 << 40, "5120.0 GiB"),
    ],
)
def test_format_size(size: int, text: str) -> None:
    assert format_size(size) == text
