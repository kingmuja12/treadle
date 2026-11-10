"""Differential tests: the fire functions against a brute-force oracle.

The oracle steps through UTC minutes and applies the DST rules directly: an
existing wall time fires at its instant (fixed-time schedules only on its first occurrence),
and a forward jump fires at the jump for fixed-time schedules when it skipped a matching wall
time. Every 2026 transition in these zones falls on a whole minute, so minute steps see them.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import pytest

from treadle.cron import fires_between, latest_fires, matches, next_fire, parse_cron, prev_fire
from treadle.models import CronSpec

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
ZONES = ("America/New_York", "Europe/London", "Australia/Lord_Howe", "Antarctica/Troll", "UTC")
FIELDS = (
    ("0", "30", "15,45", "*/20", "5-35/15", "59", "0-10/5", "*/30", "45", "*/12"),
    ("*", "*", "1", "2", "1,2", "0-3", "*/2", "2-4", "22-23", "0-23", "*/1", "3", "12"),
    ("*", "*", "*", "1-31", "7-14", "*/2", "8,25,29", "1", "31"),
    ("*", "*", "*", "3", "10-11", "MAR,APR,OCT", "*/2"),
    ("*", "*", "*", "0", "SUN", "MON-FRI", "6,0", "1-5/2", "SAT"),
)
# Dense and edge-case schedules that every window checks, besides the random ones.
FIXED_EXPRESSIONS = ("*/5 * * * *", "* 1-2 * * *", "30 1 * * *", "0 2 * * *", "@hourly")


@cache
def transitions(zone: str, year: int) -> tuple[datetime, ...]:
    """Return the UTC minutes in `year` at which the zone's UTC offset changes."""
    tz = ZoneInfo(zone)
    found: list[datetime] = []
    instant = datetime(year, 1, 1, tzinfo=UTC)
    offset = instant.astimezone(tz).utcoffset()
    while instant.year == year:
        if (instant + HOUR).astimezone(tz).utcoffset() != offset:
            change = instant + MINUTE
            while change.astimezone(tz).utcoffset() == offset:
                change += MINUTE
            found.append(change)
            offset = change.astimezone(tz).utcoffset()
        instant += HOUR
    return tuple(found)


def windows() -> list[tuple[str, datetime]]:
    """Return the start of a 48-hour window around every 2026 transition in every zone."""
    starts = [(zone, change - 24 * HOUR) for zone in ZONES for change in transitions(zone, 2026)]
    return [*starts, ("UTC", datetime(2026, 12, 31, tzinfo=UTC))]


def oracle(spec: CronSpec, tz: ZoneInfo, start: datetime, end: datetime) -> list[datetime]:
    """Return the fires in (start, end] by stepping through every UTC minute."""
    fires: list[datetime] = []
    instant = start.replace(second=0, microsecond=0) + MINUTE
    previous = (instant - MINUTE).astimezone(tz).replace(tzinfo=None)
    while instant <= end:
        local = instant.astimezone(tz)
        wall = local.replace(tzinfo=None)
        fires_now = matches(spec, wall) and (spec.interval_like or local.fold == 0)
        skipped = previous + MINUTE
        while not spec.interval_like and skipped < wall:
            fires_now = fires_now or matches(spec, skipped)
            skipped += MINUTE
        if fires_now:
            fires.append(instant)
        previous = wall
        instant += MINUTE
    return fires


def random_expression(rng: random.Random) -> str:
    return " ".join(rng.choice(choices) for choices in FIELDS)


def window_id(window: tuple[str, datetime]) -> str:
    return f"{window[0]} {window[1]:%Y-%m-%d %H:%M}"


def test_transitions_finds_both_2026_changes_in_each_dst_zone() -> None:
    assert transitions("America/New_York", 2026) == (
        datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
    )
    assert transitions("Australia/Lord_Howe", 2026) == (
        datetime(2026, 4, 4, 15, 0, tzinfo=UTC),
        datetime(2026, 10, 3, 15, 30, tzinfo=UTC),
    )
    assert transitions("UTC", 2026) == ()


@pytest.mark.parametrize(
    ("expression", "zone", "start", "fires"),
    [
        ("*/15 2 * * *", "America/New_York", "2026-03-08T00:00", ["03-08 07:00", "03-09 06:00"]),
        ("30 1 * * *", "America/New_York", "2026-10-31T12:00", ["11-01 05:30", "11-02 06:30"]),
        ("0 * * * *", "America/New_York", "2026-11-01T04:30", ["11-01 05:00", "11-01 06:00"]),
        ("*/30 * * * *", "America/New_York", "2026-03-08T06:00", ["03-08 06:30", "03-08 07:00"]),
        ("15 2 * * *", "Australia/Lord_Howe", "2026-10-03T00:00", ["10-03 15:30", "10-04 15:15"]),
        ("0 2 * * *", "Antarctica/Troll", "2026-03-28T12:00", ["03-29 01:00", "03-30 00:00"]),
    ],
)
def test_the_oracle_reproduces_verified_vectors(
    expression: str, zone: str, start: str, fires: list[str]
) -> None:
    after = datetime.fromisoformat(start).replace(tzinfo=UTC)
    expected = [
        datetime.strptime(f"2026-{fire}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC) for fire in fires
    ]

    assert oracle(parse_cron(expression), ZoneInfo(zone), after, expected[-1]) == expected


@pytest.mark.parametrize("window", windows(), ids=window_id)
def test_fire_functions_match_the_oracle(window: tuple[str, datetime]) -> None:
    zone, start = window
    tz, end = ZoneInfo(zone), start + 48 * HOUR
    rng = random.Random(f"{zone} {start.isoformat()}")
    expressions = [*(random_expression(rng) for _ in range(8)), *FIXED_EXPRESSIONS]

    for expression in expressions:
        spec = parse_cron(expression)
        expected = oracle(spec, tz, start, end)
        assert fires_between(spec, tz, start, end, limit=10_000) == expected, expression
        assert latest_fires(spec, tz, start, end, limit=3) == expected[-3:], expression
        for _ in range(4):
            instant = start + timedelta(seconds=rng.randrange(48 * 3600))
            check_next_and_prev(spec, tz, instant, expected, (start, end))


def check_next_and_prev(
    spec: CronSpec,
    tz: ZoneInfo,
    instant: datetime,
    expected: list[datetime],
    window: tuple[datetime, datetime],
) -> None:
    """Compare next_fire and prev_fire at `instant` with the oracle's fires in the window."""
    start, end = window
    later = [fire for fire in expected if fire > instant]
    earlier = [fire for fire in expected if fire <= instant]
    upcoming = next_fire(spec, tz, instant)
    previous = prev_fire(spec, tz, instant)

    if later:
        assert upcoming == later[0], (spec.source, instant)
    else:
        assert upcoming > end, (spec.source, instant)
    if earlier:
        assert previous == earlier[-1], (spec.source, instant)
    else:
        assert previous is None or previous <= start, (spec.source, instant)
