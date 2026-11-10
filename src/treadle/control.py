"""The control-request channel: how CLI processes start and cancel runs in the lock holder."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Final

from treadle.clock import Clock, to_ms
from treadle.errors import InputError, TreadleError
from treadle.models import CancelReason, ControlRequest, Origin, RunRequest, TriggerKind
from treadle.protocols import RunControl
from treadle.serialize import _error_status, error_json
from treadle.store.async_store import AsyncStore
from treadle.types import JSONObject, JSONValue

CONTROL_EXPIRY: Final = 60.0

_log = logging.getLogger(__name__)


class _BadPayload(ValueError):
    """A control request whose payload doesn't have the documented shape."""


class ControlPoller:
    """Claims control requests from the store and answers them through `control`.

    With `accept_runs=False` (a foreground `treadle run`), run requests get a `busy` error.
    Cancel requests are always handled. Requests older than `CONTROL_EXPIRY` get an
    `expired` error, because the CLI that sent them has stopped waiting.
    """

    def __init__(
        self,
        *,
        store: AsyncStore,
        control: RunControl,
        clock: Clock,
        accept_runs: bool,
        interval: float = 0.5,
    ) -> None:
        self._store = store
        self._control = control
        self._clock = clock
        self._accept_runs = accept_runs
        self._interval = interval
        self._kept: dict[int, JSONObject] = {}  # results not stored yet, by request ID

    async def process_once(self) -> int:
        """Handle every pending request and return how many were handled.

        Requests are claimed one at a time in ID order, so a failure strands at most the
        request in hand, and each result is committed as soon as it's known. A result that
        can't be stored is kept and stored first on the next pass. A cancellation waits until
        the request in hand has its result stored, so a run it created is never unreported.
        """
        await self._store_kept()
        handled = 0
        while (request := await self._claim()) is not None:
            await self._handle(request)
            handled += 1
        return handled

    async def run(self) -> None:
        """Process requests every `interval` seconds until cancelled."""
        while True:
            try:
                await self.process_once()
            except Exception:
                _log.exception("handling control requests failed; trying again")
            await asyncio.sleep(self._interval)

    async def _handle(self, request: ControlRequest) -> None:
        """Answer one claimed request and store its result, even when cancelled meanwhile.

        The run manager acts on a request whether or not its caller still waits, so the
        answer and its result run in a task of their own that a cancellation doesn't stop.
        """
        handling = asyncio.create_task(self._answer_and_store(request))
        try:
            await asyncio.shield(handling)
        except asyncio.CancelledError:
            await handling
            raise

    async def _answer_and_store(self, request: ControlRequest) -> None:
        """Answer one request, and keep its result for the next pass when storing it fails."""
        result = await self._answer(request)
        try:
            await self._complete(request.id, result)
        except Exception as exc:
            _log.warning(
                "control request %d: storing the result failed (%s); trying again",
                request.id,
                exc,
                exc_info=not isinstance(exc, TreadleError),
            )
            self._kept[request.id] = result

    async def _store_kept(self) -> None:
        """Store the results that a failed write kept; a failure ends the pass."""
        for request_id, result in list(self._kept.items()):
            await self._complete(request_id, result)
            del self._kept[request_id]

    async def _answer(self, request: ControlRequest) -> JSONObject:
        """Return the result of one request: its outcome, or an error in the API shape."""
        if self._clock.now() - request.created_at > timedelta(seconds=CONTROL_EXPIRY):
            return error_json("expired", "no Treadle process handled the request in time")
        try:
            if request.kind == "run":
                return await self._run(request.payload)
            return await self._cancel(request.payload)
        except _BadPayload as exc:
            return error_json("bad_request", str(exc))
        except TreadleError as exc:
            return _refusal(exc)
        except Exception as exc:
            _log.exception("control request %d failed", request.id)
            return error_json("internal", f"internal error: {type(exc).__name__}")

    async def _run(self, payload: JSONObject) -> JSONObject:
        """Start a run, `{"workflow", "inputs", "origin"}`, or a re-run, `{"rerun_of"}`."""
        if not self._accept_runs:
            message = (
                "a foreground `treadle run` holds the state directory and starts no other "
                "runs; start `treadle serve` to run workflows concurrently"
            )
            return error_json("busy", message)
        origin = _origin(payload)
        if "rerun_of" in payload:
            submission = await self._control.rerun(_integer(payload, "rerun_of"))
        else:
            workflow = payload.get("workflow")
            inputs = payload.get("inputs", {})
            if not isinstance(workflow, str):
                raise _BadPayload("'workflow' must be a workflow ID")
            if not isinstance(inputs, dict):
                raise _BadPayload("'inputs' must be an object")
            request = RunRequest(
                workflow_id=workflow,
                kind=TriggerKind.MANUAL,
                trigger={"kind": TriggerKind.MANUAL.value, "origin": origin.value},
                inputs=inputs,
                # The CLI sends `-i KEY=VALUE` text, so the string rules apply.
                inputs_are_strings=True,
            )
            submission = await self._control.submit(request)
        run_id = None if submission.run is None else submission.run.id
        return {"run_id": run_id, "outcome": submission.outcome.value}

    async def _cancel(self, payload: JSONObject) -> JSONObject:
        """Cancel a run, `{"run_id", "force"}`, for the user."""
        run_id = _integer(payload, "run_id")
        force = payload.get("force", False)
        if not isinstance(force, bool):
            raise _BadPayload("'force' must be true or false")
        outcome = await self._control.cancel(run_id, CancelReason.USER, force=force)
        return {
            "run_id": outcome.run_id,
            "accepted": outcome.accepted,
            "status": outcome.status.value,
            "forced": outcome.forced,
        }

    async def _claim(self) -> ControlRequest | None:
        """Claim the oldest pending request, or return None when none is pending."""
        now_ms = to_ms(self._clock.now())
        claimed = await self._store.write(lambda repo: repo.claim_controls(at_ms=now_ms, limit=1))
        return claimed[0] if claimed else None

    async def _complete(self, request_id: int, result: JSONObject) -> None:
        at_ms = to_ms(self._clock.now())
        await self._store.write(lambda repo: repo.complete_control(request_id, result, at_ms=at_ms))


def _refusal(exc: TreadleError) -> JSONObject:
    """Map a Treadle error to the API error shape; input problems carry their details."""
    # Only `invalid_inputs` carries details. The poller has no workflow paths to render issues
    # with, so the CLI gets `workflow_invalid` with the message alone.
    mapped = _error_status(exc)
    code = "internal" if mapped is None else mapped[1]
    details: JSONValue = dict[str, JSONValue](exc.errors) if isinstance(exc, InputError) else None
    return error_json(code, str(exc), details)


def _integer(payload: JSONObject, name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _BadPayload(f"'{name}' must be a run ID")
    return value


def _origin(payload: JSONObject) -> Origin:
    value = payload.get("origin", Origin.CLI.value)
    choices = [origin.value for origin in Origin]
    if not isinstance(value, str) or value not in choices:
        raise _BadPayload(f"'origin' must be one of: {', '.join(choices)}")
    return Origin(value)
