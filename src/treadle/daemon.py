"""The `treadle serve` daemon: startup, the task group, signals, and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import logging
import os
import secrets
import signal
import socket
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TypeAlias

import httpx
import uvicorn

from treadle import __version__
from treadle.clock import UTC, Clock, RandomSource, SystemClock, default_random, iso_utc
from treadle.config import Settings
from treadle.control import ControlPoller
from treadle.engine.manager import RunManager
from treadle.engine.masking import Masker
from treadle.errors import BusyError, ConfigError, ShuttingDownError
from treadle.lock import InstanceLock
from treadle.models import RunRequest, Submission
from treadle.registry import WorkflowRegistry
from treadle.retention import retention_loop
from treadle.runner import _http_client, _MaskerCache, _recover, _run_manager
from treadle.secrets import SecretSnapshot, SecretStore
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore
from treadle.triggers.manager import TriggerManager
from treadle.triggers.scheduler import Scheduler
from treadle.triggers.watch import WatchService
from treadle.triggers.webhook import WebhookRouter
from treadle.types import ExitCode, LockHolder
from treadle.web.app import WebContext, create_app
from treadle.web.security import allowed_hosts_for

# This module may import only uvicorn among third-party packages, so it can't use Starlette's
# ASGIApp. This alias names the same thing: an ASGI 3 application.
_ASGIApp: TypeAlias = Callable[[Any, Any, Any], Awaitable[None]]

_KEY_BYTES: Final = 32
_MIN_TOKEN: Final = 32
_LEVELS: Final = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
# Third-party loggers that are chatty at INFO: uvicorn announces its process, and httpx logs
# every request that an `http` step makes. They log at WARNING and above unless debugging.
_QUIET_LOGGERS: Final = ("uvicorn", "httpx", "httpcore")
_HANDLER_NAME: Final = "treadle-daemon"

_log = logging.getLogger(__name__)


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the daemon."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Yield without installing signal handlers, so uvicorn never re-raises a signal."""
        yield


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind a listening socket with `socket.create_server`. Raises OSError.

    An IPv6 address, with or without brackets, gets an IPv6 socket; anything else, including
    a host name, an IPv4 socket.
    """
    address = host.strip("[]")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return socket.create_server((address, port), family=family)


def uvicorn_config(app: _ASGIApp, settings: Settings) -> uvicorn.Config:
    """Return the uvicorn configuration for `app`.

    Lifespan is off because the daemon owns every resource, proxy headers are ignored so they
    can't spoof the client address, and uvicorn configures no logging of its own.
    """
    return uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        lifespan="off",
        http="h11",
        ws="none",
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=True,
        timeout_graceful_shutdown=3,
        timeout_keep_alive=5,
        limit_concurrency=64,
        h11_max_incomplete_event_size=16384,
    )


def load_secret_key(path: Path) -> bytes:
    """Return the session key, creating 32 random bytes with `O_EXCL` and mode 0600.

    An existing key that other users can read gets mode 0600, with a warning. A key shorter
    than 32 bytes, such as an empty file left by a crash, is replaced, which signs everyone
    out.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _existing_key(path)
    key = secrets.token_bytes(_KEY_BYTES)
    with os.fdopen(fd, "wb") as file:
        file.write(key)
    return key


class MaskingFilter(logging.Filter):
    """Masks every log record with the current masker.

    The message, its arguments, the formatted traceback, and the stack trace are all masked,
    so a secret never reaches a log stream.
    """

    def __init__(self, masker: Callable[[], Masker]) -> None:
        super().__init__()
        self._masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask the record in place, and keep it."""
        masker = self._masker()
        try:
            message = record.getMessage()
        except Exception:  # a malformed logging call: keep its text without the arguments
            message = str(record.msg)
        record.msg, record.args = masker.mask(message), ()
        if record.exc_info and not record.exc_text:
            record.exc_text = _FORMATTER.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = masker.mask(record.exc_text)
        if record.stack_info:
            record.stack_info = masker.mask(record.stack_info)
        return True


class _DaemonFormatter(logging.Formatter):
    """`2026-11-09T02:30:00.123Z INFO treadle.engine run=42 step=archive succeeded`."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Return the record's time in UTC with milliseconds."""
        return iso_utc(datetime.fromtimestamp(record.created, UTC))


_FORMATTER: Final = _DaemonFormatter()


def configure_logging(level: str, masker: Callable[[], Masker]) -> None:
    """Log to stderr in the daemon format, through a `MaskingFilter`.

    Calling it again replaces the handler that an earlier call installed.
    """
    numeric = _LEVELS.get(level.lower())
    if numeric is None:
        raise ValueError(f"unknown log level {level!r}; use debug, info, warning, or error")
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(_FORMATTER)
    handler.addFilter(MaskingFilter(masker))
    root = logging.getLogger()
    for old in [old for old in root.handlers if old.get_name() == _HANDLER_NAME]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(numeric)
    quiet = logging.NOTSET if numeric == logging.DEBUG else logging.WARNING
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(quiet)


