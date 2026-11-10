from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from tests.support.builders import make_run, make_webhook, make_workflow
from tests.support.clock import FakeClock
from tests.support.fakes import FakeSecrets, FakeSubmitter
from tests.unit.triggers.helpers import StoreSubmitter, hook_request, runs_of, sign

from treadle.errors import NotFoundError, ShuttingDownError, WorkflowInvalidError
from treadle.models import (
    Delivery,
    HookRequest,
    HookResponse,
    RunReason,
    RunRequest,
    RunStatus,
    SignatureMode,
    Submission,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    WebhookTrigger,
)
from treadle.store.async_store import AsyncStore
from treadle.triggers.webhook import WebhookRouter, delivery_digest

SECRET = "a-sixteen-char-secret"
BODY = b'{"ref": "refs/heads/main"}'


def router_for(
    *hooks: WebhookTrigger,
    submitter: FakeSubmitter | None = None,
    env: dict[str, str] | None = None,
    clock: FakeClock | None = None,
) -> tuple[WebhookRouter, FakeSubmitter, FakeSecrets]:
    chosen = submitter or FakeSubmitter()
    secrets = FakeSecrets(env={"HOOK_SECRET": SECRET} if env is None else env)
    router = WebhookRouter(submitter=chosen, secrets=lambda: secrets, clock=clock or FakeClock())
    router.update("wf", make_workflow(triggers=hooks))
    return router, chosen, secrets


def signed(body: bytes = BODY, *, secret: str = SECRET, **headers: str) -> HookRequest:
    return hook_request(body, headers={"x-hub-signature-256": sign(secret, body), **headers})


def code(response: HookResponse) -> Any:
    error: Any = response.body["error"]
    return error["code"]


class TestPrecheck:
    def test_a_known_hook_returns_its_body_limit(self) -> None:
        router, _, _ = router_for(make_webhook("push", max_body=4096))
        assert router.precheck("push") == 4096

    def test_an_unknown_hook_is_not_found(self) -> None:
        router, _, _ = router_for(make_webhook("push"))
        response = router.precheck("pull")
        assert isinstance(response, HookResponse)
        assert response.status == 404
        assert response.body["error"] == {
            "code": "not_found",
            "message": "no webhook is named 'pull'",
            "details": None,
        }

    def test_a_draining_router_answers_503_with_retry_after(self) -> None:
        router, _, _ = router_for(make_webhook("push"))
        router.set_draining()
        response = router.precheck("push")
        assert isinstance(response, HookResponse)
        assert (response.status, response.headers) == (503, {"Retry-After": "30"})
        assert code(response) == "shutting_down"

    @pytest.mark.parametrize("env", [{}, {"HOOK_SECRET": "x" * 15}], ids=["unset", "short"])
    def test_a_hook_without_a_usable_secret_is_disabled(self, env: dict[str, str]) -> None:
        router, _, _ = router_for(make_webhook("push"), env=env)
        response = router.precheck("push")
        assert isinstance(response, HookResponse)
        assert response.status == 503
        assert code(response) == "hook_disabled"
        assert "HOOK_SECRET" not in str(response.body)

    def test_a_secret_of_exactly_sixteen_characters_is_enough(self) -> None:
        router, _, _ = router_for(make_webhook("push"), env={"HOOK_SECRET": "x" * 16})
        assert router.precheck("push") == 1 << 20


