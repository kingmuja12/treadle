"""The `http` step type: one request through the shared client, checked against `expect`."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from typing import ClassVar, Final, Literal, NoReturn, cast
from urllib.parse import urlencode

import httpx

from treadle.errors import StepConfigError, StepError
from treadle.schema import (
    AnyJSON,
    AnyOf,
    Bool,
    CheckContext,
    Duration,
    Field,
    Int,
    ListOf,
    MapOf,
    Obj,
    Size,
    Str,
)
from treadle.steps.base import StepResult, StepRuntime
from treadle.types import Issue, JSONValue, Severity, YamlPath
from treadle.units import format_duration, format_size

_METHODS: Final = Str(choices=("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"))
_TEXT: Final = Str()
_JSON: Final = AnyJSON()
# Design note: status codes are 100-599.
_STATUS_CODE: Final = Int(min=100, max=599)
_STATUS_CLASS: Final = Str(choices=("1xx", "2xx", "3xx", "4xx", "5xx"))
_STATUS_EITHER: Final = AnyOf(_STATUS_CODE, _STATUS_CLASS)
_BODY_EXCERPT: Final = 2048
_MAX_JSON_DEPTH: Final = 64
_OUTPUTS: Final = frozenset({"status", "headers", "body", "json", "url", "elapsed_ms"})


class _Method:
    """An HTTP method in any letter case, normalized to uppercase."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        return _METHODS.check(value.upper() if isinstance(value, str) else value, path, ctx)

    def describe(self) -> str:
        return _METHODS.describe()


class _Url:
    """An absolute `http` or `https` URL with a host."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, str) and not _is_web_url(value):
            message = f"must be {self.describe()}, such as https://example.net/"
            ctx.issues.append(Issue(Severity.ERROR, "E104", message, path))
            return value
        return _TEXT.check(value, path, ctx)

    def describe(self) -> str:
        return "an http or https URL"


class _QueryValue:
    """A query value: a scalar, where null sends an empty value, or a list of scalars."""

    def __init__(self, *, item: bool = False) -> None:
        self._item = item

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, list) and not self._item:
            return [
                _QUERY_ITEM.check(item, (*path, index), ctx) for index, item in enumerate(value)
            ]
        if isinstance(value, (list, dict)):
            kind = "a list" if isinstance(value, list) else "a mapping"
            message = f"expected {self.describe()}, got {kind}"
            ctx.issues.append(Issue(Severity.ERROR, "E103", message, path))
            return value
        return _JSON.check(value, path, ctx)

    def describe(self) -> str:
        return "a scalar" if self._item else "a scalar or a list of scalars"


_QUERY_ITEM: Final = _QueryValue(item=True)


class _Status:
    """An expected status: a code from 100 to 599, or a class pattern in any case, like 2xx."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, str):
            return _STATUS_CLASS.check(value.lower(), path, ctx)
        if isinstance(value, int) and not isinstance(value, bool):
            return _STATUS_CODE.check(value, path, ctx)
        return _STATUS_EITHER.check(value, path, ctx)

    def describe(self) -> str:
        return _STATUS_EITHER.describe()


_SCHEMA: Final = Obj(
    "http step",
    {
        "method": Field(_Method(), default="GET"),
        "url": Field(_Url(), required=True),
        "headers": Field(MapOf(Str()), default={}),
        "query": Field(MapOf(_QueryValue()), default={}),
        "json": Field(AnyJSON()),
        "body": Field(Str()),
        # Design note: a request needs some time, and no step timeout is longer than 24h.
        "timeout": Field(Duration(min=0.001, max=86400.0), default=30.0),
        # Design note: an empty list could never succeed.
        "expect": Field(ListOf(_Status(), min_len=1), default=["2xx"]),
        "follow_redirects": Field(Bool(), default=False),
        "max_response_bytes": Field(Size(max=10 << 20), default=1 << 20),
    },
    exclusive=[("json", "body")],
)


class HttpStep:
    """Sends one HTTP request with the shared client and checks the status against `expect`.

    Outputs are `status`, `headers`, `body`, `json`, `url`, and `elapsed_ms`.
    """

    name = "http"
    output_names: ClassVar[frozenset[str] | None] = _OUTPUTS
    reserved_outputs: ClassVar[frozenset[str]] = _OUTPUTS
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {"url": "url"}

    @property
    def schema(self) -> Obj:
        """The `with` keys of the `http` table in section 2."""
        return _SCHEMA

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return no issues: the schema covers every rule that the parameters alone decide.

        Design note: comparing a static `timeout` with the step timeout needs the step, which
        this signature doesn't receive, so the loader reports that E407 (section 2's E407 row).
        """
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Send the request, log one system line, and return the response outputs.

        `timeout` covers the whole exchange, including the body. Transport errors and the
        timeout raise a retryable StepError. A body larger than `max_response_bytes` raises
        StepConfigError while it streams. A status outside `expect` raises a StepError that
        carries the outputs.
        """
        method, url = cast(str, params["method"]), cast(str, params["url"])
        timeout = cast(float, params["timeout"])
        follow = params["follow_redirects"] is True
        request = _build_request(rt.http, params, method, url, timeout)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(timeout):
                response = await rt.http.send(request, stream=True, follow_redirects=follow)
                try:
                    body = await _read_capped(response, cast(int, params["max_response_bytes"]))
                finally:
                    await response.aclose()
        except TimeoutError:
            problem = f"timed out after {format_duration(timeout)}"
            raise _request_failed(rt, method, url, problem) from None
        except httpx.RequestError as exc:
            problem = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise _request_failed(rt, method, url, problem) from exc
        except StepConfigError as exc:
            rt.log.system(f"{method} {url} -> {exc.message}")
            raise
        elapsed_ms = round((loop.time() - started) * 1000)
        rt.log.system(f"{method} {url} -> {response.status_code} ({elapsed_ms} ms)")
        outputs = _outputs(response, body, elapsed_ms)
        expect = cast("list[int | str]", params["expect"])
        if not status_matches(response.status_code, expect):
            message = _status_message(response, method, url, expect, str(outputs["body"]))
            raise StepError(message, outputs=outputs)
        return StepResult(outputs)


