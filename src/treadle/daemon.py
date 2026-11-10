"""The `treadle serve` daemon: startup, the task group, signals, and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import logging
import os
import secrets
import signal
import socket
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TypeAlias

import httpx
import uvicorn

from treadle import __version__
from treadle.clock import UTC, Clock, RandomSource, SystemClock, default_random, iso_utc
from treadle.config import ServerSettings, Settings
from treadle.control import ControlPoller
from treadle.engine.manager import RunManager
from treadle.engine.masking import Masker
from treadle.errors import BusyError, ConfigError, ShuttingDownError
from treadle.lock import InstanceLock
from treadle.models import RunRequest, Submission
from treadle.registry import WorkflowRegistry
from treadle.retention import retention_loop
from treadle.runner import (
    _kill_leftovers,
    _MaskerCache,
    _MaskingFilter,
    _prepare_state_dir,
    _recover,
    _run_manager,
)
from treadle.secrets import SecretSnapshot, SecretStore
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.steps.http import shared_client
from treadle.steps.procs import ProcessRegistry
from treadle.store.async_store import AsyncStore
from treadle.triggers.manager import TriggerManager
from treadle.triggers.scheduler import Scheduler
from treadle.triggers.watch import WatchService
from treadle.triggers.webhook import WebhookRouter
from treadle.types import ExitCode, LockHolder
from treadle.web.app import WebContext, create_app

# This module may import only uvicorn among third-party packages, so it can't use Starlette's
# ASGIApp. This alias names the same thing: an ASGI 3 application.
_ASGIApp: TypeAlias = Callable[[Any, Any, Any], Awaitable[None]]
# A long-lived job of the daemon's task group. It makes its coroutine only when its task
# starts, so a task that is cancelled before it starts leaves no coroutine unawaited.
_Job: TypeAlias = Callable[[], Awaitable[None]]

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
# How often the daemon checks whether `.env` changed the API token, in seconds.
_TOKEN_CHECK: Final = 1.0
# The bind errors of a system without IPv6, where `localhost` gets an IPv4 socket only.
_NO_IPV6: Final = frozenset({errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT})
# The variable through which systemd's `Type=notify` takes readiness notifications.
_NOTIFY_SOCKET: Final = "NOTIFY_SOCKET"

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


class MaskingFilter(_MaskingFilter):
    """Masks every log record with the current masker.

    The message, its arguments, the formatted traceback, and the stack trace are all masked,
    so a secret never reaches a log stream.
    """


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


@dataclass(frozen=True, slots=True)
class _PinnedSnapshot(SecretSnapshot):
    """Secrets that also mask `pinned`: values that still work but may have left the secrets.

    The API checks the token that the daemon started with until it restarts, even after
    `.env` changes or removes it, so that token stays masked meanwhile.
    """

    pinned: frozenset[str] = frozenset()

    def mask_values(self, hidden_env: Iterable[str] = ()) -> frozenset[str]:
        """Return what `SecretSnapshot.mask_values` returns, plus the pinned values."""
        return SecretSnapshot.mask_values(self, hidden_env) | self.pinned


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
        self._processes = ProcessRegistry()
        # The API token that the daemon started with; the API checks it until a restart.
        self._token: str | None = None
        # The last secret snapshot and token that `_snapshot` saw, and its answer for them.
        self._pinned: tuple[SecretSnapshot, str | None, SecretSnapshot] | None = None
        # The masker for daemon log records; it follows the secrets and the workflows.
        self._log_masker = _MaskerCache(self._snapshot, self._hidden_env)

    async def run(self) -> ExitCode:
        """Start up, serve until shutdown, and return the exit code.

        A startup problem raises ConfigError (exit 3) or BusyError (exit 8). After a signal
        the daemon drains and returns ExitCode.OK. It returns ExitCode.FAILURE when the HTTP
        server or a background service stopped unexpectedly, or the last startup steps
        failed, after the same graceful drain.

        The signal handlers come first, so a SIGHUP during startup, such as a reload that a
        service manager sends right after a restart, doesn't end the process. Loading the
        workflows reads every file anyway, so such a reload has nothing to add, and a
        shutdown request during startup takes effect as soon as the daemon serves.
        """
        async with contextlib.AsyncExitStack() as stack:
            self._install_signal_handlers(stack)
            services, server, sockets, lock = await self._start_up(stack)
            return await self._serve(services, server, sockets, lock)

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
        """Reload workflows now (SIGHUP). Before startup loads them, there is nothing to do."""
        if self._services is not None:
            _log.info("reloading the workflows")
            self._services.registry.request_reload()

    @property
    def bound_port(self) -> int | None:
        """The listening port once bound, else None."""
        return self._port

    async def _start_up(
        self, stack: contextlib.AsyncExitStack
    ) -> tuple[_Services, EmbeddedServer, list[socket.socket], InstanceLock]:
        """Run the startup sequence up to the task group, registering each cleanup."""
        settings = self._settings
        # The token may live in `.env`, so the secret store loads before the token check,
        # the first startup step; only the permission warning waits for its turn.
        self._token = token = self._load_token()
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
        http = shared_client()
        stack.push_async_callback(http.aclose)
        services = self._assemble(store, http)
        self._services = services
        await services.registry.load_all()
        entries = services.registry.entries()
        await services.scheduler.prune_state(
            valid_workflows={entry.workflow_id for entry in entries if entry.valid},
            known_workflows={entry.workflow_id for entry in entries},
        )
        sockets = self._bind()
        for sock in sockets:
            stack.callback(sock.close)
        app = create_app(self._web_context(services, token, key))
        server = EmbeddedServer(uvicorn_config(app, settings))
        return services, server, sockets, lock

    def _load_token(self) -> str | None:
        """Return the API token, enforcing its length and the rule for non-loopback binds."""
        server = self._settings.server
        token = self._secrets.load().lookup_env(server.token_env) or None
        problem = _token_problem(token, server)
        if problem is not None:
            raise ConfigError(problem)
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
            self._snapshot,
            steps=self._steps,
            # The service manager's socket is for the daemon alone, not for its steps.
            environ={
                name: value for name, value in self._environ.items() if name != _NOTIFY_SOCKET
            },
            clock=clock,
            rng=self._rng,
            http=http,
            processes=self._processes,
        )
        link.manager = manager
        poller = ControlPoller(store=store, control=manager, clock=clock, accept_runs=True)
        return _Services(store, registry, scheduler, watches, webhooks, triggers, manager, poller)

    def _bind(self) -> list[socket.socket]:
        """Bind the listening sockets and remember their port. A failure raises BusyError."""
        server = self._settings.server
        try:
            sockets = _bind_sockets(server.host, server.port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise BusyError(f"port {server.port} on {server.host} is in use") from None
            # Every bind failure exits 8, as the startup sequence says; only an address in use
            # gets the dedicated message.
            reason = exc.strerror or str(exc)
            raise BusyError(f"can't listen on {server.host} port {server.port}: {reason}") from None
        self._port = int(sockets[0].getsockname()[1])
        return sockets

    def _web_context(self, services: _Services, token: str | None, key: bytes) -> WebContext:
        """Give the web application the daemon's resources."""
        manager = services.manager
        return WebContext(
            settings=self._settings,
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
        self,
        services: _Services,
        server: EmbeddedServer,
        sockets: list[socket.socket],
        lock: InstanceLock,
    ) -> ExitCode:
        """Run the task group until a shutdown request, then drain and stop it.

        Every long-lived task runs supervised: when one fails or stops on its own, the daemon
        logs it and drains as it would after a signal, instead of letting the task group
        cancel the runs mid-step. The startup steps in the group's own body, such as
        re-admitting the queued runs and recording the port, fail the same way.
        """
        manager = services.manager
        async with asyncio.TaskGroup() as group:
            actor = group.create_task(
                self._supervised("the run manager", manager.run), name="treadle-runs"
            )
            background: list[asyncio.Task[None]] = []
            http: asyncio.Task[None] | None = None
            try:
                readmitted = await manager.readmit()
                if readmitted:
                    _log.info("re-admitted %d queued runs", readmitted)
                background = [
                    group.create_task(self._supervised(name, job), name=name)
                    for name, job in self._background_jobs(services)
                ]
                http = group.create_task(
                    self._supervised(
                        "the HTTP server", functools.partial(server.serve, sockets=sockets)
                    ),
                    name="treadle-http",
                )
                lock.update(self._holder(port=self._port))
                _log.info("listening on %s", _url(self._settings.server.host, self._port))
                _notify_service_manager(self._environ, "READY=1")
            except Exception:
                _log.exception("starting the services failed")
                self._fail()
            await self._stop.wait()
            await self._shut_down(services, server, actor, background, http)
        return ExitCode.FAILURE if self._failed else ExitCode.OK

    async def _supervised(self, name: str, job: _Job) -> None:
        """Run a long-lived job; if it stops before shutdown, log it and start draining."""
        try:
            await job()
        except Exception:
            _log.exception("%s failed", name)
        else:
            if self._stop.is_set():
                return
            _log.error("%s stopped unexpectedly", name)
        if not self._stop.is_set():
            _log.error("shutting down, because %s stopped", name)
            self._fail()

    def _fail(self) -> None:
        """Drain as after a signal, and exit with ExitCode.FAILURE afterward."""
        self._failed = True
        self._stop.set()

    def _background_jobs(self, services: _Services) -> list[tuple[str, _Job]]:
        settings = self._settings
        retention = functools.partial(
            retention_loop,
            services.store,
            settings.retention,
            self._clock,
            services.registry.current_digests,
        )
        jobs: list[tuple[str, _Job]] = [
            ("treadle-scheduler", services.scheduler.run),
            ("treadle-watch", services.watches.run),
            ("treadle-registry", services.registry.run),
            ("treadle-control", services.poller.run),
            ("treadle-retention", retention),
        ]
        if settings.env_file is not None:
            jobs.append(("treadle-token", functools.partial(self._watch_token, settings.env_file)))
        return jobs

    async def _watch_token(self, env_file: Path) -> None:
        """Log each change that `env_file` makes to the API token, which applies only at startup.

        Until Treadle restarts, the API checks the token it started with, and that token stays
        masked. A change back to that token logs at INFO, and any other change at WARNING.
        """
        server, source = self._settings.server, env_file.name
        seen, announced = self._secrets.snapshot, self._token
        while True:
            await asyncio.sleep(_TOKEN_CHECK)
            snapshot = self._secrets.snapshot
            if snapshot is seen:
                continue
            seen = snapshot
            token = snapshot.lookup_env(server.token_env) or None
            if token == announced:
                continue
            announced = token
            level = logging.INFO if token == self._token else logging.WARNING
            _log.log(level, "%s", _token_change(server, source, self._token, token))

    async def _shut_down(
        self,
        services: _Services,
        server: EmbeddedServer,
        actor: asyncio.Task[None],
        background: list[asyncio.Task[None]],
        http: asyncio.Task[None] | None,
    ) -> None:
        """Drain: refuse new runs, stop the triggers, stop the runs, then stop serving.

        Process groups that a forced cancel left behind are killed once no run is active, so
        none outlives the daemon. `http` is None when startup failed before the server started.
        """
        services.webhooks.set_draining()
        # The first step of `shutdown` sets the manager draining, before the cancelled
        # background tasks resume, so no new run is admitted from here on.
        draining = asyncio.create_task(
            services.manager.shutdown(grace=self._settings.runs.shutdown_grace)
        )
        for task in background:
            task.cancel()
        if background:
            await asyncio.wait(background)
        await draining
        await asyncio.wait([actor])
        await _kill_leftovers(self._processes)
        if http is not None:
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
        """The current secrets; they also mask the API token in use, as `_PinnedSnapshot` does.

        The answer is the same object while the secrets and the token stay the same, so a
        masker built from it can be reused.
        """
        current, token, pinned = self._secrets.snapshot, self._token, self._pinned
        if pinned is not None and pinned[0] is current and pinned[1] == token:
            return pinned[2]
        masked = current
        if token is not None:
            masked = _PinnedSnapshot(
                current.secrets, current.dotenv, current.environ, frozenset({token})
            )
        self._pinned = (current, token, masked)
        return masked

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


