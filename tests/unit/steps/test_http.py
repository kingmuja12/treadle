from __future__ import annotations

import asyncio
import functools
import gzip
import json
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, TypeAlias, cast

import httpx
import pytest
from tests.support.builders import make_runtime, static_template
from tests.support.fakes import RecordingLog

from treadle import __version__
from treadle.errors import StepConfigError, StepError
from treadle.models import LogStream
from treadle.schema import validate
from treadle.steps.base import StepResult
from treadle.steps.http import HttpStep, shared_client, status_matches
from treadle.types import JSONValue

URL = "https://example.net/healthz"
Handler: TypeAlias = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
Build: TypeAlias = Callable[..., httpx.AsyncClient]


class Exchange:
    """Runs one http step against a scripted handler and records the requests it sent."""

    def __init__(self, tmp_path: Path, mock_http: Build, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []
        self.log = RecordingLog()

        async def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            response = handler(request)
            return await response if isinstance(response, Awaitable) else response

        self.rt = make_runtime(tmp_path, log=self.log, http=mock_http(record))

    async def run(self, **raw: object) -> StepResult:
        return await HttpStep().run(params(url=URL, **raw), self.rt)

    @property
    def request(self) -> httpx.Request:
        [request] = self.requests
        return request


def params(**raw: object) -> dict[str, JSONValue]:
    value, issues = validate(HttpStep().schema, raw)
    assert issues == []
    return cast(dict[str, JSONValue], value)


def reply(status: int = 200, **kwargs: Any) -> Handler:
    """Answer with an `httpx.Response`. A `bytes` or `text` body is read in advance."""
    return lambda request: httpx.Response(status, **kwargs)


def raw(body: bytes, headers: dict[str, str]) -> Handler:
    """Answer 200 with a body that arrives undecoded in one chunk, as a network read gives it."""
    return lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


def nested(levels: int) -> str:
    """Return JSON text whose objects and innermost array nest `levels` deep."""
    return '{"a":' * (levels - 1) + "[1]" + "}" * (levels - 1)


@pytest.fixture
def exchange(tmp_path: Path, mock_http: Build) -> Callable[[Handler], Exchange]:
    return lambda handler: Exchange(tmp_path, mock_http, handler)


async def test_a_get_returns_every_output_and_logs_one_line(
    exchange: Callable[[Handler], Exchange],
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.123)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="ok")

    http = exchange(slow)
    result = await http.run()

    assert result.outputs == {
        "status": 200,
        "headers": {"content-type": "text/plain", "content-length": "2"},
        "body": "ok",
        "json": None,
        "url": URL,
        "elapsed_ms": 123,
    }
    assert result.exit_code is None
    assert http.log.lines == [(LogStream.SYSTEM, f"GET {URL} -> 200 (123 ms)")]
    assert http.request.headers["user-agent"].startswith("treadle/")


@pytest.mark.parametrize("method", ["get", "head", "Post", "PUT", "patch", "delete", "options"])
async def test_every_method_in_any_case(
    exchange: Callable[[Handler], Exchange], method: str
) -> None:
    http = exchange(reply(204))

    await http.run(method=method)

    assert http.request.method == method.upper()


async def test_headers_are_sent_and_response_headers_are_normalized(
    exchange: Callable[[Handler], Exchange],
) -> None:
    response_headers = [("Set-Cookie", "a=1"), ("set-cookie", "b=2"), ("X-Trace", "t")]
    http = exchange(reply(200, headers=response_headers))

    result = await http.run(headers={"X-Api-Key": "k", "User-Agent": "custom/1"})

    assert http.request.headers["x-api-key"] == "k"
    assert http.request.headers.get_list("user-agent") == ["custom/1"]
    assert result.outputs["headers"] == {"set-cookie": "a=1, b=2", "x-trace": "t"}


