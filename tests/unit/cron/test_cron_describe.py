from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from treadle.cron import FireInfo, describe_fires, parse_cron

NEW_YORK = ZoneInfo("America/New_York")


def rows(infos: list[FireInfo]) -> list[tuple[str, str, str | None]]:
    """Format fires the way `treadle cron` lays out its LOCAL, UTC, and NOTE columns."""
    return [
        (f"{info.local:%Y-%m-%d %H:%M %Z}", f"{info.at:%Y-%m-%dT%H:%MZ}", info.note)
        for info in infos
    ]


def test_describe_fires_matches_the_cli_example() -> None:
    after = datetime(2026, 3, 7, tzinfo=NEW_YORK)

    infos = describe_fires(parse_cron("30 2 * * *"), NEW_YORK, after, 3)

    assert rows(infos) == [
        ("2026-03-07 02:30 EST", "2026-03-07T07:30Z", None),
        (
            "2026-03-08 03:00 EDT",
            "2026-03-08T07:00Z",
            "02:30 doesn't exist (DST gap); runs at 03:00",
        ),
        ("2026-03-09 02:30 EDT", "2026-03-09T06:30Z", None),
    ]


def test_fire_infos_hold_the_utc_instant_and_the_local_time_in_the_zone() -> None:
    (info,) = describe_fires(
        parse_cron("0 * * * *"), NEW_YORK, datetime(2026, 11, 1, 5, 30, tzinfo=UTC), 1
    )

    assert info.at == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert info.at.tzinfo is UTC
    assert info.local.tzinfo is NEW_YORK
    assert (info.local.hour, info.local.fold) == (1, 1)


@pytest.mark.parametrize(
    ("expression", "zone", "after", "expected"),
    [
        (
            "*/15 2 * * *",
            "America/New_York",
            "2026-03-08T00:00",
            [("2026-03-08 03:00 EDT", "02:00 to 02:45 don't exist (DST gap); runs once at 03:00")],
        ),
        (
            "30 1 * * *",
            "America/New_York",
            "2026-10-31T12:00",
            [
                (
                    "2026-11-01 01:30 EDT",
                    "01:30 happens twice (DST overlap); runs only the first time",
                ),
                ("2026-11-02 01:30 EST", None),
            ],
        ),
        (
            "0 * * * *",
            "America/New_York",
            "2026-11-01T04:30",
            [
                ("2026-11-01 01:00 EDT", "01:00 happens twice (DST overlap); runs both times"),
                ("2026-11-01 01:00 EST", "01:00 happens twice (DST overlap); runs both times"),
                ("2026-11-01 02:00 EST", None),
            ],
        ),
        (
            "*/30 * * * *",
            "America/New_York",
            "2026-03-08T06:00",
            [("2026-03-08 01:30 EST", None), ("2026-03-08 03:00 EDT", None)],
        ),
        (
            "15 2 * * *",
            "Australia/Lord_Howe",
            "2026-10-03T00:00",
            [("2026-10-04 02:30 +11", "02:15 doesn't exist (DST gap); runs at 02:30")],
        ),
        (
            "0 2 * * *",
            "Antarctica/Troll",
            "2026-03-28T12:00",
            [("2026-03-29 03:00 +02", "02:00 doesn't exist (DST gap); runs at 03:00")],
        ),
        (
            "0 2,3 * * *",
            "America/New_York",
            "2026-03-08T06:00",
            [("2026-03-08 03:00 EDT", "02:00 doesn't exist (DST gap); runs at 03:00")],
        ),
        (
            "0 3 * * *",
            "America/New_York",
            "2026-03-08T06:00",
            [("2026-03-08 03:00 EDT", None)],
        ),
    ],
)
def test_describe_fires_explains_dst_gaps_and_overlaps(
    expression: str, zone: str, after: str, expected: list[tuple[str, str | None]]
) -> None:
    start = datetime.fromisoformat(after).replace(tzinfo=UTC)

    infos = describe_fires(parse_cron(expression), ZoneInfo(zone), start, len(expected))

    assert [(local, note) for local, _, note in rows(infos)] == expected


def test_describe_fires_returns_nothing_for_a_count_of_zero() -> None:
    assert (
        describe_fires(parse_cron("@daily"), NEW_YORK, datetime(2026, 11, 9, tzinfo=UTC), 0) == []
    )
