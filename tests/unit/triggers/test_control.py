from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal, TypeVar

import pytest
from tests.support.clock import FakeClock
from tests.support.fakes import FakeRunControl
from tests.unit.triggers.helpers import running

from treadle.clock import to_ms
from treadle.control import CONTROL_EXPIRY, ControlPoller
from treadle.errors import (
    BusyError,
    ConflictError,
    InputError,
    NotFoundError,
    ShuttingDownError,
    StoreError,
    WorkflowInvalidError,
)
from treadle.models import (
    CancelOutcome,
    CancelReason,
    RunRequest,
    RunStatus,
    Submission,
    SubmitOutcome,
    TriggerKind,
)
from treadle.store.async_store import AsyncStore
from treadle.store.repo import Repository
from treadle.types import JSONObject

T = TypeVar("T")


class Channel:
    """The CLI side of the channel: enqueue requests and read their results."""

    def __init__(self, store: AsyncStore, clock: FakeClock) -> None:
        self.store = store
        self.clock = clock

    async def send(
        self, kind: Literal["run", "cancel"], payload: JSONObject, *, age: float = 0.0
    ) -> int:
        at_ms = to_ms(self.clock.now() - timedelta(seconds=age))
        return await self.store.write(lambda repo: repo.enqueue_control(kind, payload, at_ms=at_ms))

    async def result(self, request_id: int) -> JSONObject | None:
        return await self.store.read(lambda repo: repo.control_result(request_id))


@pytest.fixture
def channel(store: AsyncStore, clock: FakeClock) -> Channel:
    return Channel(store, clock)


def poller_for(
    store: Any, clock: FakeClock, control: FakeRunControl, *, accept_runs: bool = True
) -> ControlPoller:
    return ControlPoller(store=store, control=control, clock=clock, accept_runs=accept_runs)


def error(code: str, message: str, details: Any = None) -> JSONObject:
    return {"error": {"code": code, "message": message, "details": details}}


