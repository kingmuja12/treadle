from __future__ import annotations

import dataclasses
from datetime import date, datetime, time

import pytest

from treadle.cron import MACROS, matches, parse_cron
from treadle.errors import CronError

LAYOUT = "minute hour day-of-month month day-of-week"
NEVER = "the expression never fires: no date matches its day-of-month and month fields"
EVERY_DAY = frozenset(range(7))


@pytest.mark.parametrize(
    ("text", "field", "values"),
    [
        ("5 * * * *", "minutes", {5}),
        ("0,15,45 * * * *", "minutes", {0, 15, 45}),
        ("10-14 * * * *", "minutes", {10, 11, 12, 13, 14}),
        ("*/20 * * * *", "minutes", {0, 20, 40}),
        ("10-30/10 * * * *", "minutes", {10, 20, 30}),
        ("50/5 * * * *", "minutes", {50, 55}),
        ("*/60 * * * *", "minutes", {0}),
        ("0-5/2,58-59 * * * *", "minutes", {0, 2, 4, 58, 59}),
        ("007 * * * *", "minutes", {7}),
        ("* */6 * * *", "hours", {0, 6, 12, 18}),
        ("* 9-17/4 * * *", "hours", {9, 13, 17}),
        ("* * 1,15 * *", "days", {1, 15}),
        ("* * */10 * *", "days", {1, 11, 21, 31}),
        ("* * * JAN *", "months", {1}),
        ("* * * jan,Jun,DEC *", "months", {1, 6, 12}),
        ("* * * MAR-MAY *", "months", {3, 4, 5}),
        ("* * * */3 *", "months", {1, 4, 7, 10}),
        ("* * * FEB/6 *", "months", {2, 8}),
        ("* * * 11-DEC *", "months", {11, 12}),
        ("* * * * MON-FRI", "weekdays", {1, 2, 3, 4, 5}),
        ("* * * * sun,Sat", "weekdays", {0, 6}),
        ("* * * * 7", "weekdays", {0}),
        ("* * * * 5-7", "weekdays", {5, 6, 0}),
        ("* * * * 0-7", "weekdays", EVERY_DAY),
        ("* * * * */2", "weekdays", {0, 2, 4, 6}),
        ("* * * * 1/2", "weekdays", {1, 3, 5, 0}),
        ("* * * * THU-SAT/2", "weekdays", {4, 6}),
    ],
)
def test_fields_accept_values_names_ranges_steps_and_lists(
    text: str, field: str, values: set[int]
) -> None:
    assert getattr(parse_cron(text), field) == frozenset(values)


def test_a_full_expression_fills_every_field() -> None:
    spec = parse_cron("30 2 * * *")

    assert spec.source == "30 2 * * *"
    assert (spec.minutes, spec.hours) == (frozenset({30}), frozenset({2}))
    assert (spec.days, spec.months, spec.weekdays) == (
        frozenset(range(1, 32)),
        frozenset(range(1, 13)),
        EVERY_DAY,
    )


@pytest.mark.parametrize("text", ["0  0 * * *", " 0 0 * * * ", "0\t0 * * *"])
def test_fields_may_be_separated_by_any_whitespace(text: str) -> None:
    assert dataclasses.replace(parse_cron(text), source="0 0 * * *") == parse_cron("0 0 * * *")


@pytest.mark.parametrize(
    ("text", "dom_restricted", "dow_restricted"),
    [
        ("0 0 * * *", False, False),
        ("0 0 1 * *", True, False),
        ("0 0 * * 1", False, True),
        ("0 0 1 * 1", True, True),
        ("0 0 */2 * */2", False, False),
        ("0 0 1-31 * 0-7", True, True),
    ],
)
def test_a_day_field_is_restricted_when_it_does_not_start_with_a_star(
    text: str, dom_restricted: bool, dow_restricted: bool
) -> None:
    spec = parse_cron(text)

    assert (spec.dom_restricted, spec.dow_restricted) == (dom_restricted, dow_restricted)


@pytest.mark.parametrize(("macro", "expansion"), MACROS.items())
def test_macros_parse_like_their_expansions(macro: str, expansion: str) -> None:
    spec = parse_cron(macro)

    assert spec.source == macro
    assert dataclasses.replace(spec, source=expansion) == parse_cron(expansion)


def test_a_macro_keeps_its_source_as_written() -> None:
    assert parse_cron(" @daily ").source == " @daily "


@pytest.mark.parametrize(
    ("text", "interval_like"),
    [
        ("*/15 * * * *", True),
        ("0 */1 * * *", True),
        ("0 0-23 * * *", True),
        ("0 * 1 * *", True),
        ("@hourly", True),
        ("@daily", False),
        ("@weekly", False),
        ("30 2 * * *", False),
        ("0 0-22 * * *", False),
        ("*/15 2 * * *", False),
    ],
)
def test_a_schedule_is_interval_like_when_it_runs_every_hour(
    text: str, interval_like: bool
) -> None:
    assert parse_cron(text).interval_like is interval_like


