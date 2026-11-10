from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from treadle.cron import (
    fires_between,
    latest_fires,
    next_fire,
    next_wall_time,
    parse_cron,
    prev_fire,
    prev_wall_time,
    wall_instants,
)
from treadle.errors import CronError
from treadle.models import CronSpec

NEW_YORK = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")
SECOND = timedelta(seconds=1)


def utc(text: str) -> datetime:
    """Parse a naive ISO time as UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# Section 6, "Verified DST vectors": (expression, zone, after, the next fires), all in UTC.
VECTORS = [
    (
        "30 2 * * *",
        "America/New_York",
        "2026-03-07T12:00",
        ["2026-03-08T07:00", "2026-03-09T06:30", "2026-03-10T06:30"],
    ),
    (
        "30 1 * * *",
        "America/New_York",
        "2026-10-31T12:00",
        ["2026-11-01T05:30", "2026-11-02T06:30", "2026-11-03T06:30"],
    ),
    (
        "0 * * * *",
        "America/New_York",
        "2026-11-01T04:30",
        ["2026-11-01T05:00", "2026-11-01T06:00", "2026-11-01T07:00", "2026-11-01T08:00"],
    ),
    (
        "*/15 * * * *",
        "America/New_York",
        "2026-03-08T06:40",
        ["2026-03-08T06:45", "2026-03-08T07:00", "2026-03-08T07:15"],
    ),
    (
        "*/15 2 * * *",
        "America/New_York",
        "2026-03-08T00:00",
        ["2026-03-08T07:00", "2026-03-09T06:00", "2026-03-09T06:15"],
    ),
    (
        "*/30 * * * *",
        "America/New_York",
        "2026-03-08T06:00",
        ["2026-03-08T06:30", "2026-03-08T07:00", "2026-03-08T07:30"],
    ),
    (
        "*/30 * * * *",
        "America/New_York",
        "2026-11-01T04:45",
        [
            "2026-11-01T05:00",
            "2026-11-01T05:30",
            "2026-11-01T06:00",
            "2026-11-01T06:30",
            "2026-11-01T07:00",
        ],
    ),
    ("30 1 * * *", "Europe/London", "2026-03-28T12:00", ["2026-03-29T01:00", "2026-03-30T00:30"]),
    ("30 1 * * *", "Europe/London", "2026-10-24T12:00", ["2026-10-25T00:30", "2026-10-26T01:30"]),
    (
        "15 2 * * *",
        "Australia/Lord_Howe",
        "2026-10-03T00:00",
        ["2026-10-03T15:30", "2026-10-04T15:15"],
    ),
    (
        "45 1 * * *",
        "Australia/Lord_Howe",
        "2026-04-04T00:00",
        ["2026-04-04T14:45", "2026-04-05T15:15"],
    ),
    ("0 2 * * *", "Antarctica/Troll", "2026-03-28T12:00", ["2026-03-29T01:00", "2026-03-30T00:00"]),
]
VECTOR_IDS = [f"{expression} {zone} {after}" for expression, zone, after, _ in VECTORS]


@pytest.mark.parametrize(("expression", "zone", "after", "expected"), VECTORS, ids=VECTOR_IDS)
def test_next_fire_matches_the_verified_dst_vectors(
    expression: str, zone: str, after: str, expected: list[str]
) -> None:
    spec, tz = parse_cron(expression), ZoneInfo(zone)
    fires: list[datetime] = []
    cursor = utc(after)
    for _ in expected:
        cursor = next_fire(spec, tz, cursor)
        fires.append(cursor)

    assert fires == [utc(fire) for fire in expected]


@pytest.mark.parametrize(("expression", "zone", "after", "expected"), VECTORS, ids=VECTOR_IDS)
def test_fire_ranges_match_the_verified_dst_vectors(
    expression: str, zone: str, after: str, expected: list[str]
) -> None:
    spec, tz = parse_cron(expression), ZoneInfo(zone)
    start, fires = utc(after), [utc(fire) for fire in expected]

    assert fires_between(spec, tz, start, fires[-1], limit=100) == fires
    assert latest_fires(spec, tz, start, fires[-1], limit=100) == fires
    assert latest_fires(spec, tz, start, fires[-1], limit=2) == fires[-2:]


@pytest.mark.parametrize(("expression", "zone", "after", "expected"), VECTORS, ids=VECTOR_IDS)
def test_prev_fire_mirrors_next_fire_on_the_verified_dst_vectors(
    expression: str, zone: str, after: str, expected: list[str]
) -> None:
    spec, tz = parse_cron(expression), ZoneInfo(zone)
    fires = [utc(fire) for fire in expected]

    for earlier, fire in pairwise(fires):
        assert prev_fire(spec, tz, fire) == fire
        assert prev_fire(spec, tz, fire - SECOND) == earlier
    previous = prev_fire(spec, tz, fires[0] - SECOND)
    assert previous is not None
    assert previous <= utc(after)


def test_next_wall_time_is_strictly_after_and_minute_precise() -> None:
    spec = parse_cron("30 2 * * *")

    assert next_wall_time(spec, datetime(2026, 11, 9, 2, 29, 59)) == datetime(2026, 11, 9, 2, 30)
    assert next_wall_time(spec, datetime(2026, 11, 9, 2, 30)) == datetime(2026, 11, 10, 2, 30)
    assert next_wall_time(spec, datetime(2026, 11, 9, 2, 30, 1)) == datetime(2026, 11, 10, 2, 30)


def test_prev_wall_time_is_strictly_before() -> None:
    spec = parse_cron("30 2 * * *")

    assert prev_wall_time(spec, datetime(2026, 11, 9, 2, 30)) == datetime(2026, 11, 8, 2, 30)
    assert prev_wall_time(spec, datetime(2026, 11, 9, 2, 30, 0, 1)) == datetime(2026, 11, 9, 2, 30)
    assert prev_wall_time(spec, datetime(2026, 11, 9, 2, 31)) == datetime(2026, 11, 9, 2, 30)


@pytest.mark.parametrize(
    ("expression", "origin", "after", "before"),
    [
        ("0 12 29 2 *", "2026-11-09T00:00", "2028-02-29T12:00", "2024-02-29T12:00"),
        ("59 23 31 12 *", "2026-12-31T23:59", "2027-12-31T23:59", "2025-12-31T23:59"),
        ("0 0 1 1 *", "2026-01-01T00:00", "2027-01-01T00:00", "2025-01-01T00:00"),
        ("0 9 * 3 MON", "2026-11-09T00:00", "2027-03-01T09:00", "2026-03-30T09:00"),
        ("0 0 13 * FRI", "2026-11-09T00:00", "2026-11-13T00:00", "2026-11-06T00:00"),
        ("*/20 9-10 * * *", "2026-11-09T10:40", "2026-11-10T09:00", "2026-11-09T10:20"),
    ],
)
def test_wall_time_searches_skip_days_months_and_years(
    expression: str, origin: str, after: str, before: str
) -> None:
    spec, wall = parse_cron(expression), datetime.fromisoformat(origin)

    assert next_wall_time(spec, wall) == datetime.fromisoformat(after)
    assert prev_wall_time(spec, wall) == datetime.fromisoformat(before)


def test_a_schedule_that_never_fires_gives_up_after_search_years() -> None:
    never = CronSpec(
        source="0 0 30 2 *",
        minutes=frozenset({0}),
        hours=frozenset({0}),
        days=frozenset({30}),
        months=frozenset({2}),
        weekdays=frozenset(range(7)),
        dom_restricted=True,
        dow_restricted=False,
    )
    now = utc("2026-11-09T12:00")

    assert next_wall_time(never, now.replace(tzinfo=None)) is None
    assert prev_wall_time(never, now.replace(tzinfo=None)) is None
    assert prev_fire(never, UTC_ZONE, now) is None
    assert fires_between(never, UTC_ZONE, now - timedelta(days=1), now, limit=5) == []
    assert latest_fires(never, UTC_ZONE, now - timedelta(days=1), now, limit=5) == []
    with pytest.raises(CronError) as caught:
        next_fire(never, UTC_ZONE, now)
    assert (str(caught.value), caught.value.source) == (
        "the expression doesn't fire within 28 years",
        "0 0 30 2 *",
    )


def test_wall_instants_maps_an_ordinary_time_to_one_instant() -> None:
    spec = parse_cron("30 2 * * *")

    assert wall_instants(spec, datetime(2026, 11, 9, 2, 30), NEW_YORK) == (utc("2026-11-09T07:30"),)


@pytest.mark.parametrize("minute", [0, 30, 59])
def test_gap_times_fire_at_the_transition_for_fixed_time_schedules(minute: int) -> None:
    spec = parse_cron(f"{minute} 2 * * *")

    assert wall_instants(spec, datetime(2026, 3, 8, 2, minute), NEW_YORK) == (
        utc("2026-03-08T07:00"),
    )


def test_gap_times_never_fire_for_interval_like_schedules() -> None:
    assert wall_instants(parse_cron("30 * * * *"), datetime(2026, 3, 8, 2, 30), NEW_YORK) == ()


def test_overlap_times_fire_once_when_fixed_and_twice_when_interval_like() -> None:
    wall = datetime(2026, 11, 1, 1, 30)
    first, second = utc("2026-11-01T05:30"), utc("2026-11-01T06:30")

    assert wall_instants(parse_cron("30 1 * * *"), wall, NEW_YORK) == (first,)
    assert wall_instants(parse_cron("30 * * * *"), wall, NEW_YORK) == (first, second)


def test_the_transition_search_finds_a_half_hour_shift() -> None:
    lord_howe = ZoneInfo("Australia/Lord_Howe")

    assert wall_instants(parse_cron("15 2 * * *"), datetime(2026, 10, 4, 2, 15), lord_howe) == (
        utc("2026-10-03T15:30"),
    )


def test_next_fire_is_strictly_after_a_time_with_seconds() -> None:
    spec = parse_cron("0 * * * *")

    assert next_fire(spec, UTC_ZONE, utc("2026-11-09T00:59:59.500")) == utc("2026-11-09T01:00")
    assert next_fire(spec, UTC_ZONE, utc("2026-11-09T01:00")) == utc("2026-11-09T02:00")
    assert prev_fire(spec, UTC_ZONE, utc("2026-11-09T01:00")) == utc("2026-11-09T01:00")
    assert prev_fire(spec, UTC_ZONE, utc("2026-11-09T00:59:59")) == utc("2026-11-09T00:00")


def test_fires_between_excludes_the_start_includes_the_end_and_stops_at_the_limit() -> None:
    spec = parse_cron("0 * * * *")
    start, end = utc("2026-11-09T00:00"), utc("2026-11-09T03:00")
    hours = [utc("2026-11-09T01:00"), utc("2026-11-09T02:00"), utc("2026-11-09T03:00")]

    assert fires_between(spec, UTC_ZONE, start, end, limit=10) == hours
    assert fires_between(spec, UTC_ZONE, start, end, limit=2) == hours[:2]
    assert fires_between(spec, UTC_ZONE, start, end, limit=0) == []
    assert fires_between(spec, UTC_ZONE, end, end, limit=10) == []


def test_latest_fires_excludes_the_start_includes_the_end_and_keeps_the_newest() -> None:
    spec = parse_cron("0 * * * *")
    start, end = utc("2026-11-09T00:00"), utc("2026-11-09T03:00")
    hours = [utc("2026-11-09T01:00"), utc("2026-11-09T02:00"), utc("2026-11-09T03:00")]

    assert latest_fires(spec, UTC_ZONE, start, end, limit=10) == hours
    assert latest_fires(spec, UTC_ZONE, start, end, limit=2) == hours[1:]
    assert latest_fires(spec, UTC_ZONE, start, end, limit=0) == []
    assert latest_fires(spec, UTC_ZONE, start, start, limit=10) == []


def test_latest_fires_stays_bounded_after_long_downtime() -> None:
    spec = parse_cron("*/5 * * * *")
    start, end = utc("2025-11-09T12:00"), utc("2026-11-09T12:02:30")

    assert latest_fires(spec, NEW_YORK, start, end, limit=3) == [
        utc("2026-11-09T11:50"),
        utc("2026-11-09T11:55"),
        utc("2026-11-09T12:00"),
    ]
