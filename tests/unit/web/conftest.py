"""Fixtures for the web tests: the application over test doubles, and an HTTP client for it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

import httpx
import pytest
from tests.support.clock import FakeClock
from tests.support.fakes import (
    FakeRunControl,
    FakeTriggerInfo,
    FakeWebhookHandler,
    FakeWorkflowSource,
    MemoryRunReader,
)
from tests.unit.web.helpers import BASE_URL, SECRET_KEY, TOKEN, MakeWeb, Web

from treadle.config import Settings
from treadle.models import TriggerState, Workflow, WorkflowEntry
from treadle.protocols import RunReader
from treadle.web.app import WebContext, create_app


@pytest.fixture
async def make_web(settings: Settings, clock: FakeClock) -> AsyncIterator[MakeWeb]:
    """Return `build(**options) -> Web`; every client closes after the test.

    Options: `token`, `settings`, `reader` (a MemoryRunReader by default), `workflows`
    (workflows or entries), `runs` (a FakeRunControl), `hooks`, `triggers` (states),
    `base_url`, and `raise_app_exceptions`.
    """
    clients: list[httpx.AsyncClient] = []

    def build(
        *,
        token: str | None = None,
        settings: Settings = settings,
        reader: RunReader | None = None,
        workflows: Iterable[Workflow | WorkflowEntry] = (),
        runs: FakeRunControl | None = None,
        hooks: FakeWebhookHandler | None = None,
        triggers: Iterable[TriggerState] = (),
        base_url: str = BASE_URL,
        raise_app_exceptions: bool = True,
    ) -> Web:
        source = FakeWorkflowSource(*workflows)
        control = runs or FakeRunControl()
        handler = hooks or FakeWebhookHandler()
        info = FakeTriggerInfo(*triggers)
        run_reader = reader or MemoryRunReader()
        holder: list[Web] = []
        ctx = WebContext(
            settings=settings,
            workflows=source,
            runs=control,
            reader=run_reader,
            hooks=handler,
            triggers=info,
            token=token,
            secret_key=SECRET_KEY,
            clock=clock,
            version="0.1.0",
            schema_version=1,
            started_at=clock.now(),
            draining=lambda: holder[0].draining,
        )
        transport = httpx.ASGITransport(create_app(ctx), raise_app_exceptions=raise_app_exceptions)
        client = httpx.AsyncClient(transport=transport, base_url=base_url)
        clients.append(client)
        web = Web(ctx, client, source, control, run_reader, handler, info, clock)
        holder.append(web)
        return web

    yield build
    for client in clients:
        await client.aclose()


@pytest.fixture
def web(make_web: MakeWeb) -> Web:
    """An application on a loopback bind without a token."""
    return make_web()


@pytest.fixture
def secured(make_web: MakeWeb) -> Web:
    """An application with authentication on."""
    return make_web(token=TOKEN)