def _token_problem(token: str | None, server: ServerSettings) -> str | None:
    """Return why the daemon can't start with `token` as its API token, or None if it can."""
    if token is not None and len(token) < _MIN_TOKEN:
        return f"{server.token_env} must be at least {_MIN_TOKEN} characters long"
    if token is None and not server.is_loopback:
        return f"set {server.token_env} (at least {_MIN_TOKEN} characters) or bind 127.0.0.1"
    return None


def _token_change(server: ServerSettings, source: str, in_use: str | None, new: str | None) -> str:
    """Describe a change that `source` made to the API token. The text never shows a token.

    `in_use` is the token that the API checks until a restart, and `new` the token that a
    restart would use.
    """
    name = server.token_env
    if new == in_use:
        return f"{name} in {source} is the token in use again"
    if in_use is None:
        meanwhile = "the API doesn't require a token"
    else:
        meanwhile = "the API accepts only the previous token"
    problem = _token_problem(new, server)
    if problem is not None:
        return (
            f"{name} changed in {source}, but Treadle can't start with the new value: "
            f"{problem}. Until a restart, {meanwhile}."
        )
    return f"{name} changed in {source}; restart Treadle to apply it. Until then, {meanwhile}."


def _bind_sockets(host: str, port: int) -> list[socket.socket]:
    """Bind the daemon's listening sockets with `bind_socket`. Raises OSError.

    `localhost` names both loopback addresses, and clients often try ::1 first, so it gets a
    socket on 127.0.0.1 and one on ::1 with the same port; otherwise another local user could
    listen on [::1] at that port and answer instead. A system without IPv6 gets the IPv4
    socket alone. Any other host gets one socket.
    """
    if host.lower() != "localhost":
        return [bind_socket(host, port)]
    first = bind_socket("127.0.0.1", port)
    try:
        second = bind_socket("::1", int(first.getsockname()[1]))
    except OSError as exc:
        if exc.errno in _NO_IPV6:
            return [first]
        first.close()
        raise
    return [first, second]


def _notify_service_manager(environ: Mapping[str, str], state: str) -> None:
    """Send `state`, such as READY=1, to systemd's notification socket, as `sd_notify` does.

    Under `Type=notify`, systemd counts the service as started only after READY=1, so a
    reload that it's asked for during startup waits until the daemon handles signals.
    Without `NOTIFY_SOCKET`, nothing happens, and a failure only logs a warning.
    """
    path = environ.get(_NOTIFY_SOCKET)
    if not path:
        return
    # A name that starts with "@" is in Linux's abstract namespace.
    address = "\0" + path[1:] if path.startswith("@") else path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("ascii"))
    except OSError as exc:
        _log.warning("can't notify the service manager at %s: %s", path, exc.strerror or exc)


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


def _url(host: str, port: int | None) -> str:
    address = host.strip("[]")
    return f"http://[{address}]:{port}" if ":" in address else f"http://{address}:{port}"
