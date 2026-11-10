"""Cron parsing and fire-time computation with the DST rules of section 6.

Naive datetimes are wall times in a trigger's zone. Aware datetimes are UTC instants.
"""

from __future__ import annotations

import dataclasses
import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Final
from zoneinfo import ZoneInfo

from treadle.errors import CronError
from treadle.models import CronSpec

MACROS: Final[Mapping[str, str]] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
SEARCH_YEARS: Final = 28

_SECOND: Final = timedelta(seconds=1)
_MINUTE: Final = timedelta(minutes=1)
_DAY: Final = timedelta(days=1)
# Wider than the largest UTC offset change in tzdata (2 hours), so a search sees every wall
# time whose instants could be the answer.
_MARGIN: Final = timedelta(hours=3)
_SEARCH_DAYS: Final = SEARCH_YEARS * 365 + SEARCH_YEARS // 4
# `parse_cron` looks for a first fire from January 1, 2000 to prove that a schedule fires.
_PROOF_START: Final = datetime(2000, 1, 1) - _MINUTE
_END_OF_TIME: Final = datetime.max.replace(tzinfo=UTC)
_LAST_MINUTE: Final = 24 * 60 - 1
_LAYOUT: Final = "minute hour day-of-month month day-of-week"
# Design note: the grammar separates fields with spaces. Any whitespace separates them here,
# and whitespace around the expression is ignored, as in a crontab line.
_WORD: Final = re.compile(r"\S+")
_ITEM: Final = re.compile(r"(?:(\*)|([^-/*]+)(?:-([^-/*]+))?)(?:/(.+))?")
# Quartz-style day specials: "L", "5L", "15W", and "LW".
_SPECIAL_DAY: Final = re.compile(r"[0-9]*(L|W|LW)")
_UNSUPPORTED: Final = {
    "?": "'?' isn't supported; use '*'",
    "#": "'#' (nth weekday of the month) isn't supported",
    "L": "'L' (last day) isn't supported",
    "W": "'W' (nearest weekday) isn't supported",
}


@dataclass(frozen=True, slots=True)
class _Field:
    """One cron field: its name, its range, and its value names in uppercase."""

    name: str
    low: int
    high: int
    names: Mapping[str, int]


_MONTHS: Final = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
_WEEKDAYS: Final = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_FIELDS: Final = (
    _Field("minute", 0, 59, {}),
    _Field("hour", 0, 23, {}),
    _Field("day-of-month", 1, 31, {}),
    _Field("month", 1, 12, {name: number for number, name in enumerate(_MONTHS, 1)}),
    _Field("day-of-week", 0, 7, {name: number for number, name in enumerate(_WEEKDAYS)}),
)


def parse_cron(text: str) -> CronSpec:
    """Parse a five-field expression or a macro.

    Rejects seconds fields, `?`, `L`, `W`, `#`, `@reboot`, descending ranges, bad steps, and
    expressions that never fire within `SEARCH_YEARS`. Raises CronError with the offset of the
    bad field.
    """
    macro = text.strip()
    if macro.startswith("@"):
        return _parse_macro(text, macro)
    words = list(_WORD.finditer(text))
    if len(words) != len(_FIELDS):
        message = (
            f"seconds fields aren't supported; use 5 fields: {_LAYOUT}"
            if len(words) == len(_FIELDS) + 1
            else f"expected 5 fields ({_LAYOUT}), got {len(words)}"
        )
        offset = words[len(_FIELDS)].start() if len(words) > len(_FIELDS) else len(text)
        raise CronError(message, source=text, offset=offset)
    minutes, hours, days, months, weekdays = (
        _parse_field(field, word.group(), word.start(), text)
        for field, word in zip(_FIELDS, words, strict=True)
    )
    spec = CronSpec(
        source=text,
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset(day % 7 for day in weekdays),  # 7 is Sunday, like 0
        dom_restricted=not words[2].group().startswith("*"),
        dow_restricted=not words[4].group().startswith("*"),
    )
    if next_wall_time(spec, _PROOF_START) is None:
        raise CronError(
            "the expression never fires: no date matches its day-of-month and month fields",
            source=text,
            offset=words[2].start(),
        )
    return spec