@pytest.mark.parametrize(
    ("text", "message", "offset"),
    [
        ("", f"expected 5 fields ({LAYOUT}), got 0", 0),
        ("0 0 * *", f"expected 5 fields ({LAYOUT}), got 4", 7),
        ("0 0 * * * *", f"seconds fields aren't supported; use 5 fields: {LAYOUT}", 10),
        ("0 0 * * * * 2026", f"expected 5 fields ({LAYOUT}), got 7", 10),
        ("0 0 ? * *", "'?' isn't supported; use '*'", 4),
        ("0 0 * * 1,?", "'?' isn't supported; use '*'", 10),
        ("0 0 L * *", "'L' (last day) isn't supported", 4),
        ("0 0 L-3 * *", "'L' (last day) isn't supported", 4),
        ("0 0 LW * *", "'L' (last day) isn't supported", 4),
        ("0 0 * * 5L", "'L' (last day) isn't supported", 9),
        ("0 0 15W * *", "'W' (nearest weekday) isn't supported", 6),
        ("0 0 1,15w * *", "'W' (nearest weekday) isn't supported", 8),
        ("0 0 * * 5#3", "'#' (nth weekday of the month) isn't supported", 9),
        ("@reboot", "@reboot isn't supported; use catch_up instead", 0),
        ("  @reboot", "@reboot isn't supported; use catch_up instead", 2),
        (
            "@every 5m",
            "unknown macro '@every 5m'; use one of "
            "@yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly",
            0,
        ),
        (
            "@DAILY",
            "unknown macro '@DAILY'; use one of "
            "@yearly, @annually, @monthly, @weekly, @daily, @midnight, @hourly",
            0,
        ),
        ("60 * * * *", "minute 60 isn't between 0 and 59", 0),
        ("* 24 * * *", "hour 24 isn't between 0 and 23", 2),
        ("* * 0 * *", "day-of-month 0 isn't between 1 and 31", 4),
        ("* * 32 * *", "day-of-month 32 isn't between 1 and 31", 4),
        ("* * * 13 *", "month 13 isn't between 1 and 12", 6),
        ("* * * * 8", "day-of-week 8 isn't between 0 and 7", 8),
        ("* * * * 1-99999", "day-of-week 99999 isn't between 0 and 7", 10),
        ("* * * * FRI-MON", "range 'FRI-MON' must be ascending", 8),
        ("30-10 * * * *", "range '30-10' must be ascending", 0),
        ("0,30-10 * * * *", "range '30-10' must be ascending", 2),
        ("*/0 * * * *", "step '0' isn't between 1 and 60", 2),
        ("*/61 * * * *", "step '61' isn't between 1 and 60", 2),
        ("* */25 * * *", "step '25' isn't between 1 and 24", 4),
        ("* * */32 * *", "step '32' isn't between 1 and 31", 6),
        ("* * * */13 *", "step '13' isn't between 1 and 12", 8),
        ("* * * * */9", "step '9' isn't between 1 and 8", 10),
        ("1-5/x * * * *", "step 'x' isn't between 1 and 60", 4),
        ("*/2/3 * * * *", "step '2/3' isn't between 1 and 60", 2),
        ("1-2-3 * * * *", "can't parse '1-2-3' in the minute field", 0),
        ("*-5 * * * *", "can't parse '*-5' in the minute field", 0),
        ("/5 * * * *", "can't parse '/5' in the minute field", 0),
        ("5/ * * * *", "can't parse '5/' in the minute field", 0),
        ("1,,2 * * * *", "empty list item in the minute field", 2),
        ("* * 1, * *", "empty list item in the day-of-month field", 6),
        ("* * * WED *", "invalid month 'WED'", 6),
        ("* * * * JUL", "invalid day-of-week 'JUL'", 8),
        ("MON * * * *", "invalid minute 'MON'", 0),
        ("* * * JANUARY *", "invalid month 'JANUARY'", 6),
        (
            "* * * * \N{ARABIC-INDIC DIGIT THREE}",
            "invalid day-of-week '\N{ARABIC-INDIC DIGIT THREE}'",
            8,
        ),
        ("0 0 30 2 *", NEVER, 4),
        ("0 0 31 4,6,9,11 *", NEVER, 4),
        ("0 0 30,31 FEB *", NEVER, 4),
    ],
)
def test_parse_cron_rejects_bad_expressions_with_offsets(
    text: str, message: str, offset: int
) -> None:
    with pytest.raises(CronError) as caught:
        parse_cron(text)

    assert (str(caught.value), caught.value.offset, caught.value.source) == (message, offset, text)


@pytest.mark.parametrize("text", ["0 0 29 2 *", "0 0 31 * *", "0 0 30 2 MON", "0 0 */30 2 *"])
def test_rare_but_possible_dates_parse(text: str) -> None:
    parse_cron(text)


@pytest.mark.parametrize(
    ("text", "day", "expected"),
    [
        ("0 0 13 * FRI", date(2026, 11, 13), True),
        ("0 0 13 * FRI", date(2026, 11, 20), True),
        ("0 0 13 * FRI", date(2026, 10, 13), True),
        ("0 0 13 * FRI", date(2026, 11, 12), False),
        ("0 0 13 2 FRI", date(2026, 11, 13), False),
        ("0 0 * * FRI", date(2026, 11, 20), True),
        ("0 0 * * FRI", date(2026, 11, 12), False),
        ("0 0 13 * *", date(2026, 11, 13), True),
        ("0 0 13 * *", date(2026, 11, 20), False),
        ("0 0 */2 * FRI", date(2026, 11, 13), True),
        ("0 0 */2 * FRI", date(2026, 11, 20), False),
        ("0 0 */2 * FRI", date(2026, 11, 11), False),
        ("0 0 1 * MON-FRI", date(2026, 11, 1), True),
        ("0 0 1 * MON-FRI", date(2026, 11, 9), True),
        ("0 0 1 * MON-FRI", date(2026, 11, 8), False),
    ],
)
def test_matches_applies_the_vixie_day_rules(text: str, day: date, expected: bool) -> None:
    assert matches(parse_cron(text), datetime.combine(day, time(0, 0))) is expected


def test_matches_checks_the_minute_and_the_hour() -> None:
    spec = parse_cron("30 2 * * *")

    assert matches(spec, datetime(2026, 11, 9, 2, 30, 59))
    assert not matches(spec, datetime(2026, 11, 9, 2, 31))
    assert not matches(spec, datetime(2026, 11, 9, 3, 30))