async def test_query_values_repeat_and_keep_the_urls_own_query(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(200))
    query = {
        "tag": ["a", None, "b"],
        "n": 1,
        "flag": True,
        "ratio": 1.5,
        "whole": 2.0,
        "q": "a b",
        "empty": None,
    }

    await HttpStep().run(params(url="https://example.net/s?sig=a%2Fb", query=query), http.rt)

    assert http.request.url.query == (
        b"sig=a%2Fb&tag=a&tag=&tag=b&n=1&flag=true&ratio=1.5&whole=2&q=a+b&empty="
    )


async def test_an_empty_query_leaves_the_url_unchanged(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(200))

    await HttpStep().run(params(url="https://example.net/s?sig=a%2Fb&x"), http.rt)

    assert str(http.request.url) == "https://example.net/s?sig=a%2Fb&x"


@pytest.mark.parametrize(
    ("value", "content"),
    [
        pytest.param({"a": [1, None], "é": True}, '{"a":[1,null],"é":true}', id="object"),
        pytest.param(None, "null", id="null"),
        pytest.param("text", '"text"', id="string"),
    ],
)
async def test_json_bodies_are_compact_utf8(
    exchange: Callable[[Handler], Exchange], value: JSONValue, content: str
) -> None:
    http = exchange(reply(200))

    await http.run(method="POST", json=value)

    assert http.request.content == content.encode()
    assert http.request.headers["content-type"] == "application/json"


async def test_a_content_type_header_wins_over_the_json_default(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(200))

    await http.run(json={"a": 1}, headers={"Content-Type": "application/merge-patch+json"})

    assert http.request.headers.get_list("content-type") == ["application/merge-patch+json"]


async def test_a_text_body_is_sent_as_utf8_without_a_content_type(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(200))

    await http.run(method="PUT", body="héllo")

    assert http.request.content == "héllo".encode()
    assert "content-type" not in http.request.headers


def test_json_and_body_are_mutually_exclusive() -> None:
    _, issues = validate(HttpStep().schema, {"url": URL, "json": {}, "body": "x"})

    assert [(issue.code, issue.message) for issue in issues] == [
        ("E105", "'json' and 'body' can't be used together")
    ]


@pytest.mark.parametrize(
    ("code", "expect", "matches"),
    [
        (200, [200], True),
        (201, [200], False),
        (204, ["2xx"], True),
        (404, ["2xx"], False),
        (301, [200, "3xx"], True),
        (500, ["5XX"], True),
        (199, ["2xx"], False),
    ],
)
def test_status_matches(code: int, expect: list[int | str], matches: bool) -> None:
    assert status_matches(code, expect) is matches


async def test_an_expected_error_status_succeeds(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(404, text="gone"))

    result = await http.run(expect=[404, "5xx"])

    assert result.outputs["status"] == 404


async def test_an_unexpected_status_fails_with_the_outputs_and_a_body_excerpt(
    exchange: Callable[[Handler], Exchange],
) -> None:
    body = "  " + "x" * 1500 + "\n" + "y" * 100 + "\n" + "z" * 5000
    http = exchange(reply(503, text=body))

    with pytest.raises(StepError) as caught:
        await http.run()

    error = caught.value
    excerpt = "x" * 1500 + "\n" + "y" * 100
    assert error.message == f"GET {URL} returned 503, expected 2xx: {excerpt}"
    assert error.retryable is True
    assert error.outputs["status"] == 503
    assert error.outputs["body"] == body


SECRET_KEY = "tok_LIVE_9f8e7d6c5b4a39281706f5e4d3c2b1a0"


