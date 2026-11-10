"""The Starlette application factory (section 8, "Middleware and limits")."""

from __future__ import annotations

from http.client import responses
from typing import Final, cast

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from treadle.serialize import error_json
from treadle.web.api import api_routes
from treadle.web.dashboard import _Pages, _sentence, dashboard_routes, make_templates
from treadle.web.hooks import hook_routes

# Design note: WebContext is defined in web.security (see the note there) and re-exported
# here, so `from treadle.web.app import WebContext` works as section 4 shows.
from treadle.web.security import (
    AuthMiddleware,
    HostCheckMiddleware,
    OriginGuardMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
    _is_api,
    _security_headers,
    allowed_hosts_for,
)
from treadle.web.security import WebContext as WebContext

_ERROR_CODES: Final = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_inputs",
    429: "rate_limited",
}


def create_app(ctx: WebContext) -> Starlette:
    """Build the Starlette application.

    It has every route, the middleware in section 8's order, and
    `max_body_size=limits.api_body_bytes`. It never runs lifespan handlers.
    """
    templates = make_templates()
    pages = _Pages(ctx, templates)

    async def http_error(request: Request, exc: Exception) -> Response:
        """Answer an HTTPException: the JSON error shape for the API and hooks, else a page."""
        error = cast(HTTPException, exc)  # this handler is registered for HTTPException only
        message = _message(error)
        if _machine_path(request.scope["path"]):
            body = error_json(_ERROR_CODES.get(error.status_code, "bad_request"), message)
            return JSONResponse(body, error.status_code, error.headers)
        page = pages.error(request, error.status_code, _sentence(message))
        page.headers.update(error.headers or {})
        return page

    middleware = [
        Middleware(RequestLogMiddleware),
        Middleware(SecurityHeadersMiddleware),
        Middleware(HostCheckMiddleware, allowed_hosts=allowed_hosts_for(ctx.settings.server)),
        Middleware(OriginGuardMiddleware),
        Middleware(
            AuthMiddleware,
            token=ctx.token,
            secret_key=ctx.secret_key,
            ttl=ctx.settings.server.session_ttl,
            clock=ctx.clock,
        ),
    ]
    # Design note: when a declared Content-Length passes the active limit, Starlette answers
    # with its own plain-text 413 from outside this middleware, so that response alone has
    # no security headers. A body that grows too large while streaming gets the JSON 413.
    return Starlette(
        routes=[*api_routes(ctx), *hook_routes(ctx), *dashboard_routes(ctx, templates)],
        middleware=middleware,
        exception_handlers={HTTPException: http_error, Exception: _internal_error},
        max_body_size=ctx.settings.limits.api_body_bytes,
    )


async def _internal_error(request: Request, exc: Exception) -> Response:
    """Answer an unexpected exception with a bare 500; Starlette then re-raises it to log it.

    This response leaves from outside the middleware, so it adds the security headers itself.
    """
    path: str = request.scope["path"]
    headers = _security_headers(path)
    if _machine_path(path):
        return JSONResponse(error_json("internal", "internal error"), 500, headers)
    return PlainTextResponse("Internal server error.", 500, headers)


def _message(error: HTTPException) -> str:
    """The error's detail, with Starlette's default reason phrases in lowercase."""
    if error.detail == responses.get(error.status_code):
        return error.detail.lower()
    return error.detail


def _machine_path(path: str) -> bool:
    """True for paths whose clients read JSON: the API and the webhooks."""
    return _is_api(path) or path.startswith("/hooks/")
