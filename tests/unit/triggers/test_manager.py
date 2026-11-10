from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.support.builders import DEFAULT_LIMITS, make_watch, make_webhook, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeSecrets
from tests.unit.triggers.helpers import StoreSubmitter, hook_request, running, schedule, sign, write

from treadle.clock import inline_runner
from treadle.models import HookResponse, RegistryChange, TriggerKind, TriggerState, Workflow
from treadle.store.async_store import AsyncStore
from treadle.triggers.manager import TriggerManager, static_trigger_states
from treadle.triggers.scheduler import Scheduler
from treadle.triggers.watch import WatchService
from treadle.triggers.webhook import WebhookRouter

SECRET = "a-sixteen-char-secret"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class Services:
    """A trigger manager over real services, with fakes behind them."""

    def __init__(self, store: AsyncStore, clock: FakeClock, *workflows: Workflow) -> None:
        self.submitter = StoreSubmitter(store, *workflows, clock=clock)
        self.secrets = FakeSecrets(env={"HOOK_SECRET": SECRET})
        self.scheduler = Scheduler(store=store, submitter=self.submitter, clock=clock)
        self.watches = WatchService(
            submitter=self.submitter, limits=DEFAULT_LIMITS, blocking=inline_runner
        )
        self.webhooks = WebhookRouter(
            submitter=self.submitter, secrets=lambda: self.secrets, clock=clock
        )
        self.manager = TriggerManager(self.scheduler, self.watches, self.webhooks)


def triggers_of(root: Path, *, workflow_id: str = "wf", source: str | None = None) -> Workflow:
    return make_workflow(
        workflow_id=workflow_id,
        triggers=[
            schedule("0 * * * *", workflow_id=workflow_id),
            make_watch(root, workflow_id=workflow_id, debounce=1.0, poll_interval=1.0),
            make_webhook(f"{workflow_id}-hook", workflow_id=workflow_id, rate_limit=6),
        ],
        source=source,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    return path


def test_apply_is_synchronous() -> None:
    assert not inspect.iscoroutinefunction(TriggerManager.apply)


async def test_apply_registers_every_trigger_at_once(
    store: AsyncStore, clock: FakeClock, root: Path
) -> None:
    workflow = triggers_of(root)
    services = Services(store, clock, workflow)

    services.manager.apply(RegistryChange("wf", None, workflow))

    assert services.webhooks.precheck("wf-hook") == 1 << 20
    states = services.manager.trigger_states()
    assert [(state.kind, state.summary) for state in states] == [
        (TriggerKind.SCHEDULE, "cron 0 * * * * (UTC)"),
        (TriggerKind.WATCH, f"watch {root}"),
        (TriggerKind.WEBHOOK, "webhook /hooks/wf-hook"),
    ]
    assert states[0].next_fire == datetime(2026, 11, 9, 13, 0, tzinfo=UTC)


async def test_trigger_states_group_by_workflow_and_filter(
    store: AsyncStore, clock: FakeClock, root: Path
) -> None:
    first, second = triggers_of(root, workflow_id="b"), triggers_of(root, workflow_id="a")
    services = Services(store, clock, first, second)
    services.manager.apply(RegistryChange("b", None, first))
    services.manager.apply(RegistryChange("a", None, second))

    states = services.manager.trigger_states()
    assert [(state.workflow_id, state.kind) for state in states] == [
        ("a", TriggerKind.SCHEDULE),
        ("a", TriggerKind.WATCH),
        ("a", TriggerKind.WEBHOOK),
        ("b", TriggerKind.SCHEDULE),
        ("b", TriggerKind.WATCH),
        ("b", TriggerKind.WEBHOOK),
    ]
    assert {state.workflow_id for state in services.manager.trigger_states("b")} == {"b"}


async def test_equal_keys_keep_their_runtime_state(
    store: AsyncStore, clock: FakeClock, root: Path
) -> None:
    old = triggers_of(root)
    new = triggers_of(root, source="# a new version with the same triggers\n")
    services = Services(store, clock, old)
    services.manager.apply(RegistryChange("wf", None, old))
    await services.scheduler.process_due()
    signed = hook_request(b"{}", headers={"x-hub-signature-256": sign(SECRET, b"{}")})
    assert (await services.webhooks.handle("wf-hook", signed)).status == 202

    async with running(services.watches.run):
        await asyncio.sleep(3)
        write(root / "a.txt")
        services.manager.apply(RegistryChange("wf", old, new))
        await asyncio.sleep(5)

    assert [request.kind for request in services.submitter.requests] == [
        TriggerKind.WEBHOOK,
        TriggerKind.WATCH,
    ]
    assert (await services.webhooks.handle("wf-hook", signed)).status == 429


async def test_changed_webhook_settings_apply_at_once(
    store: AsyncStore, clock: FakeClock, root: Path
) -> None:
    old = triggers_of(root)
    gitea = make_webhook("wf-hook", rate_limit=6, signature_header="X-Gitea-Signature")
    new = make_workflow(triggers=[*old.triggers[:2], gitea], source="# gitea\n")
    services = Services(store, clock, old)
    services.manager.apply(RegistryChange("wf", None, old))

    services.manager.apply(RegistryChange("wf", old, new))

    body = b"{}"
    github = hook_request(body, headers={"x-hub-signature-256": sign(SECRET, body)})
    forgejo = hook_request(body, headers={"x-gitea-signature": sign(SECRET, body)})
    assert (await services.webhooks.handle("wf-hook", github)).status == 401
    assert (await services.webhooks.handle("wf-hook", forgejo)).status == 202


async def test_a_removed_workflow_loses_every_trigger(
    store: AsyncStore, clock: FakeClock, root: Path
) -> None:
    workflow = triggers_of(root)
    services = Services(store, clock, workflow)
    services.manager.apply(RegistryChange("wf", None, workflow))

    services.manager.apply(RegistryChange("wf", workflow, None))

    assert services.manager.trigger_states() == []
    response = services.webhooks.precheck("wf-hook")
    assert isinstance(response, HookResponse)
    assert response.status == 404


def test_static_states_need_no_services(root: Path) -> None:
    workflow = make_workflow(
        triggers=[
            schedule("30 2 * * *", timezone="Europe/London"),
            make_watch(root, key="wf:watch:k"),
            make_webhook("push"),
        ]
    )
    assert static_trigger_states(workflow, now=NOW) == [
        TriggerState(
            "wf",
            "wf:schedule:30 2 * * *",
            TriggerKind.SCHEDULE,
            "cron 30 2 * * * (Europe/London)",
            datetime(2026, 11, 10, 2, 30, tzinfo=UTC),
            None,
        ),
        TriggerState("wf", "wf:watch:k", TriggerKind.WATCH, f"watch {root}", None, None),
        TriggerState(
            "wf", "wf:webhook:push", TriggerKind.WEBHOOK, "webhook /hooks/push", None, None
        ),
    ]


def test_static_states_of_a_workflow_without_triggers_are_empty() -> None:
    assert static_trigger_states(make_workflow(), now=NOW) == []
