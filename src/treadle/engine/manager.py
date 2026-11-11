"""The run manager: the actor that admits, dispatches, and cancels runs.

Each workflow's concurrency policy decides whether a new run queues, is skipped, or replaces
the runs before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, TypeVar, cast

from treadle.clock import Clock, to_ms
from treadle.config import Settings
from treadle.engine.executor import Executor, RunHandle
from treadle.engine.masking import Masker
from treadle.errors import ConflictError, NotFoundError, ShuttingDownError, WorkflowInvalidError
from treadle.models import (
    CancelOutcome,
    CancelReason,
    ConcurrencyPolicy,
    ManagerStats,
    RunReason,
    RunRecord,
    RunRequest,
    RunStatus,
    Submission,
    SubmitOutcome,
    Workflow,
)
from treadle.protocols import WorkflowSource
from treadle.secrets import SecretSnapshot
from treadle.store.async_store import AsyncStore
from treadle.store.repo import CreateResult, NewRun, Repository, StepSeed
from treadle.types import JSONObject
from treadle.workflow.inputs import coerce_inputs

_T = TypeVar("_T")
_log = logging.getLogger("treadle.engine")
_KILL_WAIT: Final = 2.0  # after SIGKILL, for the run tasks to finish
_CANCEL_WAIT: Final = 1.0  # after cancelling the run tasks
_VERSION_GONE: Final = "the workflow version this run was created with no longer loads"
_STOPPED: Final = "Treadle is shutting down and accepts no requests"


@dataclass(slots=True)
class _Queued:
    """An admitted run waiting in `pending`. Inputs and trigger are unmasked when this
    process admitted it, and the stored, masked values when it was re-admitted."""

    run: RunRecord
    workflow: Workflow
    inputs: JSONObject
    trigger: JSONObject


@dataclass(frozen=True, slots=True)
class _Active:
    """The one running run of a workflow, with its handle and task."""

    run: RunRecord
    workflow: Workflow
    handle: RunHandle
    task: asyncio.Task[None]


@dataclass(frozen=True, slots=True)
class _Command:
    """One operation for the actor. `reply` gets its result; fire-and-forget has none."""

    operation: Callable[[], Awaitable[object]]
    reply: asyncio.Future[object] | None


class RunManager:
    """Admits, dispatches, cancels, and re-runs runs; implements RunControl.

    One actor task processes commands in order, so admission decisions never interleave.
    """

    _group: asyncio.TaskGroup  # the group of run tasks, which `run` opens

    def __init__(
        self,
        *,
        store: AsyncStore,
        executor: Executor,
        workflows: WorkflowSource,
        secrets: Callable[[], SecretSnapshot],
        hidden_env: Callable[[], frozenset[str]],
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._store = store
        self._executor = executor
        self._workflows = workflows
        self._secrets = secrets
        self._hidden_env = hidden_env
        self._settings = settings
        self._clock = clock
        self._commands: asyncio.Queue[_Command | None] = asyncio.Queue()
        self._active: dict[str, _Active] = {}  # by workflow ID
        self._pending: dict[str, deque[_Queued]] = {}  # by workflow ID, FIFO
        # A waiter gets the terminal status, or a ShuttingDownError as its result, never as
        # an exception, so a waiter whose caller left doesn't log "exception never retrieved".
        self._waiters: dict[int, list[asyncio.Future[RunStatus | ShuttingDownError]]] = {}
        self._idle = asyncio.Event()  # set while no run is active
        self._idle.set()
        self._draining = False
        self._stopped = False

    async def run(self) -> None:
        """Run the actor loop. It owns the run tasks and returns after shutdown."""
        try:
            async with asyncio.TaskGroup() as group:
                self._group = group
                while (command := await self._commands.get()) is not None:
                    await self._perform(command)
        finally:
            self._stopped = True
            self._abandon()

    async def submit(self, request: RunRequest) -> Submission:
        """Admit or reject a run under the workflow's policy.

        Raises NotFoundError, WorkflowInvalidError, InputError, or ShuttingDownError.
        """
        return await self._call(lambda: self._submit(request))

    async def cancel(
        self, run_id: int, reason: CancelReason = CancelReason.USER, *, force: bool = False
    ) -> CancelOutcome:
        """Cancel a queued or active run. Raises NotFoundError."""
        return await self._call(lambda: self._cancel(run_id, reason, force))

    async def rerun(self, run_id: int) -> Submission:
        """Submit a finished run again with the current workflow version.

        Raises NotFoundError, ConflictError, or WorkflowInvalidError.
        """
        return await self._call(lambda: self._rerun(run_id))

    async def readmit(self) -> int:
        """Replay queued runs from the database through the policy.

        Returns how many runs were admitted. It runs once at startup, after the workflows
        load and before any run is active, and binds each run to its stored workflow version.
        A run whose workflow has no entry was deleted while Treadle was stopped, and is
        cancelled with reason `workflow_removed`, as a deletion at run time is. When the
        source has no entry at all, the workflows directory may have been unreadable, so such
        runs stay queued, with a warning.
        """
        return await self._call(self._readmit)

    async def workflow_removed(self, workflow_id: str) -> None:
        """Cancel the workflow's queued runs with reason `workflow_removed`."""
        await self._call(lambda: self._remove(workflow_id))

    async def shutdown(self, *, grace: float) -> None:
        """Drain: cancel active runs with reason `shutdown`, wait up to `grace`, then force.

        A run that is already cancelling keeps its reason, and its cleanup steps keep
        running within the grace. Queued runs stay queued in the database. Runs that outlast
        the force and the cancellation of their tasks are recorded as cancelled. Then the
        actor loop exits. Call it while `run()` runs. A second call, such as `grace=0` after
        a second signal, forces the active runs when its own grace ends, which skips the rest
        of the first call's grace.
        """
        if self._stopped:
            return
        self._draining = True
        # The actor may stop while a second, faster shutdown finishes first.
        with contextlib.suppress(ShuttingDownError):
            await self._call(self._drain)
            stopping = list(self._active.values())
            if not await self._settled(grace):
                await self._force(stopping)
        self._commands.put_nowait(None)

    async def wait(self, run_id: int) -> RunStatus:
        """Wait until a run is terminal and return its status.

        Raises NotFoundError, or ShuttingDownError when shutdown leaves the run queued.
        """
        watched = await self._call(lambda: self._watch(run_id))
        outcome = await watched
        if isinstance(outcome, ShuttingDownError):
            raise outcome
        return outcome

    def stats(self) -> ManagerStats:
        """Return the active and queued counts."""
        queued = sum(len(queue) for queue in self._pending.values())
        return ManagerStats(active=len(self._active), queued=queued, draining=self._draining)

    @property
    def draining(self) -> bool:
        """True once shutdown started."""
        return self._draining

    # The actor

    async def _call(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """Run `operation` on the actor and return its result."""
        if self._stopped:
            raise ShuttingDownError(_STOPPED)
        reply: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._commands.put_nowait(_Command(operation, reply))
        return cast(_T, await reply)

    def _post(self, operation: Callable[[], Awaitable[object]]) -> None:
        """Queue a fire-and-forget operation, such as `Finished`."""
        self._commands.put_nowait(_Command(operation, None))

    async def _perform(self, command: _Command) -> None:
        try:
            result = await command.operation()
        except asyncio.CancelledError:
            _refuse(command)  # the actor itself is cancelled, so this command never ends
            raise
        except Exception as exc:
            if command.reply is None:
                _log.exception("the run manager failed to handle a command")
            elif not command.reply.done():
                command.reply.set_exception(exc)
            return
        if command.reply is not None and not command.reply.done():
            command.reply.set_result(result)

    def _abandon(self) -> None:
        """Fail the requests and waiters that the stopped actor can no longer answer."""
        while not self._commands.empty():
            command = self._commands.get_nowait()
            if command is not None:
                _refuse(command)
        for run_id in list(self._waiters):
            self._fail_waiters(run_id, f"Treadle stopped before run {run_id} finished")

    # Commands

    async def _submit(self, request: RunRequest) -> Submission:
        if self._draining:
            raise ShuttingDownError("Treadle is shutting down and accepts no new runs")
        workflow = self._current(request.workflow_id)
        inputs = coerce_inputs(
            workflow.inputs, request.inputs, from_strings=request.inputs_are_strings
        )
        trigger = dict(request.trigger)
        masker = Masker(self._secrets().mask_values(self._hidden_env()))
        queue = self._pending.get(workflow.id, deque())
        active = self._active.get(workflow.id)
        reason = self._admission(workflow, queue, active)
        replace = reason is None and workflow.concurrency.policy is ConcurrencyPolicy.REPLACE
        now = self._now_ms()
        new = NewRun(
            workflow=workflow,
            status=RunStatus.QUEUED if reason is None else RunStatus.SKIPPED,
            reason=reason,
            trigger_kind=request.kind,
            trigger_key=request.trigger_key,
            trigger=_masked(masker, trigger),
            inputs=_masked(masker, inputs),
            attempt=request.attempt,
            rerun_of=request.rerun_of,
            slot=request.slot,
            delivery=request.delivery,
            cancel_queued=tuple(item.run.id for item in queue) if replace else (),
            replaced_active=active.run.id if replace and active is not None else None,
            created_at_ms=now,
        )
        result = await self._store.write(lambda repo: _create(repo, new, now))
        if result.outcome == "slot_taken":
            return Submission(SubmitOutcome.SLOT_TAKEN, None)
        if result.outcome == "duplicate":
            return Submission(SubmitOutcome.DUPLICATE, result.run)
        run = cast(RunRecord, result.run)
        if reason is not None:
            _log.info("run=%d workflow=%s skipped (%s)", run.id, workflow.id, reason.value)
            return Submission(SubmitOutcome.SKIPPED, run)
        if replace:
            self._replace(workflow.id, active)
        self._pending.setdefault(workflow.id, deque()).append(
            _Queued(run, workflow, inputs, trigger)
        )
        _log.info("run=%d workflow=%s queued", run.id, workflow.id)
        await self._dispatch()
        return Submission(SubmitOutcome.QUEUED, run)

    async def _cancel(self, run_id: int, reason: CancelReason, force: bool) -> CancelOutcome:
        queued = self._find_queued(run_id)
        if queued is not None:
            queue = self._pending[queued.workflow.id]
            queue.remove(queued)
            if not queue:
                del self._pending[queued.workflow.id]
            return await self._cancel_stored(run_id, reason)
        active = self._find_active(run_id)
        if active is not None:
            handle = active.handle
            if handle.finalizing:
                return CancelOutcome(
                    run_id, RunStatus.RUNNING, accepted=False, forced=handle.forced
                )
            handle.request_cancel(reason, force=force)
            now = self._now_ms()
            await self._store.write(lambda repo: repo.mark_cancel_requested(run_id, at_ms=now))
            _log.info("run=%d cancel requested (%s)", run_id, handle.reason)
            return CancelOutcome(run_id, RunStatus.RUNNING, accepted=True, forced=handle.forced)
        run = await self._store.read(lambda repo: repo.get_run(run_id))
        if run is None:
            raise NotFoundError(f"run {run_id} doesn't exist")
        if run.status is RunStatus.QUEUED:
            # A queued run that this process never admitted, such as one an earlier
            # daemon session left while a foreground `treadle run` holds the lock.
            return await self._cancel_stored(run_id, reason)
        return CancelOutcome(run_id, run.status, accepted=False, forced=False)

    async def _rerun(self, run_id: int) -> Submission:
        run = await self._store.read(lambda repo: repo.get_run(run_id))
        if run is None:
            raise NotFoundError(f"run {run_id} doesn't exist")
        if not run.status.terminal:
            message = f"run {run_id} is {run.status.value}; only a finished run can be re-run"
            raise ConflictError(message)
        request = RunRequest(
            workflow_id=run.workflow_id,
            kind=run.trigger_kind,
            trigger=run.trigger,
            inputs=run.inputs,
            rerun_of=run.id,
            attempt=run.attempt + 1,
        )
        return await self._submit(request)

    async def _readmit(self) -> int:
        queued = await self._store.read(lambda repo: repo.queued_runs())
        listed = bool(self._workflows.entries())
        readmitted: set[int] = set()
        for run in queued:
            if self._workflows.get(run.workflow_id) is None:
                await self._readmit_unlisted(run, listed=listed)
                continue
            workflow = await self._workflows.version(run.digest, workflow_id=run.workflow_id)
            if workflow is None:
                await self._fail_unloadable(run.id)
            elif await self._replay(run, workflow):
                readmitted.add(run.id)
        waiting = {item.run.id for queue in self._pending.values() for item in queue}
        await self._dispatch()
        return len(readmitted & waiting)

    async def _readmit_unlisted(self, run: RunRecord, *, listed: bool) -> None:
        """Cancel a queued run whose workflow is gone, or leave it while nothing is listed."""
        if not listed:
            _log.warning("run=%d stays queued: workflow '%s' isn't loaded", run.id, run.workflow_id)
            return
        now = self._now_ms()
        cancelled = await self._store.write(
            lambda repo: repo.cancel_queued([run.id], reason=RunReason.WORKFLOW_REMOVED, at_ms=now)
        )
        for run_id in cancelled:
            _log.info("run=%d cancelled: workflow %s was removed", run_id, run.workflow_id)
            self._resolve(run_id, RunStatus.CANCELLED)

    async def _remove(self, workflow_id: str) -> None:
        queue = self._pending.pop(workflow_id, None)
        if not queue:
            return
        run_ids, now = [item.run.id for item in queue], self._now_ms()
        cancelled = await self._store.write(
            lambda repo: repo.cancel_queued(run_ids, reason=RunReason.WORKFLOW_REMOVED, at_ms=now)
        )
        for run_id in cancelled:
            self._resolve(run_id, RunStatus.CANCELLED)
        _log.info("workflow=%s removed; cancelled %d queued runs", workflow_id, len(cancelled))

    async def _drain(self) -> None:
        self._draining = True
        for queue in self._pending.values():
            for item in queue:
                self._fail_waiters(
                    item.run.id, f"Treadle is shutting down; run {item.run.id} stays queued"
                )
        self._pending.clear()
        for active in self._active.values():
            active.handle.cancel_for(CancelReason.SHUTDOWN)

    async def _finished(self, workflow_id: str, run_id: int, status: RunStatus | None) -> None:
        del self._active[workflow_id]
        if not self._active:
            self._idle.set()
        if status is None:  # the run task was cancelled, or couldn't record its failure
            await self._resolve_stored(run_id)
        else:
            self._resolve(run_id, status)
        await self._dispatch()

    async def _watch(self, run_id: int) -> asyncio.Future[RunStatus | ShuttingDownError]:
        watched: asyncio.Future[RunStatus | ShuttingDownError]
        watched = asyncio.get_running_loop().create_future()
        if self._find_queued(run_id) is None and self._find_active(run_id) is None:
            status = await self._stored_status(run_id)
            if status is None:
                raise NotFoundError(f"run {run_id} doesn't exist")
            if status.terminal:
                watched.set_result(status)
                return watched
        self._waiters.setdefault(run_id, []).append(watched)
        return watched

    async def _finish_stopped(self, runs: list[_Active]) -> None:
        """Record runs that are still running after the forced shutdown as cancelled."""
        run_ids, now = [active.run.id for active in runs], self._now_ms()
        error = f"run cancelled ({CancelReason.SHUTDOWN.value})"

        def finish(repo: Repository) -> list[int]:
            return [
                run_id
                for run_id in run_ids
                if repo.finish_abandoned_run(
                    run_id, RunStatus.CANCELLED, at_ms=now, reason=RunReason.SHUTDOWN, error=error
                )
            ]

        for run_id in await self._store.write(finish):
            _log.warning("run=%d didn't stop in time; recorded it as cancelled", run_id)
            self._resolve(run_id, RunStatus.CANCELLED)

    # Admission and dispatch

    def _current(self, workflow_id: str) -> Workflow:
        entry = self._workflows.get(workflow_id)
        if entry is None:
            raise NotFoundError(f"workflow '{workflow_id}' doesn't exist")
        if entry.current is None:
            raise WorkflowInvalidError(workflow_id, entry.issues)
        return entry.current

    def _admission(
        self, workflow: Workflow, queue: deque[_Queued], active: _Active | None
    ) -> RunReason | None:
        """Return why the policy skips a new run, or None to queue it."""
        policy = workflow.concurrency
        if policy.policy is ConcurrencyPolicy.QUEUE and len(queue) >= policy.max_queued:
            return RunReason.QUEUE_FULL
        if policy.policy is ConcurrencyPolicy.SKIP and (active is not None or queue):
            return RunReason.ALREADY_ACTIVE
        return None

    def _replace(self, workflow_id: str, active: _Active | None) -> None:
        """Forget the queued runs that `create_run` cancelled, and cancel the active run."""
        for item in self._pending.pop(workflow_id, deque()):
            self._resolve(item.run.id, RunStatus.CANCELLED)
        if active is not None:
            active.handle.cancel_for(CancelReason.REPLACED)

    async def _replay(self, run: RunRecord, workflow: Workflow) -> bool:
        """Apply the policy to a re-admitted run, and return True when it stays queued."""
        queue = self._pending.get(workflow.id, deque())
        reason = self._admission(workflow, queue, self._active.get(workflow.id))
        now = self._now_ms()
        if reason is not None:
            await self._store.write(
                lambda repo: repo.skip_queued([run.id], reason=reason, at_ms=now)
            )
            self._resolve(run.id, RunStatus.SKIPPED)
            return False
        if workflow.concurrency.policy is ConcurrencyPolicy.REPLACE and queue:
            older = [item.run.id for item in queue]
            await self._store.write(
                lambda repo: repo.cancel_queued(
                    older, reason=RunReason.REPLACED, at_ms=now, replaced_by=run.id
                )
            )
            for run_id in older:
                self._resolve(run_id, RunStatus.CANCELLED)
            queue.clear()
        queue.append(_Queued(run, workflow, run.inputs, run.trigger))
        self._pending[workflow.id] = queue
        return True

    async def _fail_unloadable(self, run_id: int) -> None:
        now = self._now_ms()
        if await self._store.write(
            lambda repo: repo.fail_queued(run_id, at_ms=now, error=_VERSION_GONE)
        ):
            _log.warning("run=%d failed: %s", run_id, _VERSION_GONE)
            self._resolve(run_id, RunStatus.FAILED)

    async def _dispatch(self) -> None:
        """Start pending runs in run-ID order while the global cap allows.

        Only a workflow without an active run can start one, so a run that replaces another
        waits until the old one is terminal.
        """
        limit = self._settings.runs.max_concurrent
        while not self._draining and len(self._active) < limit:
            heads = [
                queue[0]
                for workflow_id, queue in self._pending.items()
                if queue and workflow_id not in self._active
            ]
            if not heads:
                return
            await self._start(min(heads, key=lambda item: item.run.id))

    async def _start(self, item: _Queued) -> None:
        """Commit `queued -> running`, then start the run task."""
        run_id, workflow = item.run.id, item.workflow
        seeds = [StepSeed(step.id, step.name, step.position, step.uses) for step in workflow.steps]
        now = self._now_ms()

        def start(repo: Repository) -> RunRecord | None:
            if repo.start_run(run_id, seeds, at_ms=now, pid=os.getpid()):
                return repo.get_run(run_id)
            return None

        run = await self._store.write(start)
        queue = self._pending[workflow.id]
        queue.popleft()
        if not queue:
            del self._pending[workflow.id]
        if run is None:
            _log.debug("run=%d left the queue elsewhere; dropping it", run_id)
            await self._resolve_stored(run_id)
            return
        handle = RunHandle(run_id)
        task = self._group.create_task(self._execute(run, item, handle))
        self._active[workflow.id] = _Active(run, workflow, handle, task)
        self._idle.clear()

    async def _execute(self, run: RunRecord, item: _Queued, handle: RunHandle) -> None:
        """The run task: execute the run, and always tell the actor that it finished."""
        status: RunStatus | None = None
        try:
            status = await self._executor.execute(
                run, item.workflow, handle, inputs=item.inputs, trigger=item.trigger
            )
        except Exception as exc:
            _log.exception("run=%d: internal error in the executor", run.id)
            status = await self._fail_run(run.id, exc)
        finally:
            self._post(functools.partial(self._finished, item.workflow.id, run.id, status))

    async def _fail_run(self, run_id: int, exc: Exception) -> RunStatus | None:
        """Finish a run whose executor failed unexpectedly as failed, if it's still running.

        The steps and attempts that the executor left unfinished close in the same write.
        """
        error, now = f"internal error: {type(exc).__name__}", self._now_ms()
        try:
            if await self._store.write(
                lambda repo: repo.finish_abandoned_run(
                    run_id, RunStatus.FAILED, at_ms=now, reason=None, error=error
                )
            ):
                return RunStatus.FAILED
            return await self._stored_status(run_id)
        except Exception:
            _log.exception("run=%d: couldn't record the internal error", run_id)
            return None

    async def _cancel_stored(self, run_id: int, reason: CancelReason) -> CancelOutcome:
        """Cancel a queued run in the database."""
        now = self._now_ms()
        cancelled = await self._store.write(
            lambda repo: repo.cancel_queued([run_id], reason=RunReason(reason.value), at_ms=now)
        )
        if cancelled:
            _log.info("run=%d cancelled while queued (%s)", run_id, reason.value)
            self._resolve(run_id, RunStatus.CANCELLED)
            return CancelOutcome(run_id, RunStatus.CANCELLED, accepted=True, forced=False)
        status = await self._stored_status(run_id)
        return CancelOutcome(run_id, status or RunStatus.QUEUED, accepted=False, forced=False)

    # Helpers

    async def _force(self, stopping: list[_Active]) -> None:
        """Force the runs that outlast the grace: SIGKILL, then cancel their tasks."""
        for active in list(self._active.values()):
            active.handle.force()
        self._executor.kill_processes()
        if not await self._settled(_KILL_WAIT):
            for active in list(self._active.values()):
                active.task.cancel()
            await self._settled(_CANCEL_WAIT)
        await self._call(lambda: self._finish_stopped(stopping))

    async def _settled(self, seconds: float) -> bool:
        """Wait up to `seconds` for no run to be active, and return True when none is."""
        try:
            async with asyncio.timeout(seconds):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True

    def _find_queued(self, run_id: int) -> _Queued | None:
        for queue in self._pending.values():
            for item in queue:
                if item.run.id == run_id:
                    return item
        return None

    def _find_active(self, run_id: int) -> _Active | None:
        return next((item for item in self._active.values() if item.run.id == run_id), None)

    async def _stored_status(self, run_id: int) -> RunStatus | None:
        run = await self._store.read(lambda repo: repo.get_run(run_id))
        return None if run is None else run.status

    async def _resolve_stored(self, run_id: int) -> None:
        """Resolve a run's waiters with its stored status, once that status is terminal."""
        status = await self._stored_status(run_id)
        if status is not None and status.terminal:
            self._resolve(run_id, status)

    def _resolve(self, run_id: int, status: RunStatus) -> None:
        for waiter in self._waiters.pop(run_id, []):
            if not waiter.done():
                waiter.set_result(status)

    def _fail_waiters(self, run_id: int, message: str) -> None:
        for waiter in self._waiters.pop(run_id, []):
            if not waiter.done():
                waiter.set_result(ShuttingDownError(message))

    def _now_ms(self) -> int:
        return to_ms(self._clock.now())


def _create(repo: Repository, new: NewRun, now_ms: int) -> CreateResult:
    """Create the run; a replaced active run also records when its cancel was requested."""
    result = repo.create_run(new)
    if result.outcome == "created" and new.replaced_active is not None:
        repo.mark_cancel_requested(new.replaced_active, at_ms=now_ms)
    return result


def _masked(masker: Masker, value: JSONObject) -> JSONObject:
    return cast(JSONObject, masker.mask_json(value))


def _refuse(command: _Command) -> None:
    """Answer a command that the stopping actor won't finish with ShuttingDownError."""
    if command.reply is not None and not command.reply.done():
        command.reply.set_exception(ShuttingDownError(_STOPPED))
