"""Web security: sessions, CSRF tokens, body limits, and the security middleware.

This module also defines `WebContext`, the dependencies every route module receives.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from treadle.clock import Clock
from treadle.config import ServerSettings, Settings
from treadle.protocols import RunControl, RunReader, TriggerInfo, WebhookHandler, WorkflowSource
from treadle.serialize import error_json
from treadle.units import format_duration, format_size

SESSION_COOKIE: Final = "treadle_session"
CSRF_COOKIE: Final = "treadle_csrf"
CSRF_FIELD: Final = "csrf_token"

_LOOPBACK_HOSTS: Final = ("127.0.0.1", "localhost", "[::1]")
_ANONYMOUS: Final = "anonymous"
_SESSION_RE: Final = re.compile(r"v1\.(\d{1,15})\.([0-9a-f]{16})\.([0-9a-f]{64})", re.ASCII)
_CSRF_RE: Final = re.compile(r"([A-Za-z0-9_-]{16,128})\.([0-9a-f]{64})", re.ASCII)
_MAX_FORM_FIELDS: Final = 200
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_SAME_ORIGIN_FETCHES: Final = frozenset({"same-origin", "none"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_OPEN_PATHS: Final = frozenset({"/api/v1/health", "/login"})
_OPEN_PREFIXES: Final = ("/static/", "/hooks/")
_CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_SECURITY_HEADERS: Final = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_log = logging.getLogger("treadle.web")


# WebContext belongs with the application factory in web.app, but web.api, web.dashboard,
# and web.hooks take it and come before web.app in layer 8, and the architecture test
# counts TYPE_CHECKING imports. So it lives here, in the first layer-8 module, and web.app
# re-exports it.
@dataclass(frozen=True, slots=True)
class WebContext:
    """Everything the web application needs. The daemon creates and owns each resource."""

    settings: Settings
    workflows: WorkflowSource
    runs: RunControl
    reader: RunReader
    hooks: WebhookHandler
    triggers: TriggerInfo
    token: str | None
    secret_key: bytes
    clock: Clock
    version: str
    schema_version: int
    started_at: datetime
    draining: Callable[[], bool]


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings in constant time through their SHA-256 digests."""
    return hmac.compare_digest(_digest(a), _digest(b))


def allowed_hosts_for(server: ServerSettings) -> list[str]:
    """Return the Host allowlist.

    On a loopback bind it's the loopback names, the bound address itself (an IPv6 address in
    brackets), and `allowed_hosts`; otherwise it's `allowed_hosts`, or `*` when that's empty.
    DNS rebinding sends a host name, never the address literal, so allowing the bound address
    keeps that protection.
    """
    if server.is_loopback:
        hosts = list(_LOOPBACK_HOSTS)
        own = _host_literal(server.host)
        if own.lower() not in hosts:
            hosts.append(own)
        return list(dict.fromkeys([*hosts, *server.allowed_hosts]))
    return list(server.allowed_hosts) or ["*"]


def issue_session(secret_key: bytes, token: str, now: float) -> str:
    """Return a session cookie value, `v1.<issued>.<token fingerprint>.<mac>`."""
    payload = f"v1.{int(now)}.{_fingerprint(token)}"
    return f"{payload}.{_mac(secret_key, payload)}"


def check_session(
    secret_key: bytes, token: str, cookie: str | None, now: float, ttl: float
) -> bool:
    """True when the session cookie is valid for the current token.

    Valid means the MAC verifies, the token fingerprint matches, and the cookie is younger
    than `ttl` seconds.
    """
    match = _SESSION_RE.fullmatch(cookie) if cookie else None
    if match is None:
        return False
    payload, _, mac = match.string.rpartition(".")
    return (
        constant_time_equals(mac, _mac(secret_key, payload))
        and constant_time_equals(match[2], _fingerprint(token))
        and now - int(match[1]) < ttl
    )


def session_binding(cookie: str | None) -> str:
    """Return the session cookie's MAC, or "anonymous" without a session."""
    match = _SESSION_RE.fullmatch(cookie) if cookie else None
    return match[3] if match else _ANONYMOUS


def issue_csrf(secret_key: bytes, binding: str) -> str:
    """Return a CSRF token, `<nonce>.<mac>`, bound to the session binding."""
    nonce = secrets.token_urlsafe(16)
    return f"{nonce}.{_mac(secret_key, 'csrf' + nonce + binding)}"


def check_csrf(secret_key: bytes, cookie: str | None, submitted: str | None, binding: str) -> bool:
    """True when the submitted token equals the cookie and its MAC verifies for `binding`."""
    if cookie is None or submitted is None or not constant_time_equals(cookie, submitted):
        return False
    match = _CSRF_RE.fullmatch(cookie)
    return match is not None and constant_time_equals(
        match[2], _mac(secret_key, "csrf" + match[1] + binding)
    )