@pytest.mark.parametrize(
    ("body", "excerpt"),
    [
        pytest.param(
            "Unauthorized\n" + "x" * 2020 + f" key {SECRET_KEY} rejected\n",
            "Unauthorized",
            id="secret-across-the-cut",
        ),
        pytest.param("x" * 2040 + f" key {SECRET_KEY}", "", id="one-long-line"),
        pytest.param("a" * 2047 + "\n" + "b" * 10, "a" * 2047, id="cut-at-a-line-end"),
        pytest.param("a" * 2048 + "\r\nb", "a" * 2048, id="cut-before-crlf"),
    ],
)
async def test_the_body_excerpt_never_ends_inside_a_line_that_the_cut_splits(
    exchange: Callable[[Handler], Exchange], body: str, excerpt: str
) -> None:
    http = exchange(reply(401, text=body))

    with pytest.raises(StepError) as caught:
        await http.run()

    expected = f"GET {URL} returned 401, expected 2xx"
    assert caught.value.message == (f"{expected}: {excerpt}" if excerpt else expected)
    assert all(SECRET_KEY[:size] not in caught.value.message for size in range(5, 41))


async def test_an_unexpected_empty_response_has_no_excerpt(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(500))

    with pytest.raises(StepError, match=r"returned 500, expected 200, 3xx$"):
        await http.run(expect=[200, "3xx"])


