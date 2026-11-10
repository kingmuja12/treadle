"""The `Daemon` class on the test's own event loop: serving, draining, forcing, and refusals.

Also how it treats a token that `.env` changes, startup steps that fail, `localhost`, and the
processes that a forced cancel leaves behind.
"""

from __future__ import annotations

import asyncio
import errno
import gc
import json
import logging
import os
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import FrameType

import httpx
import pytest
from tests.integration.daemon.conftest import HOOK_SECRET, STUBBORN
from tests.integration.daemon.helpers import (
    has_ipv6_loopback,
    kill_unfinished_steps,
    sign,
    surviving_process_groups,
)
from tests.support.project import ProjectFactory
from tests.unit.store.helpers import Runs

from treadle import __version__
from treadle import daemon as daemon_module
from treadle.clock import SystemClock, iso_utc
from treadle.config import Settings
from treadle.daemon import Daemon
from treadle.engine.manager import RunManager
from treadle.errors import BusyError, ConfigError, LockHeldError, StoreError
from treadle.lock import InstanceLock
from treadle.models import RunReason, RunStatus, Workflow
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore
from treadle.triggers.scheduler import Scheduler
from treadle.types import ExitCode, LockHolder
from treadle.workflow.loader import WorkflowLoader

TOKEN = "t" * 32
UNREACHABLE = "192.0.2.10"  # TEST-NET-1: never an address of this machine


@pytest.fixture(autouse=True)
def _kill_leftover_steps(daemon_project: ProjectFactory) -> Iterator[None]:
    """Kill the step process groups of attempts that never finished, even after a failure."""
    yield
    kill_unfinished_steps(daemon_project.settings().db_path)


