"""Helpers for trigger tests: a store-backed submitter, a crashing store, and builders."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from tests.support.builders import make_schedule

from treadle.clock import Clock, to_ms
from treadle.cron import parse_cron
from treadle.models import (
    CatchUp,
    HookRequest,
    RunFilter,
    RunRecord,
    RunRequest,
    RunStatus,
    ScheduleTrigger,
    Submission,
    SubmitOutcome,
    Workflow,
)
from treadle.store.async_store import AsyncStore
from treadle.store.repo import NewRun, Repository

T = TypeVar("T")

_OUTCOMES = {
    "created": SubmitOutcome.QUEUED,
    "duplicate": SubmitOutcome.DUPLICATE,
    "slot_taken": SubmitOutcome.SLOT_TAKEN,
}


def schedule(
    cron: str,
    *,
    workflow_id: str = "wf",
    timezone: str = "UTC",
    catch_up: CatchUp = CatchUp.LATEST,
) -> ScheduleTrigger:
    """Build a schedule trigger from cron text."""
    return make_schedule(
        parse_cron(cron), workflow_id=workflow_id, timezone=timezone, catch_up=catch_up
    )


class StoreSubmitter:
    """A RunSubmitter that creates queued runs in the store, as the run manager does.

    It applies no concurrency policy, but the slot compare-and-set and the delivery
    deduplication happen inside `create_run`, so schedule and webhook guarantees are real.
    """

    def __init__(self, store: AsyncStore, *workflows: Workflow, clock: Clock) -> None:
        self.store = store
        self.workflows = {workflow.id: workflow for workflow in workflows}
        self.clock = clock
        self.requests: list[RunRequest] = []

    async def submit(self, request: RunRequest) -> Submission:
        """Record the request and create its run."""
        self.requests.append(request)
        new = NewRun(
            workflow=self.workflows[request.workflow_id],
            status=RunStatus.QUEUED,
            reason=None,
            trigger_kind=request.kind,
            trigger_key=request.trigger_key,
            trigger=dict(request.trigger),
            inputs=dict(request.inputs),
            attempt=request.attempt,
            rerun_of=request.rerun_of,
            slot=request.slot,
            delivery=request.delivery,
            cancel_queued=(),
            replaced_active=None,
            created_at_ms=to_ms(self.clock.now()),
        )
        result = await self.store.write(lambda repo: repo.create_run(new))
        return Submission(_OUTCOMES[result.outcome], result.run)


class Crash(BaseException):
    """The simulated death of the process. Like SystemExit, `except Exception` misses it."""


class CrashingStore:
    """Wraps a store and raises Crash right after its `crash_after`-th committed write."""

    def __init__(self, inner: AsyncStore, crash_after: int) -> None:
        self.inner = inner
        self.crash_after = crash_after
        self.commits = 0

    async def write(self, fn: Callable[[Repository], T], **kwargs: Any) -> T:
        """Commit the write, then crash when it's the chosen one."""
        result = await self.inner.write(fn, **kwargs)
        self.commits += 1
        if self.commits == self.crash_after:
            raise Crash(f"crashed after commit {self.commits}")
        return result

    async def read(self, fn: Callable[[Repository], T]) -> T:
        """Read through the wrapped store."""
        return await self.inner.read(fn)


async def runs_of(store: AsyncStore, workflow_id: str = "wf") -> list[RunRecord]:
    """Return a workflow's runs, oldest first."""
    page = await store.read(lambda repo: repo.list_runs(RunFilter(workflow_id, limit=200)))
    return sorted(page.runs, key=lambda run: run.id)


async def scheduled_slots(store: AsyncStore, workflow_id: str = "wf") -> list[datetime]:
    """Return the slots of a workflow's schedule runs, oldest run first."""
    return [run.scheduled_for for run in await runs_of(store, workflow_id) if run.scheduled_for]


async def last_slot(store: AsyncStore, key: str) -> int | None:
    """Return the stored `last_slot` of a schedule key."""
    return await store.read(lambda repo: repo.schedule_slot(key))


async def set_last_slot(
    store: AsyncStore, key: str, when: datetime, workflow_id: str = "wf"
) -> None:
    """Store `last_slot` for a key, as an earlier daemon session would have left it."""
    slot_ms = to_ms(when)
    await store.write(lambda repo: repo.init_schedule(key, workflow_id, slot_ms, at_ms=slot_ms))
    current = await last_slot(store, key)
    if current != slot_ms:
        assert current is not None
        await store.write(lambda repo: repo.advance_schedule(key, current, slot_ms, at_ms=slot_ms))


@contextlib.asynccontextmanager
async def running(
    job: Callable[[], Coroutine[Any, Any, None]],
) -> AsyncIterator[asyncio.Task[None]]:
    """Run a service loop in a task for the block, then cancel it and wait for it to stop."""
    task = asyncio.create_task(job())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def sign(secret: str, body: bytes, *, timestamp: str | None = None) -> str:
    """Return a GitHub-style `sha256=<hex>` signature of the body, or of `<timestamp>.<body>`."""
    message = body if timestamp is None else timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def hook_request(
    body: bytes = b"{}",
    *,
    headers: dict[str, str] | None = None,
    content_type: str | None = "application/json",
    query: dict[str, str] | None = None,
) -> HookRequest:
    """Build a webhook request; header names must be lowercase."""
    all_headers = {"content-type": content_type} if content_type else {}
    all_headers.update(headers or {})
    return HookRequest("POST", all_headers, dict(query or {}), body, content_type)


def write(path: Path, text: str = "x") -> Path:
    """Write a file, creating its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
