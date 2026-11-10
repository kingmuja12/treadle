"""The executor: runs one admitted run's step graph."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import traceback
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TypeVar, cast

from treadle.clock import Clock, RandomSource, to_ms
from treadle.config import Settings
from treadle.engine.context import (
    AncestorStatus,
    RunState,
    base_values,
    filtered_environ,
    merge_env,
    step_values,
    treadle_env,
)
from treadle.engine.logsink import MAX_RUNTIME_MASKS, AttemptLog, LogSink
from treadle.engine.masking import Masker
from treadle.errors import EvaluationError, StepError
from treadle.expr.analysis import calls
from treadle.expr.ast import STATUS_FUNCTIONS, Condition, Template, TemplateTree
from treadle.expr.evaluator import EvalContext, evaluate_condition, render, render_str
from treadle.models import (
    AttemptStatus,
    CancelReason,
    RetryPolicy,
    RunReason,
    RunRecord,
    RunStatus,
    SkipReason,
    Step,
    StepStatus,
    Workflow,
)
from treadle.schema import validate
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepRegistry, StepRuntime, StepType
from treadle.steps.procs import LeftoverGroups, ProcessRegistry
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository, StepUpdate
from treadle.types import JSONObject, JSONValue, Severity, YamlPath, format_path
from treadle.units import format_duration, format_size

if TYPE_CHECKING:
    import httpx

_T = TypeVar("_T")
_log = logging.getLogger("treadle.engine")
_PENDING: Final = frozenset({StepStatus.PENDING})
_ACTIVE: Final = frozenset({StepStatus.RUNNING, StepStatus.RETRYING})
_UNFINISHED: Final = _PENDING | _ACTIVE
_FAILED: Final = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})
_TOO_MANY_MASKS: Final = f"too many ::add-mask:: values: a run masks at most {MAX_RUNTIME_MASKS}"
_ATTEMPT_STATUS: Final = {
    StepStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
    StepStatus.FAILED: AttemptStatus.FAILED,
    StepStatus.TIMED_OUT: AttemptStatus.TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class ExecutorDeps:
    """What the executor needs from the process around it."""

    store: AsyncStore
    steps: StepRegistry
    settings: Settings
    secrets: Callable[[], SecretSnapshot]
    hidden_env: Callable[[], frozenset[str]]
    environ: Mapping[str, str]
    clock: Clock
    rng: RandomSource
    processes: ProcessRegistry
    http: httpx.AsyncClient


class RunHandle:
    """The cancel state of one active run, shared by the manager and the executor."""

    finalizing: bool

    def __init__(self, run_id: int) -> None:
        self.finalizing = False
        self._run_id = run_id
        self._reason: CancelReason | None = None
        self._forced = False
        self._changed = asyncio.Event()

    @property
    def run_id(self) -> int:
        """The run this handle controls."""
        return self._run_id

    @property
    def reason(self) -> CancelReason | None:
        """The first cancel reason, or None while the run isn't cancelling."""
        return self._reason

    @property
    def forced(self) -> bool:
        """True once cancellation was forced."""
        return self._forced

    def request_cancel(self, reason: CancelReason, *, force: bool = False) -> bool:
        """Request cancellation, and return True when the state changed.

        The first reason wins. A second request, or `force=True`, sets `forced`.
        """
        if self._reason is not None:
            return self.force()
        self._reason = reason
        self._forced = force
        self._notify()
        return True

    def force(self) -> bool:
        """Force cancellation. Returns True when the state changed.

        A run that wasn't cancelling yet gets the reason `user`.
        """
        if self._forced:
            return False
        # A forced run ends cancelled, which needs a reason; nothing in Treadle
        # forces a run before requesting a cancel, so a bare force counts as the user's.
        if self._reason is None:
            self._reason = CancelReason.USER
        self._forced = True
        self._notify()
        return True

    def wait_changed(self) -> Coroutine[Any, Any, bool]:
        """Return a coroutine that finishes at the next change of `reason` or `forced`.

        The wait begins when this method is called, not when the coroutine first runs, so a
        task made from it sees a change that lands before the task's first step.
        """
        return self._changed.wait()

    def _notify(self) -> None:
        """Wake every current waiter, and start a new event for later ones."""
        self._changed.set()
        self._changed = asyncio.Event()