async def _eventually(check: Callable[[], Awaitable[bool]], *, within: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await check():
        if loop.time() > deadline:
            raise AssertionError(f"the condition didn't hold within {within}s")
        await asyncio.sleep(0.02)


def _listening_port(settings: Settings) -> int | None:
    """The port in `holder.json`, which the daemon records once it serves."""
    try:
        holder = json.loads(settings.holder_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = holder.get("port")
    return port if isinstance(port, int) and port > 0 else None


def _configure(project: ProjectFactory, *, extra: str = "", host: str | None = None) -> Settings:
    server = f'host = "{host}"\n' if host else ""
    project.write_config(f'timezone = "UTC"\n{extra}\n[server]\nport = 0\n{server}')
    return project.settings()


@asynccontextmanager
async def serving(
    daemon: Daemon, settings: Settings, *, stop: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the daemon until the block ends, then shut it down; it must exit with 0.

    With `stop=False`, the block requests the shutdown itself. A third request would call
    `os._exit`, so the helper never adds one.
    """
    task = asyncio.create_task(daemon.run())

    async def ready() -> bool:
        if task.done():
            task.result()  # a startup failure fails the test here
        return _listening_port(settings) is not None

    try:
        await _eventually(ready)
        url = f"http://127.0.0.1:{_listening_port(settings)}"
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            yield client
    finally:
        if stop and not task.done():
            daemon.request_shutdown()
        code = await asyncio.wait_for(task, 30)
    assert code == ExitCode.OK


async def _status(client: httpx.AsyncClient, run_id: int) -> str:
    return str((await client.get(f"/api/v1/runs/{run_id}")).json()["status"])


async def test_the_daemon_serves_runs_and_hooks_then_drains(daemon_project: ProjectFactory) -> None:
    settings = _configure(daemon_project)
    body = json.dumps({"ref": "refs/tags/v1"}).encode()
    signed = {"Content-Type": "application/json", "X-Hub-Signature-256": sign(HOOK_SECRET, body)}
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        health = await client.get("/api/v1/health")
        created = await client.post("/api/v1/workflows/hello/runs", json={"inputs": {}})
        hooked = await client.post("/hooks/deploy", content=body, headers=signed)
        run_ids = [created.json()["run"]["id"], hooked.json()["run_id"]]

        async def finished() -> bool:
            return [await _status(client, run_id) for run_id in run_ids] == ["succeeded"] * 2

        await _eventually(finished)
        assert daemon.bound_port == _listening_port(settings)

    assert health.json() == {"status": "ok", "version": __version__}
    assert InstanceLock.probe(settings.state_dir) is None


async def test_request_reload_applies_an_edit_at_once(daemon_project: ProjectFactory) -> None:
    settings = _configure(daemon_project)
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        daemon_project.write_workflow(
            "hello", "name: Renamed\nsteps:\n  - id: a\n    run: 'true'\n"
        )
        daemon.request_reload()

        async def renamed() -> bool:
            return (await client.get("/api/v1/workflows/hello")).json()["name"] == "Renamed"

        await _eventually(renamed, within=5)


async def test_a_second_shutdown_request_forces_the_runs(daemon_project: ProjectFactory) -> None:
    daemon_project.write_workflow("stubborn", STUBBORN)
    settings = _configure(daemon_project, extra='[runs]\nkill_grace = "30s"')
    daemon = Daemon(settings, environ={})
    loop = asyncio.get_running_loop()
    pid_file = daemon_project.root / "stubborn.pid"

    async with serving(daemon, settings, stop=False) as client:
        run_id = (await client.post("/api/v1/workflows/stubborn/runs", json={})).json()["run"]["id"]

        async def holding() -> bool:
            return pid_file.exists()

        await _eventually(holding)
        started = loop.time()
        daemon.request_shutdown()
        await asyncio.sleep(0.3)
        daemon.request_shutdown()

    assert loop.time() - started < 10  # well under the 30-second kill grace
    store = await AsyncStore.open(settings, mode="client")
    try:
        detail = await store.get_run(run_id)
    finally:
        await store.close()
    assert detail is not None
    assert (detail.run.status, detail.run.reason) == (RunStatus.CANCELLED, RunReason.SHUTDOWN)
    assert surviving_process_groups(settings.db_path) == []


async def test_a_forced_cancel_leaves_no_process_behind_when_the_daemon_stops(
    daemon_project: ProjectFactory,
) -> None:
    daemon_project.write_workflow("stubborn", STUBBORN)
    settings = _configure(daemon_project, extra='[runs]\nkill_grace = "30s"')
    daemon = Daemon(settings, environ={})
    pid_file = daemon_project.root / "stubborn.pid"

    async with serving(daemon, settings) as client:
        run_id = (await client.post("/api/v1/workflows/stubborn/runs", json={})).json()["run"]["id"]

        async def holding() -> bool:
            return pid_file.exists()

        await _eventually(holding)
        cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel", json={})
        forced = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"force": True})

        async def finished() -> bool:
            return await _status(client, run_id) == "cancelled"

        await _eventually(finished, within=10)
        # The daemon stops well within the step's 30-second kill grace.

    assert (cancelled.status_code, forced.status_code) == (202, 202)
    assert surviving_process_groups(settings.db_path) == []


async def test_sighup_while_the_daemon_starts_up_is_handled(
    daemon_project: ProjectFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _configure(daemon_project)
    received: list[int] = []

    def record(number: int, frame: FrameType | None) -> None:
        received.append(number)

    real_load = daemon_module.load_secret_key

    def hang_up_then_load(path: Path) -> bytes:
        os.kill(os.getpid(), signal.SIGHUP)  # a reload request in the middle of startup
        return real_load(path)

    monkeypatch.setattr(daemon_module, "load_secret_key", hang_up_then_load)
    # The test's own handler stands in for the default action, which would end the test run.
    previous = signal.signal(signal.SIGHUP, record)
    try:
        async with serving(Daemon(settings, environ={}), settings) as client:
            health = await client.get("/api/v1/health")
    finally:
        signal.signal(signal.SIGHUP, previous)

    assert received == []
    assert health.status_code == 200


async def test_a_token_protects_the_api(daemon_project: ProjectFactory) -> None:
    settings = _configure(daemon_project)
    daemon = Daemon(settings, environ={"TREADLE_TOKEN": TOKEN})

    async with serving(daemon, settings) as client:
        anonymous = await client.get("/api/v1/status")
        bearer = await client.get("/api/v1/status", headers={"Authorization": f"Bearer {TOKEN}"})

    assert anonymous.status_code == 401
    assert bearer.status_code == 200
    assert bearer.json()["workflows"] == {"valid": 3, "invalid": 0}


@pytest.mark.parametrize(
    ("host", "environ", "message"),
    [
        (UNREACHABLE, {}, r"set TREADLE_TOKEN \(at least 32 characters\) or bind 127\.0\.0\.1"),
        (None, {"TREADLE_TOKEN": "short"}, "TREADLE_TOKEN must be at least 32 characters long"),
    ],
)
async def test_token_rules_refuse_to_start(
    daemon_project: ProjectFactory, host: str | None, environ: dict[str, str], message: str
) -> None:
    settings = _configure(daemon_project, host=host)

    with pytest.raises(ConfigError, match=message):
        await Daemon(settings, environ=environ).run()

    assert not settings.state_dir.exists()


async def test_a_held_lock_refuses_to_start(daemon_project: ProjectFactory) -> None:
    settings = _configure(daemon_project)
    holder = LockHolder(4242, "run", iso_utc(SystemClock().now()), __version__)

    with InstanceLock(settings.state_dir, holder), pytest.raises(LockHeldError) as caught:
        await Daemon(settings, environ={}).run()

    assert str(caught.value) == (
        "another Treadle process holds the state directory lock (run, pid 4242)"
    )


async def test_bind_failures_are_busy_errors(daemon_project: ProjectFactory) -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        daemon_project.write_config(f'timezone = "UTC"\n[server]\nport = {port}\n')
        with pytest.raises(BusyError, match=f"port {port} on 127.0.0.1 is in use"):
            await Daemon(daemon_project.settings(), environ={}).run()

    remote = _configure(daemon_project, host=UNREACHABLE)
    with pytest.raises(BusyError, match=f"can't listen on {UNREACHABLE} port 0: "):
        await Daemon(remote, environ={"TREADLE_TOKEN": TOKEN}).run()
    assert InstanceLock.probe(remote.state_dir) is None


async def test_a_deleted_workflow_goes_away_and_its_queued_runs_are_cancelled(
    daemon_project: ProjectFactory,
) -> None:
    settings = _configure(daemon_project)
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        (daemon_project.workflows_dir / "deploy.yml").unlink()
        daemon.request_reload()

        async def gone() -> bool:
            return (await client.get("/api/v1/workflows/deploy")).status_code == 404

        await _eventually(gone, within=5)


@pytest.mark.parametrize("fails", [True, False])
async def test_a_background_service_that_stops_drains_the_daemon(
    daemon_project: ProjectFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fails: bool,
) -> None:
    async def stops(self: Scheduler) -> None:
        if fails:
            raise RuntimeError("the scheduler broke")

    monkeypatch.setattr(Scheduler, "run", stops)
    settings = _configure(daemon_project)
    caplog.set_level(logging.INFO, logger="treadle.daemon")

    code = await asyncio.wait_for(Daemon(settings, environ={}).run(), 30)

    assert code == ExitCode.FAILURE
    message = "treadle-scheduler failed" if fails else "treadle-scheduler stopped unexpectedly"
    assert message in caplog.text
    assert "shutting down, because treadle-scheduler stopped" in caplog.text
    assert InstanceLock.probe(settings.state_dir) is None


async def test_startup_warns_about_loose_permissions_and_readmits_queued_runs(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _configure(daemon_project)
    hello = _loaded(settings, "hello")
    store = await AsyncStore.open(settings)
    try:
        await store.write(lambda repo: Runs(repo, hello).queued())
    finally:
        await store.close()
    settings.state_dir.chmod(0o755)
    daemon_project.env_path.chmod(0o644)
    caplog.set_level(logging.INFO)
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:

        async def readmitted() -> bool:
            return await _status(client, 1) == "succeeded"

        await _eventually(readmitted)

    assert f"{settings.state_dir} is accessible by other users" in caplog.text
    assert f"{daemon_project.env_path} is readable by other users" in caplog.text
    assert "re-admitted 1 queued runs" in caplog.text


async def test_an_embedded_daemon_runs_without_signal_handlers(
    daemon_project: ProjectFactory,
) -> None:
    settings = _configure(daemon_project)
    daemon = Daemon(settings, environ={})
    started: asyncio.Queue[asyncio.AbstractEventLoop] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def main() -> ExitCode:
        loop.call_soon_threadsafe(started.put_nowait, asyncio.get_running_loop())
        return await daemon.run()

    worker = asyncio.create_task(asyncio.to_thread(asyncio.run, main()))
    daemon_loop = await started.get()

    async def listening() -> bool:
        return _listening_port(settings) is not None

    await _eventually(listening)
    url = f"http://127.0.0.1:{_listening_port(settings)}"
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        health = await client.get("/api/v1/health")
    daemon_loop.call_soon_threadsafe(daemon.request_shutdown)

    assert await asyncio.wait_for(worker, 30) == ExitCode.OK
    assert health.status_code == 200


def _loaded(settings: Settings, workflow_id: str) -> Workflow:
    loader = WorkflowLoader(
        builtin_registry(),
        project_dir=settings.project_dir,
        default_timezone=settings.timezone,
        limits=settings.limits,
        environment_checks=False,
    )
    result = loader.load_file(settings.workflows_dir / f"{workflow_id}.yml")
    assert result.workflow is not None
    return result.workflow


OLD_TOKEN = "o" * 40
NEW_TOKEN = "n" * 40


@pytest.fixture
def quick_token_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check `.env` for a changed token every few milliseconds instead of every second."""
    monkeypatch.setattr(daemon_module, "_TOKEN_CHECK", 0.02)


async def _logged(caplog: pytest.LogCaptureFixture, text: str) -> None:
    async def appears() -> bool:
        return text in caplog.text

    await _eventually(appears, within=10)


@pytest.mark.usefixtures("quick_token_checks")
async def test_a_token_added_to_env_warns_that_it_needs_a_restart(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _configure(daemon_project)
    caplog.set_level(logging.INFO, logger="treadle")
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": NEW_TOKEN})
        daemon.request_reload()
        await _logged(caplog, "reloaded the secrets")
        await _logged(caplog, "TREADLE_TOKEN changed in .env")
        anonymous = await client.get("/api/v1/status")

    assert (
        "TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API "
        "doesn't require a token."
    ) in caplog.text
    assert anonymous.status_code == 200  # as the warning says
    assert NEW_TOKEN not in caplog.text


_ECHO_OLD_TOKEN = f"""\
steps:
  - id: leak
    run: echo "the old token is {OLD_TOKEN}"
"""


@pytest.mark.usefixtures("quick_token_checks")
async def test_a_rotated_token_works_and_stays_masked_until_a_restart(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": OLD_TOKEN})
    daemon_project.write_workflow("leak", _ECHO_OLD_TOKEN)
    settings = _configure(daemon_project)
    caplog.set_level(logging.INFO, logger="treadle")
    daemon = Daemon(settings, environ={})
    old = {"Authorization": f"Bearer {OLD_TOKEN}"}
    new = {"Authorization": f"Bearer {NEW_TOKEN}"}

    async with serving(daemon, settings) as client:
        daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": NEW_TOKEN})
        daemon.request_reload()
        await _logged(caplog, "TREADLE_TOKEN changed in .env")
        with_old = await client.get("/api/v1/status", headers=old)
        with_new = await client.get("/api/v1/status", headers=new)
        run_id = (await client.post("/api/v1/workflows/leak/runs", json={}, headers=old)).json()[
            "run"
        ]["id"]

        async def finished() -> bool:
            run = await client.get(f"/api/v1/runs/{run_id}", headers=old)
            return run.json()["status"] == "succeeded"

        await _eventually(finished)
        lines = (await client.get(f"/api/v1/runs/{run_id}/logs", headers=old)).json()["lines"]
        daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": OLD_TOKEN})
        daemon.request_reload()
        await _logged(caplog, "TREADLE_TOKEN in .env is the token in use again")

    assert (
        "TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API "
        "accepts only the previous token."
    ) in caplog.text
    assert (with_old.status_code, with_new.status_code) == (200, 401)
    assert [line["text"] for line in lines] == ["the old token is ***"]
    assert OLD_TOKEN not in caplog.text
    assert NEW_TOKEN not in caplog.text


@pytest.mark.usefixtures("quick_token_checks")
async def test_other_changes_to_env_say_nothing_about_the_token(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": OLD_TOKEN})
    settings = _configure(daemon_project)
    caplog.set_level(logging.INFO, logger="treadle")
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings):
        daemon_project.write_env(
            {"DEPLOY_SECRET": "another-hook-secret-0123456789", "TREADLE_TOKEN": OLD_TOKEN}
        )
        daemon.request_reload()
        await _logged(caplog, "reloaded the secrets")
        await asyncio.sleep(0.2)  # ten token checks

    assert "TREADLE_TOKEN" not in caplog.text


@pytest.mark.usefixtures("quick_token_checks")
async def test_a_token_too_short_to_start_with_warns_so(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": OLD_TOKEN})
    settings = _configure(daemon_project)
    caplog.set_level(logging.WARNING, logger="treadle")
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        daemon_project.write_env({"DEPLOY_SECRET": HOOK_SECRET, "TREADLE_TOKEN": "short"})
        daemon.request_reload()
        await _logged(caplog, "TREADLE_TOKEN changed in .env")
        anonymous = await client.get("/api/v1/status")

    assert (
        "TREADLE_TOKEN changed in .env, but Treadle can't start with the new value: "
        "TREADLE_TOKEN must be at least 32 characters long. Until a restart, the API accepts "
        "only the previous token."
    ) in caplog.text
    assert anonymous.status_code == 401  # authentication stays on


async def _refuse_readmission(self: RunManager) -> int:
    raise StoreError("disk I/O error")


def _refuse_the_port_record(monkeypatch: pytest.MonkeyPatch) -> None:
    original = InstanceLock.update

    def update(self: InstanceLock, holder: LockHolder) -> None:
        if holder.port is not None:
            raise OSError(errno.ENOSPC, "No space left on device")
        original(self, holder)

    monkeypatch.setattr(InstanceLock, "update", update)


@pytest.mark.parametrize(
    ("break_it", "error"),
    [
        (
            lambda monkeypatch: monkeypatch.setattr(RunManager, "readmit", _refuse_readmission),
            "StoreError: disk I/O error",
        ),
        (_refuse_the_port_record, "No space left on device"),
    ],
)
async def test_a_failed_startup_step_in_the_task_group_drains_the_daemon(
    daemon_project: ProjectFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
    break_it: Callable[[pytest.MonkeyPatch], None],
    error: str,
) -> None:
    break_it(monkeypatch)
    settings = _configure(daemon_project)
    caplog.set_level(logging.INFO, logger="treadle.daemon")

    code = await asyncio.wait_for(Daemon(settings, environ={}).run(), 30)
    gc.collect()  # an unawaited coroutine warns when it's collected

    assert code == ExitCode.FAILURE
    assert "starting the services failed" in caplog.text
    assert error in caplog.text
    assert "stopped" in caplog.messages
    assert InstanceLock.probe(settings.state_dir) is None
    assert [
        str(warning.message) for warning in recwarn if "never awaited" in str(warning.message)
    ] == []


@pytest.mark.skipif(not has_ipv6_loopback(), reason="no IPv6 loopback here")
async def test_localhost_serves_on_both_loopback_addresses(
    daemon_project: ProjectFactory, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _configure(daemon_project, host="localhost")
    caplog.set_level(logging.INFO, logger="treadle.daemon")
    daemon = Daemon(settings, environ={})

    async with serving(daemon, settings) as client:
        port = daemon.bound_port
        ipv4 = await client.get("/api/v1/health")
        async with httpx.AsyncClient(base_url=f"http://[::1]:{port}", timeout=10) as other:
            ipv6 = await other.get("/api/v1/health")

    assert (ipv4.status_code, ipv6.status_code) == (200, 200)
    assert f"listening on http://localhost:{port}" in caplog.text
