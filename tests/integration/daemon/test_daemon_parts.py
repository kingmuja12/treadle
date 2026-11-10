"""The daemon's building blocks: sockets, uvicorn, the session key, logging, and Host rules.

Also the messages about API token changes and the readiness notice for systemd.
"""

from __future__ import annotations

import dataclasses
import errno
import io
import logging
import os
import signal
import socket
import stat
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from tests.integration.daemon.helpers import has_ipv6_loopback
from tests.support.clock import FakeClock
from tests.support.fakes import (
    FakeRunControl,
    FakeTriggerInfo,
    FakeWebhookHandler,
    FakeWorkflowSource,
    MemoryRunReader,
)
from tests.support.project import ProjectFactory

from treadle import __version__
from treadle import daemon as daemon_module
from treadle.config import ServerSettings, Settings
from treadle.daemon import (
    EmbeddedServer,
    MaskingFilter,
    _bind_sockets,
    _notify_service_manager,
    _token_change,
    bind_socket,
    configure_logging,
    load_secret_key,
    uvicorn_config,
)
from treadle.engine.masking import Masker
from treadle.web.app import WebContext, create_app


@pytest.fixture
def settings(project: ProjectFactory) -> Settings:
    return project.settings()


def test_bind_socket_listens_on_ipv4_with_a_free_port() -> None:
    with bind_socket("127.0.0.1", 0) as sock:
        assert sock.family == socket.AF_INET
        assert sock.getsockname()[1] > 0


@pytest.mark.parametrize("host", ["::1", "[::1]"])
def test_bind_socket_listens_on_ipv6(host: str) -> None:
    try:
        sock = bind_socket(host, 0)
    except OSError as exc:
        pytest.skip(f"no IPv6 loopback here: {exc}")
    with sock:
        assert sock.family == socket.AF_INET6