def status_matches(code: int, expect: Sequence[int | str]) -> bool:
    """True when `code` equals an integer in `expect` or matches an "Nxx" pattern."""
    pattern = f"{code // 100}xx"
    return any(
        item == code if isinstance(item, int) else item.lower() == pattern for item in expect
    )


def _is_web_url(text: str) -> bool:
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL:
        return False
    return url.scheme in ("http", "https") and bool(url.host)


def _build_request(
    client: httpx.AsyncClient,
    params: Mapping[str, JSONValue],
    method: str,
    url: str,
    timeout: float,
) -> httpx.Request:
    """Build the request; a value that can't be sent, such as a non-ASCII header, fails it."""
    content = params.get("body")
    try:
        headers = httpx.Headers(cast("dict[str, str]", params["headers"]))
        if "json" in params:
            content = json.dumps(params["json"], ensure_ascii=False, separators=(",", ":"))
            headers.setdefault("content-type", "application/json")
        target = _with_query(url, cast("dict[str, JSONValue]", params["query"]))
        return client.build_request(
            method, target, headers=headers, content=cast("str | None", content), timeout=timeout
        )
    except (ValueError, httpx.InvalidURL) as exc:
        raise StepConfigError(f"can't build the {method} request: {exc}") from exc


def _with_query(url: str, query: Mapping[str, JSONValue]) -> httpx.URL:
    """Append `query` to the URL's own query string, which httpx would otherwise rewrite."""
    pairs = [
        (key, _display(item))
        for key, value in query.items()
        for item in (value if isinstance(value, list) else [value])
    ]
    parsed = httpx.URL(url)
    if not pairs:
        return parsed
    added = urlencode(pairs).encode("ascii")
    return parsed.copy_with(query=parsed.query + b"&" + added if parsed.query else added)


def _display(value: JSONValue) -> str:
    """Section 3's display string of a scalar query value; null is an empty value."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read the decoded body, failing as soon as it grows past `limit` bytes."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            message = f"the response is larger than max_response_bytes ({format_size(limit)})"
            raise StepConfigError(message)
    return bytes(body)


def _request_failed(rt: StepRuntime, method: str, url: str, problem: str) -> StepError:
    rt.log.system(f"{method} {url} -> {problem}")
    return StepError(f"{method} {url} failed: {problem}")


def _outputs(response: httpx.Response, body: bytes, elapsed_ms: int) -> dict[str, JSONValue]:
    text = body.decode(response.encoding or "utf-8", "replace")
    headers: dict[str, JSONValue] = dict(response.headers.items())
    return {
        "status": response.status_code,
        "headers": headers,
        "body": text,
        "json": _parse_json(response, text),
        "url": str(response.url),
        "elapsed_ms": elapsed_ms,
    }


def _parse_json(response: httpx.Response, text: str) -> JSONValue:
    """Return the parsed body for a JSON media type, or None.

    None also stands for a body that doesn't parse, holds NaN or an infinite number, or
    nests deeper than 64 levels.
    """
    # Design note: response bodies are untrusted, so the `json` output keeps the limits that
    # fromJSON and webhook payloads have: finite numbers and at most 64 levels of nesting.
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media != "application/json" and not media.endswith("+json"):
        return None
    try:
        value: JSONValue = json.loads(text, parse_constant=_reject_constant, parse_float=_finite)
    except (ValueError, RecursionError):
        return None
    return None if _deeper_than(value, _MAX_JSON_DEPTH) else value


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} isn't a JSON value")


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{text} is out of range")
    return number


def _deeper_than(value: JSONValue, levels: int) -> bool:
    """True when lists and objects nest more than `levels` deep. Recursion stops there."""
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return False
    return levels == 0 or any(_deeper_than(item, levels - 1) for item in value)


def _status_message(
    response: httpx.Response, method: str, url: str, expect: Sequence[int | str], body: str
) -> str:
    expected = ", ".join(str(item) for item in expect)
    message = f"{method} {url} returned {response.status_code}, expected {expected}"
    # httpx sets next_request for a redirect it didn't follow, with the Location made absolute.
    if response.next_request is not None:
        target = response.next_request.url
        message += f"; the server redirected to {target}; set follow_redirects: true to follow it"
    excerpt = body[:_BODY_EXCERPT].strip()
    return f"{message}: {excerpt}" if excerpt else message