def matches(spec: CronSpec, wall: datetime) -> bool:
    """True when a naive wall time matches, with Vixie day-of-month or day-of-week rules."""
    return (
        wall.minute in spec.minutes and wall.hour in spec.hours and _day_matches(spec, wall.date())
    )


def next_wall_time(spec: CronSpec, after: datetime) -> datetime | None:
    """Return the first matching naive wall time strictly after `after`, to the minute.

    Returns None when nothing matches within `SEARCH_YEARS`.
    """
    return _search(spec, after.replace(second=0, microsecond=0) + _MINUTE, forward=True)


def prev_wall_time(spec: CronSpec, before: datetime) -> datetime | None:
    """Return the last matching naive wall time strictly before `before`, to the minute.

    Returns None when nothing matches within `SEARCH_YEARS`.
    """
    latest = (before - timedelta(microseconds=1)).replace(second=0, microsecond=0)
    return _search(spec, latest, forward=False)


def wall_instants(spec: CronSpec, wall: datetime, tz: ZoneInfo) -> tuple[datetime, ...]:
    """Map a matching naive wall time to its UTC fire instants under the DST rules.

    A gap time fires once at the transition for fixed-time schedules and never for
    interval-like ones. An overlap time fires on its first occurrence only for fixed-time
    schedules and on both for interval-like ones.
    """
    first = wall.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = wall.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if _local(first, tz) != wall:  # a gap: this wall time doesn't exist
        return () if spec.interval_like else (_transition(tz, second, first),)
    if first != second and spec.interval_like:  # an overlap: this wall time happens twice
        return (first, second)
    return (first,)


def next_fire(spec: CronSpec, tz: ZoneInfo, after: datetime) -> datetime:
    """Return the first fire instant after the aware `after`, in UTC.

    Raises CronError when nothing fires within `SEARCH_YEARS`.
    """
    fire = _next_instant(spec, tz, after)
    if fire is None:
        raise CronError(
            f"the expression doesn't fire within {SEARCH_YEARS} years", source=spec.source
        )
    return fire


def prev_fire(spec: CronSpec, tz: ZoneInfo, at_or_before: datetime) -> datetime | None:
    """Return the last fire instant at or before the aware `at_or_before`, in UTC, or None.

    It mirrors `next_fire`: it walks back from 3 hours past the local time, and stops 3 hours
    before the best fire found.
    """
    best: datetime | None = None
    stop: datetime | None = None
    cursor = _local(at_or_before, tz) + _MARGIN
    while (wall := prev_wall_time(spec, cursor)) is not None:
        if stop is not None and wall < stop:
            break
        cursor = wall
        earlier = [instant for instant in wall_instants(spec, wall, tz) if instant <= at_or_before]
        if earlier and (best is None or earlier[-1] > best):
            best = earlier[-1]
            stop = _local(best, tz) - _MARGIN
    return best


def fires_between(
    spec: CronSpec, tz: ZoneInfo, start: datetime, end: datetime, *, limit: int
) -> list[datetime]:
    """Return the fires in (start, end], oldest first, at most `limit` of them."""
    fires: list[datetime] = []
    cursor = start
    while len(fires) < limit and (fire := _next_instant(spec, tz, cursor)) is not None:
        if fire > end:
            break
        fires.append(fire)
        cursor = fire
    return fires


def latest_fires(
    spec: CronSpec, tz: ZoneInfo, start: datetime, end: datetime, *, limit: int
) -> list[datetime]:
    """Return the newest `limit` fires in (start, end], oldest first.

    It walks back from `end` with `prev_fire`, so the work stays bounded after long downtime.
    """
    fires: list[datetime] = []
    cursor = end
    while len(fires) < limit and (fire := prev_fire(spec, tz, cursor)) is not None:
        if fire <= start:
            break
        fires.append(fire)
        cursor = fire - _SECOND
    fires.reverse()
    return fires


@dataclass(frozen=True, slots=True)
class FireInfo:
    """One fire for `treadle cron`: the UTC instant, the zone's local time, and a DST note."""

    at: datetime
    local: datetime
    note: str | None