class TestRunRequests:
    async def test_a_run_request_submits_a_manual_run_with_string_inputs(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        request_id = await channel.send(
            "run", {"workflow": "hello", "inputs": {"name": "Ada"}, "origin": "cli"}
        )

        assert await poller_for(store, clock, control).process_once() == 1

        assert await channel.result(request_id) == {"run_id": 1, "outcome": "queued"}
        assert control.requests == [
            RunRequest(
                workflow_id="hello",
                kind=TriggerKind.MANUAL,
                trigger={"kind": "manual", "origin": "cli"},
                inputs={"name": "Ada"},
                inputs_are_strings=True,
            )
        ]

    async def test_inputs_and_origin_are_optional(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        await channel.send("run", {"workflow": "hello"})
        await poller_for(store, clock, control).process_once()
        [request] = control.requests
        assert (request.inputs, request.trigger["origin"]) == ({}, "cli")

    @pytest.mark.parametrize("origin", ["api", "dashboard"])
    async def test_the_origin_reaches_the_trigger_payload(
        self, store: AsyncStore, clock: FakeClock, channel: Channel, origin: str
    ) -> None:
        control = FakeRunControl()
        await channel.send("run", {"workflow": "hello", "origin": origin})
        await poller_for(store, clock, control).process_once()
        assert control.requests[0].trigger == {"kind": "manual", "origin": origin}

    async def test_a_rerun_request_reruns_the_run(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        request_id = await channel.send("run", {"rerun_of": 42, "origin": "cli"})

        await poller_for(store, clock, control).process_once()

        assert control.reruns == [42]
        assert await channel.result(request_id) == {"run_id": 1, "outcome": "queued"}

    async def test_a_skipped_run_reports_its_outcome(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl(outcome=SubmitOutcome.SKIPPED)
        request_id = await channel.send("run", {"workflow": "hello"})
        await poller_for(store, clock, control).process_once()
        assert await channel.result(request_id) == {"run_id": 1, "outcome": "skipped"}

    async def test_a_submission_without_a_run_has_a_null_run_id(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        def no_run(request: RunRequest) -> Submission:
            return Submission(SubmitOutcome.DUPLICATE, None)

        request_id = await channel.send("run", {"workflow": "hello"})
        await poller_for(store, clock, FakeRunControl(handler=no_run)).process_once()
        assert await channel.result(request_id) == {"run_id": None, "outcome": "duplicate"}

    async def test_a_foreground_run_refuses_run_requests_as_busy(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        run_id = await channel.send("run", {"workflow": "hello"})
        rerun_id = await channel.send("run", {"rerun_of": 1})

        await poller_for(store, clock, control, accept_runs=False).process_once()

        for request_id in (run_id, rerun_id):
            result: Any = await channel.result(request_id)
            assert result["error"]["code"] == "busy"
            assert "start `treadle serve`" in result["error"]["message"]
        assert (control.requests, control.reruns) == ([], [])


class TestCancelRequests:
    @pytest.mark.parametrize("accept_runs", [True, False])
    @pytest.mark.parametrize("force", [False, True])
    async def test_a_cancel_request_cancels_the_run_for_the_user(
        self,
        store: AsyncStore,
        clock: FakeClock,
        channel: Channel,
        accept_runs: bool,
        force: bool,
    ) -> None:
        control = FakeRunControl()
        request_id = await channel.send("cancel", {"run_id": 42, "force": force})

        await poller_for(store, clock, control, accept_runs=accept_runs).process_once()

        assert control.cancels == [(42, CancelReason.USER, force)]
        assert await channel.result(request_id) == {
            "run_id": 42,
            "accepted": True,
            "status": "running",
            "forced": force,
        }

    async def test_a_refused_cancel_reports_the_run_status(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        finished = CancelOutcome(42, RunStatus.SUCCEEDED, accepted=False, forced=False)
        request_id = await channel.send("cancel", {"run_id": 42})
        await poller_for(store, clock, FakeRunControl(cancel_outcome=finished)).process_once()
        assert await channel.result(request_id) == {
            "run_id": 42,
            "accepted": False,
            "status": "succeeded",
            "forced": False,
        }


class TestErrors:
    async def test_a_request_older_than_the_expiry_expires_unhandled(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        old = await channel.send("cancel", {"run_id": 1}, age=CONTROL_EXPIRY + 1)
        fresh = await channel.send("cancel", {"run_id": 2}, age=CONTROL_EXPIRY)

        assert await poller_for(store, clock, control).process_once() == 2

        assert await channel.result(old) == error(
            "expired", "no Treadle process handled the request in time"
        )
        assert control.cancels == [(2, CancelReason.USER, False)]
        assert (await channel.result(fresh) or {}).get("accepted") is True

    @pytest.mark.parametrize(
        ("raised", "code", "details"),
        [
            (NotFoundError("workflow 'nope' doesn't exist"), "not_found", None),
            (
                InputError({"count": "must be an integer"}),
                "invalid_inputs",
                {"count": "must be an integer"},
            ),
            (WorkflowInvalidError("hello", []), "workflow_invalid", None),
            (ConflictError("run 3 is still running"), "conflict", None),
            (ShuttingDownError("Treadle is shutting down"), "shutting_down", None),
            (BusyError("the run manager is busy"), "busy", None),
            (StoreError("the store is closed"), "internal", None),
        ],
    )
    async def test_refusals_map_to_api_error_codes(
        self,
        store: AsyncStore,
        clock: FakeClock,
        channel: Channel,
        raised: Exception,
        code: str,
        details: Any,
    ) -> None:
        request_id = await channel.send("run", {"workflow": "hello"})
        await poller_for(store, clock, FakeRunControl(error=raised)).process_once()
        assert await channel.result(request_id) == error(code, str(raised), details)

    async def test_an_unexpected_failure_is_an_internal_error(
        self,
        store: AsyncStore,
        clock: FakeClock,
        channel: Channel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        request_id = await channel.send("cancel", {"run_id": 1})
        control = FakeRunControl(error=RuntimeError("boom"))

        await poller_for(store, clock, control).process_once()

        assert await channel.result(request_id) == error("internal", "internal error: RuntimeError")
        assert f"control request {request_id} failed" in caplog.text

    @pytest.mark.parametrize(
        ("kind", "payload", "message"),
        [
            ("run", {}, "'workflow' must be a workflow ID"),
            ("run", {"workflow": 7}, "'workflow' must be a workflow ID"),
            ("run", {"workflow": "hello", "inputs": ["a"]}, "'inputs' must be an object"),
            (
                "run",
                {"workflow": "hello", "origin": "cron"},
                "'origin' must be one of: cli, api, dashboard",
            ),
            (
                "run",
                {"workflow": "hello", "origin": 1},
                "'origin' must be one of: cli, api, dashboard",
            ),
            ("run", {"rerun_of": "42"}, "'rerun_of' must be a run ID"),
            ("run", {"rerun_of": True}, "'rerun_of' must be a run ID"),
            ("cancel", {}, "'run_id' must be a run ID"),
            ("cancel", {"run_id": 1, "force": "yes"}, "'force' must be true or false"),
        ],
    )
    async def test_a_malformed_payload_is_a_bad_request(
        self,
        store: AsyncStore,
        clock: FakeClock,
        channel: Channel,
        kind: Literal["run", "cancel"],
        payload: JSONObject,
        message: str,
    ) -> None:
        control = FakeRunControl()
        request_id = await channel.send(kind, payload)
        await poller_for(store, clock, control).process_once()
        assert await channel.result(request_id) == error("bad_request", message)
        assert (control.requests, control.cancels, control.reruns) == ([], [], [])


class TestPolling:
    async def test_requests_are_handled_in_order_and_consumed_once(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        for run_id in (3, 1, 2):
            await channel.send("cancel", {"run_id": run_id})
        poller = poller_for(store, clock, control)

        assert await poller.process_once() == 3
        assert await poller.process_once() == 0
        assert [run_id for run_id, _, _ in control.cancels] == [3, 1, 2]

    async def test_every_pending_request_is_handled_in_one_pass(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        for run_id in range(120):
            await channel.send("cancel", {"run_id": run_id})

        assert await poller_for(store, clock, control).process_once() == 120
        assert len(control.cancels) == 120

    async def test_the_loop_answers_requests_as_they_arrive(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        poller = poller_for(store, clock, control)

        async with running(poller.run):
            await asyncio.sleep(1)
            request_id = await channel.send("run", {"workflow": "hello"})
            await asyncio.sleep(0.5)
            assert await channel.result(request_id) == {"run_id": 1, "outcome": "queued"}

    async def test_the_loop_survives_a_failing_pass(
        self,
        store: AsyncStore,
        clock: FakeClock,
        channel: Channel,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class FlakyStore:
            def __init__(self) -> None:
                self.failed = False

            async def write(self, fn: Callable[[Repository], T], **kwargs: Any) -> T:
                if not self.failed:
                    self.failed = True
                    raise StoreError("database is locked")
                return await store.write(fn, **kwargs)

        control = FakeRunControl()
        poller = poller_for(FlakyStore(), clock, control)
        request_id = await channel.send("cancel", {"run_id": 5})

        async with running(poller.run):
            await asyncio.sleep(1.2)

        assert "handling control requests failed; trying again" in caplog.text
        assert (await channel.result(request_id) or {}).get("run_id") == 5

    async def test_the_poll_interval_is_configurable(
        self, store: AsyncStore, clock: FakeClock, channel: Channel
    ) -> None:
        control = FakeRunControl()
        poller = ControlPoller(
            store=store, control=control, clock=clock, accept_runs=True, interval=5.0
        )

        async with running(poller.run):
            await asyncio.sleep(0.1)
            request_id = await channel.send("cancel", {"run_id": 9})
            await asyncio.sleep(4.8)
            assert await channel.result(request_id) is None
            await asyncio.sleep(0.2)
            assert await channel.result(request_id) is not None


def test_the_expiry_matches_the_design() -> None:
    assert CONTROL_EXPIRY == 60.0
