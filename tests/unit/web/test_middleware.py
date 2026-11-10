from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tests.support.clock import FakeClock

from treadle.web.security import (
    SESSION_COOKIE,
    AuthMiddleware,
    HostCheckMiddleware,
    OriginGuardMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
    issue_session,
)

TOKEN = "t" * 40
KEY = b"k" * 32
BASE_URL = "http://127.0.0.1:8765"
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


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """The inner application: answers 200 "ok" with a Cache-Control of its own."""
    await PlainTextResponse("ok", headers={"Cache-Control": "max-age=60"})(scope, receive, send)


async def broken_app(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError("boom")


def client(app: ASGIApp, *, base_url: str = BASE_URL) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=base_url)


def auth(clock: FakeClock) -> ASGIApp:
    return AuthMiddleware(ok_app, token=TOKEN, secret_key=KEY, ttl=3600.0, clock=clock)


MIDDLEWARE: list[Callable[[ASGIApp], ASGIApp]] = [
    SecurityHeadersMiddleware,
    lambda app: HostCheckMiddleware(app, allowed_hosts=["127.0.0.1"]),
    OriginGuardMiddleware,
    lambda app: AuthMiddleware(app, token=TOKEN, secret_key=KEY, ttl=1.0, clock=FakeClock()),
    RequestLogMiddleware,
]


@pytest.mark.parametrize("wrap", MIDDLEWARE)
async def test_middleware_passes_non_http_scopes_through(
    wrap: Callable[[ASGIApp], ASGIApp],
) -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        raise AssertionError("not called")

    async def send(message: Message) -> None:
        raise AssertionError("not called")

    await wrap(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]


async def test_security_headers_are_added_to_every_response() -> None:
    async with client(SecurityHeadersMiddleware(ok_app)) as http:
        response = await http.get("/runs")

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["cache-control"] == "no-store"


async def test_static_files_may_be_cached() -> None:
    async with client(SecurityHeadersMiddleware(ok_app)) as http:
        response = await http.get("/static/treadle.css")

    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "treadle.lan"])
async def test_host_check_accepts_allowed_hosts(host: str) -> None:
    app = HostCheckMiddleware(
        ok_app, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "treadle.lan"]
    )
    async with client(app) as http:
        response = await http.get("/", headers={"Host": host})

    assert response.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "", "bad host"])
async def test_host_check_refuses_other_hosts(host: str) -> None:
    async with client(HostCheckMiddleware(ok_app, allowed_hosts=["127.0.0.1"])) as http:
        response = await http.get("/api/v1/status", headers={"Host": host})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


async def test_host_check_skips_the_exempt_prefixes() -> None:
    async with client(HostCheckMiddleware(ok_app, allowed_hosts=["127.0.0.1"])) as http:
        hook = await http.post("/hooks/deploy", headers={"Host": "tunnel.example"})
        other = await http.post("/hookshot", headers={"Host": "tunnel.example"})

    assert hook.status_code == 200
    assert other.status_code == 400


async def test_host_check_with_a_wildcard_accepts_any_host() -> None:
    async with client(HostCheckMiddleware(ok_app, allowed_hosts=["*"])) as http:
        response = await http.get("/", headers={"Host": "anything.example"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({}, True),
        ({"Sec-Fetch-Site": "same-origin"}, True),
        ({"Sec-Fetch-Site": "none"}, True),
        ({"Sec-Fetch-Site": "Same-Origin"}, True),
        ({"Sec-Fetch-Site": "same-site"}, False),
        ({"Sec-Fetch-Site": "cross-site"}, False),
        ({"Origin": BASE_URL}, True),
        ({"Origin": "http://127.0.0.1:8765/"}, True),
        ({"Origin": "http://evil.example"}, False),
        ({"Origin": "http://127.0.0.1:9999"}, False),
        ({"Origin": "https://127.0.0.1:8765"}, False),
        ({"Origin": "http://localhost:8765"}, False),
        ({"Origin": "null"}, False),
        ({"Origin": "http://127.0.0.1:99999"}, False),
        # A browser that sends Sec-Fetch-Site decides alone: Referrer-Policy: no-referrer
        # makes it send Origin: null with the dashboard's own form posts.
        ({"Sec-Fetch-Site": "same-origin", "Origin": "null"}, True),
        ({"Sec-Fetch-Site": "cross-site", "Origin": BASE_URL}, False),
    ],
)
async def test_origin_guard_refuses_cross_site_unsafe_requests(
    headers: dict[str, str], allowed: bool
) -> None:
    async with client(OriginGuardMiddleware(ok_app)) as http:
        response = await http.post("/runs/1/cancel", headers=headers)

    assert (response.status_code == 200) is allowed
    if not allowed:
        assert response.status_code == 403
        assert response.text == "cross-site request refused"