class Executor:
    """Runs admitted runs: setup, the step loop, and finalization."""

    def __init__(self, deps: ExecutorDeps) -> None:
        self._deps = deps

    async def execute(
        self,
        run: RunRecord,
        workflow: Workflow,
        handle: RunHandle,
        *,
        inputs: JSONObject,
        trigger: JSONObject,
    ) -> RunStatus:
        """Run every step of a running run and commit its terminal status.

        `run` must already be `running`, with its steps stored as `pending`. Cancelling the
        call stops every step, records the run as cancelled (reason `shutdown` unless the
        handle has one), and re-raises the CancelledError.
        """
        last_seq = await self._deps.store.read(lambda repo: repo.last_log_seq(run.id))
        execution = _Execution(self._deps, run, workflow, handle, inputs, trigger, last_seq + 1)
        return await execution.run()

    def kill_processes(self) -> int:
        """Send SIGKILL to every tracked process group and return the count."""
        return self._deps.processes.kill_all()


def backoff_delay(policy: RetryPolicy, failed_attempt: int, rng: RandomSource) -> float:
    """Return the wait after failed attempt `n` (1-based).

    `min(max_delay, delay * backoff ** (n - 1))`, reduced by up to `jitter` of itself.
    """
    base = min(policy.max_delay, policy.delay * policy.backoff ** (failed_attempt - 1))
    if policy.jitter == 0:
        return base
    return base * (1 - policy.jitter * rng.random())


def conclude(state: RunState, reason: CancelReason | None) -> tuple[RunStatus, str | None]:
    """Return the run's terminal status and one-line error.

    A run timed out when its timeout fired, and was cancelled for any other cancel reason.
    Otherwise it failed when a step failed or timed out without being tolerated, and the
    error names the first such step; else it succeeded.
    """
    workflow = state.workflow
    if reason is CancelReason.TIMEOUT:
        return RunStatus.TIMED_OUT, f"run timed out after {format_duration(workflow.timeout)}"
    if reason is not None:
        return RunStatus.CANCELLED, f"run cancelled ({reason.value})"
    for step in workflow.steps:
        status = state.statuses.get(step.id)
        if status in _FAILED and step.id not in state.tolerated:
            return RunStatus.FAILED, _step_summary(state, step, status)
    return RunStatus.SUCCEEDED, None


def _step_summary(state: RunState, step: Step, status: StepStatus | None) -> str:
    """Name a failed step in the run error, such as "step 'archive' failed: exit code 23"."""
    attempts = state.attempts.get(step.id, 0)
    if status is StepStatus.TIMED_OUT:
        tries = f" ({attempts} attempts)" if attempts > 1 else ""
        return f"step '{step.id}' timed out after {format_duration(step.timeout)}{tries}"
    summary = f"step '{step.id}' failed"
    if attempts > 1:
        summary += f" after {attempts} attempts"
    error = state.errors.get(step.id)
    return f"{summary}: {error}" if error else summary


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How a step's attempts, or its preparation, ended."""

    status: StepStatus  # succeeded, failed, or timed_out
    error: str | None = None
    retryable: bool = False
    outputs: dict[str, JSONValue] = field(default_factory=dict)
    exit_code: int | None = None


@dataclass(slots=True)
class _Track:
    """A step task's progress, which its cancellation handler needs."""

    attempts: int = 0  # attempts started
    open_log: AttemptLog | None = None  # the log of an attempt that isn't finished yet
    final: bool = False  # the final status is being recorded