class TestHandle:
    async def test_a_signed_delivery_submits_a_run(self) -> None:
        trigger = make_webhook("push", dedupe_window=3600.0)
        router, submitter, _ = router_for(trigger)

        response = await router.handle("push", signed(**{"x-github-delivery": "d-1"}))

        assert response == HookResponse(202, {"status": "queued", "run_id": 1})
        [request] = submitter.requests
        assert request.workflow_id == "wf"
        assert request.kind is TriggerKind.WEBHOOK
        assert request.trigger_key == "wf:webhook:push"
        assert request.delivery == Delivery("push", delivery_digest(trigger, BODY), 3600.0)
        assert request.trigger["body"] == {"ref": "refs/heads/main"}
        assert request.trigger["delivery"] == "d-1"
        assert request.trigger["received_at"] == "2026-11-09T12:00:00.000Z"
        headers: Any = request.trigger["headers"]
        assert "x-hub-signature-256" not in headers

    async def test_every_authentication_failure_gets_the_same_401(self) -> None:
        router, submitter, _ = router_for(make_webhook("push"))
        attempts = [
            hook_request(BODY),
            signed(secret="another-sixteen-chars"),
            hook_request(BODY, headers={"x-hub-signature-256": "sha256=zz"}),
            hook_request(BODY, headers={"x-hub-signature-256": sign(SECRET, b"{}")}),
        ]
        responses = [await router.handle("push", attempt) for attempt in attempts]
        unauthorized = {
            "error": {
                "code": "unauthorized",
                "message": "the request signature didn't verify",
                "details": None,
            }
        }
        assert [(r.status, r.body, r.headers) for r in responses] == [(401, unauthorized, {})] * 4
        assert submitter.requests == []

    async def test_a_timestamped_hook_signs_the_timestamp_too(self) -> None:
        clock = FakeClock()
        trigger = make_webhook("push", timestamp_header="X-Signature-Timestamp", tolerance=60.0)
        router, submitter, _ = router_for(trigger, clock=clock)
        now = str(int(clock.now().timestamp()))

        good = hook_request(
            BODY,
            headers={
                "x-signature-timestamp": now,
                "x-hub-signature-256": sign(SECRET, BODY, timestamp=now),
            },
        )
        stale_stamp = str(int(clock.now().timestamp()) - 61)
        stale = hook_request(
            BODY,
            headers={
                "x-signature-timestamp": stale_stamp,
                "x-hub-signature-256": sign(SECRET, BODY, timestamp=stale_stamp),
            },
        )
        body_only = signed(**{"x-signature-timestamp": now})

        assert (await router.handle("push", good)).status == 202
        assert (await router.handle("push", stale)).status == 401
        assert (await router.handle("push", body_only)).status == 401
        [request] = submitter.requests
        message = f"{now}.".encode() + BODY
        assert request.delivery == Delivery("push", delivery_digest(trigger, message), 86400.0)

    async def test_token_mode_compares_the_header_with_the_secret(self) -> None:
        trigger = make_webhook("lab", signature=SignatureMode.TOKEN)
        router, submitter, _ = router_for(trigger)

        accepted = await router.handle(
            "lab", hook_request(BODY, headers={"x-gitlab-token": SECRET})
        )
        refused = await router.handle("lab", hook_request(BODY, headers={"x-gitlab-token": "nope"}))

        assert (accepted.status, refused.status) == (202, 401)
        [request] = submitter.requests
        assert request.delivery == Delivery("lab", delivery_digest(trigger, BODY), 86400.0)

    async def test_the_rate_limit_applies_only_after_authentication(self) -> None:
        router, submitter, _ = router_for(make_webhook("push", rate_limit=6))

        unsigned = [await router.handle("push", hook_request(BODY)) for _ in range(5)]
        first = await router.handle("push", signed())
        second = await router.handle("push", signed())

        assert {response.status for response in unsigned} == {401}
        assert first.status == 202
        assert second.status == 429
        assert second.headers == {"Retry-After": "10"}
        assert code(second) == "rate_limited"
        assert len(submitter.requests) == 1

        await asyncio.sleep(10)
        assert (await router.handle("push", signed())).status == 202

    async def test_a_zero_rate_limit_never_limits(self) -> None:
        router, _, _ = router_for(make_webhook("push", rate_limit=0))
        statuses = {(await router.handle("push", signed())).status for _ in range(50)}
        assert statuses == {202}

    async def test_an_invalid_json_body_is_a_bad_request(self) -> None:
        router, submitter, _ = router_for(make_webhook("push"))
        response = await router.handle("push", signed(b"{not json"))
        assert response.status == 400
        error: Any = response.body["error"]
        assert error["code"] == "bad_request"
        assert error["message"].startswith("the body isn't valid JSON: ")
        assert submitter.requests == []

    async def test_a_body_over_the_hook_limit_is_too_large(self) -> None:
        router, submitter, _ = router_for(make_webhook("push", max_body=8))
        response = await router.handle("push", signed())
        assert response.status == 413
        assert code(response) == "payload_too_large"
        assert submitter.requests == []

    async def test_handle_repeats_the_precheck(self) -> None:
        router, _, secrets = router_for(make_webhook("push"))
        assert (await router.handle("pull", signed())).status == 404
        secrets.env.clear()
        assert (await router.handle("push", signed())).status == 503
        router.set_draining()
        assert (await router.handle("push", signed())).status == 503

    async def test_a_skipped_run_reports_its_reason(self) -> None:
        def skip(request: RunRequest) -> Submission:
            run = make_run(43, status=RunStatus.SKIPPED, reason=RunReason.ALREADY_ACTIVE)
            return Submission(SubmitOutcome.SKIPPED, run)

        router, _, _ = router_for(make_webhook("push"), submitter=FakeSubmitter(handler=skip))
        response = await router.handle("push", signed())
        assert response == HookResponse(
            202, {"status": "skipped", "run_id": 43, "reason": "already_active"}
        )

    async def test_a_skipped_run_without_a_reason_reports_null(self) -> None:
        submitter = FakeSubmitter(outcome=SubmitOutcome.SKIPPED)
        router, _, _ = router_for(make_webhook("push"), submitter=submitter)
        response = await router.handle("push", signed())
        assert response.body == {"status": "skipped", "run_id": 1, "reason": None}

    @pytest.mark.parametrize(
        ("run", "run_id"), [(make_run(7), 7), (None, None)], ids=["kept", "pruned"]
    )
    async def test_a_duplicate_delivery_answers_200(self, run: Any, run_id: int | None) -> None:
        submitter = FakeSubmitter(handler=lambda request: Submission(SubmitOutcome.DUPLICATE, run))
        router, _, _ = router_for(make_webhook("push"), submitter=submitter)
        response = await router.handle("push", signed())
        assert response == HookResponse(200, {"status": "duplicate", "run_id": run_id})

    @pytest.mark.parametrize(
        ("error", "status", "code", "headers"),
        [
            (
                ShuttingDownError("Treadle is shutting down"),
                503,
                "shutting_down",
                {"Retry-After": "30"},
            ),
            (WorkflowInvalidError("wf", []), 503, "workflow_invalid", {}),
            (NotFoundError("workflow 'wf' doesn't exist"), 404, "not_found", {}),
        ],
    )
    async def test_submit_errors_map_to_responses(
        self, error: Exception, status: int, code: str, headers: dict[str, str]
    ) -> None:
        router, _, _ = router_for(make_webhook("push"), submitter=FakeSubmitter(error=error))
        response = await router.handle("push", signed())
        assert (response.status, response.headers) == (status, headers)
        assert response.body["error"] == {"code": code, "message": str(error), "details": None}

    async def test_the_secret_is_read_at_request_time(self) -> None:
        router, _, secrets = router_for(make_webhook("push"))
        secrets.env["HOOK_SECRET"] = "a-rotated-sixteen-secret"
        assert (await router.handle("push", signed())).status == 401
        assert (
            await router.handle("push", signed(secret="a-rotated-sixteen-secret"))
        ).status == 202


