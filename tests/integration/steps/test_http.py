"""The `http` step over real sockets, against a small HTTP/1.1 server on 127.0.0.1."""

from __future__ import annotations

import asyncio
import gzip
import tracemalloc
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from types import TracebackType
from typing import cast

import httpx
import pytest
from tests.support.builders import make_runtime

from treadle.errors import StepConfigError
from treadle.schema import validate
from treadle.steps.base import StepResult
from treadle.steps.http import HttpStep, shared_client
from treadle.types import JSONValue

CHUNK = b"x" * (64 * 1024)


def head(status: str, *headers: str, length: int) -> bytes:
    """Return the head of an HTTP/1.1 response whose body has `length` bytes."""
    lines = [f"HTTP/1.1 {status}", f"Content-Length: {length}", "Connection: close", *headers]
    return "\r\n".join(lines).encode() + b"\r\n\r\n"


def response(status: str, *headers: str, body: bytes = b"") -> list[bytes]:
    """Return an HTTP/1.1 response, as chunks to write."""
    return [head(status, *headers, length=len(body)), body]


class LocalServer:
    """Answers each request with the chunks that `respond(path)` returns, then closes.

    `requests` records each request's path and Cookie header.
    """

    def __init__(self, respond: Callable[[str], Iterable[bytes]]) -> None:
        self._respond = respond
        self._server: asyncio.Server | None = None
        self.port = 0
        self.requests: list[tuple[str, str | None]] = []

    async def __aenter__(self) -> LocalServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            request_line, *header_lines = request.strip().split("\r\n")
            fields = (line.split(": ", 1) for line in header_lines)
            headers = {name.lower(): value for name, value in fields}
            path = request_line.split(" ")[1]
            self.requests.append((path, headers.get("cookie")))
            for chunk in self._respond(path):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the client closed the connection before it read everything
        finally:
            writer.close()


async def get(url: str, client: httpx.AsyncClient, tmp_path: Path, **raw: object) -> StepResult:
    value, issues = validate(HttpStep().schema, {"url": url, **raw})
    assert issues == []
    params = cast(dict[str, JSONValue], value)
    return await HttpStep().run(params, make_runtime(tmp_path, http=client))


async def peak_while(work: asyncio.Future[StepResult]) -> int:
    """Await `work` and return the peak of traced memory meanwhile, in bytes."""
    tracemalloc.start()
    try:
        await asyncio.wait([work])
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """The client that `http` steps share, over real sockets."""
    async with shared_client() as shared:
        yield shared


async def test_a_redirect_with_a_large_body_is_followed_without_reading_it(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    def respond(path: str) -> Iterable[bytes]:
        if path == "/start":  # a 16 MiB body behind the redirect
            yield head("302 Found", "Location: /final", length=256 * len(CHUNK))
            yield from [CHUNK] * 256
        else:
            yield from response("200 OK", body=b"done")

    async with LocalServer(respond) as server:
        work = asyncio.ensure_future(
            get(server.url("/start"), client, tmp_path, follow_redirects=True)
        )
        peak = await peak_while(work)
        result = work.result()

    assert (result.outputs["status"], result.outputs["body"]) == (200, "done")
    assert result.outputs["url"] == server.url("/final")
    assert [path for path, _ in server.requests] == ["/start", "/final"]
    assert peak < 8 << 20


async def test_a_gzip_body_never_inflates_past_the_limit(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    bomb = gzip.compress(bytes(32 << 20), 6)  # 32 MiB of zeros in about 32 KiB

    def respond(path: str) -> Iterable[bytes]:
        return response("200 OK", "Content-Encoding: gzip", body=bomb)

    async with LocalServer(respond) as server:
        work = asyncio.ensure_future(get(server.url("/bomb"), client, tmp_path))
        peak = await peak_while(work)

    with pytest.raises(StepConfigError, match="larger than max_response_bytes"):
        work.result()
    assert peak < 8 << 20


async def test_a_gzip_body_is_decoded(client: httpx.AsyncClient, tmp_path: Path) -> None:
    def respond(path: str) -> Iterable[bytes]:
        return response("200 OK", "Content-Encoding: gzip", body=gzip.compress(b"hello" * 100))

    async with LocalServer(respond) as server:
        result = await get(server.url("/hello"), client, tmp_path)

    assert result.outputs["body"] == "hello" * 100


async def test_no_cookie_passes_from_one_request_to_the_next(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    def respond(path: str) -> Iterable[bytes]:
        if path == "/login":
            return response("200 OK", "Set-Cookie: session=admin-77; Path=/")
        return response("200 OK", body=b"ok")

    async with LocalServer(respond) as server:
        await get(server.url("/login"), client, tmp_path)
        await get(server.url("/status"), client, tmp_path)

    assert server.requests == [("/login", None), ("/status", None)]
    assert len(client.cookies.jar) == 0
