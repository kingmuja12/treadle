from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import Message

from treadle.config import ServerSettings
from treadle.web.security import (
    allowed_hosts_for,
    check_csrf,
    check_session,
    constant_time_equals,
    issue_csrf,
    issue_session,
    read_body_capped,
    read_form,
    safe_next,
    session_binding,
)

KEY = b"k" * 32
OTHER_KEY = b"o" * 32
TOKEN = "t" * 40
NOW = 1_800_000_000.0
TTL = 43_200.0


class Body:
    """A request whose body arrives in chunks, counting how often the app reads."""

    def __init__(self, chunks: Sequence[bytes], *, headers: dict[str, str] | None = None) -> None:
        self.reads = 0
        self.messages: list[Message] = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks or [b""])
        ]
        raw = [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]
        scope = {"type": "http", "method": "POST", "path": "/", "headers": raw, "query_string": b""}
        self.request = Request(scope, self.receive)

    async def receive(self) -> Message:
        self.reads += 1
        return self.messages.pop(0)


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        ("secret", "secret", True),
        ("secret", "Secret", False),
        ("secret", "secret-but-longer", False),
        ("", "", True),
        ("pässwörd", "pässwörd", True),
        ("pässwörd", "passwort", False),
    ],
)
def test_constant_time_equals(a: str, b: str, equal: bool) -> None:
    assert constant_time_equals(a, b) is equal


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (ServerSettings(), ["127.0.0.1", "localhost", "[::1]"]),
        (
            ServerSettings(allowed_hosts=("treadle.lan", "localhost")),
            ["127.0.0.1", "localhost", "[::1]", "treadle.lan"],
        ),
        (ServerSettings(host="::1"), ["127.0.0.1", "localhost", "[::1]"]),
        (ServerSettings(host="[::1]"), ["127.0.0.1", "localhost", "[::1]"]),
        (ServerSettings(host="LocalHost"), ["127.0.0.1", "localhost", "[::1]"]),
        (ServerSettings(host="127.0.0.2"), ["127.0.0.1", "localhost", "[::1]", "127.0.0.2"]),
        (
            ServerSettings(host="0:0:0:0:0:0:0:1"),
            ["127.0.0.1", "localhost", "[::1]", "[0:0:0:0:0:0:0:1]"],
        ),
        (
            ServerSettings(host="127.0.0.2", allowed_hosts=("127.0.0.2", "treadle.lan")),
            ["127.0.0.1", "localhost", "[::1]", "127.0.0.2", "treadle.lan"],
        ),
        (ServerSettings(host="192.168.1.20"), ["*"]),
        (ServerSettings(host="192.168.1.20", allowed_hosts=("treadle.lan",)), ["treadle.lan"]),
    ],
)
def test_allowed_hosts_for(server: ServerSettings, expected: list[str]) -> None:
    assert allowed_hosts_for(server) == expected


def test_a_session_cookie_has_the_documented_format() -> None:
    cookie = issue_session(KEY, TOKEN, NOW + 0.9)

    assert re.fullmatch(r"v1\.1800000000\.[0-9a-f]{16}\.[0-9a-f]{64}", cookie)
    assert check_session(KEY, TOKEN, cookie, NOW, TTL)


@pytest.mark.parametrize(("age", "valid"), [(0, True), (TTL - 1, True), (TTL, False), (-60, True)])
def test_a_session_expires_after_its_ttl(age: float, valid: bool) -> None:
    cookie = issue_session(KEY, TOKEN, NOW)

    assert check_session(KEY, TOKEN, cookie, NOW + age, TTL) is valid


def test_a_session_is_bound_to_the_token_and_the_key() -> None:
    cookie = issue_session(KEY, TOKEN, NOW)

    assert not check_session(KEY, "a-rotated-token-" + "x" * 32, cookie, NOW, TTL)
    assert not check_session(OTHER_KEY, TOKEN, cookie, NOW, TTL)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda cookie: cookie.replace("v1.", "v2.", 1),
        lambda cookie: cookie.replace(".1800000000.", ".1900000000.", 1),
        lambda cookie: cookie[:-1] + ("0" if cookie[-1] != "0" else "1"),
        lambda cookie: cookie.upper(),
        lambda cookie: cookie + ".extra",
        lambda cookie: cookie.rpartition(".")[0],
    ],
)
def test_a_tampered_session_is_invalid(tamper: Callable[[str], str]) -> None:
    cookie = issue_session(KEY, TOKEN, NOW)

    assert not check_session(KEY, TOKEN, tamper(cookie), NOW, TTL)


@pytest.mark.parametrize("cookie", [None, "", "garbage", "v1...", "v1.12.abc.def"])
def test_a_missing_or_malformed_session_is_invalid(cookie: str | None) -> None:
    assert not check_session(KEY, TOKEN, cookie, NOW, TTL)


def test_session_binding_is_the_cookie_mac_or_anonymous() -> None:
    cookie = issue_session(KEY, TOKEN, NOW)

    assert session_binding(cookie) == cookie.rpartition(".")[2]
    assert session_binding(None) == "anonymous"
    assert session_binding("") == "anonymous"
    assert session_binding("not-a-session") == "anonymous"