class TestReplayProtection:
    async def test_a_replayed_delivery_is_a_duplicate_of_the_first_run(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = make_webhook("push", rate_limit=0)
        workflow = make_workflow(triggers=[trigger])
        submitter = StoreSubmitter(store, workflow, clock=clock)
        secrets = FakeSecrets(env={"HOOK_SECRET": SECRET})
        router = WebhookRouter(submitter=submitter, secrets=lambda: secrets, clock=clock)
        router.update("wf", workflow)

        first = await router.handle("push", signed())
        replay = await router.handle("push", signed())
        other = await router.handle("push", signed(b'{"ref": "refs/heads/dev"}'))

        assert first == HookResponse(202, {"status": "queued", "run_id": 1})
        assert replay == HookResponse(200, {"status": "duplicate", "run_id": 1})
        assert other == HookResponse(202, {"status": "queued", "run_id": 2})
        assert [run.id for run in await runs_of(store)] == [1, 2]

    async def test_a_timestamped_replay_with_a_new_timestamp_is_a_new_delivery(
        self, store: AsyncStore, clock: FakeClock
    ) -> None:
        trigger = make_webhook("push", rate_limit=0, timestamp_header="X-Timestamp")
        workflow = make_workflow(triggers=[trigger])
        secrets = FakeSecrets(env={"HOOK_SECRET": SECRET})
        router = WebhookRouter(
            submitter=StoreSubmitter(store, workflow, clock=clock),
            secrets=lambda: secrets,
            clock=clock,
        )
        router.update("wf", workflow)

        def stamped(seconds: int) -> HookRequest:
            stamp = str(int(clock.now().timestamp()) + seconds)
            signature = sign(SECRET, BODY, timestamp=stamp)
            return hook_request(
                BODY, headers={"x-timestamp": stamp, "x-hub-signature-256": signature}
            )

        responses = [await router.handle("push", stamped(n)) for n in (0, 0, 1)]
        assert [response.status for response in responses] == [202, 200, 202]


class TestUpdate:
    def test_update_is_synchronous(self) -> None:
        assert not inspect.iscoroutinefunction(WebhookRouter.update)

    async def test_new_settings_apply_and_the_bucket_is_kept(self) -> None:
        old = make_webhook("push", rate_limit=6)
        router, _, _ = router_for(old)
        assert (await router.handle("push", signed())).status == 202

        new = make_webhook("push", rate_limit=6, signature_header="X-Gitea-Signature")
        router.update("wf", make_workflow(triggers=[new], source="# edited\n"))

        gitea = hook_request(BODY, headers={"x-gitea-signature": sign(SECRET, BODY)})
        assert (await router.handle("push", signed())).status == 401
        assert (await router.handle("push", gitea)).status == 429

    async def test_a_changed_rate_limit_starts_a_new_bucket(self) -> None:
        router, _, _ = router_for(make_webhook("push", rate_limit=6))
        assert (await router.handle("push", signed())).status == 202
        assert (await router.handle("push", signed())).status == 429

        router.update("wf", make_workflow(triggers=[make_webhook("push", rate_limit=12)]))
        assert (await router.handle("push", signed())).status == 202

    def test_removed_hooks_stop_answering(self) -> None:
        router, _, _ = router_for(make_webhook("push"), make_webhook("pull"))
        router.update("wf", make_workflow(triggers=[make_webhook("pull")]))
        response = router.precheck("push")
        assert isinstance(response, HookResponse)
        assert response.status == 404
        assert router.precheck("pull") == 1 << 20

        router.update("wf", None)
        assert router.states() == []

    def test_other_workflows_keep_their_hooks(self) -> None:
        router, _, _ = router_for(make_webhook("push"))
        router.update("other", make_workflow(workflow_id="other", triggers=[]))
        assert router.precheck("push") == 1 << 20


class TestStates:
    def test_states_show_the_route_and_why_a_hook_is_disabled(self) -> None:
        router, _, secrets = router_for(
            make_webhook("a-ok"),
            make_webhook("b-unset", secret_env="UNSET"),
            make_webhook("c-short", secret_env="SHORT"),
        )
        secrets.env["SHORT"] = "short"
        assert router.states() == [
            TriggerState(
                "wf", "wf:webhook:a-ok", TriggerKind.WEBHOOK, "webhook /hooks/a-ok", None, None
            ),
            TriggerState(
                "wf",
                "wf:webhook:b-unset",
                TriggerKind.WEBHOOK,
                "webhook /hooks/b-unset",
                None,
                "disabled: secret is not set",
            ),
            TriggerState(
                "wf",
                "wf:webhook:c-short",
                TriggerKind.WEBHOOK,
                "webhook /hooks/c-short",
                None,
                "disabled: secret is shorter than 16 characters",
            ),
        ]

    def test_states_filter_by_workflow(self) -> None:
        router, _, _ = router_for(make_webhook("push"))
        other = make_webhook("other", workflow_id="other")
        router.update("other", make_workflow(workflow_id="other", triggers=[other]))
        assert [state.key for state in router.states("other")] == ["other:webhook:other"]
        assert [state.workflow_id for state in router.states()] == ["other", "wf"]