class _ManagerLink:
    """The run manager, for the services that must exist before it.

    The triggers submit through it, and the registry reports removed workflows to it. The run
    manager needs the registry, which needs the triggers, so the link closes that cycle.
    """

    def __init__(self) -> None:
        self.manager: RunManager | None = None

    async def submit(self, request: RunRequest) -> Submission:
        """Submit a run to the run manager."""
        return await self._target().submit(request)

    async def workflow_removed(self, workflow_id: str) -> None:
        """Cancel a removed workflow's queued runs."""
        await self._target().workflow_removed(workflow_id)

    def _target(self) -> RunManager:
        if self.manager is None:
            raise ShuttingDownError("Treadle is still starting up")
        return self.manager


@dataclass(frozen=True, slots=True)
class _Services:
    """The long-lived parts of a running daemon."""

    store: AsyncStore
    registry: WorkflowRegistry
    scheduler: Scheduler
    watches: WatchService
    webhooks: WebhookRouter
    triggers: TriggerManager
    manager: RunManager
    poller: ControlPoller


class Daemon:
    """The long-running process: one event loop, one task group, one lock."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Clock | None = None,
        rng: RandomSource | None = None,
        steps: StepRegistry | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or SystemClock()
        self._rng = rng or default_random()
        self._steps = steps or builtin_registry()
        self._environ = dict(os.environ if environ is None else environ)
        self._secrets = SecretStore(settings.env_file, self._environ)
        self._started_at = iso_utc(self._clock.now())
        self._stop = asyncio.Event()
        self._signals = 0
        self._failed = False
        self._port: int | None = None
        self._services: _Services | None = None
        self._forcing: set[asyncio.Task[None]] = set()
        # The masker for daemon log records; it follows the secrets and the workflows.
        self._log_masker = _MaskerCache(self._secrets, self._hidden_env)

    async def run(self) -> ExitCode:
        """Start up, serve until shutdown, and return the exit code.

        A startup problem raises ConfigError (exit 3) or BusyError (exit 8). After a signal
        the daemon drains and returns ExitCode.OK. It returns ExitCode.FAILURE when the HTTP
        server or a background service stopped unexpectedly, after the same graceful drain.
        """
        async with contextlib.AsyncExitStack() as stack:
            services, server, sock, lock = await self._start_up(stack)
            return await self._serve(services, server, sock, lock)

    def request_shutdown(self) -> None:
        """Shut down: gracefully the first time, forced the second, `os._exit(130)` the third."""
        self._signals += 1
        if self._signals == 1:
            _log.info("shutting down; signal again to stop the runs at once")
            self._stop.set()
        elif self._signals == 2:
            _log.warning("stopping the runs at once")
            self._force()
        else:
            os._exit(ExitCode.INTERRUPTED)

    def request_reload(self) -> None:
        """Reload workflows now (SIGHUP)."""
        if self._services is not None:
            _log.info("reloading the workflows")
            self._services.registry.request_reload()

    @property
    def bound_port(self) -> int | None:
        """The listening port once bound, else None."""
        return self._port

    async def _start_up(
        self, stack: contextlib.AsyncExitStack
    ) -> tuple[_Services, EmbeddedServer, socket.socket, InstanceLock]:
        """Run the startup sequence up to the task group, registering each cleanup."""
        settings = self._settings
        # The token may live in `.env`, so the secret store loads before the token check,
        # the first startup step; only the permission warning waits for its turn.
        token = self._load_token()
        _prepare_state_dir(settings.state_dir)
        lock = InstanceLock(settings.state_dir, self._holder(port=None))
        lock.acquire()
        stack.callback(lock.release)
        store = await AsyncStore.open(settings)
        stack.push_async_callback(store.close)
        key = load_secret_key(settings.secret_key_path)
        await _recover(store, settings, self._clock)
        warning = self._secrets.permission_warning()
        if warning is not None:
            _log.warning("%s", warning)
        http = _http_client()
        stack.push_async_callback(http.aclose)
        services = self._assemble(store, http)
        self._services = services
        await services.registry.load_all()
        entries = services.registry.entries()
        await services.scheduler.prune_state(
            valid_workflows={entry.workflow_id for entry in entries if entry.valid},
            known_workflows={entry.workflow_id for entry in entries},
        )
        sock = self._bind()
        stack.callback(sock.close)
        app = create_app(self._web_context(services, token, key))
        server = EmbeddedServer(uvicorn_config(app, settings))
        self._install_signal_handlers(stack)
        return services, server, sock, lock

    def _load_token(self) -> str | None:
        """Return the API token, enforcing its length and the rule for non-loopback binds."""
        server = self._settings.server
        token = self._secrets.load().lookup_env(server.token_env) or None
        if token is not None and len(token) < _MIN_TOKEN:
            raise ConfigError(f"{server.token_env} must be at least {_MIN_TOKEN} characters long")
        if token is None and not server.is_loopback:
            raise ConfigError(
                f"set {server.token_env} (at least {_MIN_TOKEN} characters) or bind 127.0.0.1"
            )
        return token

    def _assemble(self, store: AsyncStore, http: httpx.AsyncClient) -> _Services:
        """Build the services; triggers submit through a link to the run manager."""
        settings, clock = self._settings, self._clock
        link = _ManagerLink()
        scheduler = Scheduler(store=store, submitter=link, clock=clock)
        watches = WatchService(submitter=link, limits=settings.limits)
        webhooks = WebhookRouter(submitter=link, secrets=self._snapshot, clock=clock)
        triggers = TriggerManager(scheduler, watches, webhooks)
        registry = WorkflowRegistry(
            settings=settings,
            steps=self._steps,
            secrets=self._secrets,
            store=store,
            sinks=[triggers],
            on_removed=link.workflow_removed,
            clock=clock,
        )
        manager = _run_manager(
            settings,
            store,
            registry,
            self._secrets,
            steps=self._steps,
            environ=self._environ,
            clock=clock,
            rng=self._rng,
            http=http,
        )
        link.manager = manager
        poller = ControlPoller(store=store, control=manager, clock=clock, accept_runs=True)
        return _Services(store, registry, scheduler, watches, webhooks, triggers, manager, poller)

    def _bind(self) -> socket.socket:
        """Bind the listening socket and remember its port. A failure raises BusyError."""
        server = self._settings.server
        try:
            sock = bind_socket(server.host, server.port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise BusyError(f"port {server.port} on {server.host} is in use") from None
            # Every bind failure exits 8, as the startup sequence says; only an address in use
            # gets the dedicated message.
            reason = exc.strerror or str(exc)
            raise BusyError(f"can't listen on {server.host} port {server.port}: {reason}") from None
        self._port = int(sock.getsockname()[1])
        return sock

    def _web_context(self, services: _Services, token: str | None, key: bytes) -> WebContext:
        """Give the web application the daemon's resources."""
        manager = services.manager
        return WebContext(
            settings=_web_settings(self._settings),
            workflows=services.registry,
            runs=manager,
            reader=services.store,
            hooks=services.webhooks,
            triggers=services.triggers,
            token=token,
            secret_key=key,
            clock=self._clock,
            version=__version__,
            schema_version=services.store.schema_version,
            started_at=self._clock.now(),
            draining=lambda: manager.draining,
        )

    def _install_signal_handlers(self, stack: contextlib.AsyncExitStack) -> None:
        """SIGINT and SIGTERM shut down, and SIGHUP reloads, on the event loop."""
        loop = asyncio.get_running_loop()
        handlers = {
            signal.SIGINT: self.request_shutdown,
            signal.SIGTERM: self.request_shutdown,
            signal.SIGHUP: self.request_reload,
        }
        for number, handler in handlers.items():
            try:
                loop.add_signal_handler(number, handler)
            except (ValueError, RuntimeError):  # not the main thread: the embedder handles it
                _log.debug("can't handle %s outside the main thread", number.name)
                continue
            stack.callback(loop.remove_signal_handler, number)

    async def _serve(
        self, services: _Services, server: EmbeddedServer, sock: socket.socket, lock: InstanceLock
    ) -> ExitCode:
        """Run the task group until a shutdown request, then drain and stop it.

        Every long-lived task runs supervised: when one fails or stops on its own, the daemon
        logs it and drains as it would after a signal, instead of letting the task group
        cancel the runs mid-step.
        """
        manager = services.manager
        async with asyncio.TaskGroup() as group:
            actor = group.create_task(
                self._supervised("the run manager", manager.run()), name="treadle-runs"
            )
            readmitted = await manager.readmit()
            if readmitted:
                _log.info("re-admitted %d queued runs", readmitted)
            background = [
                group.create_task(self._supervised(name, job), name=name)
                for name, job in self._background_jobs(services)
            ]
            http = group.create_task(
                self._supervised("the HTTP server", server.serve(sockets=[sock])),
                name="treadle-http",
            )
            lock.update(self._holder(port=self._port))
            _log.info("listening on %s", _url(self._settings.server.host, self._port))
            await self._stop.wait()
            await self._shut_down(services, server, actor, background, http)
        return ExitCode.FAILURE if self._failed else ExitCode.OK

    async def _supervised(self, name: str, job: Coroutine[Any, Any, None]) -> None:
        """Run a long-lived task; if it stops before shutdown, log it and start draining."""
        try:
            await job
        except Exception:
            _log.exception("%s failed", name)
        else:
            if self._stop.is_set():
                return
            _log.error("%s stopped unexpectedly", name)
        if not self._stop.is_set():
            _log.error("shutting down, because %s stopped", name)
            self._failed = True
            self._stop.set()

    def _background_jobs(self, services: _Services) -> list[tuple[str, Coroutine[Any, Any, None]]]:
        settings = self._settings
        return [
            ("treadle-scheduler", services.scheduler.run()),
            ("treadle-watch", services.watches.run()),
            ("treadle-registry", services.registry.run()),
            ("treadle-control", services.poller.run()),
            (
                "treadle-retention",
                retention_loop(
                    services.store,
                    settings.retention,
                    self._clock,
                    services.registry.current_digests,
                ),
            ),
        ]

    async def _shut_down(
        self,
        services: _Services,
        server: EmbeddedServer,
        actor: asyncio.Task[None],
        background: list[asyncio.Task[None]],
        http: asyncio.Task[None],
    ) -> None:
        """Drain: refuse new runs, stop the triggers, stop the runs, then stop serving."""
        services.webhooks.set_draining()
        # The first step of `shutdown` sets the manager draining, before the cancelled
        # background tasks resume, so no new run is admitted from here on.
        draining = asyncio.create_task(
            services.manager.shutdown(grace=self._settings.runs.shutdown_grace)
        )
        for task in background:
            task.cancel()
        await asyncio.wait(background)
        await draining
        await asyncio.wait([actor])
        server.should_exit = True
        await asyncio.wait([http])
        _log.info("stopped")

    def _force(self) -> None:
        """Skip the remaining grace: force the runs and kill their process groups now."""
        services = self._services
        if services is None:
            return
        services.webhooks.set_draining()
        task = asyncio.get_running_loop().create_task(services.manager.shutdown(grace=0))
        self._forcing.add(task)
        task.add_done_callback(self._forced)

    def _forced(self, task: asyncio.Task[None]) -> None:
        self._forcing.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error("forcing the shutdown failed", exc_info=task.exception())

    def _holder(self, *, port: int | None) -> LockHolder:
        host = self._settings.server.host
        return LockHolder(os.getpid(), "serve", self._started_at, __version__, host, port)

    def _snapshot(self) -> SecretSnapshot:
        return self._secrets.snapshot

    def _hidden_env(self) -> frozenset[str]:
        if self._services is None:
            return frozenset({self._settings.server.token_env})
        return self._services.registry.hidden_env()


