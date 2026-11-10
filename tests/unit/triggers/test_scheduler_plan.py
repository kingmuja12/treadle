from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.triggers.helpers import schedule

from treadle.models import CatchUp
from treadle.triggers.scheduler import (
    CATCH_UP_CAP,
    MAX_NAP,
    MISFIRE_GRACE,
    plan_slots,
    schedule_payload,
)

NOW = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)
TEN, ELEVEN, NOON = (datetime(2026, 11, 9, hour, tzinfo=UTC) for hour in (10, 11, 12))


def test_the_constants_match_the_design() -> None:
    assert (MISFIRE_GRACE, MAX_NAP, CATCH_UP_CAP) == (60.0, 30.0, 100)


@pytest.mark.parametrize("catch_up", list(CatchUp))
def test_nothing_due_plans_nothing(catch_up: CatchUp) -> None:
    plan = plan_slots([], NOW, catch_up=catch_up)
    assert (plan.fire, plan.advance_to, plan.dropped) == ((), None, 0)


@pytest.mark.parametrize(
    ("catch_up", "fire", "dropped"),
    [
        (CatchUp.NONE, (), 3),
        (CatchUp.LATEST, ((NOON, True),), 2),
        (CatchUp.ALL, ((TEN, True), (ELEVEN, True), (NOON, True)), 0),
    ],
)
def test_late_slots_after_downtime(
    catch_up: CatchUp, fire: tuple[tuple[datetime, bool], ...], dropped: int
) -> None:
    plan = plan_slots([TEN, ELEVEN, NOON], NOW, catch_up=catch_up)
    assert plan.fire == fire
    assert plan.advance_to == NOON
    assert plan.dropped == dropped


@pytest.mark.parametrize(
    ("catch_up", "fire"),
    [
        (CatchUp.NONE, ((NOON, False),)),
        (CatchUp.LATEST, ((NOON, False),)),
        (CatchUp.ALL, ((TEN, True), (ELEVEN, True), (NOON, False))),
    ],
)
def test_a_slot_that_isnt_late_fires_as_a_normal_run(
    catch_up: CatchUp, fire: tuple[tuple[datetime, bool], ...]
) -> None:
    now = NOON + timedelta(seconds=30)
    assert plan_slots([TEN, ELEVEN, NOON], now, catch_up=catch_up).fire == fire


def test_a_slot_is_late_only_after_the_grace_period() -> None:
    on_time = plan_slots([NOON], NOON + timedelta(seconds=60), catch_up=CatchUp.NONE)
    late = plan_slots([NOON], NOON + timedelta(seconds=60.001), catch_up=CatchUp.NONE)
    assert on_time.fire == ((NOON, False),)
    assert late.fire == ()
    assert late.dropped == 1


def test_the_grace_period_can_change() -> None:
    plan = plan_slots([NOON], NOON + timedelta(seconds=5), catch_up=CatchUp.LATEST, grace=1.0)
    assert plan.fire == ((NOON, True),)


def test_the_payload_describes_the_slot_in_the_trigger_zone() -> None:
    trigger = schedule("30 2 * * *", timezone="Europe/London")
    slot = datetime(2026, 3, 29, 2, 0, tzinfo=UTC)
    payload = schedule_payload(trigger, slot, now=slot + timedelta(seconds=0.512), catch_up=False)
    assert payload == {
        "kind": "schedule",
        "cron": "30 2 * * *",
        "timezone": "Europe/London",
        "scheduled_for": "2026-03-29T03:00:00+01:00",
        "scheduled_for_utc": "2026-03-29T02:00:00.000Z",
        "fired_at": "2026-03-29T02:00:00.512Z",
        "late_by_s": 0,
        "catch_up": False,
    }


def test_a_late_payload_counts_whole_seconds() -> None:
    trigger = schedule("@daily")
    payload = schedule_payload(
        trigger, NOON, now=NOON + timedelta(minutes=2, seconds=5.9), catch_up=True
    )
    assert payload["late_by_s"] == 125
    assert payload["catch_up"] is True
    assert payload["cron"] == "@daily"
    assert payload["scheduled_for"] == "2026-11-09T12:00:00+00:00"


def test_a_payload_fired_before_its_slot_isnt_late() -> None:
    payload = schedule_payload(
        schedule("@hourly"), NOON, now=NOON - timedelta(seconds=3), catch_up=False
    )
    assert payload["late_by_s"] == 0
