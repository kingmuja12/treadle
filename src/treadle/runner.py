"""The foreground runner behind `treadle run` when no daemon holds the state directory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

import httpx

from treadle import __version__
from treadle.clock import (
    BlockingRunner,
    Clock,
    RandomSource,
    SystemClock,
    default_random,
    inline_runner,
    iso_utc,
    thread_runner,
    to_ms,
)
from treadle.config import Settings
from treadle.control import ControlPoller
from treadle.engine.executor import Executor, ExecutorDeps
from treadle.engine.manager import RunManager
from treadle.engine.masking import Masker
from treadle.lock import InstanceLock
from treadle.models import (
    CancelOutcome,
    CancelReason,
    Origin,
    RunRequest,
    RunStatus,
    Submission,
    TriggerKind,
)
from treadle.registry import WorkflowRegistry
from treadle.retention import run_retention
from treadle.secrets import SecretSnapshot, SecretStore
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.steps.procs import ProcessRegistry
from treadle.store.async_store import AsyncStore
from treadle.store.repo import RecoveryReport
from treadle.types import LockHolder

# A foreground run prunes at most this many batches of 100 runs after it finishes.
_RETENTION_BATCHES: Final = 5
_MAX_REDIRECTS: Final = 5

_log = logging.getLogger(__name__)


class LocalRunner:
    """Executes runs in this process while it holds the state-directory lock as `run`.

    It recovers what a crashed lock holder left behind, loads the workflows without starting
    any trigger, and starts each submitted run at once: no other process can execute runs
    while the lock is held, so the concurrency policy never makes a run wait. Queued runs
    that an earlier daemon session left stay queued for the next `treadle serve`. A
    `ControlPoller` with `accept_runs=False` answers cancel requests from other terminals.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        rng: RandomSource | None = None,
        steps: StepRegistry | None = None,
        environ: Mapping[str, str] | None = None,
        inline_store: bool = False,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._rng = rng or default_random()
        self._steps = steps or builtin_registry()
        self._environ = dict(os.environ if environ is None else environ)
        self._inline = inline_store
        self._secrets = SecretStore(settings.env_file, self._environ)
        self._registry: WorkflowRegistry | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._store: AsyncStore | None = None
        self._manager: RunManager | None = None
        # The masker for log records, such as the CLI's; it follows the loaded secrets.
        self._log_masker = _MaskerCache(self._secrets, self._hidden_env)

    async def __aenter__(self) -> LocalRunner:
        """Acquire the lock, open the store, and recover crashed runs.

        Raises LockHeldError, ConfigError, or SchemaTooNewError. Nothing stays open when it
        raises.
        """
        if self._stack is not None:
            raise RuntimeError("the runner is already open")
        stack = contextlib.AsyncExitStack()
        try:
            await self._open(stack)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop the runs, run a bounded retention pass, close the store, and release the lock.

        A run that is still active gets the `shutdown` cancel reason and `[runs]
        shutdown_grace` to finish.
        """
        stack, self._stack = self._stack, None
        self._store = self._manager = None
        if stack is not None:
            await stack.aclose()

    @property
    def store(self) -> AsyncStore:
        """The open store."""
        if self._store is None:
            raise RuntimeError("the runner isn't open; use `async with LocalRunner(...)`")
        return self._store

    async def start(self, workflow_id: str, inputs: Mapping[str, str]) -> Submission:
        """Start a manual run with string inputs, such as `-i KEY=VALUE` values.

        Raises NotFoundError, WorkflowInvalidError, or InputError.
        """
        request = RunRequest(
            workflow_id=workflow_id,
            kind=TriggerKind.MANUAL,
            trigger={"kind": TriggerKind.MANUAL.value, "origin": Origin.CLI.value},
            inputs=dict(inputs),
            inputs_are_strings=True,
        )
        return await self._open_manager().submit(request)

    async def start_rerun(self, run_id: int) -> Submission:
        """Re-run a finished run with the workflow's current version.

        Raises NotFoundError, ConflictError, or WorkflowInvalidError.
        """
        return await self._open_manager().rerun(run_id)

    async def cancel(self, run_id: int, *, force: bool = False) -> CancelOutcome:
        """Cancel a run for the user; a second call, or `force`, forces it."""
        return await self._open_manager().cancel(run_id, CancelReason.USER, force=force)

    async def wait(self, run_id: int) -> RunStatus:
        """Wait until the run is terminal and return its status."""
        return await self._open_manager().wait(run_id)

    async def _open(self, stack: contextlib.AsyncExitStack) -> None:
        """Set everything up, registering each cleanup on `stack` as soon as it's needed.

        The stack unwinds in reverse: the control poller stops, then the run manager, then
        retention runs, and finally the HTTP client, the store, and the lock close.
        """
        settings, clock = self._settings, self._clock
        blocking: BlockingRunner = inline_runner if self._inline else thread_runner
        lock = InstanceLock(
            settings.state_dir, LockHolder(os.getpid(), "run", iso_utc(clock.now()), __version__)
        )
        lock.acquire()
        stack.callback(lock.release)
        store = await AsyncStore.open(settings, inline=self._inline)
        stack.push_async_callback(store.close)
        await _recover(store, settings, clock, blocking=blocking)
        self._secrets.load()
        registry = WorkflowRegistry(
            settings=settings,
            steps=self._steps,
            secrets=self._secrets,
            store=store,
            sinks=(),
            on_removed=None,
            clock=clock,
            blocking=blocking,
        )
        await registry.load_all()
        self._registry = registry
        http = _http_client()
        stack.push_async_callback(http.aclose)
        manager = _run_manager(
            settings,
            store,
            registry,
            self._secrets,
            steps=self._steps,
            environ=self._environ,
            clock=clock,
            rng=self._rng,
            http=http,
        )
        stack.push_async_callback(self._prune, store, registry)
        actor = asyncio.create_task(manager.run(), name="treadle-runs")
        stack.push_async_callback(_stop_manager, manager, actor, settings.runs.shutdown_grace)
        poller = ControlPoller(store=store, control=manager, clock=clock, accept_runs=False)
        control = asyncio.create_task(poller.run(), name="treadle-control")
        stack.push_async_callback(_stop_task, control)
        self._store, self._manager = store, manager

    async def _prune(self, store: AsyncStore, registry: WorkflowRegistry) -> None:
        """Run one bounded retention pass; a failure only logs a warning."""
        try:
            await run_retention(
                store,
                self._settings.retention,
                now_ms=to_ms(self._clock.now()),
                keep_digests=registry.current_digests(),
                max_batches=_RETENTION_BATCHES,
            )
        except Exception:
            _log.warning("retention failed; the next run or the daemon tries again", exc_info=True)

    def _open_manager(self) -> RunManager:
        if self._manager is None:
            raise RuntimeError("the runner isn't open; use `async with LocalRunner(...)`")
        return self._manager

    def _hidden_env(self) -> frozenset[str]:
        if self._registry is None:
            return frozenset({self._settings.server.token_env})
        return self._registry.hidden_env()


class _MaskerCache:
    """Returns a masker for the current secrets and hidden variables, rebuilt when they change.

    Log records may come from other threads. A registry that changes while it's read keeps
    the previous masker for that record.
    """

    def __init__(self, secrets: SecretStore, hidden_env: Callable[[], frozenset[str]]) -> None:
        self._secrets = secrets
        self._hidden_env = hidden_env
        self._cached: tuple[SecretSnapshot, frozenset[str], Masker] | None = None

    def __call__(self) -> Masker:
        snapshot, cached = self._secrets.snapshot, self._cached
        try:
            hidden = self._hidden_env()
        except RuntimeError:  # the registry changed during the read, on the event loop
            return Masker() if cached is None else cached[2]
        if cached is not None and cached[0] is snapshot and cached[1] == hidden:
            return cached[2]
        masker = Masker(snapshot.mask_values(hidden))
        self._cached = (snapshot, hidden, masker)
        return masker


async def _recover(
    store: AsyncStore, settings: Settings, clock: Clock, *, blocking: BlockingRunner = thread_runner
) -> RecoveryReport:
    """Recover what a stopped lock holder left behind, as every new lock holder must.

    Running runs become interrupted, and old control requests expire. Leftover process groups
    are only reported, never signalled, because the operating system may have reused their
    IDs. Then the per-run scratch directories go.
    """
    at_ms = to_ms(clock.now())
    report = await store.write(lambda repo: repo.recover(at_ms=at_ms))
    for run_id in report.interrupted_runs:
        _log.warning("run=%d was interrupted: the Treadle process stopped while it ran", run_id)
    for pgid in report.orphan_pgids:
        _log.warning(
            "process group %d of an interrupted run still exists; Treadle doesn't signal it, "
            "because its ID may belong to another process now",
            pgid,
        )
    await blocking(_clear_run_dirs, settings.tmp_dir)
    return report


def _clear_run_dirs(tmp_dir: Path) -> None:
    """Delete the `run-*` scratch directories that an earlier process left in `tmp_dir`."""
    for leftover in tmp_dir.glob("run-*"):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)


def _http_client() -> httpx.AsyncClient:
    """Return the HTTP client that `http` steps share."""
    return httpx.AsyncClient(
        max_redirects=_MAX_REDIRECTS, headers={"User-Agent": f"treadle/{__version__}"}
    )


def _run_manager(
    settings: Settings,
    store: AsyncStore,
    registry: WorkflowRegistry,
    secrets: SecretStore,
    *,
    steps: StepRegistry,
    environ: Mapping[str, str],
    clock: Clock,
    rng: RandomSource,
    http: httpx.AsyncClient,
) -> RunManager:
    """Build the executor and the run manager over the registry's workflows."""

    def snapshot() -> SecretSnapshot:
        return secrets.snapshot

    executor = Executor(
        ExecutorDeps(
            store=store,
            steps=steps,
            settings=settings,
            secrets=snapshot,
            hidden_env=registry.hidden_env,
            environ=environ,
            clock=clock,
            rng=rng,
            processes=ProcessRegistry(),
            http=http,
        )
    )
    return RunManager(
        store=store,
        executor=executor,
        workflows=registry,
        secrets=snapshot,
        hidden_env=registry.hidden_env,
        settings=settings,
        clock=clock,
    )


async def _stop_manager(manager: RunManager, actor: asyncio.Task[None], grace: float) -> None:
    """Shut the run manager down, then wait for its actor task."""
    await manager.shutdown(grace=grace)
    await _finished(actor)


async def _stop_task(task: asyncio.Task[None]) -> None:
    """Cancel a background task and wait until it ends."""
    task.cancel()
    await _finished(task)


async def _finished(task: asyncio.Task[None]) -> None:
    """Wait for a task without raising, and log its failure, if any."""
    await asyncio.wait([task])
    if not task.cancelled() and task.exception() is not None:
        _log.error("%s failed", task.get_name(), exc_info=task.exception())