def serve(settings: Settings, *, environ: Mapping[str, str] | None = None) -> ExitCode:
    """Run the daemon with `asyncio.run` and return its exit code.

    Logging goes to stderr in the daemon format, masked with the daemon's secrets.
    """
    daemon = Daemon(settings, environ=environ)
    configure_logging(settings.logging.level, daemon._log_masker)
    return asyncio.run(daemon.run())


def _web_settings(settings: Settings) -> Settings:
    """Return the settings for the web application: a loopback bind also allows its own host.

    The Host allowlist of a loopback bind names 127.0.0.1, localhost, and [::1]. A bind on
    another loopback address, such as 127.0.0.2 or a long form of ::1, must accept the Host
    header that its own URL produces, so that address joins `[server] allowed_hosts`.
    """
    server = settings.server
    if not server.is_loopback:
        return settings
    address = server.host.strip("[]")
    own = f"[{address}]" if ":" in address else address
    if own.lower() in (host.lower() for host in allowed_hosts_for(server)):
        return settings
    allowed = (*server.allowed_hosts, own)
    return dataclasses.replace(settings, server=dataclasses.replace(server, allowed_hosts=allowed))


def _existing_key(path: Path) -> bytes:
    """Read an existing key, tightening its permissions and replacing it when it's too short."""
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        path.chmod(0o600)
        _log.warning("%s was readable by other users; its mode is now 0600", path)
    key = path.read_bytes()
    if len(key) >= _KEY_BYTES:
        return key
    _log.warning("%s holds fewer than %d bytes; creating a new key", path, _KEY_BYTES)
    key = secrets.token_bytes(_KEY_BYTES)
    fd, temporary = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(key)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return key


def _prepare_state_dir(path: Path) -> None:
    """Create the state directory with mode 0700, or warn when others can read it."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        return
    if mode & 0o077:
        _log.warning("%s is accessible by other users; run `chmod 700 %s`", path, path)


def _url(host: str, port: int | None) -> str:
    address = host.strip("[]")
    return f"http://[{address}]:{port}" if ":" in address else f"http://{address}:{port}"