def safe_next(target: str | None) -> str:
    """Return `target` when it's a same-site relative path, else "/"."""
    # Control characters are refused too, because browsers drop tabs and newlines from
    # URLs, which would turn "/\t/example.com" into "//example.com".
    if (
        not target
        or not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in target)
    ):
        return "/"
    return target


async def read_body_capped(request: Request, limit: int) -> bytes:
    """Read the request body, raising HTTPException(413) past `limit` bytes.

    A `Content-Length` over the limit fails before anything is read.
    """
    declared = _declared_length(request.headers)
    if declared is not None and declared > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    size = 0
    try:
        async with aclosing(request.stream()) as stream:
            async for chunk in stream:
                size += len(chunk)
                if size > limit:
                    raise _too_large(limit)
                chunks.append(chunk)
    except HTTPException:
        # Only a body limit raises here: the check above, or BodyLimitMiddleware's, which is
        # never below `limit` and may stop the stream first. Both get this limit's message.
        raise _too_large(limit) from None
    return b"".join(chunks)


async def read_form(request: Request, limit: int) -> dict[str, list[str]]:
    """Parse a URL-encoded form with `parse_qsl(max_num_fields=200)` from a capped body.

    Each field's values keep their submission order. More than 200 fields raise
    HTTPException(400).
    """
    body = await read_body_capped(request, limit)
    try:
        pairs = parse_qsl(
            body.decode("utf-8", "replace"),
            keep_blank_values=True,
            max_num_fields=_MAX_FORM_FIELDS,
        )
    except ValueError as exc:
        raise HTTPException(400, f"the form has more than {_MAX_FORM_FIELDS} fields") from exc
    form: dict[str, list[str]] = {}
    for name, value in pairs:
        form.setdefault(name, []).append(value)
    return form


class SecurityHeadersMiddleware:
    """Adds the security headers and `Cache-Control` to every response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, and add the headers when the response starts."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = _security_headers(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(headers)
            await send(message)

        await self._app(scope, receive, send_with_headers)


class BodyLimitMiddleware:
    """Limits request bodies to `hook_limit` bytes under /hooks/ and to `limit` bytes elsewhere.

    A request that declares a larger `Content-Length` gets 413 before anything reads its
    body: the JSON error shape on API and hook paths, plain text elsewhere. A body that
    streams past the limit makes `receive` raise HTTPException(413), which the route's error
    handler answers.
    """

    def __init__(self, app: ASGIApp, *, limit: int, hook_limit: int) -> None:
        self._app = app
        self._limit = limit
        self._hook_limit = hook_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse a declared body over the limit, or pass the request on and count its body."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path: str = scope["path"]
        limit = self._hook_limit if path.startswith("/hooks/") else self._limit
        declared = _declared_length(Headers(scope=scope))
        if declared is not None and declared > limit:
            error = _too_large(limit)
            refusal = _denied(path, error.status_code, "payload_too_large", error.detail)
            await refusal(scope, receive, send)
            return
        received = 0

        async def receive_within_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _too_large(limit)
            return message

        await self._app(scope, receive_within_limit, send)


class HostCheckMiddleware:
    """Rejects Host headers outside the allowlist, except under the exempt prefixes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Sequence[str],
        exempt_prefixes: tuple[str, ...] = ("/hooks/",),
    ) -> None:
        self._app = app
        self._checked = TrustedHostMiddleware(app, list(allowed_hosts), www_redirect=False)
        self._exempt = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the Host header with Starlette's TrustedHostMiddleware, which answers 400."""
        if scope["type"] == "http" and scope["path"].startswith(self._exempt):
            await self._app(scope, receive, send)
        else:
            await self._checked(scope, receive, send)


class OriginGuardMiddleware:
    """Returns 403 for cross-site unsafe requests, by `Sec-Fetch-Site` and `Origin`."""

    def __init__(self, app: ASGIApp, *, exempt_prefixes: tuple[str, ...] = ("/hooks/",)) -> None:
        self._app = app
        self._exempt = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse an unsafe cross-site request before it reaches a route."""
        if (
            scope["type"] != "http"
            or scope["method"] in _SAFE_METHODS
            or scope["path"].startswith(self._exempt)
            or _same_origin(scope)
        ):
            await self._app(scope, receive, send)
            return
        response = _denied(scope["path"], 403, "forbidden", "cross-site request refused")
        await response(scope, receive, send)


class AuthMiddleware:
    """Requires a bearer token or a session cookie when a token is set."""

    def __init__(
        self, app: ASGIApp, *, token: str | None, secret_key: bytes, ttl: float, clock: Clock
    ) -> None:
        self._app = app
        self._token = token
        self._secret_key = secret_key
        self._ttl = ttl
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Let the request through, or answer 401 or a redirect to the login page.

        Open paths need nothing. API paths take a bearer token or a session cookie, and
        dashboard paths take only a session cookie.
        """
        if scope["type"] != "http" or self._token is None or _is_open(scope["path"]):
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        path: str = scope["path"]
        now = self._clock.now().timestamp()
        cookie = request.cookies.get(SESSION_COOKIE)
        if check_session(self._secret_key, self._token, cookie, now, self._ttl) or (
            _is_api(path) and _bearer_matches(request.headers, self._token)
        ):
            await self._app(scope, receive, send)
            return
        response: Response
        if _is_api(path):
            headers = {"WWW-Authenticate": "Bearer"}
            response = _denied(path, 401, "unauthorized", "authentication required", headers)
        elif request.method in ("GET", "HEAD"):
            query = scope["query_string"].decode("latin-1")
            target = f"{path}?{query}" if query else path
            response = RedirectResponse(f"/login?{urlencode({'next': target})}", 303)
        else:
            response = _denied(path, 401, "unauthorized", "your session expired; sign in again")
        await response(scope, receive, send)


