"""Schedule triggers: slot planning, catch-up, and the scheduler loop.

Each (trigger key, slot) creates at most one run, and a schedule that Treadle sees for the
first time starts at the current time, so new and edited schedules never catch up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from treadle.clock import Clock, from_ms, iso_utc, to_ms
from treadle.cron import latest_fires, next_fire
from treadle.errors import ShuttingDownError, TreadleError
from treadle.models import (
    CatchUp,
    RunRequest,
    ScheduleSlot,
    ScheduleTrigger,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    Workflow,
)
from treadle.protocols import RunSubmitter
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository
from treadle.types import JSONObject

MISFIRE_GRACE: Final = 60.0
MAX_NAP: Final = 30.0
CATCH_UP_CAP: Final = 100

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """Which due slots fire, oldest first, as (slot, is_catch_up) pairs.

    `advance_to` is the newest due slot, and `dropped` counts due slots that won't fire.
    """

    fire: tuple[tuple[datetime, bool], ...]
    advance_to: datetime | None
    dropped: int


def plan_slots(
    due: Sequence[datetime], now: datetime, *, catch_up: CatchUp, grace: float = MISFIRE_GRACE
) -> SlotPlan:
    """Plan due slots: `none` fires slots that aren't late, `latest` the newest, `all` every one.

    A slot is late when it is more than `grace` seconds before `now`. `due` is oldest first,
    and a late slot that fires is a catch-up.
    """
    if not due:
        return SlotPlan((), None, 0)
    limit = timedelta(seconds=grace)
    if catch_up is CatchUp.LATEST:
        chosen = [due[-1]]
    elif catch_up is CatchUp.ALL:
        chosen = list(due)
    else:
        chosen = [slot for slot in due if now - slot <= limit]
    fire = tuple((slot, now - slot > limit) for slot in chosen)
    return SlotPlan(fire, due[-1], len(due) - len(fire))


def schedule_payload(
    trigger: ScheduleTrigger, slot: datetime, *, now: datetime, catch_up: bool
) -> JSONObject:
    """Return the `trigger` payload of a schedule run.

    `scheduled_for` is the slot in the trigger's zone, and `late_by_s` counts whole seconds
    between the slot and `now`, the time the run fired.
    """
    local = slot.astimezone(ZoneInfo(trigger.timezone))
    return {
        "kind": TriggerKind.SCHEDULE.value,
        "cron": trigger.cron.source,
        "timezone": trigger.timezone,
        "scheduled_for": local.isoformat(timespec="seconds"),
        "scheduled_for_utc": iso_utc(slot),
        "fired_at": iso_utc(now),
        "late_by_s": max(0, int((now - slot).total_seconds())),
        "catch_up": catch_up,
    }


@dataclass(slots=True)
class _Schedule:
    """One registered schedule. `last_slot` (epoch ms) is None until read from the store.

    `returning` marks a key that `update` removed earlier in this session: its stored slot
    dates from before it left, so it starts again at now, like a key seen for the first time.
    """

    workflow_id: str
    trigger: ScheduleTrigger
    zone: ZoneInfo
    index: int
    last_slot: int | None = None
    returning: bool = False


class Scheduler:
    """Fires schedule slots at most once each, through the run submitter.

    Each slot reaches the submitter with a compare-and-set from the stored `last_slot`, which
    the run insert performs in the same transaction, so a slot never fires twice.
    """

    def __init__(self, *, store: AsyncStore, submitter: RunSubmitter, clock: Clock) -> None:
        self._store = store
        self._submitter = submitter
        self._clock = clock
        self._schedules: dict[str, _Schedule] = {}
        self._removed: set[str] = set()  # keys that `update` removed in this session
        self._wake = asyncio.Event()

    def update(self, workflow_id: str, workflow: Workflow | None) -> None:
        """Swap a workflow's schedules synchronously; equal keys keep their state.

        A key that comes back after an earlier `update` removed it starts again at now, as
        the startup pruning of its stored state would have made it do after a restart.
        """
        triggers = {} if workflow is None else _schedules_of(workflow)
        for key in [key for key, s in self._schedules.items() if s.workflow_id == workflow_id]:
            if key not in triggers:
                del self._schedules[key]
                self._removed.add(key)
        for key, (index, trigger) in triggers.items():
            existing = self._schedules.get(key)
            if existing is None:
                returning = key in self._removed
                self._removed.discard(key)
                self._schedules[key] = _Schedule(
                    workflow_id, trigger, ZoneInfo(trigger.timezone), index, returning=returning
                )
            else:
                existing.index = index
        self._wake.set()

    def states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return the schedule trigger states, optionally for one workflow.

        `next_fire` is the next slot after now, or after `last_slot` when the wall clock
        moved back before it.
        """
        now = self._clock.now()
        chosen = [s for s in self._schedules.values() if workflow_id in (None, s.workflow_id)]
        chosen.sort(key=lambda s: (s.workflow_id, s.index))
        return [
            _schedule_state(s.workflow_id, s.trigger, s.zone, after=self._after(s, now))
            for s in chosen
        ]

    async def prune_state(
        self, *, valid_workflows: Collection[str], known_workflows: Collection[str]
    ) -> int:
        """Delete stored schedule state that no current trigger uses; return the row count.

        Rows of workflows that have no file go, and so do rows of valid workflows whose
        current schedules don't include the key.
        """
        keep = frozenset(self._schedules)
        return await self._store.write(
            lambda repo: repo.prune_schedules(
                keep=keep, valid_workflows=valid_workflows, known_workflows=known_workflows
            )
        )

    async def process_due(self) -> int:
        """Submit every due slot once and return how many runs were submitted.

        Registrations run in key order, and a failure in one is logged without stopping the
        others. A draining submitter ends the pass, leaving the slot for the next start.
        """
        submitted = 0
        for key in sorted(self._schedules):
            schedule = self._schedules.get(key)
            if schedule is None:
                continue  # removed by a reload while this pass awaited
            try:
                submitted += await self._process(schedule)
            except ShuttingDownError:
                _log.info("not firing schedules while Treadle shuts down")
                break
            except Exception:
                _log.exception("schedule %s failed; trying again on the next pass", key)
        return submitted

    async def run(self) -> None:
        """Process due slots, sleeping at most `MAX_NAP` between passes, until cancelled.

        The loop wakes early when `update` changes the schedules. Short naps bound the
        delay after laptop sleep or a clock jump, because asyncio timers use a monotonic
        clock while slots follow the wall clock.
        """
        while True:
            self._wake.clear()
            await self.process_due()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._nap()):
                    await self._wake.wait()

    async def _process(self, schedule: _Schedule) -> int:
        """Fire one registration's due slots and advance its `last_slot`."""
        trigger = schedule.trigger
        now = self._clock.now()
        last = schedule.last_slot
        if last is None:
            last = await self._load_slot(schedule, now)
        due = latest_fires(trigger.cron, schedule.zone, from_ms(last), now, limit=CATCH_UP_CAP)
        plan = plan_slots(due, now, catch_up=trigger.catch_up)
        if plan.advance_to is None:
            return 0
        fired = 0
        previous = last
        for slot, catch_up in plan.fire:
            outcome = await self._submit(schedule, slot, catch_up=catch_up, previous=previous)
            if outcome is SubmitOutcome.SLOT_TAKEN:
                await self._reload(schedule)
                return fired
            if outcome is not None:
                fired += 1
                previous = schedule.last_slot = to_ms(slot)
        await self._advance(schedule, previous, to_ms(plan.advance_to))
        self._warn_dropped(schedule, due, plan, last)
        return fired

    async def _submit(
        self, schedule: _Schedule, slot: datetime, *, catch_up: bool, previous: int
    ) -> SubmitOutcome | None:
        """Submit one slot. A failure other than shutting down is logged and returns None."""
        trigger = schedule.trigger
        payload = schedule_payload(trigger, slot, now=self._clock.now(), catch_up=catch_up)
        request = RunRequest(
            workflow_id=schedule.workflow_id,
            kind=TriggerKind.SCHEDULE,
            trigger=payload,
            trigger_key=trigger.key,
            slot=ScheduleSlot(trigger.key, to_ms(slot), previous),
        )
        try:
            submission = await self._submitter.submit(request)
        except ShuttingDownError:
            raise
        except Exception as exc:
            _log.warning(
                "schedule %s: the run for %s wasn't created: %s",
                trigger.key,
                iso_utc(slot),
                exc,
                exc_info=not isinstance(exc, TreadleError),
            )
            return None
        return submission.outcome

    async def _load_slot(self, schedule: _Schedule, now: datetime) -> int:
        """Read the stored `last_slot`, creating it at now for a key seen for the first time.

        Starting at now means that new and edited schedules never catch up. A returning key
        moves its stored slot up to now in the same transaction, but never back, so a
        backward clock jump can't fire a slot twice.
        """
        key, workflow_id, now_ms = schedule.trigger.key, schedule.workflow_id, to_ms(now)
        returning = schedule.returning

        def start(repo: Repository) -> int:
            stored = repo.init_schedule(key, workflow_id, now_ms, at_ms=now_ms)
            if not returning or stored >= now_ms:
                return stored
            # The compare-and-set can't miss: it runs in the transaction that read `stored`.
            repo.advance_schedule(key, stored, now_ms, at_ms=now_ms)
            return now_ms

        schedule.last_slot = await self._store.write(start)
        schedule.returning = False
        return schedule.last_slot

    async def _advance(self, schedule: _Schedule, previous: int, target: int) -> None:
        """Move `last_slot` from `previous` to the newest due slot, past slots that didn't fire."""
        if previous >= target:
            return
        key, at_ms = schedule.trigger.key, to_ms(self._clock.now())
        moved = await self._store.write(
            lambda repo: repo.advance_schedule(key, previous, target, at_ms=at_ms)
        )
        if moved:
            schedule.last_slot = target
        else:
            await self._reload(schedule)

    async def _reload(self, schedule: _Schedule) -> None:
        """Read `last_slot` again after another writer moved it."""
        key = schedule.trigger.key
        schedule.last_slot = await self._store.read(lambda repo: repo.schedule_slot(key))

    def _warn_dropped(
        self, schedule: _Schedule, due: Sequence[datetime], plan: SlotPlan, last: int
    ) -> None:
        """Log the missed slots that won't fire, including any older than the catch-up cap."""
        trigger = schedule.trigger
        older = len(due) == CATCH_UP_CAP and (
            next_fire(trigger.cron, schedule.zone, from_ms(last)) < due[0]
        )
        if not plan.dropped and not older:
            return
        # When `due` is full, slots older than the cap exist exactly when the cron fired
        # between `last_slot` and the oldest due slot; they count as "at least".
        count = plan.dropped + (1 if older else 0)
        text = f"at least {count}" if older else str(count)
        noun = "slot" if count == 1 else "slots"
        _log.warning(
            "schedule %s: skipped %s missed %s (catch_up: %s)",
            trigger.key,
            text,
            noun,
            trigger.catch_up.value,
        )

    def _nap(self) -> float:
        """Return the seconds until the earliest next fire, at most `MAX_NAP`."""
        now = self._clock.now()
        nap = MAX_NAP
        for schedule in self._schedules.values():
            fire = next_fire(schedule.trigger.cron, schedule.zone, self._after(schedule, now))
            nap = min(nap, (fire - now).total_seconds())
        return max(nap, 0.0)

    def _after(self, schedule: _Schedule, now: datetime) -> datetime:
        """Return the instant after which the next slot can fire: now, or a later `last_slot`."""
        if schedule.last_slot is None:
            return now
        return max(now, from_ms(schedule.last_slot))


def _schedules_of(workflow: Workflow) -> dict[str, tuple[int, ScheduleTrigger]]:
    """Return a workflow's schedules by key, with their positions among its triggers."""
    return {
        trigger.key: (index, trigger)
        for index, trigger in enumerate(workflow.triggers)
        if isinstance(trigger, ScheduleTrigger)
    }


def _schedule_state(
    workflow_id: str, trigger: ScheduleTrigger, zone: ZoneInfo, *, after: datetime
) -> TriggerState:
    """Return a schedule's state: its summary and the next fire after `after`."""
    summary = f"cron {trigger.cron.source} ({trigger.timezone})"
    fire = next_fire(trigger.cron, zone, after)
    return TriggerState(workflow_id, trigger.key, TriggerKind.SCHEDULE, summary, fire, None)