def _close_all(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        sock.close()


@pytest.mark.skipif(not has_ipv6_loopback(), reason="no IPv6 loopback here")
def test_localhost_binds_both_loopback_addresses_on_one_port() -> None:
    sockets = _bind_sockets("localhost", 0)
    try:
        ipv4, ipv6 = sockets
        port = ipv4.getsockname()[1]
        assert (ipv4.family, ipv4.getsockname()) == (socket.AF_INET, ("127.0.0.1", port))
        assert (ipv6.family, ipv6.getsockname()[:2]) == (socket.AF_INET6, ("::1", port))
        # Nobody else can take [::1] at the port that `localhost` URLs reach.
        with pytest.raises(OSError, match=os.strerror(errno.EADDRINUSE)) as caught:
            socket.create_server(("::1", port), family=socket.AF_INET6)
        assert caught.value.errno == errno.EADDRINUSE
    finally:
        _close_all(sockets)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_an_address_gets_one_socket(host: str) -> None:
    try:
        sockets = _bind_sockets(host, 0)
    except OSError as exc:
        pytest.skip(f"can't bind {host} here: {exc}")
    try:
        (sock,) = sockets
        assert sock.getsockname()[0] == host
    finally:
        _close_all(sockets)


def test_localhost_without_ipv6_listens_on_ipv4_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(host: str, port: int) -> socket.socket:
        if ":" in host:
            raise OSError(errno.EADDRNOTAVAIL, "can't assign the address")
        return bind_socket(host, port)

    monkeypatch.setattr(daemon_module, "bind_socket", bind)

    sockets = _bind_sockets("localhost", 0)
    try:
        assert [sock.family for sock in sockets] == [socket.AF_INET]
    finally:
        _close_all(sockets)


def test_localhost_fails_when_ipv6_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[socket.socket] = []

    def bind(host: str, port: int) -> socket.socket:
        if ":" in host:
            raise OSError(errno.EADDRINUSE, "in use")
        opened.append(bind_socket(host, port))
        return opened[-1]

    monkeypatch.setattr(daemon_module, "bind_socket", bind)

    with pytest.raises(OSError, match="in use") as caught:
        _bind_sockets("localhost", 0)

    assert caught.value.errno == errno.EADDRINUSE
    assert [sock.fileno() for sock in opened] == [-1]  # the IPv4 socket was closed


_LOOPBACK = ServerSettings(host="127.0.0.1")
_PUBLIC = ServerSettings(host="192.0.2.10")
_OLD, _NEW = "o" * 32, "n" * 32


@pytest.mark.parametrize(
    ("server", "in_use", "new", "message"),
    [
        (
            _LOOPBACK,
            None,
            _NEW,
            "TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API "
            "doesn't require a token.",
        ),
        (
            _LOOPBACK,
            _OLD,
            _NEW,
            "TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API "
            "accepts only the previous token.",
        ),
        (
            _LOOPBACK,
            _OLD,
            None,
            "TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API "
            "accepts only the previous token.",
        ),
        (
            _LOOPBACK,
            _OLD,
            "short",
            "TREADLE_TOKEN changed in .env, but Treadle can't start with the new value: "
            "TREADLE_TOKEN must be at least 32 characters long. Until a restart, the API "
            "accepts only the previous token.",
        ),
        (
            _PUBLIC,
            _OLD,
            None,
            "TREADLE_TOKEN changed in .env, but Treadle can't start with the new value: set "
            "TREADLE_TOKEN (at least 32 characters) or bind 127.0.0.1. Until a restart, the API "
            "accepts only the previous token.",
        ),
        (_LOOPBACK, _OLD, _OLD, "TREADLE_TOKEN in .env is the token in use again"),
    ],
)
def test_a_token_change_says_what_the_api_accepts_until_a_restart(
    server: ServerSettings, in_use: str | None, new: str | None, message: str
) -> None:
    text = _token_change(server, ".env", in_use, new)

    assert text == message
    assert _OLD not in text
    assert _NEW not in text


@pytest.fixture
def notify_socket() -> Iterator[tuple[str, socket.socket]]:
    """A datagram socket that plays systemd's notification socket.

    It lives in a short directory, because a socket path holds about 100 characters at most.
    """
    with tempfile.TemporaryDirectory(prefix="tn-", dir="/tmp") as directory:
        path = f"{directory}/notify"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
            listener.bind(path)
            listener.settimeout(5)
            yield path, listener


def test_the_service_manager_hears_the_state(notify_socket: tuple[str, socket.socket]) -> None:
    path, listener = notify_socket

    _notify_service_manager({"NOTIFY_SOCKET": path}, "READY=1")

    assert listener.recv(64) == b"READY=1"


def test_without_a_notification_socket_nothing_is_sent(caplog: pytest.LogCaptureFixture) -> None:
    _notify_service_manager({}, "READY=1")

    assert caplog.records == []


def test_an_unreachable_notification_socket_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = str(tmp_path / "gone")

    _notify_service_manager({"NOTIFY_SOCKET": missing}, "READY=1")

    assert f"can't notify the service manager at {missing}: " in caplog.text


def test_uvicorn_runs_without_lifespan_proxy_headers_or_its_own_logging(
    settings: Settings,
) -> None:
    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("never called")

    config = uvicorn_config(app, settings)

    assert (config.lifespan, config.http, config.ws) == ("off", "h11", "none")
    assert config.log_config is None
    assert not config.access_log
    assert not config.proxy_headers
    assert not config.server_header
    assert config.timeout_graceful_shutdown == 3
    assert config.limit_concurrency == 64
    assert config.h11_max_incomplete_event_size == 16384


def test_the_embedded_server_installs_no_signal_handlers(settings: Settings) -> None:
    async def app(scope: object, receive: object, send: object) -> None:
        raise AssertionError("never called")

    before = signal.getsignal(signal.SIGTERM)
    server = EmbeddedServer(uvicorn_config(app, settings))

    with server.capture_signals():
        assert signal.getsignal(signal.SIGTERM) is before


def test_a_new_secret_key_is_32_random_bytes_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"

    key = load_secret_key(path)

    assert len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_secret_key(path) == key
    assert load_secret_key(tmp_path / "other.key") != key


def test_a_readable_key_is_tightened_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "secret.key"
    path.write_bytes(b"k" * 32)
    path.chmod(0o644)

    key = load_secret_key(path)

    assert key == b"k" * 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "was readable by other users; its mode is now 0600" in caplog.text


def test_a_truncated_key_is_replaced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "secret.key"
    path.write_bytes(b"")
    path.chmod(0o600)

    key = load_secret_key(path)

    assert len(key) == 32
    assert path.read_bytes() == key
    assert "holds fewer than 32 bytes; creating a new key" in caplog.text


def _record(message: str, *args: object, exc: bool = False) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise ValueError("token=hunter2-secret leaked")
        except ValueError:
            exc_info = sys.exc_info()
    return logging.LogRecord("treadle.test", logging.ERROR, __file__, 1, message, args, exc_info)


def test_masking_covers_the_message_arguments_and_traceback() -> None:
    masking = MaskingFilter(lambda: Masker(["hunter2-secret"]))
    record = _record("calling %s failed", "https://x/?key=hunter2-secret", exc=True)
    record.stack_info = "Stack: hunter2-secret"

    assert masking.filter(record)

    assert record.getMessage() == "calling https://x/?key=*** failed"
    assert record.exc_text is not None
    assert "hunter2-secret" not in record.exc_text
    assert "token=*** leaked" in record.exc_text
    assert record.stack_info == "Stack: ***"


def test_masking_keeps_a_malformed_logging_call() -> None:
    record = _record("%d items from hunter2-secret", "not a number")

    MaskingFilter(lambda: Masker(["hunter2-secret"])).filter(record)

    assert record.getMessage() == "%d items from ***"


def test_daemon_logs_use_utc_timestamps_and_mask_secrets(
    root_logging: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)

    configure_logging("info", lambda: Masker(["hunter2-secret"]))
    configure_logging("info", lambda: Masker(["hunter2-secret"]))  # replaces, doesn't add
    logging.getLogger("treadle.engine").info("run=%d used hunter2-secret", 42)
    logging.getLogger("httpx").info("chatty")

    (line,) = stream.getvalue().splitlines()
    stamp, level, name, message = line.split(" ", 3)
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert (level, name, message) == ("INFO", "treadle.engine", "run=42 used ***")
    assert root_logging.level == logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING


def test_debug_logging_lets_third_party_loggers_through(root_logging: logging.Logger) -> None:
    configure_logging("DEBUG", Masker)

    assert root_logging.level == logging.DEBUG
    assert logging.getLogger("uvicorn").level == logging.NOTSET


def test_an_unknown_log_level_is_refused(root_logging: logging.Logger) -> None:
    with pytest.raises(ValueError, match="unknown log level 'loud'"):
        configure_logging("loud", Masker)


async def test_the_dashboard_answers_its_own_loopback_address(settings: Settings) -> None:
    bound = dataclasses.replace(settings, server=ServerSettings(host="127.0.0.2"))
    context = WebContext(
        settings=bound,
        workflows=FakeWorkflowSource(),
        runs=FakeRunControl(),
        reader=MemoryRunReader(),
        hooks=FakeWebhookHandler(),
        triggers=FakeTriggerInfo(),
        token=None,
        secret_key=b"k" * 32,
        clock=FakeClock(),
        version=__version__,
        schema_version=1,
        started_at=datetime(2026, 11, 9, tzinfo=UTC),
        draining=lambda: False,
    )
    transport = httpx.ASGITransport(app=create_app(context))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.2:8765") as client:
        own = await client.get("/api/v1/health")
        other = await client.get("/api/v1/health", headers={"Host": "evil.example:8765"})

    assert own.status_code == 200
    assert other.status_code == 400