class RequestLogMiddleware:
    """Logs each request's method, path, status, and duration, never bodies or headers."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log one INFO line on the `treadle.web` logger after the request finishes."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # what the client gets when the application raises before answering

        async def send_and_record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, send_and_record)
        finally:
            # The raw path is the target as the client sent it, without the query string.
            # Escaping keeps a decoded control character from forging a log line.
            raw_path: bytes | None = scope.get("raw_path")
            path = raw_path.decode("latin-1") if raw_path else scope["path"]
            elapsed = format_duration(time.perf_counter() - started)
            safe_path = path.encode("unicode_escape").decode("ascii")
            _log.info("%s %s %d %s", scope["method"], safe_path, status, elapsed)


def _host_literal(host: str) -> str:
    """Return a bind address as a Host header names it, with an IPv6 address in brackets."""
    address = host.strip("[]")
    return f"[{address}]" if ":" in address else address


def _security_headers(path: str) -> dict[str, str]:
    """Return the headers that every response carries, `Cache-Control` included."""
    cache = "public, max-age=3600" if path.startswith("/static/") else "no-store"
    return {**_SECURITY_HEADERS, "Cache-Control": cache}


def _media_type(content_type: str | None) -> str | None:
    """Return the lowercase media type of a `Content-Type` value, without parameters."""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type or None


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _is_open(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


def _denied(
    path: str, status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> Response:
    """A middleware's refusal: the JSON error shape for API and hook paths, else plain text."""
    if _is_api(path) or path.startswith("/hooks/"):
        return JSONResponse(error_json(code, message), status, headers)
    return PlainTextResponse(message, status, headers)


def _same_origin(scope: Scope) -> bool:
    """True unless the browser marks the request as cross-site.

    `Sec-Fetch-Site` decides when it's present. Otherwise a present `Origin` must match the
    request's own scheme, host, and port. A request with neither header comes from a
    non-browser client, or from an old browser on the same origin.
    """
    # Refusing a request when either header disagrees would break the dashboard's own
    # forms: every Treadle page sends `Referrer-Policy: no-referrer`, and browsers then
    # send `Origin: null` with same-origin form posts. A browser that sends
    # `Sec-Fetch-Site` computes it from the real origins, so it decides alone when
    # present, as in Go's net/http CrossOriginProtection.
    headers = Headers(scope=scope)
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() in _SAME_ORIGIN_FETCHES
    origin = headers.get("origin")
    if origin is None:
        return True
    own = _origin_parts(f"{scope['scheme']}://{headers.get('host', '')}")
    return own is not None and _origin_parts(origin) == own


def _origin_parts(url: str) -> tuple[str, str, int] | None:
    """Return (scheme, host, port) with the scheme's default port filled in, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    if not parts.hostname or port is None:
        return None
    return parts.scheme, parts.hostname, port


def _bearer_matches(headers: Headers, token: str) -> bool:
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and constant_time_equals(credentials.strip(), token)


def _declared_length(headers: Headers) -> int | None:
    """Return the `Content-Length` value, or None when it's missing or not ASCII digits."""
    declared = headers.get("content-length", "")
    return int(declared) if declared.isascii() and declared.isdigit() else None


def _too_large(limit: int) -> HTTPException:
    return HTTPException(413, f"the request body is larger than {format_size(limit)}")


def _digest(text: str) -> bytes:
    return hashlib.sha256(_utf8(text)).digest()


def _fingerprint(token: str) -> str:
    return hashlib.sha256(_utf8(token)).hexdigest()[:16]


def _mac(secret_key: bytes, message: str) -> str:
    return hmac.new(secret_key, _utf8(message), hashlib.sha256).hexdigest()


def _utf8(text: str) -> bytes:
    # surrogatepass keeps a lone surrogate from raising; header text never holds one.
    return text.encode("utf-8", "surrogatepass")
