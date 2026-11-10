from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from treadle.clock import (
    SystemClock,
    default_random,
    from_ms,
    inline_runner,
    iso_utc,
    parse_iso,
    thread_runner,
    to_ms,
)

INSTANT = datetime(2026, 11, 9, 2, 30, tzinfo=UTC)


def test_to_ms_and_from_ms_round_trip() -> None:
    ms = to_ms(INSTANT)

    assert ms == 1_794_191_400_000
    assert from_ms(ms) == INSTANT
    assert from_ms(ms).tzinfo is UTC


def test_to_ms_rounds_down_and_handles_other_offsets() -> None:
    plus_one = timezone(timedelta(hours=1))

    assert to_ms(INSTANT + timedelta(microseconds=999)) == to_ms(INSTANT)
    assert to_ms(datetime(2026, 11, 9, 3, 30, tzinfo=plus_one)) == to_ms(INSTANT)
    assert to_ms(datetime(1969, 12, 31, 23, 59, 59, 999500, tzinfo=UTC)) == -1


def test_to_ms_rejects_naive_datetimes() -> None:
    with pytest.raises(TypeError):
        to_ms(datetime(2026, 11, 9))


def test_iso_utc_formats_milliseconds_with_z() -> None:
    plus_two = timezone(timedelta(hours=2))

    assert iso_utc(INSTANT) == "2026-11-09T02:30:00.000Z"
    assert iso_utc(INSTANT + timedelta(microseconds=123_999)) == "2026-11-09T02:30:00.123Z"
    assert iso_utc(datetime(2026, 11, 9, 4, 30, tzinfo=plus_two)) == "2026-11-09T02:30:00.000Z"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-09T02:30:00.000Z", INSTANT),
        ("2026-11-09T03:30:00+01:00", INSTANT),
        ("  2026-11-09T02:30:00Z  ", INSTANT),
        ("2026-11-09T02:30", INSTANT),
        ("2026-11-09", datetime(2026, 11, 9, tzinfo=UTC)),
    ],
)
def test_parse_iso_returns_utc(text: str, expected: datetime) -> None:
    parsed = parse_iso(text)

    assert parsed == expected
    assert parsed.tzinfo is UTC


def test_parse_iso_reads_naive_values_as_wall_time_in_the_default_zone() -> None:
    new_york = ZoneInfo("America/New_York")

    assert parse_iso("2026-03-07", default_tz=new_york) == datetime(2026, 3, 7, 5, tzinfo=UTC)
    assert parse_iso("2026-07-01T12:00Z", default_tz=new_york).hour == 12


@pytest.mark.parametrize("text", ["", "yesterday", "2026-13-01", "09/11/2026"])
def test_parse_iso_rejects_other_text(text: str) -> None:
    with pytest.raises(ValueError, match=r"isoformat|must be in"):
        parse_iso(text)


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is UTC
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


def test_default_random_returns_unit_interval_floats() -> None:
    values = [default_random().random() for _ in range(20)]

    assert all(0.0 <= value < 1.0 for value in values)


async def test_inline_runner_calls_on_the_loop_thread() -> None:
    assert await inline_runner(threading.get_ident) == threading.get_ident()
    assert await inline_runner(divmod, 7, 2) == (3, 1)


async def test_runners_re_raise_errors() -> None:
    with pytest.raises(ZeroDivisionError):
        await inline_runner(divmod, 1, 0)
    with pytest.raises(ZeroDivisionError):
        await thread_runner(divmod, 1, 0)


async def test_thread_runner_uses_to_thread_which_virtual_time_runs_inline() -> None:
    assert await thread_runner(divmod, 9, 4) == (2, 1)
    assert await thread_runner(threading.get_ident) == threading.get_ident()
