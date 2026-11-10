"""The authentication matrix of section 8: who may reach which route, with which credentials."""

from __future__ import annotations

import dataclasses

import pytest
from tests.support.fakes import FakeWebhookHandler
from tests.unit.web.helpers import FORM, SECRET_KEY, TOKEN, MakeWeb, Web, form_body

from treadle.config import Settings
from treadle.web.security import SESSION_COOKIE, issue_session

BEARER = {"Authorization": f"Bearer {TOKEN}"}
API_READS = ["/api/v1/status", "/api/v1/workflows", "/api/v1/runs"]
PAGES = ["/", "/runs", "/workflows/deploy"]
OPEN = ["/api/v1/health", "/static/treadle.css", "/static/treadle.js", "/login"]


def lan(settings: Settings, *allowed_hosts: str) -> Settings:
    """Settings for a non-loopback bind."""
    server = dataclasses.replace(
        settings.server, host="192.168.1.20", allowed_hosts=tuple(allowed_hosts)
    )
    return dataclasses.replace(settings, server=server)


async def test_without_a_token_every_route_is_open(web: Web) -> None:
    for path in [*API_READS, "/", "/runs"]:
        response = await web.client.get(path)

        assert response.status_code == 200, path
    response = await web.post_form("/runs/1/cancel")
    assert response.status_code == 303


async def test_with_a_token_and_no_credentials_the_api_answers_401(secured: Web) -> None:
    for path in API_READS:
        response = await secured.client.get(path)

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized"
    post = await secured.client.post("/api/v1/workflows/deploy/runs", json={})
    assert post.status_code == 401
    assert secured.runs.requests == []


async def test_with_a_token_and_no_credentials_pages_redirect_to_login(secured: Web) -> None:
    for path in PAGES:
        response = await secured.client.get(path)

        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
    response = await secured.client.post("/runs/1/cancel", content="", headers=FORM)
    assert response.status_code == 401
    assert secured.runs.cancels == []


async def test_with_a_token_the_open_paths_stay_open(make_web: MakeWeb) -> None:
    hooks = FakeWebhookHandler(precheck=1024)
    web = make_web(token=TOKEN, hooks=hooks)

    for path in OPEN:
        assert (await web.client.get(path)).status_code == 200, path
    hook = await web.client.post("/hooks/deploy", content=b"{}")
    assert hook.status_code == 202
    assert len(hooks.requests) == 1


async def test_a_bearer_token_opens_the_api_but_not_the_dashboard(secured: Web) -> None:
    for path in API_READS:
        assert (await secured.client.get(path, headers=BEARER)).status_code == 200
    start = await secured.client.post("/api/v1/workflows/deploy/runs", json={}, headers=BEARER)
    page = await secured.client.get("/", headers=BEARER)

    assert start.status_code == 202
    assert page.status_code == 303


async def test_a_wrong_bearer_token_is_refused(secured: Web) -> None:
    response = await secured.client.get("/api/v1/status", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


async def test_a_session_opens_the_api_and_the_dashboard(secured: Web) -> None:
    await secured.login()

    for path in [*API_READS, *PAGES[:2]]:
        assert (await secured.client.get(path)).status_code == 200, path
    cancel = await secured.post_form("/runs/1/cancel", page="/runs")
    assert cancel.status_code == 303
    assert secured.runs.cancels


async def test_login_sends_you_to_the_page_you_asked_for(secured: Web) -> None:
    first = await secured.client.get("/runs?status=failed")
    next_url = first.headers["location"].removeprefix("/login?next=")

    response = await secured.login(next_url="/runs?status=failed")

    assert next_url == "%2Fruns%3Fstatus%3Dfailed"
    assert response.headers["location"] == "/runs?status=failed"


async def test_an_expired_session_signs_you_out(secured: Web) -> None:
    await secured.login()
    secured.clock.jump(secured.ctx.settings.server.session_ttl)

    page = await secured.client.get("/")
    api = await secured.client.get("/api/v1/status")

    assert page.status_code == 303
    assert api.status_code == 401


async def test_rotating_the_token_signs_everyone_out(make_web: MakeWeb, secured: Web) -> None:
    session = issue_session(SECRET_KEY, TOKEN, secured.clock.now().timestamp())
    rotated = make_web(token="a-brand-new-token-for-everyone-0123456789")
    rotated.set_cookie(SESSION_COOKIE, session)
    secured.set_cookie(SESSION_COOKIE, session)

    assert (await secured.client.get("/")).status_code == 200
    assert (await rotated.client.get("/")).status_code == 303


async def test_a_non_loopback_bind_accepts_any_host_and_still_requires_auth(
    make_web: MakeWeb, settings: Settings
) -> None:
    web = make_web(token=TOKEN, settings=lan(settings), base_url="http://192.168.1.20:8765")

    anonymous = await web.client.get("/api/v1/status", headers={"Host": "treadle.home.arpa"})
    bearer = await web.client.get("/api/v1/status", headers={**BEARER, "Host": "treadle.home.arpa"})

    assert anonymous.status_code == 401
    assert bearer.status_code == 200


async def test_a_non_loopback_bind_with_allowed_hosts_checks_them(
    make_web: MakeWeb, settings: Settings
) -> None:
    web = make_web(token=TOKEN, settings=lan(settings, "treadle.lan"))

    allowed = await web.client.get("/api/v1/health", headers={"Host": "treadle.lan:8765"})
    refused = await web.client.get("/api/v1/health", headers={"Host": "127.0.0.1:8765"})

    assert allowed.status_code == 200
    assert refused.status_code == 400


@pytest.mark.parametrize("path", ["/", "/api/v1/health", "/login", "/static/treadle.css"])
async def test_a_loopback_bind_refuses_rebound_host_names(secured: Web, path: str) -> None:
    response = await secured.client.get(path, headers={"Host": "attacker.example:8765"})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


async def test_webhooks_skip_the_host_check_on_a_loopback_bind(make_web: MakeWeb) -> None:
    hooks = FakeWebhookHandler(precheck=1024)
    web = make_web(hooks=hooks)

    response = await web.client.post("/hooks/x", content=b"{}", headers={"Host": "tunnel.example"})

    assert response.status_code == 202
    assert hooks.prechecks == ["x"]


async def test_a_cross_site_form_post_is_refused_even_with_a_valid_csrf_token(web: Web) -> None:
    token = await web.csrf()
    body = form_body(("csrf_token", token))

    cross = await web.client.post(
        "/runs/1/cancel", content=body, headers={**FORM, "Sec-Fetch-Site": "cross-site"}
    )
    same = await web.client.post(
        "/runs/1/cancel", content=body, headers={**FORM, "Sec-Fetch-Site": "same-origin"}
    )

    assert cross.status_code == 403
    assert same.status_code == 303
    assert len(web.runs.cancels) == 1