def test_a_csrf_token_verifies_for_its_binding_only() -> None:
    token = issue_csrf(KEY, "anonymous")

    assert re.fullmatch(r"[A-Za-z0-9_-]{22}\.[0-9a-f]{64}", token)
    assert check_csrf(KEY, token, token, "anonymous")
    assert not check_csrf(KEY, token, token, "f" * 64)
    assert not check_csrf(OTHER_KEY, token, token, "anonymous")


def test_csrf_tokens_are_unique() -> None:
    assert len({issue_csrf(KEY, "anonymous") for _ in range(50)}) == 50


@pytest.mark.parametrize(
    ("cookie", "submitted"),
    [
        (None, "token"),
        ("token", None),
        (None, None),
        ("token", "other"),
    ],
)
def test_csrf_needs_the_cookie_and_the_field_to_match(
    cookie: str | None, submitted: str | None
) -> None:
    valid = issue_csrf(KEY, "anonymous")
    cookie = valid if cookie == "token" else cookie
    submitted = valid if submitted == "token" else submitted

    assert not check_csrf(KEY, cookie, submitted, "anonymous")


@pytest.mark.parametrize(
    "token",
    ["short.mac", "no-dot-at-all", "nonce-that-is-long-enough." + "g" * 64, ".".join(["a"] * 3)],
)
def test_a_malformed_csrf_token_is_invalid_even_when_both_copies_match(token: str) -> None:
    assert not check_csrf(KEY, token, token, "anonymous")


def test_a_tampered_csrf_mac_is_invalid() -> None:
    nonce, _, mac = issue_csrf(KEY, "anonymous").partition(".")
    forged = f"{nonce}.{'0' * 64 if mac != '0' * 64 else '1' * 64}"

    assert not check_csrf(KEY, forged, forged, "anonymous")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/", "/"),
        ("/runs", "/runs"),
        ("/runs?status=failed&workflow=nightly", "/runs?status=failed&workflow=nightly"),
        ("/workflows/x#run-now", "/workflows/x#run-now"),
        ("//evil.example", "/"),
        ("///evil.example", "/"),
        ("https://evil.example/", "/"),
        ("runs", "/"),
        ("/\\evil.example", "/"),
        ("/runs\\..", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("/runs\x7f", "/"),
        ("javascript:alert(1)", "/"),
    ],
)
def test_safe_next_accepts_only_same_site_relative_paths(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected


async def test_read_body_capped_joins_the_chunks() -> None:
    body = Body([b"abc", b"def", b"g"])

    assert await read_body_capped(body.request, 7) == b"abcdefg"
    assert body.reads == 3


async def test_read_body_capped_reads_an_empty_body() -> None:
    assert await read_body_capped(Body([]).request, 10) == b""


async def test_read_body_capped_refuses_a_declared_length_without_reading() -> None:
    body = Body([b"x" * 11], headers={"Content-Length": "11"})

    with pytest.raises(HTTPException) as caught:
        await read_body_capped(body.request, 10)

    assert caught.value.status_code == 413
    assert caught.value.detail == "the request body is larger than 10 B"
    assert body.reads == 0


async def test_read_body_capped_stops_a_streamed_body_at_the_limit() -> None:
    body = Body([b"x" * 6, b"x" * 6, b"never read"])

    with pytest.raises(HTTPException) as caught:
        await read_body_capped(body.request, 10)

    assert caught.value.status_code == 413
    assert body.reads == 2


@pytest.mark.parametrize("declared", ["5", "nonsense", "²"])
async def test_read_body_capped_still_counts_when_the_length_lies(declared: str) -> None:
    body = Body([b"x" * 11], headers={"Content-Length": declared})

    with pytest.raises(HTTPException):
        await read_body_capped(body.request, 10)


async def test_read_form_keeps_repeated_values_in_order() -> None:
    raw = b"input.flag=false&input.flag=true&name=Ada+Lovelace&note=&city=Z%C3%BCrich&bare"

    form = await read_form(Body([raw]).request, 1024)

    assert form == {
        "input.flag": ["false", "true"],
        "name": ["Ada Lovelace"],
        "note": [""],
        "city": ["Zürich"],
        "bare": [""],
    }


async def test_read_form_refuses_more_than_200_fields() -> None:
    raw = "&".join(f"f{index}=1" for index in range(201)).encode()

    with pytest.raises(HTTPException) as caught:
        await read_form(Body([raw]).request, 1 << 20)

    assert caught.value.status_code == 400
    assert await read_form(Body([raw.rpartition(b"&")[0]]).request, 1 << 20)


async def test_read_form_caps_the_body() -> None:
    with pytest.raises(HTTPException) as caught:
        await read_form(Body([b"a=" + b"x" * 100]).request, 50)

    assert caught.value.status_code == 413


async def test_read_form_replaces_invalid_utf8() -> None:
    form = await read_form(Body([b"name=\xff%FF"]).request, 100)

    assert form == {"name": ["��"]}
