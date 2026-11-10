from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator

import pytest
from tests.support.fakes import FakeWebhookHandler
from tests.unit.web.helpers import TOKEN, MakeWeb, Web

from treadle.config import Limits, Settings
from treadle.models import HookRequest, HookResponse


class Chunks:
    """A streamed request body that records whether the server read any of it."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.started = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started = True
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def hooks() -> FakeWebhookHandler:
    return FakeWebhookHandler(precheck=1024)


@pytest.fixture
def hooked(make_web: MakeWeb, hooks: FakeWebhookHandler) -> Web:
    return make_web(hooks=hooks)


async def test_a_delivery_reaches_the_handler_and_its_response_comes_back(
    make_web: MakeWeb,
) -> None:
    response_body = {"status": "skipped", "run_id": 43, "reason": "already_active"}
    hooks = FakeWebhookHandler(
        precheck=1024, response=HookResponse(202, response_body, {"X-A": "b"})
    )
    web = make_web(hooks=hooks)

    response = await web.client.post("/hooks/blog-push", content=b'{"ref": "main"}')

    assert response.status_code == 202
    assert response.json() == response_body
    assert response.headers["x-a"] == "b"
    assert hooks.prechecks == ["blog-push"]
    [(name, request)] = hooks.requests
    assert name == "blog-push"
    assert request.body == b'{"ref": "main"}'


async def test_the_hook_request_is_built_from_the_http_request(
    hooked: Web, hooks: FakeWebhookHandler
) -> None:
    await hooked.client.post(
        "/hooks/blog-push?ref=a&ref=b&empty=&space=x+y",
        content=b"payload",
        headers=[
            ("Content-Type", "Application/JSON; charset=UTF-8"),
            ("X-Gitea-Event", "push"),
            ("X-Multi", "one"),
            ("x-multi", "two"),
            ("X-Hub-Signature-256", "sha256=abc"),
        ],
    )

    [(_, request)] = hooks.requests
    assert request == HookRequest(
        method="POST",
        headers=request.headers,
        query={"ref": "b", "empty": "", "space": "x y"},
        body=b"payload",
        content_type="application/json",
    )
    assert request.headers["content-type"] == "Application/JSON; charset=UTF-8"
    assert request.headers["x-gitea-event"] == "push"
    assert request.headers["x-multi"] == "one, two"
    assert request.headers["x-hub-signature-256"] == "sha256=abc"
    assert all(name == name.lower() for name in request.headers)


async def test_a_request_without_a_content_type_has_none(
    hooked: Web, hooks: FakeWebhookHandler
) -> None:
    await hooked.client.post("/hooks/x", content=b"")

    [(_, request)] = hooks.requests
    assert request.content_type is None
    assert request.body == b""


@pytest.mark.parametrize(
    ("status", "code", "headers"),
    [
        (404, "not_found", {}),
        (503, "shutting_down", {"Retry-After": "30"}),
        (503, "hook_disabled", {}),
    ],
)
async def test_a_precheck_refusal_is_sent_without_reading_the_body(
    make_web: MakeWeb, status: int, code: str, headers: dict[str, str]
) -> None:
    refusal = HookResponse(
        status, {"error": {"code": code, "message": code, "details": None}}, headers
    )
    hooks = FakeWebhookHandler(precheck=refusal)
    web = make_web(hooks=hooks)
    body = Chunks(b"x" * 10)

    response = await web.client.post("/hooks/nope", content=body)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    for name, value in headers.items():
        assert response.headers[name] == value
    assert hooks.requests == []
    assert not body.started


async def test_a_content_length_over_the_hook_limit_is_413_before_reading(
    hooked: Web, hooks: FakeWebhookHandler
) -> None:
    body = Chunks(b"x" * 2048)

    response = await hooked.client.post(
        "/hooks/blog-push", content=body, headers={"Content-Length": "2048"}
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "payload_too_large",
            "message": "the request body is larger than 1.0 KiB",
            "details": None,
        }
    }
    assert not body.started
    assert hooks.requests == []


async def test_a_content_length_over_the_webhook_limit_is_413_before_the_precheck(
    make_web: MakeWeb, settings: Settings
) -> None:
    hooks = FakeWebhookHandler(precheck=8192)
    limits = Limits(webhook_body_bytes=8192)
    web = make_web(settings=dataclasses.replace(settings, limits=limits), hooks=hooks)
    body = Chunks(b"x" * 8193)

    response = await web.client.post(
        "/hooks/blog-push", content=body, headers={"Content-Length": "8193"}
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "payload_too_large",
            "message": "the request body is larger than 8.0 KiB",
            "details": None,
        }
    }
    assert hooks.prechecks == []
    assert not body.started


async def test_a_streamed_body_over_the_hook_limit_is_413(
    hooked: Web, hooks: FakeWebhookHandler
) -> None:
    body = Chunks(b"x" * 600, b"x" * 600)

    response = await hooked.client.post("/hooks/blog-push", content=body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert hooks.requests == []


async def test_a_body_at_the_hook_limit_is_accepted(hooked: Web, hooks: FakeWebhookHandler) -> None:
    response = await hooked.client.post("/hooks/blog-push", content=Chunks(b"x" * 1024))

    assert response.status_code == 202
    assert len(hooks.requests[0][1].body) == 1024


async def test_the_hook_route_replaces_the_api_body_limit(
    make_web: MakeWeb, settings: Settings
) -> None:
    limits = Limits(api_body_bytes=4096, webhook_body_bytes=16384)
    hooks = FakeWebhookHandler(precheck=16384)
    web = make_web(settings=dataclasses.replace(settings, limits=limits), hooks=hooks)

    accepted = await web.client.post("/hooks/big", content=b"x" * 10_000)
    refused = await web.client.post("/hooks/big", content=Chunks(b"x" * 10_000, b"x" * 10_000))

    assert accepted.status_code == 202
    assert refused.status_code == 413
    assert len(hooks.requests) == 1


async def test_hooks_skip_the_host_check_the_origin_guard_and_authentication(
    make_web: MakeWeb, hooks: FakeWebhookHandler
) -> None:
    web = make_web(token=TOKEN, hooks=hooks)
    headers = {
        "Host": "tunnel.example",
        "Sec-Fetch-Site": "cross-site",
        "Origin": "https://x.example",
    }

    response = await web.client.post("/hooks/blog-push", content=b"{}", headers=headers)

    assert response.status_code == 202
    assert len(hooks.requests) == 1


async def test_a_hook_takes_only_post(hooked: Web, hooks: FakeWebhookHandler) -> None:
    response = await hooked.client.get("/hooks/blog-push")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "bad_request"
    assert hooks.prechecks == []
