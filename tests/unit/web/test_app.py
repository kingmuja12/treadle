from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx
import pytest
from starlette.applications import Starlette
from starlette.types import ASGIApp, Message, Scope
from tests.support.fakes import FakeRunControl, FakeWebhookHandler, MemoryRunReader
from tests.support.project import ProjectFactory
from tests.unit.web.helpers import FORM, TOKEN, MakeWeb, Web

from treadle.models import HookResponse
from treadle.web.app import create_app
from treadle.web.security import (
    AuthMiddleware,
    HostCheckMiddleware,
    OriginGuardMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
)

SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}
Call = Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]
# (label, whether authentication is on, expected status, request)
REQUESTS: list[tuple[str, bool, int, Call]] = [
    ("dashboard page", False, 200, lambda http: http.get("/")),
    ("API read", False, 200, lambda http: http.get("/api/v1/workflows")),
    ("static file", False, 200, lambda http: http.get("/static/treadle.css")),
    ("log text", False, 404, lambda http: http.get("/runs/9/log.txt")),
    ("API 404", False, 404, lambda http: http.get("/api/v1/nothing")),
    ("page 404", False, 404, lambda http: http.get("/nothing")),
    ("405", False, 405, lambda http: http.delete("/api/v1/health")),
    ("host 400", False, 400, lambda http: http.get("/", headers={"Host": "evil.example"})),
    ("CSRF 403", False, 403, lambda http: http.post("/runs/1/cancel", content="", headers=FORM)),
    ("hook response", False, 202, lambda http: http.post("/hooks/x", content=b"{}")),
    (
        "hook 413",
        False,
        413,
        lambda http: http.post("/hooks/x", content=b"x" * 2048, headers={"Content-Length": "2048"}),
    ),
    ("login page", True, 200, lambda http: http.get("/login")),
    ("login redirect", True, 303, lambda http: http.get("/runs")),
    ("API 401", True, 401, lambda http: http.get("/api/v1/status")),
    (
        "dashboard 401",
        True,
        401,
        lambda http: http.post("/runs/1/cancel", content="", headers=FORM),
    ),
    (
        "origin 403",
        True,
        403,
        lambda http: http.post("/login", content="", headers={"Sec-Fetch-Site": "cross-site"}),
    ),
]


@pytest.mark.parametrize(
    ("label", "secured", "status", "call"), REQUESTS, ids=[request[0] for request in REQUESTS]
)
async def test_every_response_carries_the_security_headers(
    make_web: MakeWeb, label: str, secured: bool, status: int, call: Call
) -> None:
    web = make_web(token=TOKEN if secured else None, hooks=FakeWebhookHandler(precheck=1024))

    response = await call(web.client)

    assert response.status_code == status
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value, f"{label}: {name}"
    cache = "public, max-age=3600" if label == "static file" else "no-store"
    assert response.headers["cache-control"] == cache


async def test_a_dashboard_500_is_plain_text_with_the_security_headers(make_web: MakeWeb) -> None:
    class BrokenReader(MemoryRunReader):
        async def last_runs(self) -> dict[str, object]:  # type: ignore[override]
            raise RuntimeError("database is gone")

    web = make_web(reader=BrokenReader(), raise_app_exceptions=False)

    response = await web.client.get("/")

    assert response.status_code == 500
    assert response.text == "Internal server error."
    assert "database" not in response.text
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_an_unexpected_error_is_raised_for_logging(make_web: MakeWeb) -> None:
    web = make_web(runs=FakeRunControl(error=RuntimeError("bug")))

    with pytest.raises(RuntimeError, match="bug"):
        await web.client.post("/api/v1/runs/1/rerun", json={})


async def send_part_of_a_body(app: ASGIApp, path: str, content_type: str) -> list[int]:
    """POST part of a body, then disconnect, as a sender that drops the connection does.

    Returns the status of each response that the application started.
    """
    messages: list[Message] = [
        {"type": "http.request", "body": b"partial=", "more_body": True},
        {"type": "http.disconnect"},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    headers = [
        (b"host", b"127.0.0.1:8765"),
        (b"content-type", content_type.encode()),
        (b"content-length", b"100"),
    ]
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }
    await app(scope, receive, send)
    return [message["status"] for message in sent if message["type"] == "http.response.start"]


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/hooks/x", "application/json"),
        ("/api/v1/workflows/wf/runs", "application/json"),
        ("/runs/1/cancel", "application/x-www-form-urlencoded"),
    ],
)
async def test_a_client_that_disconnects_mid_body_ends_the_request_quietly(
    make_web: MakeWeb, caplog: pytest.LogCaptureFixture, path: str, content_type: str
) -> None:
    caplog.set_level(logging.DEBUG, logger="treadle.web")
    web = make_web(hooks=FakeWebhookHandler(precheck=1024))

    statuses = await send_part_of_a_body(create_app(web.ctx), path, content_type)

    assert statuses == [400]
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
    lines = [record.getMessage() for record in caplog.records if record.levelno == logging.INFO]
    assert [line.rsplit(" ", 1)[0] for line in lines] == [f"POST {path} 400"]
    assert "the client disconnected before sending the whole body" in caplog.text


async def test_a_loopback_bind_answers_the_host_of_its_own_address_only(
    make_web: MakeWeb, project: ProjectFactory
) -> None:
    web = make_web(settings=project.settings(host="127.0.0.2"), base_url="http://127.0.0.2:8765")

    own = await web.client.get("/api/v1/health")
    neighbor = await web.client.get("/api/v1/health", headers={"Host": "127.0.0.3:8765"})
    rebound = await web.client.get("/api/v1/health", headers={"Host": "evil.example:8765"})

    assert (own.status_code, neighbor.status_code, rebound.status_code) == (200, 400, 400)


async def test_requests_are_logged(web: Web, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="treadle.web")

    await web.client.get("/api/v1/health?verbose=1")

    [record] = [record for record in caplog.records if record.name == "treadle.web"]
    assert record.getMessage().startswith("GET /api/v1/health 200 ")


async def test_create_app_installs_the_middleware_in_order_and_the_body_limit(web: Web) -> None:
    app = create_app(web.ctx)

    assert isinstance(app, Starlette)
    assert [middleware.cls for middleware in app.user_middleware] == [
        RequestLogMiddleware,
        SecurityHeadersMiddleware,
        HostCheckMiddleware,
        OriginGuardMiddleware,
        AuthMiddleware,
    ]
    assert app.max_body_size == web.ctx.settings.limits.api_body_bytes
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/api/v1/health", "/hooks/{name}", "/", "/static/{name}", "/login"} <= paths


async def test_create_app_never_runs_lifespan_handlers(web: Web) -> None:
    app = create_app(web.ctx)
    sent: list[str] = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive() -> dict[str, str]:
        return next(messages)

    async def send(message: dict[str, str]) -> None:
        sent.append(message["type"])

    await app({"type": "lifespan"}, receive, send)  # type: ignore[arg-type]

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_a_hook_refusal_keeps_its_headers(make_web: MakeWeb) -> None:
    draining = HookResponse(503, {"error": {"code": "shutting_down"}}, {"Retry-After": "30"})
    web = make_web(hooks=FakeWebhookHandler(precheck=draining))

    response = await web.client.post("/hooks/x", content=b"{}")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-frame-options"] == "DENY"