def describe_fires(spec: CronSpec, tz: ZoneInfo, after: datetime, count: int) -> list[FireInfo]:
    """Return the next `count` fires after the aware `after`, with notes for DST changes.

    A fire that stands in for wall times in a DST gap gets a note such as "02:30 doesn't
    exist (DST gap); runs at 03:00", and a fire at a repeated wall time gets one such as
    "01:30 happens twice (DST overlap); runs only the first time".
    """
    fires = fires_between(spec, tz, after, _END_OF_TIME, limit=count)
    return [FireInfo(at, at.astimezone(tz), _dst_note(spec, tz, at)) for at in fires]


def _parse_macro(text: str, macro: str) -> CronSpec:
    offset = text.index("@")
    if macro == "@reboot":
        raise CronError("@reboot isn't supported; use catch_up instead", source=text, offset=offset)
    if macro not in MACROS:
        raise CronError(
            f"unknown macro '{macro}'; use one of {', '.join(MACROS)}", source=text, offset=offset
        )
    return dataclasses.replace(parse_cron(MACROS[macro]), source=text)


def _parse_field(field: _Field, word: str, at: int, text: str) -> set[int]:
    """Return the values of one field; `at` is the offset of `word` inside `text`."""
    for char in "?#":
        if (index := word.find(char)) >= 0:
            raise CronError(_UNSUPPORTED[char], source=text, offset=at + index)
    values: set[int] = set()
    for item in word.split(","):
        if not item:
            raise CronError(f"empty list item in the {field.name} field", source=text, offset=at)
        values |= _parse_item(field, item, at, text)
        at += len(item) + 1
    return values


def _parse_item(field: _Field, item: str, at: int, text: str) -> set[int]:
    """Return the values of `*`, `a`, or `a-b`, each with an optional `/step`.

    `a/n` means `a-max/n`, where max is the top of the field's range (7 for day-of-week).
    """
    match = _ITEM.fullmatch(item)
    if match is None:
        raise CronError(f"can't parse '{item}' in the {field.name} field", source=text, offset=at)
    star, first, last, step = match.groups()
    low, high = field.low, field.high
    if star is None:
        low = _value(field, first, at, text)
        if last is not None:
            high = _value(field, last, at + match.start(3), text)
            if high < low:
                raise CronError(f"range '{first}-{last}' must be ascending", source=text, offset=at)
        elif step is None:
            high = low
    stride = 1 if step is None else _step(field, step, at + match.start(4), text)
    return set(range(low, high + 1, stride))


def _value(field: _Field, token: str, at: int, text: str) -> int:
    """Return the number or name `token` as a value of `field`."""
    name = token.upper()
    if name in field.names:
        return field.names[name]
    number = _digits(token)
    if number is None:
        if special := _SPECIAL_DAY.fullmatch(name):
            letter = special.group(1)[0]
            raise CronError(_UNSUPPORTED[letter], source=text, offset=at + special.start(1))
        raise CronError(f"invalid {field.name} '{token}'", source=text, offset=at)
    if not field.low <= number <= field.high:
        raise CronError(
            f"{field.name} {token} isn't between {field.low} and {field.high}",
            source=text,
            offset=at,
        )
    return number


def _step(field: _Field, token: str, at: int, text: str) -> int:
    span = field.high - field.low + 1
    number = _digits(token)
    if number is None or not 1 <= number <= span:
        raise CronError(f"step '{token}' isn't between 1 and {span}", source=text, offset=at)
    return number


def _digits(token: str) -> int | None:
    """Return the value of a run of ASCII digits, or None. Values past 99 come back as 100."""
    if not (token.isascii() and token.isdigit()):
        return None
    significant = token.lstrip("0")
    return int(significant or "0") if len(significant) <= 2 else 100


def _day_matches(spec: CronSpec, day: date) -> bool:
    """Apply the month and the Vixie day rule: either day field when both are restricted."""
    if day.month not in spec.months:
        return False
    in_month = day.day in spec.days
    in_week = day.isoweekday() % 7 in spec.weekdays
    if spec.dom_restricted and spec.dow_restricted:
        return in_month or in_week
    return in_month and in_week


