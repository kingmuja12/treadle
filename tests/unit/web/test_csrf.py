"""CSRF protection of the dashboard forms: signed double-submit tokens bound to the session."""

from __future__ import annotations

import pytest
from tests.unit.web.helpers import FORM, SECRET_KEY, TOKEN, MakeWeb, Web, form_body, text

from treadle.web.security import CSRF_COOKIE, SESSION_COOKIE, issue_csrf, issue_session

EXPIRED = "This form expired. Reload the page and try again."
FORMS = [
    ("/runs/4/cancel", []),
    ("/runs/4/rerun", []),
    ("/workflows/deploy/run", [("input.count", "1")]),
]


async def submit(web: Web, path: str, csrf: str | None, fields: list[tuple[str, str]]) -> int:
    body = form_body(*([("csrf_token", csrf)] if csrf is not None else []), *fields)
    response = await web.client.post(path, content=body, headers=FORM)
    if response.status_code == 403:
        assert EXPIRED in text(response.text)
    return response.status_code


def nothing_happened(web: Web) -> bool:
    return web.runs.cancels == web.runs.reruns == web.runs.requests == []


@pytest.mark.parametrize(("path", "fields"), FORMS)
async def test_a_missing_field_is_refused(
    web: Web, path: str, fields: list[tuple[str, str]]
) -> None:
    await web.csrf()

    assert await submit(web, path, None, fields) == 403
    assert nothing_happened(web)


@pytest.mark.parametrize(("path", "fields"), FORMS)
async def test_a_missing_cookie_is_refused(
    web: Web, path: str, fields: list[tuple[str, str]]
) -> None:
    token = await web.csrf()
    web.client.cookies.clear()

    assert await submit(web, path, token, fields) == 403
    assert nothing_happened(web)


@pytest.mark.parametrize(("path", "fields"), FORMS)
async def test_a_mismatched_field_is_refused(
    web: Web, path: str, fields: list[tuple[str, str]]
) -> None:
    await web.csrf()
    other = issue_csrf(SECRET_KEY, "anonymous")

    assert await submit(web, path, other, fields) == 403
    assert nothing_happened(web)


async def test_a_forged_token_without_the_key_is_refused(web: Web) -> None:
    forged = issue_csrf(b"x" * 32, "anonymous")
    web.set_cookie(CSRF_COOKIE, forged)

    assert await submit(web, "/runs/4/cancel", forged, []) == 403


async def test_a_token_bound_to_another_session_is_refused(secured: Web) -> None:
    await secured.login()
    session = secured.client.cookies[SESSION_COOKIE]
    anonymous = issue_csrf(SECRET_KEY, "anonymous")
    other_session = issue_session(SECRET_KEY, TOKEN, secured.clock.now().timestamp() - 1)
    stolen = issue_csrf(SECRET_KEY, other_session.rpartition(".")[2])

    for token in (anonymous, stolen):
        secured.set_cookie(CSRF_COOKIE, token)
        assert await submit(secured, "/runs/4/cancel", token, []) == 403
    assert secured.client.cookies[SESSION_COOKIE] == session
    assert secured.runs.cancels == []


async def test_a_form_from_before_login_expires_at_login(secured: Web) -> None:
    before = await secured.csrf("/login")
    await secured.login()
    secured.set_cookie(CSRF_COOKIE, before)

    assert await submit(secured, "/runs/4/cancel", before, []) == 403


async def test_an_expired_session_cannot_submit_a_form(secured: Web) -> None:
    await secured.login()
    token = await secured.csrf("/runs")
    secured.clock.jump(secured.ctx.settings.server.session_ttl + 1)

    assert await submit(secured, "/runs/4/cancel", token, []) == 401
    assert secured.runs.cancels == []


async def test_a_valid_token_passes(web: Web) -> None:
    token = await web.csrf()

    assert await submit(web, "/runs/4/cancel", token, []) == 303
    assert web.runs.cancels


async def test_a_page_keeps_a_valid_csrf_cookie(web: Web) -> None:
    first = await web.client.get("/")
    second = await web.client.get("/runs")

    assert CSRF_COOKIE in first.headers["set-cookie"]
    assert "set-cookie" not in second.headers


async def test_a_page_replaces_an_invalid_csrf_cookie(web: Web) -> None:
    web.set_cookie(CSRF_COOKIE, "tampered.value")

    response = await web.client.get("/")

    assert response.headers["set-cookie"].startswith(f"{CSRF_COOKIE}=")
    assert web.client.cookies[CSRF_COOKIE] != "tampered.value"


async def test_the_login_form_is_protected_too(make_web: MakeWeb) -> None:
    web = make_web(token=TOKEN)
    await web.csrf("/login")

    status = await submit(web, "/login", "wrong", [("token", TOKEN), ("next", "/")])

    assert status == 403
    assert SESSION_COOKIE not in web.client.cookies
