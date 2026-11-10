from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest
from tests.support.builders import make_run, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeSubmitter
from tests.unit.triggers.helpers import (
    Crash,
    CrashingStore,
    StoreSubmitter,
    last_slot,
    running,
    schedule,
    scheduled_slots,
    set_last_slot,
)

from treadle.clock import to_ms
from treadle.errors import NotFoundError, ShuttingDownError
from treadle.models import (
    CatchUp,
    RunRequest,
    ScheduleSlot,
    ScheduleTrigger,
    Submission,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    Workflow,
)
from treadle.protocols import RunSubmitter
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository
from treadle.triggers.scheduler import Scheduler

T = TypeVar("T")
START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 9, hour, minute, tzinfo=UTC)


def build(
    store: Any,
    clock: FakeClock,
    *triggers: ScheduleTrigger,
    submitter: RunSubmitter | None = None,
    workflow_id: str = "wf",
) -> tuple[Scheduler, Any, Workflow]:
    workflow = make_workflow(workflow_id=workflow_id, triggers=triggers)
    chosen = submitter or StoreSubmitter(store, workflow, clock=clock)
    scheduler = Scheduler(store=store, submitter=chosen, clock=clock)
    scheduler.update(workflow.id, workflow)
    return scheduler, chosen, workflow