def _search(spec: CronSpec, origin: datetime, *, forward: bool) -> datetime | None:
    """Return the first matching wall time at or after `origin`, or the last at or before it.

    It walks day by day, skips whole months that don't match, and gives up after
    `SEARCH_YEARS`.
    """
    times = _clock_times(spec.hours, spec.minutes)
    day = first_day = origin.date()
    bound = origin.hour * 60 + origin.minute
    while abs((day - first_day).days) < _SEARCH_DAYS:
        if day.month not in spec.months:
            day = _next_month(day) if forward else day.replace(day=1) - _DAY
        else:
            if _day_matches(spec, day):
                index = bisect_left(times, bound) if forward else bisect_right(times, bound) - 1
                if 0 <= index < len(times):
                    return datetime.combine(day, time(*divmod(times[index], 60)))
            day += _DAY if forward else -_DAY
        bound = 0 if forward else _LAST_MINUTE
    return None


@lru_cache(maxsize=256)
def _clock_times(hours: frozenset[int], minutes: frozenset[int]) -> tuple[int, ...]:
    """Return every matching minute of the day, sorted, as minutes since midnight.

    Searches call this for every wall time they visit, so it is computed once per schedule.
    """
    return tuple(sorted(hour * 60 + minute for hour in hours for minute in minutes))


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def _local(instant: datetime, tz: ZoneInfo) -> datetime:
    """Return the naive wall time of an aware instant in `tz`."""
    return instant.astimezone(tz).replace(tzinfo=None, fold=0)


def _transition(tz: ZoneInfo, before: datetime, after: datetime) -> datetime:
    """Return the first whole second in (before, after] that has the UTC offset of `after`."""
    target = after.astimezone(tz).utcoffset()
    low, high = 0, (after - before) // _SECOND
    while high - low > 1:
        middle = (low + high) // 2
        if (before + middle * _SECOND).astimezone(tz).utcoffset() == target:
            high = middle
        else:
            low = middle
    return before + high * _SECOND


def _next_instant(spec: CronSpec, tz: ZoneInfo, after: datetime) -> datetime | None:
    """Return the first fire after `after`, or None when none comes within `SEARCH_YEARS`.

    It starts 3 hours before the local time of `after` and stops 3 hours past the best fire.
    """
    best: datetime | None = None
    stop: datetime | None = None
    cursor = _local(after, tz).replace(second=0, microsecond=0) - _MARGIN
    while (wall := next_wall_time(spec, cursor)) is not None:
        if stop is not None and wall > stop:
            break
        cursor = wall
        later = [instant for instant in wall_instants(spec, wall, tz) if instant > after]
        if later and (best is None or later[0] < best):
            best = later[0]
            stop = _local(best, tz) + _MARGIN
    return best


def _dst_note(spec: CronSpec, tz: ZoneInfo, at: datetime) -> str | None:
    """Explain a fire at a repeated wall time, or one that stands in for gap times."""
    wall = _local(at, tz)
    if wall.replace(tzinfo=tz, fold=1).utcoffset() != wall.replace(tzinfo=tz).utcoffset():
        runs = "runs both times" if spec.interval_like else "runs only the first time"
        return f"{wall:%H:%M} happens twice (DST overlap); {runs}"
    if spec.interval_like:
        return None
    # A fixed-time fire at a transition stands in for the matching wall times in the gap,
    # which lie between the local time one second earlier and the fire's own local time.
    skipped = _walls_between(spec, _local(at - _SECOND, tz) + _SECOND, wall)
    if not skipped:
        return None
    if len(skipped) == 1:
        return f"{skipped[0]:%H:%M} doesn't exist (DST gap); runs at {wall:%H:%M}"
    return (
        f"{skipped[0]:%H:%M} to {skipped[-1]:%H:%M} don't exist (DST gap); "
        f"runs once at {wall:%H:%M}"
    )


def _walls_between(spec: CronSpec, start: datetime, end: datetime) -> list[datetime]:
    """Return the matching wall times in [start, end)."""
    walls: list[datetime] = []
    cursor = start - _MINUTE
    while (wall := next_wall_time(spec, cursor)) is not None and wall < end:
        walls.append(wall)
        cursor = wall
    return walls
