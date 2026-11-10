"""The `POST /hooks/{name}` route."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from treadle.models import HookRequest, HookResponse
from treadle.web.security import WebContext, _media_type, read_body_capped


def hook_routes(ctx: WebContext) -> list[BaseRoute]:
    """Return the webhook route.

    The application's BodyLimitMiddleware limits every webhook body to
    `limits.webhook_body_bytes`. The route runs the checks that come before the signature:
    the handler's precheck, then the hook's body limit, by `Content-Length` before reading
    and by size while streaming. The handler verifies and submits what's left.
    """

    async def receive_hook(request: Request) -> Response:
        name: str = request.path_params["name"]
        checked = ctx.hooks.precheck(name)
        if isinstance(checked, HookResponse):
            return _response(checked)
        body = await read_body_capped(request, checked)
        return _response(await ctx.hooks.handle(name, _hook_request(request, body)))

    return [Route("/hooks/{name}", receive_hook, methods=["POST"])]


def _hook_request(request: Request, body: bytes) -> HookRequest:
    """Build the HookRequest: lowercase header names, repeated headers joined with ", "."""
    headers: dict[str, str] = {}
    for raw_name, raw_value in request.headers.raw:
        name, value = raw_name.decode("latin-1").lower(), raw_value.decode("latin-1")
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return HookRequest(
        method=request.method,
        headers=headers,
        query=dict(request.query_params.items()),  # the last value of a repeated key wins
        body=body,
        content_type=_media_type(headers.get("content-type")),
    )


def _response(response: HookResponse) -> Response:
    return JSONResponse(response.body, response.status, dict(response.headers))