class TestFirstSeenKeys:
    async def test_a_new_key_starts_at_now_so_it_never_catches_up(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *", catch_up=CatchUp.ALL)
        scheduler, submitter, _ = build(store, clock, trigger)

        assert await scheduler.process_due() == 0
        assert await last_slot(store, trigger.key) == to_ms(START)

        await asyncio.sleep(5 * 60)
        assert await scheduler.process_due() == 1
        assert await scheduled_slots(store) == [at(12, 5)]
        [request] = submitter.requests
        assert request.kind is TriggerKind.SCHEDULE
        assert request.trigger_key == trigger.key
        assert request.slot == ScheduleSlot(trigger.key, to_ms(at(12, 5)), to_ms(START))
        assert request.trigger["catch_up"] is False
        assert request.trigger["late_by_s"] == 0

    async def test_an_edited_schedule_is_a_new_key_that_ignores_the_old_state(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        old = schedule("0 * * * *", catch_up=CatchUp.ALL)
        await set_last_slot(store, old.key, START - 5 * HOUR)
        edited = schedule("30 * * * *", catch_up=CatchUp.ALL)
        scheduler, _, _ = build(store, clock, edited)

        assert await scheduler.process_due() == 0
        assert await last_slot(store, edited.key) == to_ms(START)

    async def test_a_known_key_resumes_from_its_stored_slot(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *", catch_up=CatchUp.ALL)
        await set_last_slot(store, trigger.key, START - 2 * HOUR)
        scheduler, _, _ = build(store, clock, trigger)

        assert await scheduler.process_due() == 2
        assert await scheduled_slots(store) == [at(11), at(12)]


class TestCatchUp:
    @pytest.mark.parametrize(
        ("catch_up", "slots"),
        [
            (CatchUp.NONE, []),
            (CatchUp.LATEST, [at(12)]),
            (CatchUp.ALL, [at(10), at(11), at(12)]),
        ],
    )
    async def test_downtime_fires_late_slots_by_policy(
        self, store: AsyncStore, catch_up: CatchUp, slots: list[datetime]
    ) -> None:
        clock = FakeClock(at(12, 30))
        trigger = schedule("0 * * * *", catch_up=catch_up)
        await set_last_slot(store, trigger.key, at(9))
        scheduler, submitter, _ = build(store, clock, trigger)

        assert await scheduler.process_due() == len(slots)
        assert await scheduled_slots(store) == slots
        assert all(request.trigger["catch_up"] is True for request in submitter.requests)
        assert await last_slot(store, trigger.key) == to_ms(at(12))
        assert await scheduler.process_due() == 0

    @pytest.mark.parametrize("catch_up", list(CatchUp))
    async def test_a_slot_within_the_grace_period_fires_as_a_normal_run(
        self, store: AsyncStore, catch_up: CatchUp
    ) -> None:
        clock = FakeClock(at(12, 0) + timedelta(seconds=45))
        trigger = schedule("0 * * * *", catch_up=catch_up)
        await set_last_slot(store, trigger.key, at(11, 30))
        scheduler, submitter, _ = build(store, clock, trigger)

        assert await scheduler.process_due() == 1
        [request] = submitter.requests
        assert request.trigger["catch_up"] is False
        assert request.trigger["late_by_s"] == 45

    async def test_catch_up_runs_fire_oldest_first(self, store: AsyncStore) -> None:
        clock = FakeClock(at(12, 30))
        trigger = schedule("*/10 * * * *", catch_up=CatchUp.ALL)
        await set_last_slot(store, trigger.key, at(11, 55))
        scheduler, submitter, _ = build(store, clock, trigger)

        await scheduler.process_due()
        fired = [request.trigger["scheduled_for_utc"] for request in submitter.requests]
        assert fired == [
            "2026-11-09T12:00:00.000Z",
            "2026-11-09T12:10:00.000Z",
            "2026-11-09T12:20:00.000Z",
            "2026-11-09T12:30:00.000Z",
        ]
        previous = [request.slot.previous_ms for request in submitter.requests if request.slot]
        assert previous == [to_ms(at(11, 55)), *(to_ms(at(12, m)) for m in (0, 10, 20))]

    @pytest.mark.parametrize(
        ("catch_up", "missed", "message"),
        [
            (CatchUp.LATEST, 3, "skipped 2 missed slots"),
            (CatchUp.NONE, 3, "skipped 1 missed slot "),
            (CatchUp.LATEST, 100, "skipped 99 missed slots"),
            (CatchUp.LATEST, 180, "skipped at least 100 missed slots"),
            (CatchUp.ALL, 180, "skipped at least 1 missed slot "),
        ],
    )
    async def test_dropped_slots_are_logged(
        self,
        store: AsyncStore,
        caplog: pytest.LogCaptureFixture,
        catch_up: CatchUp,
        missed: int,
        message: str,
    ) -> None:
        clock = FakeClock(at(12) + 30 * MINUTE)
        trigger = schedule("* * * * *", catch_up=catch_up)
        await set_last_slot(store, trigger.key, clock.start - missed * MINUTE)
        scheduler, _, _ = build(store, clock, trigger)

        await scheduler.process_due()
        assert message in caplog.text
        assert "(catch_up: " + catch_up.value + ")" in caplog.text

    async def test_catch_up_all_fires_at_most_the_newest_hundred(self, store: AsyncStore) -> None:
        clock = FakeClock(at(12))
        trigger = schedule("* * * * *", catch_up=CatchUp.ALL)
        await set_last_slot(store, trigger.key, at(9))
        scheduler, _, _ = build(store, clock, trigger)

        assert await scheduler.process_due() == 100
        slots = await scheduled_slots(store)
        assert (slots[0], slots[-1]) == (at(12) - 99 * MINUTE, at(12))

    async def test_nothing_is_logged_when_every_due_slot_fires(
        self, store: AsyncStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock(at(12, 30))
        trigger = schedule("0 * * * *", catch_up=CatchUp.ALL)
        await set_last_slot(store, trigger.key, at(9))
        scheduler, _, _ = build(store, clock, trigger)

        await scheduler.process_due()
        assert "missed" not in caplog.text


class TestClockJumps:
    async def test_a_forward_jump_catches_up_like_downtime(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *")
        scheduler, submitter, _ = build(store, clock, trigger)
        await scheduler.process_due()

        clock.jump(3 * HOUR + 10 * MINUTE)
        assert await scheduler.process_due() == 1
        assert await scheduled_slots(store) == [at(15)]
        [request] = submitter.requests
        assert request.trigger["catch_up"] is True
        assert request.trigger["late_by_s"] == 600

    async def test_a_backward_jump_never_fires_a_slot_twice(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *")
        scheduler, _, _ = build(store, clock, trigger)
        await scheduler.process_due()
        await asyncio.sleep(3600)
        assert await scheduler.process_due() == 1

        clock.jump(-2 * HOUR)
        assert await scheduler.process_due() == 0
        await asyncio.sleep(3600)
        assert await scheduler.process_due() == 0
        await asyncio.sleep(2 * 3600)
        assert await scheduler.process_due() == 1
        assert await scheduled_slots(store) == [at(13), at(14)]

    async def test_after_a_backward_jump_the_next_fire_follows_the_last_slot(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *")
        scheduler, _, _ = build(store, clock, trigger)
        await scheduler.process_due()
        await asyncio.sleep(3600)
        await scheduler.process_due()

        clock.jump(-2 * HOUR)
        [state] = scheduler.states()
        assert state.next_fire == at(14)


class TestSubmitOutcomes:
    async def test_a_taken_slot_reloads_the_stored_slot_and_ends_the_pass(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *", catch_up=CatchUp.ALL)
        scheduler, submitter, _ = build(store, clock, trigger)
        await scheduler.process_due()
        start, taken = to_ms(START), to_ms(at(12, 10))
        await store.write(
            lambda repo: repo.advance_schedule(trigger.key, start, taken, at_ms=taken)
        )

        await asyncio.sleep(15 * 60)
        assert await scheduler.process_due() == 0
        assert await scheduler.process_due() == 1
        assert await scheduled_slots(store) == [at(12, 15)]
        slots = [request.slot.slot_ms for request in submitter.requests if request.slot]
        assert slots == [to_ms(at(12, 5)), to_ms(at(12, 15))]

    async def test_a_reported_taken_slot_also_ends_the_pass(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *", catch_up=CatchUp.ALL)
        submitter = FakeSubmitter(outcome=SubmitOutcome.SLOT_TAKEN)
        scheduler, _, _ = build(store, clock, trigger, submitter=submitter)
        await scheduler.process_due()

        await asyncio.sleep(15 * 60)
        assert await scheduler.process_due() == 0
        assert len(submitter.requests) == 1
        assert await last_slot(store, trigger.key) == to_ms(START)

    @pytest.mark.parametrize(
        "error", [NotFoundError("workflow 'wf' doesn't exist"), RuntimeError("boom")]
    )
    async def test_a_failed_submit_is_logged_and_the_slot_still_advances(
        self,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
    ) -> None:
        trigger = schedule("*/5 * * * *")
        submitter = FakeSubmitter(error=error)
        scheduler, _, _ = build(store, clock, trigger, submitter=submitter)
        await scheduler.process_due()

        await asyncio.sleep(5 * 60)
        assert await scheduler.process_due() == 0
        assert f"the run for 2026-11-09T12:05:00.000Z wasn't created: {error}" in caplog.text
        assert await last_slot(store, trigger.key) == to_ms(at(12, 5))
        assert await scheduler.process_due() == 0
        assert len(submitter.requests) == 1

    async def test_shutting_down_ends_the_pass_without_advancing(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        first, second = (
            schedule("*/5 * * * *", workflow_id="a"),
            schedule("*/5 * * * *", workflow_id="b"),
        )
        submitter = FakeSubmitter(error=ShuttingDownError("Treadle is shutting down"))
        scheduler = Scheduler(store=store, submitter=submitter, clock=clock)
        scheduler.update("a", make_workflow(workflow_id="a", triggers=[first]))
        scheduler.update("b", make_workflow(workflow_id="b", triggers=[second]))
        await scheduler.process_due()

        await asyncio.sleep(5 * 60)
        assert await scheduler.process_due() == 0
        assert [request.workflow_id for request in submitter.requests] == ["a"]
        assert await last_slot(store, first.key) == to_ms(START)

    async def test_a_skipped_run_still_counts_as_submitted(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *")
        submitter = FakeSubmitter(outcome=SubmitOutcome.SKIPPED)
        scheduler, _, _ = build(store, clock, trigger, submitter=submitter)
        await scheduler.process_due()
        await asyncio.sleep(5 * 60)
        assert await scheduler.process_due() == 1

    async def test_a_failing_registration_doesnt_stop_the_others(
        self, store: AsyncStore, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        class FirstWriteFails:
            def __init__(self) -> None:
                self.failed = False

            async def write(self, fn: Callable[[Repository], T], **kwargs: Any) -> T:
                if not self.failed:
                    self.failed = True
                    raise RuntimeError("disk on fire")
                return await store.write(fn, **kwargs)

            async def read(self, fn: Callable[[Repository], T]) -> T:
                return await store.read(fn)

        flaky: Any = FirstWriteFails()
        first, second = (
            schedule("*/5 * * * *", workflow_id="a"),
            schedule("*/5 * * * *", workflow_id="b"),
        )
        workflows = [
            make_workflow(workflow_id=w, triggers=[t]) for w, t in (("a", first), ("b", second))
        ]
        scheduler = Scheduler(
            store=flaky, submitter=StoreSubmitter(store, *workflows, clock=clock), clock=clock
        )
        for workflow in workflows:
            scheduler.update(workflow.id, workflow)

        await scheduler.process_due()
        assert f"schedule {first.key} failed" in caplog.text
        assert await last_slot(store, first.key) is None
        assert await last_slot(store, second.key) == to_ms(START)

    async def test_a_lost_compare_and_set_while_advancing_reloads_the_slot(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *", catch_up=CatchUp.NONE)
        scheduler, _, _ = build(store, clock, trigger)
        await scheduler.process_due()
        moved, start = to_ms(at(12, 20)), to_ms(START)
        await store.write(
            lambda repo: repo.advance_schedule(trigger.key, start, moved, at_ms=moved)
        )

        await asyncio.sleep(12 * 60)
        assert await scheduler.process_due() == 0
        assert await last_slot(store, trigger.key) == moved
        await asyncio.sleep(13 * 60)
        assert await scheduler.process_due() == 1
        assert await scheduled_slots(store) == [at(12, 25)]

    async def test_a_reload_that_removes_a_schedule_mid_pass_skips_it(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        first, second = (
            schedule("*/5 * * * *", workflow_id="a"),
            schedule("*/5 * * * *", workflow_id="b"),
        )
        scheduler: Scheduler

        def remove_b(request: RunRequest) -> Submission:
            scheduler.update("b", None)
            return Submission(SubmitOutcome.QUEUED, make_run(workflow_id=request.workflow_id))

        submitter = FakeSubmitter(handler=remove_b)
        scheduler = Scheduler(store=store, submitter=submitter, clock=clock)
        scheduler.update("a", make_workflow(workflow_id="a", triggers=[first]))
        scheduler.update("b", make_workflow(workflow_id="b", triggers=[second]))
        await set_last_slot(store, first.key, START - 5 * MINUTE, workflow_id="a")

        assert await scheduler.process_due() == 1
        assert [request.workflow_id for request in submitter.requests] == ["a"]
        assert await last_slot(store, second.key) is None


class TestRegistrations:
    async def test_states_summarize_each_schedule_with_its_next_fire(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        london = schedule("30 2 * * *", workflow_id="b", timezone="Europe/London")
        hourly = schedule("0 * * * *", workflow_id="a")
        scheduler = Scheduler(store=store, submitter=FakeSubmitter(), clock=clock)
        scheduler.update("b", make_workflow(workflow_id="b", triggers=[london]))
        scheduler.update("a", make_workflow(workflow_id="a", triggers=[hourly]))

        assert scheduler.states() == [
            TriggerState(
                "a", hourly.key, TriggerKind.SCHEDULE, "cron 0 * * * * (UTC)", at(13), None
            ),
            TriggerState(
                "b",
                london.key,
                TriggerKind.SCHEDULE,
                "cron 30 2 * * * (Europe/London)",
                datetime(2026, 11, 10, 2, 30, tzinfo=UTC),
                None,
            ),
        ]
        assert [state.workflow_id for state in scheduler.states("b")] == ["b"]

    async def test_states_follow_the_trigger_order_within_a_workflow(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        late, early = schedule("0 3 * * *"), schedule("0 1 * * *")
        scheduler, _, _ = build(store, clock, late, early, submitter=FakeSubmitter())
        assert [state.key for state in scheduler.states()] == [late.key, early.key]

    async def test_removing_a_workflow_drops_its_schedules(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *")
        scheduler, submitter, _ = build(store, clock, trigger, submitter=FakeSubmitter())
        await scheduler.process_due()

        scheduler.update("wf", None)
        await asyncio.sleep(10 * 60)
        assert await scheduler.process_due() == 0
        assert scheduler.states() == []
        assert submitter.requests == []

    async def test_a_new_version_with_an_equal_key_keeps_the_cached_slot(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *")
        scheduler, _, _ = build(store, clock, trigger)
        await scheduler.process_due()
        await asyncio.sleep(3600)
        await scheduler.process_due()
        clock.jump(-2 * HOUR)

        edited = make_workflow(workflow_id="wf", triggers=[trigger], source="# edited\n")
        scheduler.update("wf", edited)
        [state] = scheduler.states()
        assert state.next_fire == at(14)

    async def test_prune_state_deletes_rows_without_a_current_trigger(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        current = schedule("0 * * * *")
        scheduler, _, _ = build(store, clock, current, submitter=FakeSubmitter())
        await scheduler.process_due()
        rows = {"gone:schedule:a": "gone", "wf:schedule:old": "wf", "bad:schedule:b": "bad"}
        for key, workflow_id in rows.items():
            await set_last_slot(store, key, START, workflow_id=workflow_id)

        deleted = await scheduler.prune_state(valid_workflows={"wf"}, known_workflows={"wf", "bad"})
        assert deleted == 2
        assert await last_slot(store, "gone:schedule:a") is None
        assert await last_slot(store, "wf:schedule:old") is None
        assert await last_slot(store, "bad:schedule:b") == to_ms(START)
        assert await last_slot(store, current.key) == to_ms(START)


class TestLoop:
    async def test_the_loop_fires_each_slot_on_time(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("*/5 * * * *")
        scheduler, submitter, _ = build(store, clock, trigger)

        async with running(scheduler.run):
            await asyncio.sleep(11 * 60)

        assert await scheduled_slots(store) == [at(12, 5), at(12, 10)]
        fired = [request.trigger["fired_at"] for request in submitter.requests]
        assert fired == ["2026-11-09T12:05:00.000Z", "2026-11-09T12:10:00.000Z"]

    async def test_an_update_wakes_the_loop(self, store: AsyncStore, clock: FakeClock) -> None:
        scheduler = Scheduler(store=store, submitter=FakeSubmitter(), clock=clock)
        later = schedule("0 * * * *", workflow_id="later")

        async with running(scheduler.run):
            await asyncio.sleep(10)
            scheduler.update("later", make_workflow(workflow_id="later", triggers=[later]))
            await asyncio.sleep(0.001)
            assert await last_slot(store, later.key) == to_ms(START + timedelta(seconds=10))

    async def test_the_loop_naps_at_most_thirty_seconds(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = schedule("0 * * * *")
        scheduler, submitter, _ = build(store, clock, trigger)

        async with running(scheduler.run):
            await asyncio.sleep(5)
            clock.jump(2 * HOUR)
            await asyncio.sleep(MINUTE.total_seconds() / 2)
            assert len(submitter.requests) == 1

        assert submitter.requests[0].trigger["scheduled_for_utc"] == "2026-11-09T14:00:00.000Z"


class TestCrashInjection:
    @pytest.mark.parametrize("crash_after", range(1, 12))
    @pytest.mark.parametrize("catch_up", [CatchUp.ALL, CatchUp.LATEST])
    async def test_no_slot_fires_twice_across_a_crash_and_restart(
        self, store: AsyncStore, clock: FakeClock, crash_after: int, catch_up: CatchUp
    ) -> None:
        trigger = schedule("* * * * *", catch_up=catch_up)
        workflow = make_workflow(triggers=[trigger])
        await set_last_slot(store, trigger.key, START - 10 * MINUTE)

        crashing: Any = CrashingStore(store, crash_after)
        first = Scheduler(
            store=crashing,
            submitter=StoreSubmitter(crashing, workflow, clock=clock),
            clock=clock,
        )
        first.update("wf", workflow)
        with contextlib.suppress(Crash):
            await first.process_due()

        restarted = Scheduler(
            store=store, submitter=StoreSubmitter(store, workflow, clock=clock), clock=clock
        )
        restarted.update("wf", workflow)
        await restarted.process_due()
        await restarted.process_due()

        slots = await scheduled_slots(store)
        assert len(slots) == len(set(slots))
        if catch_up is CatchUp.ALL:
            assert slots == [START - n * MINUTE for n in range(9, -1, -1)]
        else:
            assert slots == [START]
        assert await last_slot(store, trigger.key) == to_ms(START)