def redirecting(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/old":
        return httpx.Response(301, headers={"Location": "/new"})
    return httpx.Response(200, text="moved here")


async def test_redirects_are_not_followed_by_default_and_the_error_says_how(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(redirecting)

    with pytest.raises(StepError) as caught:
        await HttpStep().run(params(url="https://example.net/old"), http.rt)

    assert caught.value.message == (
        "GET https://example.net/old returned 301, expected 2xx; the server redirected to "
        "https://example.net/new; set follow_redirects: true to follow it"
    )
    assert caught.value.outputs["status"] == 301
    assert len(http.requests) == 1


async def test_followed_redirects_report_the_final_url(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(redirecting)

    result = await HttpStep().run(
        params(url="https://example.net/old", follow_redirects=True), http.rt
    )

    assert (result.outputs["status"], result.outputs["url"]) == (200, "https://example.net/new")
    assert [request.url.path for request in http.requests] == ["/old", "/new"]


async def test_a_redirect_loop_is_a_retryable_failure(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(302, headers={"Location": URL}))

    with pytest.raises(StepError, match="TooManyRedirects") as caught:
        await http.run(follow_redirects=True)

    assert caught.value.retryable is True
    assert len(http.requests) == 6


class LazyBody:
    """A response body of `chunks` 64 KiB chunks that records how many were read."""

    def __init__(self, chunks: int) -> None:
        self.chunks = chunks
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.chunks):
            self.read += 1
            yield bytes(64 * 1024)


def gzipped(data: bytes, layers: int = 1) -> bytes:
    for _ in range(layers):
        data = gzip.compress(data, 6)
    return data


@functools.cache
def compressed_zeros(encoding: str) -> bytes:
    """Return 16 MiB of zero bytes, compressed to about 16 KiB."""
    zeros = bytes(16 << 20)
    return gzipped(zeros) if encoding == "gzip" else zlib.compress(zeros, 6)


async def peak_memory(run: Callable[[], Awaitable[object]]) -> int:
    """Await `run()` and return the peak of the memory that it allocated, in bytes."""
    tracemalloc.start()
    try:
        await run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


async def test_a_redirect_body_is_never_read(exchange: Callable[[Handler], Exchange]) -> None:
    redirect_body = LazyBody(chunks=256)  # 16 MiB, if anything read it

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"}, content=redirect_body)
        return httpx.Response(200, text="done")

    http = exchange(handler)
    results: list[StepResult] = []

    async def run() -> None:
        start = params(url="https://example.net/start", follow_redirects=True)
        results.append(await HttpStep().run({**start, "max_response_bytes": 1024}, http.rt))

    peak = await peak_memory(run)

    assert peak < 4 << 20
    assert [(result.outputs["status"], result.outputs["body"]) for result in results] == [
        (200, "done")
    ]
    assert redirect_body.read == 0
    assert [request.url.path for request in http.requests] == ["/start", "/final"]


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_compressed_body_never_inflates_past_the_limit(
    exchange: Callable[[Handler], Exchange], encoding: str
) -> None:
    http = exchange(raw(compressed_zeros(encoding), {"Content-Encoding": encoding}))
    caught: list[StepConfigError] = []

    async def run() -> None:
        try:
            await http.run()
        except StepConfigError as exc:
            caught.append(exc)

    peak = await peak_memory(run)

    assert [error.message for error in caught] == [
        "the response is larger than max_response_bytes (1.0 MiB)"
    ]
    assert peak < 4 << 20


async def test_a_body_compressed_more_than_once_is_refused(
    exchange: Callable[[Handler], Exchange],
) -> None:
    content = gzipped(bytes(16 << 20), layers=2)  # a few hundred bytes
    http = exchange(raw(content, {"Content-Encoding": "gzip, gzip"}))

    async def run() -> None:
        with pytest.raises(StepConfigError) as caught:
            await http.run()
        assert caught.value.message == (
            "the response is compressed more than once (Content-Encoding: gzip, gzip), "
            "which the http step doesn't decode"
        )

    assert await peak_memory(run) < 4 << 20


@pytest.mark.parametrize(
    ("encoding", "content"),
    [
        pytest.param("gzip", gzipped(b"hello " * 1000), id="gzip"),
        pytest.param("deflate", zlib.compress(b"hello " * 1000), id="zlib-deflate"),
        pytest.param("deflate", zlib.compress(b"hello " * 1000)[2:-4], id="raw-deflate"),
        pytest.param("identity, GZIP", gzipped(b"hello " * 1000), id="with-identity"),
        pytest.param("br", b"hello " * 1000, id="unknown-passes-through"),
    ],
)
async def test_compressed_bodies_are_decoded(
    exchange: Callable[[Handler], Exchange], encoding: str, content: bytes
) -> None:
    http = exchange(raw(content, {"Content-Encoding": encoding}))

    result = await http.run()

    assert result.outputs["body"] == "hello " * 1000
    assert http.request.headers["accept-encoding"] == "gzip, deflate"


async def test_a_body_that_isnt_valid_gzip_is_a_retryable_failure(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(raw(b"not gzip", {"Content-Encoding": "gzip"}))

    with pytest.raises(StepError) as caught:
        await http.run()

    assert caught.value.message.startswith(f"GET {URL} failed: DecodingError: ")
    assert caught.value.retryable is True


def login(request: httpx.Request) -> httpx.Response:
    """A server whose /login sets a session cookie, as a router's admin page might."""
    if request.url.path == "/login":
        return httpx.Response(200, headers={"Set-Cookie": "session=admin-77; Path=/"})
    return httpx.Response(200, text="ok")


async def test_a_cookie_that_one_step_receives_is_never_sent_by_another(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(login)

    await HttpStep().run(params(url="https://router.home.test/login"), http.rt)
    await HttpStep().run(params(url="https://router.home.test/status"), http.rt)

    assert [request.headers.get("cookie") for request in http.requests] == [None, None]


async def test_an_explicit_cookie_header_is_sent_as_written(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(login)
    status = params(url="https://router.home.test/status", headers={"Cookie": "session=mine"})

    await HttpStep().run(params(url="https://router.home.test/login"), http.rt)
    await HttpStep().run(status, http.rt)

    assert http.requests[1].headers.get_list("cookie") == ["session=mine"]


async def test_a_cookie_that_a_redirect_sets_reaches_only_the_rest_of_that_request(
    exchange: Callable[[Handler], Exchange],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            cookie = "session=s1; Path=/"
            return httpx.Response(302, headers={"Location": "/home", "Set-Cookie": cookie})
        if request.url.path == "/home":
            signed_in = request.headers.get("cookie") == "session=s1"
            return httpx.Response(200 if signed_in else 401)
        if request.url.host == "example.net":
            return httpx.Response(302, headers={"Location": "https://elsewhere.test/"})
        return httpx.Response(200)

    http = exchange(handler)
    sign_in = params(url="https://example.net/login", method="POST", follow_redirects=True)

    result = await HttpStep().run(sign_in, http.rt)
    await HttpStep().run(params(url="https://example.net/away", follow_redirects=True), http.rt)

    assert result.outputs["status"] == 200
    assert [(request.method, request.url.path) for request in http.requests] == [
        ("POST", "/login"),
        ("GET", "/home"),
        ("GET", "/away"),
        ("GET", "/"),
    ]
    assert [request.headers.get("cookie") for request in http.requests] == [
        None,
        "session=s1",
        None,
        None,
    ]


async def test_the_shared_client_never_keeps_a_cookie(tmp_path: Path) -> None:
    client = shared_client(transport=httpx.MockTransport(login))
    try:
        rt = make_runtime(tmp_path, http=client)

        await HttpStep().run(params(url="https://router.home.test/login"), rt)

        assert len(client.cookies.jar) == 0
        assert client.headers["user-agent"] == f"treadle/{__version__}"
        assert client.max_redirects == 5
    finally:
        await client.aclose()


async def test_a_large_response_aborts_while_it_streams(
    exchange: Callable[[Handler], Exchange],
) -> None:
    produced: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for index in range(100):
            produced.append(index)
            yield b"x" * 1024

    http = exchange(reply(200, content=chunks()))

    with pytest.raises(StepConfigError) as caught:
        await http.run(max_response_bytes="2KiB")

    message = "the response is larger than max_response_bytes (2.0 KiB)"
    assert caught.value.message == message
    assert caught.value.retryable is False
    assert len(produced) == 3
    assert http.log.lines == [(LogStream.SYSTEM, f"GET {URL} -> {message}")]


@pytest.mark.parametrize("read_in_advance", [False, True], ids=["streamed", "read"])
async def test_a_response_at_the_size_limit_is_kept_and_one_past_it_isnt(
    exchange: Callable[[Handler], Exchange], read_in_advance: bool
) -> None:
    def body(size: int) -> Handler:
        content = b"x" * size
        return reply(200, content=content) if read_in_advance else raw(content, {})

    kept = await exchange(body(2048)).run(max_response_bytes="2KiB")
    with pytest.raises(StepConfigError, match="larger than max_response_bytes"):
        await exchange(body(2049)).run(max_response_bytes="2KiB")

    assert kept.outputs["body"] == "x" * 2048


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        pytest.param("application/json", '{"ok": true}', {"ok": True}, id="json"),
        pytest.param("application/json; charset=utf-8", "[1, 2.5]", [1, 2.5], id="charset"),
        pytest.param("Application/Problem+JSON", '{"title": "x"}', {"title": "x"}, id="suffix"),
        pytest.param("text/plain", '{"ok": true}', None, id="not-json"),
        pytest.param("application/json", "not json", None, id="invalid"),
        pytest.param("application/json", "[NaN]", None, id="nan"),
        pytest.param("application/json", "[-Infinity]", None, id="infinity"),
        pytest.param("application/json", "[1e999]", None, id="overflow"),
        pytest.param("application/json", nested(64), json.loads(nested(64)), id="64-levels"),
        pytest.param("application/json", nested(65), None, id="65-levels"),
        pytest.param("application/json", "[" * 100_000 + "]" * 100_000, None, id="too-deep"),
        pytest.param(None, '{"ok": true}', None, id="no-content-type"),
    ],
)
async def test_json_responses_are_parsed(
    exchange: Callable[[Handler], Exchange],
    content_type: str | None,
    body: str,
    expected: JSONValue,
) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    http = exchange(reply(200, headers=headers, content=body.encode()))

    result = await http.run()

    assert result.outputs["json"] == expected
    assert result.outputs["body"] == body


async def test_the_body_uses_the_response_encoding_with_replacement(
    exchange: Callable[[Handler], Exchange],
) -> None:
    latin = exchange(
        reply(200, headers={"Content-Type": "text/plain; charset=latin-1"}, content=b"caf\xe9")
    )
    broken = exchange(reply(200, content=b"caf\xc3"))

    assert (await latin.run()).outputs["body"] == "café"
    assert (await broken.run()).outputs["body"] == "caf�"


@pytest.mark.parametrize("charset", ["base64", "rot13", "zlib_codec", "hex", "idna", "undefined"])
async def test_a_charset_that_cant_decode_text_falls_back_to_utf8(
    exchange: Callable[[Handler], Exchange], charset: str
) -> None:
    headers = {"Content-Type": f"application/json; charset={charset}"}
    http = exchange(reply(200, headers=headers, content=b'{"name": "caf\xc3\xa9"}\xff'))

    result = await http.run()

    assert result.outputs["body"] == '{"name": "café"}\N{REPLACEMENT CHARACTER}'
    assert result.outputs["json"] is None
    assert http.log.texts() == [
        f"GET {URL} -> 200 (0 ms)",
        f"the response charset {charset} can't decode text; decoded the body as UTF-8",
    ]


async def test_a_json_body_with_an_unusable_charset_still_parses(
    exchange: Callable[[Handler], Exchange],
) -> None:
    headers = {"Content-Type": "application/json; charset=base64"}
    http = exchange(reply(200, headers=headers, content=b'{"ok": true}'))

    result = await http.run()

    assert result.outputs["json"] == {"ok": True}


async def test_transport_errors_are_retryable(exchange: Callable[[Handler], Exchange]) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = exchange(refuse)

    with pytest.raises(StepError) as caught:
        await http.run()

    assert caught.value.message == f"GET {URL} failed: ConnectError: connection refused"
    assert caught.value.retryable is True
    assert http.log.lines == [(LogStream.SYSTEM, f"GET {URL} -> ConnectError: connection refused")]


async def test_a_transport_error_without_details_names_its_type(
    exchange: Callable[[Handler], Exchange],
) -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    with pytest.raises(StepError) as caught:
        await exchange(stall).run()

    assert caught.value.message == f"GET {URL} failed: ReadTimeout"


async def test_the_request_timeout_is_a_retryable_failure(
    exchange: Callable[[Handler], Exchange],
) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        return httpx.Response(200)

    http = exchange(hang)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(StepError) as caught:
        await http.run(timeout="1.5s")

    assert caught.value.message == f"GET {URL} failed: timed out after 1.5s"
    assert caught.value.retryable is True
    assert loop.time() - started == pytest.approx(1.5)


async def test_the_timeout_covers_a_slow_body(exchange: Callable[[Handler], Exchange]) -> None:
    async def drip() -> AsyncIterator[bytes]:
        for _ in range(10):
            await asyncio.sleep(1)
            yield b"x"

    http = exchange(reply(200, content=drip()))

    with pytest.raises(StepError, match=r"timed out after 3\.5s"):
        await http.run(timeout=3.5)


async def test_a_header_that_cant_be_encoded_is_a_config_error(
    exchange: Callable[[Handler], Exchange],
) -> None:
    http = exchange(reply(200))

    with pytest.raises(StepConfigError, match="can't build the GET request: 'ascii' codec"):
        await http.run(headers={"X-Name": "Jürgen"})

    assert http.requests == []


def test_the_schema_fills_defaults() -> None:
    assert params(url=URL) == {
        "method": "GET",
        "url": URL,
        "headers": {},
        "query": {},
        "timeout": 30.0,
        "expect": ["2xx"],
        "follow_redirects": False,
        "max_response_bytes": 1 << 20,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"url": URL, "method": "FETCH"},
            ("E104", "must be one of: GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"),
            id="method",
        ),
        pytest.param(
            {"url": "ftp://example.net/f"},
            ("E104", "must be an http or https URL, such as https://example.net/"),
            id="scheme",
        ),
        pytest.param(
            {"url": "localhost:8080"},
            ("E104", "must be an http or https URL, such as https://example.net/"),
            id="no-scheme",
        ),
        pytest.param(
            {"url": "http://[::1"},
            ("E104", "must be an http or https URL, such as https://example.net/"),
            id="invalid",
        ),
        pytest.param({"url": 42}, ("E103", "expected a string, got a number"), id="url-type"),
        pytest.param({}, ("E102", "missing required key 'url'"), id="missing-url"),
        pytest.param(
            {"url": URL, "expect": ["2xy"]},
            ("E104", "must be one of: 1xx, 2xx, 3xx, 4xx, 5xx"),
            id="expect-pattern",
        ),
        pytest.param(
            {"url": URL, "expect": [700]},
            ("E104", "must be between 100 and 599"),
            id="expect-range",
        ),
        pytest.param(
            {"url": URL, "expect": [True]},
            ("E103", "expected an integer or one of: 1xx, 2xx, 3xx, 4xx, 5xx, got a boolean"),
            id="expect-type",
        ),
        pytest.param(
            {"url": URL, "expect": []}, ("E104", "must have at least 1 item"), id="no-expect"
        ),
        pytest.param(
            {"url": URL, "timeout": "2d"}, ("E104", "must be between 1ms and 1d"), id="timeout"
        ),
        pytest.param(
            {"url": URL, "max_response_bytes": "11MiB"},
            ("E104", "must be between 0 B and 10.0 MiB"),
            id="max-response",
        ),
        pytest.param(
            {"url": URL, "query": {"q": {"a": 1}}},
            ("E103", "expected a scalar or a list of scalars, got a mapping"),
            id="query-mapping",
        ),
        pytest.param(
            {"url": URL, "query": {"q": 1e999}},
            ("E104", "must be a finite number"),
            id="query-infinity",
        ),
        pytest.param(
            {"url": URL, "headers": {"X": 1}},
            ("E103", "expected a string, got a number"),
            id="header-type",
        ),
    ],
)
def test_the_schema_rejects_bad_parameters(
    raw: dict[str, object], expected: tuple[str, str]
) -> None:
    _, issues = validate(HttpStep().schema, raw)

    assert [(issue.code, issue.message) for issue in issues] == [expected]


def test_a_misspelled_method_gets_a_suggestion() -> None:
    _, [issue] = validate(HttpStep().schema, {"url": URL, "method": "psot"})

    assert issue.hint == "did you mean 'POST'?"


def test_a_nested_list_in_a_query_value_points_at_the_item() -> None:
    _, issues = validate(HttpStep().schema, {"url": URL, "query": {"q": [1, [2]]}}, path=("with",))

    assert [(issue.code, issue.message, issue.path) for issue in issues] == [
        ("E103", "expected a scalar, got a list", ("with", "query", "q", 1))
    ]


def test_status_patterns_are_case_insensitive_and_stored_lowercase() -> None:
    assert params(url=URL, expect=["2XX", 404, "5xx"])["expect"] == ["2xx", 404, "5xx"]


def test_the_custom_field_specs_describe_their_values() -> None:
    fields = HttpStep().schema.fields

    assert fields["method"].spec.describe() == (
        "one of: GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
    )
    assert fields["url"].spec.describe() == "an http or https URL"


def test_templates_pass_the_schema_at_load_time() -> None:
    raw = {
        "url": static_template("u"),
        "method": static_template("m"),
        "json": static_template("j"),
        "query": {"one": static_template("q"), "many": [1, static_template("q")]},
        "expect": [static_template("e")],
    }

    _, issues = validate(HttpStep().schema, raw, allow_templates=True)

    assert issues == []


def test_the_step_type_attributes_and_check() -> None:
    step = HttpStep()

    assert step.name == "http"
    assert step.output_names == {"status", "headers", "body", "json", "url", "elapsed_ms"}
    assert step.reserved_outputs == step.output_names
    assert step.taint_sinks == {"url": "url"}
    assert step.check({"url": URL, "timeout": 99999.0}, ("with",)) == []
