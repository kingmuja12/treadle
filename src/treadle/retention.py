"""Retention: pruning old runs in small batches (section 7, "Retention")."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Collection
from typing import Final

from treadle.clock import Clock, to_ms
from treadle.config import RetentionSettings
from treadle.store.async_store import AsyncStore
from treadle.store.repo import PruneStats
from treadle.units import format_duration

_BATCH: Final = 100
_log = logging.getLogger(__name__)


async def run_retention(
    store: AsyncStore,
    policy: RetentionSettings,
    *,
    now_ms: int,
    keep_digests: Collection[str],
    max_batches: int | None = None,
) -> PruneStats:
    """Run one retention pass and return what it deleted.

    It prunes batches of at most 100 runs, each in its own transaction, until a batch comes
    up short or `max_batches` batches ran, and yields to the event loop between batches so
    the writer is never held for long. Then it prunes the other tables, keeping the workflow
    versions in `keep_digests`, and returns free pages with an incremental vacuum outside
    any transaction. Queued and running runs are never touched.
    """
    runs = batches = 0
    while max_batches is None or batches < max_batches:
        deleted = await store.write(
            lambda repo: repo.prune_batch(policy, now_ms=now_ms, batch=_BATCH)
        )
        runs += deleted
        batches += 1
        if deleted < _BATCH:
            break
        await asyncio.sleep(0)
    misc = await store.write(lambda repo: repo.prune_misc(now_ms=now_ms, keep_digests=keep_digests))
    await store.write(lambda repo: repo.incremental_vacuum(), transaction=False)
    return dataclasses.replace(misc, runs=runs)


async def retention_loop(
    store: AsyncStore,
    policy: RetentionSettings,
    clock: Clock,
    keep_digests: Callable[[], Collection[str]],
) -> None:
    """Run a retention pass now and then every `policy.interval` seconds, until cancelled.

    Each pass reads the time from `clock` and the current workflow versions from
    `keep_digests`. A failed pass is logged, and the next one runs on schedule.
    """
    while True:
        try:
            stats = await run_retention(
                store, policy, now_ms=to_ms(clock.now()), keep_digests=keep_digests()
            )
        except Exception:
            # Design note: like each trigger task (section 8), the loop logs a failure and
            # keeps its schedule, so one bad pass never stops retention for good.
            _log.exception("retention failed; trying again in %s", format_duration(policy.interval))
        else:
            if stats != PruneStats():
                _log.info(
                    "retention pruned runs=%d versions=%d deliveries=%d controls=%d",
                    stats.runs,
                    stats.versions,
                    stats.deliveries,
                    stats.controls,
                )
        await asyncio.sleep(policy.interval)
