"""The trigger manager: applies registry changes to every trigger service at once."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from treadle.models import RegistryChange, ScheduleTrigger, TriggerState, WatchTrigger, Workflow
from treadle.triggers.scheduler import Scheduler, _schedule_state
from treadle.triggers.watch import WatchService, _watch_state
from treadle.triggers.webhook import WebhookRouter, _webhook_state


def static_trigger_states(workflow: Workflow, *, now: datetime) -> list[TriggerState]:
    """Return trigger summaries and next schedule fires without runtime health.

    `treadle list` uses it without a daemon. The states follow the workflow's trigger order,
    and a schedule's next fire is the first one after `now`.
    """
    states = []
    for trigger in workflow.triggers:
        if isinstance(trigger, ScheduleTrigger):
            zone = ZoneInfo(trigger.timezone)
            states.append(_schedule_state(workflow.id, trigger, zone, after=now))
        elif isinstance(trigger, WatchTrigger):
            states.append(_watch_state(workflow.id, trigger, None))
        else:
            states.append(_webhook_state(workflow.id, trigger, None))
    return states


class TriggerManager:
    """Fans registry changes out to the scheduler, watches, and webhooks.

    Implements TriggerSink and TriggerInfo.
    """

    def __init__(
        self, scheduler: Scheduler, watches: WatchService, webhooks: WebhookRouter
    ) -> None:
        self._scheduler = scheduler
        self._watches = watches
        self._webhooks = webhooks

    def apply(self, change: RegistryChange) -> None:
        """Apply a change synchronously, with no await, so each swap is atomic.

        Triggers only in the old version stop, triggers only in the new one start, and
        equal keys keep their runtime state.
        """
        self._scheduler.update(change.workflow_id, change.new)
        self._watches.update(change.workflow_id, change.new)
        self._webhooks.update(change.workflow_id, change.new)

    def trigger_states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return every trigger state, optionally for one workflow.

        States are grouped by workflow ID, with schedules, then watches, then webhooks, each
        in declaration order, which is the order of a workflow's triggers.
        """
        states = [
            *self._scheduler.states(workflow_id),
            *self._watches.states(workflow_id),
            *self._webhooks.states(workflow_id),
        ]
        return sorted(states, key=lambda state: state.workflow_id)