async def test_origin_guard_answers_api_requests_with_json() -> None:
    async with client(OriginGuardMiddleware(ok_app)) as http:
        response = await http.post(
            "/api/v1/runs/1/cancel", headers={"Sec-Fetch-Site": "cross-site"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_origin_guard_compares_ipv6_and_default_ports() -> None:
    async with client(OriginGuardMiddleware(ok_app), base_url="http://[::1]:8765") as http:
        ipv6 = await http.post("/x", headers={"Origin": "http://[::1]:8765"})
    async with client(OriginGuardMiddleware(ok_app), base_url="http://treadle.lan") as http:
        default_port = await http.post("/x", headers={"Origin": "http://treadle.lan:80"})
        no_host = await http.post("/x", headers={"Origin": "http://treadle.lan", "Host": ""})

    assert ipv6.status_code == 200
    assert default_port.status_code == 200
    assert no_host.status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_origin_guard_ignores_safe_methods(method: str) -> None:
    async with client(OriginGuardMiddleware(ok_app)) as http:
        response = await http.request(method, "/", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_origin_guard_checks_every_unsafe_method(method: str) -> None:
    async with client(OriginGuardMiddleware(ok_app)) as http:
        response = await http.request(method, "/", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 403


async def test_origin_guard_skips_webhooks() -> None:
    async with client(OriginGuardMiddleware(ok_app)) as http:
        response = await http.post("/hooks/deploy", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200


async def test_auth_without_a_token_lets_everything_through(clock: FakeClock) -> None:
    app = AuthMiddleware(ok_app, token=None, secret_key=KEY, ttl=3600.0, clock=clock)
    async with client(app) as http:
        api = await http.get("/api/v1/status")
        page = await http.post("/runs/1/cancel")

    assert api.status_code == 200
    assert page.status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/v1/health", "/static/treadle.css", "/login", "/hooks/deploy"]
)
async def test_auth_leaves_the_open_paths_open(clock: FakeClock, path: str) -> None:
    async with client(auth(clock)) as http:
        response = await http.get(path)

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/healthz", "/login/", "/static", "/api"])
async def test_auth_protects_lookalike_paths(clock: FakeClock, path: str) -> None:
    async with client(auth(clock)) as http:
        response = await http.get(path)

    assert response.status_code in (303, 401)


@pytest.mark.parametrize(
    ("authorization", "status"),
    [
        (f"Bearer {TOKEN}", 200),
        (f"bearer {TOKEN}", 200),
        (f"Bearer  {TOKEN} ", 200),
        ("Bearer wrong", 401),
        (f"Basic {TOKEN}", 401),
        (TOKEN, 401),
        ("", 401),
    ],
)
async def test_auth_checks_the_bearer_token_on_the_api(
    clock: FakeClock, authorization: str, status: int
) -> None:
    async with client(auth(clock)) as http:
        response = await http.get("/api/v1/runs", headers={"Authorization": authorization})

    assert response.status_code == status
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {
            "error": {"code": "unauthorized", "message": "authentication required", "details": None}
        }


async def test_auth_accepts_a_session_on_api_and_dashboard_paths(clock: FakeClock) -> None:
    session = issue_session(KEY, TOKEN, clock.now().timestamp())
    async with client(auth(clock)) as http:
        http.cookies.set(SESSION_COOKIE, session)
        api = await http.get("/api/v1/runs")
        page = await http.post("/runs/1/cancel")

    assert api.status_code == 200
    assert page.status_code == 200


async def test_auth_sends_a_dashboard_get_to_the_login_page(clock: FakeClock) -> None:
    async with client(auth(clock)) as http:
        response = await http.get("/runs?status=failed&workflow=nightly")
        head = await http.head("/workflows/nightly")

    assert response.status_code == 303
    assert (
        response.headers["location"] == "/login?next=%2Fruns%3Fstatus%3Dfailed%26workflow%3Dnightly"
    )
    assert head.status_code == 303
    assert head.headers["location"] == "/login?next=%2Fworkflows%2Fnightly"


async def test_auth_refuses_a_dashboard_post_without_a_session(clock: FakeClock) -> None:
    async with client(auth(clock)) as http:
        response = await http.post("/runs/1/cancel", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 401
    assert response.text == "your session expired; sign in again"


async def test_auth_refuses_an_expired_session(clock: FakeClock) -> None:
    session = issue_session(KEY, TOKEN, clock.now().timestamp())
    clock.jump(3600)
    async with client(auth(clock)) as http:
        http.cookies.set(SESSION_COOKIE, session)
        response = await http.get("/api/v1/runs")

    assert response.status_code == 401


async def test_request_log_records_method_path_status_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="treadle.web")
    async with client(RequestLogMiddleware(ok_app)) as http:
        await http.post(
            "/api/v1/runs/1/cancel?secret=hunter2",
            headers={"Authorization": f"Bearer {TOKEN}"},
            content=b'{"force": true}',
        )

    [record] = caplog.records
    assert record.name == "treadle.web"
    assert record.levelno == logging.INFO
    method, path, status, duration = record.getMessage().split(" ")
    assert (method, path, status) == ("POST", "/api/v1/runs/1/cancel", "200")
    assert duration.endswith(("ms", "s"))
    for secret in ("hunter2", TOKEN, "force"):
        assert secret not in caplog.text


async def test_request_log_escapes_control_characters_in_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="treadle.web")
    seen: list[Scope] = []

    async def capture(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        await ok_app(scope, receive, send)

    async with client(RequestLogMiddleware(capture)) as http:
        await http.get("/runs/%0Afake 200 line")

    assert "\n" not in caplog.records[0].getMessage()
    scope = {**seen[0], "raw_path": None}

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    caplog.clear()
    await RequestLogMiddleware(ok_app)(scope, receive, send)
    assert caplog.records[0].getMessage().startswith("GET /runs/\\nfake 200 line 200 ")


async def test_request_log_records_500_when_the_app_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="treadle.web")
    async with client(RequestLogMiddleware(broken_app)) as http:
        with pytest.raises(RuntimeError, match="boom"):
            await http.get("/runs")

    assert caplog.records[0].getMessage().startswith("GET /runs 500 ")