class _StepFailure(Exception):
    """A step that fails before its first attempt. `message` is the whole step error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _AttemptProcesses:
    """Tracks one attempt's process groups in the registry, and records each in the store.

    Recovery after a crash warns about the recorded groups that still exist. `live` holds
    the groups that the attempt hasn't released, which outlive it when it ends.
    """

    def __init__(
        self, registry: ProcessRegistry, store: AsyncStore, run_id: int, step_id: str, attempt: int
    ) -> None:
        self._registry = registry
        self._store = store
        self._key = (run_id, step_id, attempt)
        self._writes: set[asyncio.Task[None]] = set()
        self.live: set[int] = set()

    def add(self, pgid: int) -> None:
        """Track a process group, and start recording it on the attempt."""
        self._registry.add(pgid)
        self.live.add(pgid)
        task = asyncio.get_running_loop().create_task(self._record(pgid))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    def discard(self, pgid: int) -> None:
        """Stop tracking a process group."""
        self._registry.discard(pgid)
        self.live.discard(pgid)

    async def _record(self, pgid: int) -> None:
        run_id, step_id, attempt = self._key
        try:
            await self._store.write(
                lambda repo: repo.set_attempt_pgid(run_id, step_id, attempt, pgid)
            )
        except Exception:
            _log.warning("run=%d step=%s: couldn't record process group %d", run_id, step_id, pgid)


class _Execution:
    """One call of `Executor.execute`: setup, the step loop, and finalization."""

    _flusher: asyncio.Task[None]  # the log sink's flush loop, which `run` starts

    def __init__(
        self,
        deps: ExecutorDeps,
        run: RunRecord,
        workflow: Workflow,
        handle: RunHandle,
        inputs: JSONObject,
        trigger: JSONObject,
        first_seq: int,
    ) -> None:
        settings = deps.settings
        snapshot, hidden = deps.secrets(), deps.hidden_env()
        self._deps = deps
        self._workflow = workflow
        self._handle = handle
        self._masker = Masker(snapshot.mask_values(hidden))
        self._state = RunState(
            run=run,
            workflow=workflow,
            inputs=inputs,
            trigger=trigger,
            secrets=snapshot.secrets,
            process_env=filtered_environ(deps.environ, hidden),
            statuses={step.id: StepStatus.PENDING for step in workflow.steps},
            project_dir=settings.project_dir,
        )
        self._sink = LogSink(
            deps.store,
            run.id,
            self._masker,
            limits=settings.limits,
            clock=deps.clock,
            first_seq=first_seq,
        )
        self._run_dir = settings.tmp_dir / f"run-{run.id}"
        self._event_path = self._run_dir / "event.json"
        self._default_cwd = _default_cwd(settings.project_dir, workflow.defaults.cwd)
        self._file_roots = settings.file_roots()
        self._protected = settings.protected_paths()
        self._pending = list(workflow.steps)
        self._running: dict[asyncio.Task[None], Step] = {}
        self._timers: list[asyncio.TimerHandle] = []
        self._forced = False
        # Process groups that steps left running in the background, until the run ends.
        self._leftovers = LeftoverGroups(deps.processes)

    async def run(self) -> RunStatus:
        self._flusher = asyncio.create_task(self._sink.run())
        setup_error: str | None = None
        try:
            setup_error = await self._setup()
            if setup_error is None:
                await self._loop()
        except asyncio.CancelledError:
            # The run task is cancelled only when a forced shutdown stops waiting for it.
            if self._handle.reason is None:
                self._handle.request_cancel(CancelReason.SHUTDOWN)
            await asyncio.shield(self._stop_steps())
            await asyncio.shield(self._finalize(setup_error))
            raise
        except Exception:
            await asyncio.shield(self._stop_steps())
            await asyncio.shield(self._close())
            raise
        # Shielded, so a forced shutdown that cancels the run task now can't lose the status.
        return await asyncio.shield(self._finalize(setup_error))

    # Setup

    async def _setup(self) -> str | None:
        """Create the run directory and render workflow `env`; return a setup error or None."""
        run = self._state.run
        _log.info("run=%d workflow=%s started", run.id, run.workflow_id)
        error = self._prepare_files() or self._render_workflow_env()
        if error is not None:
            self._system(f"setup failed: {error}")
            await self._skip_pending(SkipReason.SETUP_FAILED)
        return error

    def _prepare_files(self) -> str | None:
        """Create `run-<id>/` and `shared/` (0700) and the masked `event.json` (0600)."""
        try:
            for directory in (self._run_dir, self._run_dir / "shared"):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            payload = json.dumps(self._masker.mask_json(self._state.trigger))
            descriptor = os.open(self._event_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(descriptor, "w", encoding="utf-8") as file:
                file.write(payload + "\n")
        except OSError as exc:
            return f"cannot prepare the run directory {self._run_dir}: {exc.strerror or exc}"
        return None

    def _render_workflow_env(self) -> str | None:
        """Render workflow `env` in order; each entry sees only the process environment."""
        context = EvalContext(base_values(self._state))
        for name, template in self._workflow.env:
            try:
                self._state.workflow_env[name] = render_str(template, context)
            except EvaluationError as exc:
                return f"in env.{name}: {exc.message}"
        return None

    # The step loop

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        self._timers.append(
            loop.call_later(
                self._workflow.timeout, self._handle.request_cancel, CancelReason.TIMEOUT
            )
        )
        # The waiter exists before each read of the handle, so no change goes unseen.
        changed = asyncio.create_task(self._handle.wait_changed())
        try:
            while True:
                self._follow_handle()
                await self._scan()
                if not self._pending and not self._running:
                    return
                done, _ = await asyncio.wait(
                    {changed, *self._running}, return_when=asyncio.FIRST_COMPLETED
                )
                if changed in done:
                    changed = asyncio.create_task(self._handle.wait_changed())
                self._collect(done)
        finally:
            changed.cancel()

    def _follow_handle(self) -> None:
        """React to the first cancel reason, and to force."""
        handle = self._handle
        if handle.reason is not None and not self._state.cancelling:
            self._begin_cancel(handle.reason)
        if handle.forced and not self._forced:
            self._forced = True
            self._system("forcing cancellation: stopping every running step")
            for task in self._running:
                task.cancel()

    def _begin_cancel(self, reason: CancelReason) -> None:
        """Start cancelling: arm the cleanup timer, and stop steps that no longer qualify.

        Each running step's `if` is evaluated again with `cancelled()` true. Steps whose
        condition is now false, or raises, are cancelled; cleanup steps keep running.
        """
        self._state.cancelling = True
        workflow = self._workflow
        # The cleanup timer bounds the rest of the run, so the run timer stops;
        # otherwise it would force a user-cancelled run before its cleanup budget ran out.
        for timer in self._timers:
            timer.cancel()
        loop = asyncio.get_running_loop()
        self._timers.append(loop.call_later(workflow.cleanup_timeout, self._handle.force))
        if reason is CancelReason.TIMEOUT:
            self._system(f"the run timed out after {format_duration(workflow.timeout)}; cancelling")
        else:
            self._system(f"cancelling the run ({reason.value})")
        for task, step in self._running.items():
            if not self._still_wanted(step):
                task.cancel()

    async def _scan(self) -> None:
        """Settle or start pending steps in list order, repeating while anything changes."""
        progressed = True
        while progressed:
            progressed = False
            for step in list(self._pending):
                if not all(self._state.statuses[need].terminal for need in step.needs):
                    continue
                if not self._handle.forced and len(self._running) >= self._workflow.max_parallel:
                    break
                progressed = True
                await self._settle(step)

    async def _settle(self, step: Step) -> None:
        """Skip, fail, or start a step whose needs are terminal."""
        self._pending.remove(step)
        if self._handle.forced:
            await self._skip(step, SkipReason.RUN_CANCELLED)
            return
        try:
            wanted = self._condition(step)
        except EvaluationError as exc:
            error = f"in steps.{step.id}.if: {exc.message}"
            self._system(f"step failed: {error}", step_id=step.id)
            await self._record_final(step, _Outcome(StepStatus.FAILED, error), 0, allowed=_PENDING)
            return
        if not wanted:
            await self._skip(step, self._skip_reason(step))
            return
        self._state.statuses[step.id] = StepStatus.RUNNING
        self._running[asyncio.create_task(self._run_step(step))] = step

    def _collect(self, done: Iterable[asyncio.Task[Any]]) -> None:
        """Forget finished step tasks. A task that crashed fails the whole execution."""
        for task in done:
            if self._running.pop(task, None) is not None and not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error

    def _condition(self, step: Step) -> bool:
        """Evaluate a step's `if` against its ancestors. Raises EvaluationError."""
        state = self._state
        values = step_values(state, step, env=merge_env(state.process_env, state.workflow_env))
        return evaluate_condition(
            step.condition, EvalContext(values, AncestorStatus(state, step.id))
        )

    def _still_wanted(self, step: Step) -> bool:
        try:
            return self._condition(step)
        except EvaluationError:
            return False

    def _skip_reason(self, step: Step) -> SkipReason:
        """Return why a step whose condition is false is skipped.

        That is `run_cancelled` while the run cancels, `upstream_failed` when a failed
        ancestor made a success-only condition false, and `condition` otherwise.
        """
        if self._state.cancelling or self._handle.forced:
            return SkipReason.RUN_CANCELLED
        if _success_only(step.condition) and AncestorStatus(self._state, step.id).failure():
            return SkipReason.UPSTREAM_FAILED
        return SkipReason.CONDITION

    async def _skip(self, step: Step, reason: SkipReason) -> None:
        self._state.statuses[step.id] = StepStatus.SKIPPED
        await self._update(
            step, StepUpdate(StepStatus.SKIPPED, _PENDING, self._now_ms(), skip_reason=reason)
        )
        _log.info("run=%d step=%s skipped (%s)", self._state.run.id, step.id, reason.value)

    # Step tasks

    async def _run_step(self, step: Step) -> None:
        track = _Track()
        try:
            await self._step(step, track)
        except asyncio.CancelledError:
            if not track.final:
                await asyncio.shield(self._record_cancelled(step, track))
            raise

    async def _step(self, step: Step, track: _Track) -> None:
        """Render once, run the attempts, evaluate outputs, and record the final status."""
        await self._update(step, StepUpdate(StepStatus.RUNNING, _PENDING, self._now_ms()))
        try:
            step_type, env, params = self._prepare(step)
        except _StepFailure as exc:
            self._system(f"step failed: {exc.message}", step_id=step.id)
            outcome = _Outcome(StepStatus.FAILED, exc.message)
        else:
            outcome = await self._attempts(step, track, step_type, env, params)
            outcome = self._bounded(step, self._declared_outputs(step, outcome, env))
        track.final = True
        await asyncio.shield(self._record_final(step, outcome, track.attempts, allowed=_ACTIVE))

    def _prepare(self, step: Step) -> tuple[StepType, dict[str, str], dict[str, JSONValue]]:
        """Render step `env`, then `with`, and validate the parameters. Raises _StepFailure.

        Returns the step type, the environment for `with` and `outputs` (process, workflow,
        and step env), and the validated parameters.
        """
        step_type = self._deps.steps.get(step.uses)
        if step_type is None:
            raise _StepFailure(f"in steps.{step.id}.uses: unknown step type '{step.uses}'")
        state = self._state
        base_env = merge_env(state.process_env, state.workflow_env)
        context = EvalContext(step_values(state, step, env=base_env))
        step_env: dict[str, str] = {}
        for name, template in step.env:
            try:
                step_env[name] = render_str(template, context)
            except EvaluationError as exc:
                raise _StepFailure(f"in steps.{step.id}.env.{name}: {exc.message}") from exc
        env = merge_env(base_env, step_env)
        context = EvalContext(step_values(state, step, env=env))
        rendered = _render_tree(step.params, context, ("steps", step.id, "with"))
        return step_type, env, _validated(step, step_type, rendered)

    async def _attempts(
        self,
        step: Step,
        track: _Track,
        step_type: StepType,
        env: dict[str, str],
        params: dict[str, JSONValue],
    ) -> _Outcome:
        """Run attempts until one succeeds, the error isn't retryable, or none are left.

        No attempt starts once the run is cancelling, including after a backoff wait.
        """
        policy = step.retry
        outcome = await self._attempt(step, track, step_type, env, params, delay=0.0)
        while track.attempts < policy.attempts and self._should_retry(outcome):
            delay = backoff_delay(policy, track.attempts, self._deps.rng)
            await self._move(step, StepStatus.RETRYING, StepStatus.RUNNING)
            following = f"attempt {track.attempts + 1} of {policy.attempts}"
            self._system(
                f"retrying in {format_duration(delay)} ({following})", step.id, track.attempts
            )
            await asyncio.sleep(delay)
            if self._state.cancelling:
                break
            await self._move(step, StepStatus.RUNNING, StepStatus.RETRYING)
            outcome = await self._attempt(step, track, step_type, env, params, delay=delay)
        return outcome

    def _should_retry(self, outcome: _Outcome) -> bool:
        succeeded = outcome.status is StepStatus.SUCCEEDED
        return not succeeded and outcome.retryable and not self._state.cancelling

    async def _attempt(
        self,
        step: Step,
        track: _Track,
        step_type: StepType,
        env: dict[str, str],
        params: dict[str, JSONValue],
        *,
        delay: float,
    ) -> _Outcome:
        """Run one attempt, then commit its logs and its result, in that order."""
        run_id, number = self._state.run.id, track.attempts + 1
        log = self._sink.attempt_log(step.id, number)
        # Recorded before the write, so a cancel that arrives during it still closes the attempt.
        track.attempts, track.open_log = number, log
        started = self._now_ms()
        await self._write(
            lambda repo: repo.start_attempt(
                run_id, step.id, number, at_ms=started, delay_before_ms=round(delay * 1000)
            )
        )
        outcome = await self._run_attempt(step, step_type, env, params, number, log)
        if log.refused_mask:
            # The outputs may hold the value that the run couldn't mask, so they're dropped,
            # and a retry would only refuse it again.
            outcome = _Outcome(StepStatus.FAILED, _TOO_MANY_MASKS, exit_code=outcome.exit_code)
        if outcome.status is StepStatus.FAILED:
            log.system(f"attempt {number} failed: {outcome.error}")
        elif outcome.status is StepStatus.TIMED_OUT:
            log.system(f"attempt {number} {outcome.error}")
        await self._sink.flush()
        status, finished = _ATTEMPT_STATUS[outcome.status], self._now_ms()
        error = self._mask(outcome.error)
        await self._write(
            lambda repo: _close_attempt(
                repo, (run_id, step.id, number), log, status, finished, outcome.exit_code, error
            )
        )
        track.open_log = None
        return outcome

    async def _run_attempt(
        self,
        step: Step,
        step_type: StepType,
        env: dict[str, str],
        params: dict[str, JSONValue],
        number: int,
        log: AttemptLog,
    ) -> _Outcome:
        """Run the step type under the step timeout, and classify how the attempt ended.

        Process groups that outlive an attempt stay tracked until the run ends. A cancelled
        attempt has none: its step type stops its processes, even after a forced cancel.
        """
        deps, run_id = self._deps, self._state.run.id
        processes = _AttemptProcesses(deps.processes, deps.store, run_id, step.id, number)
        outcome = await self._classify(step, step_type, env, params, number, log, processes)
        for pgid in processes.live:
            self._leftovers.adopt(pgid)
        return outcome

    async def _classify(
        self,
        step: Step,
        step_type: StepType,
        env: dict[str, str],
        params: dict[str, JSONValue],
        number: int,
        log: AttemptLog,
        processes: _AttemptProcesses,
    ) -> _Outcome:
        """Run the step type under the step timeout, and return how the attempt ended."""
        timeout = asyncio.timeout(step.timeout)
        try:
            # The attempt's files are created here, once the attempt is recorded, so that a
            # file-system error is a failed attempt with a traceback, not an unrecorded crash.
            runtime = self._runtime(step, number, env, log, processes)
            async with timeout:
                result = await step_type.run(params, runtime)
        except TimeoutError as exc:
            if timeout.expired():
                error = f"timed out after {format_duration(step.timeout)}"
                return _Outcome(StepStatus.TIMED_OUT, error, retryable=_interruptible(step_type))
            return _Outcome(StepStatus.FAILED, _describe(exc), retryable=True)
        except StepError as exc:
            return _Outcome(
                StepStatus.FAILED,
                exc.message,
                retryable=exc.retryable,
                outputs=dict(exc.outputs),
                exit_code=exc.exit_code,
            )
        except Exception as exc:
            log.system("".join(traceback.format_exception(exc)))
            return _Outcome(StepStatus.FAILED, f"internal error: {_describe(exc)}")
        return _Outcome(
            StepStatus.SUCCEEDED, outputs=dict(result.outputs), exit_code=result.exit_code
        )

    def _runtime(
        self,
        step: Step,
        number: int,
        env: dict[str, str],
        log: AttemptLog,
        processes: _AttemptProcesses,
    ) -> StepRuntime:
        """Create the attempt directory (0700) and its empty output file (0600)."""
        deps, settings, run_id = self._deps, self._deps.settings, self._state.run.id
        attempt_dir = self._run_dir / f"{step.id}-{number}"
        attempt_dir.mkdir(mode=0o700)
        output_path = attempt_dir / "output"
        os.close(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        variables = treadle_env(
            self._state,
            step,
            number,
            run_dir=self._run_dir,
            attempt_dir=attempt_dir,
            output_path=output_path,
            event_path=self._event_path,
            project_dir=settings.project_dir,
        )
        return StepRuntime(
            run_id=run_id,
            workflow_id=self._workflow.id,
            step_id=step.id,
            attempt=number,
            env=merge_env(env, variables),
            project_dir=settings.project_dir,
            attempt_dir=attempt_dir,
            output_path=output_path,
            default_shell=self._workflow.defaults.shell,
            default_cwd=self._default_cwd,
            file_roots=self._file_roots,
            protected_paths=self._protected,
            state_dir=settings.state_dir,
            kill_grace=settings.runs.kill_grace,
            limits=settings.limits,
            log=log,
            processes=processes,
            http=deps.http,
        )

    def _declared_outputs(self, step: Step, outcome: _Outcome, env: dict[str, str]) -> _Outcome:
        """Evaluate declared outputs over a success, with `result` as the raw outputs."""
        if outcome.status is not StepStatus.SUCCEEDED or not step.outputs:
            return outcome
        context = EvalContext(step_values(self._state, step, env=env, result=outcome.outputs))
        merged = dict(outcome.outputs)
        for name, template in step.outputs:
            try:
                merged[name] = render(template, context)
            except EvaluationError as exc:
                error = f"in steps.{step.id}.outputs.{name}: {exc.message}"
                self._system(f"step failed: {error}", step_id=step.id)
                return dataclasses.replace(outcome, status=StepStatus.FAILED, error=error)
        return dataclasses.replace(outcome, outputs=merged)

    def _bounded(self, step: Step, outcome: _Outcome) -> _Outcome:
        """Fail a success whose serialized outputs pass `limits.outputs_bytes`."""
        limit = self._deps.settings.limits.outputs_bytes
        if _json_size(outcome.outputs) <= limit:
            return outcome
        error = f"outputs exceed {format_size(limit)}"
        if outcome.status is StepStatus.SUCCEEDED:
            self._system(f"step failed: {error}", step_id=step.id)
            return dataclasses.replace(outcome, status=StepStatus.FAILED, error=error, outputs={})
        # A failed step keeps the partial outputs of its last StepError, but
        # never more than the cap; larger ones are dropped, as for a success.
        self._system(f"dropped the outputs: {error}", step_id=step.id)
        return dataclasses.replace(outcome, outputs={})

    async def _record_final(
        self, step: Step, outcome: _Outcome, attempts: int, *, allowed: frozenset[StepStatus]
    ) -> None:
        """Keep the unmasked result for downstream steps, then commit the logs and status."""
        state, status = self._state, outcome.status
        tolerated = step.continue_on_error and status in _FAILED
        state.statuses[step.id] = status
        state.outputs[step.id] = outcome.outputs
        state.attempts[step.id] = attempts
        if tolerated:
            state.tolerated.add(step.id)
        if outcome.error is not None:
            state.errors[step.id] = outcome.error
        await self._sink.flush()
        update = StepUpdate(
            status,
            allowed,
            self._now_ms(),
            attempts=attempts,
            tolerated=tolerated,
            outputs=cast(JSONObject, self._masker.mask_json(outcome.outputs)),
            error=self._mask(outcome.error),
        )
        await self._update(step, update)
        note = " (tolerated)" if tolerated else ""
        _log.info("run=%d step=%s %s%s", state.run.id, step.id, status.value, note)

    async def _record_cancelled(self, step: Step, track: _Track) -> None:
        """Commit a cancelled step and its open attempt, if any, after flushing its logs."""
        run_id, number, log = self._state.run.id, track.attempts, track.open_log
        self._state.statuses[step.id] = StepStatus.CANCELLED
        self._state.attempts[step.id] = number
        await self._sink.flush()
        at = self._now_ms()

        def commit(repo: Repository) -> bool:
            if log is not None:
                key = (run_id, step.id, number)
                _close_attempt(repo, key, log, AttemptStatus.CANCELLED, at, None, None)
            update = StepUpdate(StepStatus.CANCELLED, _UNFINISHED, at, attempts=number)
            return repo.update_step(run_id, step.id, update)

        await self._write(commit)
        _log.info("run=%d step=%s cancelled", run_id, step.id)

    # Finalization

    async def _stop_steps(self) -> None:
        """Cancel every step task and wait for each; each records its own cancellation.

        This runs in a shielded task scheduled after every step task, so each step task has
        started, and its handler records it, before it's cancelled.
        """
        tasks = dict(self._running)
        self._running.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        for task, step in tasks.items():
            if not task.cancelled() and task.exception() is not None:
                _log.debug("run=%d step=%s: the step task failed", self._state.run.id, step.id)

    async def _finalize(self, setup_error: str | None) -> RunStatus:
        """Conclude the run, close the log, commit the terminal status, and clean up."""
        handle, state, run_id = self._handle, self._state, self._state.run.id
        handle.finalizing = True
        self._cancel_timers()
        await self._skip_pending(SkipReason.RUN_CANCELLED)  # what an aborted loop left
        await self._stop_leftovers()
        error: str | None
        reason: RunReason | None
        if setup_error is not None:
            status, error, reason = RunStatus.FAILED, setup_error, RunReason.SETUP_FAILED
        else:
            status, error = conclude(state, handle.reason)
            reason = None if handle.reason is None else RunReason(handle.reason.value)
        await self._close_sink()
        at, masked = self._now_ms(), self._mask(error)
        applied = await self._write(
            lambda repo: repo.finish_run(run_id, status, at_ms=at, reason=reason, error=masked)
        )
        await self._remove_run_dir()
        if not applied:
            _log.debug("run=%d: another writer already finished the run", run_id)
            stored = await self._deps.store.read(lambda repo: repo.get_run(run_id))
            status = stored.status if stored is not None else status
        _log.info("run=%d %s", run_id, status.value)
        return status

    async def _skip_pending(self, reason: SkipReason) -> None:
        """Skip every step that is still pending, in one write."""
        if not self._pending:
            return
        steps = [step.id for step in self._pending]
        self._pending.clear()
        for step_id in steps:
            self._state.statuses[step_id] = StepStatus.SKIPPED
        run_id = self._state.run.id
        update = StepUpdate(StepStatus.SKIPPED, _PENDING, self._now_ms(), skip_reason=reason)
        await self._write(lambda repo: [repo.update_step(run_id, s, update) for s in steps])

    async def _close(self) -> None:
        """Release what an execution that failed unexpectedly holds; the manager ends the run."""
        self._handle.finalizing = True
        self._cancel_timers()
        await self._stop_leftovers()
        try:
            await self._close_sink()
        except Exception:
            _log.exception("run=%d: couldn't write the last log lines", self._state.run.id)
        await self._remove_run_dir()

    async def _stop_leftovers(self) -> None:
        """Stop the processes that steps left running in the background."""
        stopped = await self._leftovers.stop(grace=self._deps.settings.runs.kill_grace)
        if stopped:
            attempts = "attempt" if stopped == 1 else "attempts"
            self._system(f"stopped the background processes of {stopped} step {attempts}")

    async def _close_sink(self) -> None:
        self._flusher.cancel()
        await asyncio.wait({self._flusher})
        await self._sink.aclose()

    def _cancel_timers(self) -> None:
        for timer in self._timers:
            timer.cancel()

    async def _remove_run_dir(self) -> None:
        await asyncio.to_thread(shutil.rmtree, self._run_dir, ignore_errors=True)

    # Helpers

    async def _move(self, step: Step, status: StepStatus, previous: StepStatus) -> None:
        """Move a step between running and retrying."""
        self._state.statuses[step.id] = status
        await self._update(step, StepUpdate(status, frozenset({previous}), self._now_ms()))

    async def _update(self, step: Step, update: StepUpdate) -> None:
        run_id = self._state.run.id
        if not await self._write(lambda repo: repo.update_step(run_id, step.id, update)):
            _log.debug("run=%d step=%s: stale move to %s", run_id, step.id, update.status.value)

    async def _write(self, fn: Callable[[Repository], _T]) -> _T:
        return await self._deps.store.write(fn)

    def _system(self, text: str, step_id: str | None = None, attempt: int | None = None) -> None:
        self._sink.system(text, step_id=step_id, attempt=attempt)

    def _mask(self, text: str | None) -> str | None:
        return None if text is None else self._masker.mask(text)

    def _now_ms(self) -> int:
        return to_ms(self._deps.clock.now())


def _close_attempt(
    repo: Repository,
    key: tuple[int, str, int],
    log: AttemptLog,
    status: AttemptStatus,
    at_ms: int,
    exit_code: int | None,
    error: str | None,
) -> bool:
    """Finish the attempt `key` (run ID, step ID, number) with its log size and truncation."""
    run_id, step_id, number = key
    return repo.finish_attempt(
        run_id,
        step_id,
        number,
        status=status,
        at_ms=at_ms,
        exit_code=exit_code,
        error=error,
        log_bytes=log.bytes_written,
        log_truncated=log.truncated,
    )


def _default_cwd(project_dir: Path, cwd: str) -> Path:
    """Return `defaults.cwd` with "~" expanded, relative to the project directory.

    A "~user" without a known home directory stays as written, so each step that uses the
    default fails and says why, and the other steps still run.
    """
    try:
        return project_dir / Path(cwd).expanduser()
    except RuntimeError:
        return Path(cwd)


def _success_only(condition: Condition) -> bool:
    """True when a condition calls no status function other than `success()`."""
    # `upstream_failed` is the reason both for a condition without a status function and for
    # the default `if`, which the loader stores as `success()`. Both hold when `success()`
    # alone doesn't count, as the implicit `success() &&` of a bare expression doesn't.
    return all(
        call.name == "success"
        for call in calls(condition.expression)
        if call.name in STATUS_FUNCTIONS
    )


def _render_tree(tree: TemplateTree, context: EvalContext, path: YamlPath) -> JSONValue:
    """Render `with` like `render_tree`, but name the path of a template that fails.

    Raises _StepFailure, such as "in steps.deploy.with.url: cannot compare null with number".
    """
    if isinstance(tree, Template):
        try:
            return render(tree, context)
        except EvaluationError as exc:
            raise _StepFailure(f"in {format_path(path)}: {exc.message}") from exc
    if isinstance(tree, list):
        return [_render_tree(item, context, (*path, index)) for index, item in enumerate(tree)]
    if isinstance(tree, dict):
        return {key: _render_tree(item, context, (*path, key)) for key, item in tree.items()}
    return tree


def _validated(step: Step, step_type: StepType, rendered: JSONValue) -> dict[str, JSONValue]:
    """Validate rendered parameters with the step type's schema, then its `check`.

    Raises _StepFailure naming every problem, each at its path.
    """
    path: YamlPath = ("steps", step.id, "with")
    normalized, issues = validate(step_type.schema, rendered, path=path)
    params = cast(dict[str, JSONValue], normalized)
    if not issues:
        issues = [
            issue for issue in step_type.check(params, path) if issue.severity is Severity.ERROR
        ]
    if issues:
        problems = (f"in {format_path(issue.path or path)}: {issue.message}" for issue in issues)
        raise _StepFailure("; ".join(problems))
    return params


def _json_size(value: JSONValue) -> int:
    """Return the UTF-8 size of a value in the compact JSON form that the store writes."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8", "surrogatepass"))


def _interruptible(step_type: StepType) -> bool:
    """True unless the step type sets `interruptible = False`; see StepType."""
    return getattr(step_type, "interruptible", True) is not False


def _describe(exc: BaseException) -> str:
    """Return "<type>: <message>", or the type alone when the message is empty."""
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
