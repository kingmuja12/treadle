"""`treadle serve` as a real process: HTTP, webhooks, signals, restarts, and hot reload."""

from __future__ import annotations

import json
import signal
import socket
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.integration.daemon.conftest import HOOK_SECRET, STUBBORN
from tests.integration.daemon.helpers import Daemon, eventually, kill_group, sign
from tests.support.project import ProjectFactory

from treadle import __version__
from treadle.lock import InstanceLock

StartDaemon = Callable[..., Daemon]


def _stored_run(db_path: Path, run_id: int) -> tuple[str, str | None]:
    conn = sqlite3.connect(db_path)
    try:
        status, reason = conn.execute(
            "SELECT status, reason FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    return status, reason


def test_health_and_a_run_through_the_api(start_daemon: StartDaemon) -> None:
    daemon = start_daemon().wait_ready()

    with daemon.client() as client:
        health = client.get("/api/v1/health")
        created = client.post("/api/v1/workflows/hello/runs", json={"inputs": {"name": "Ada"}})

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": __version__}
    assert created.status_code == 202
    assert created.json()["outcome"] == "queued"
    run_id = created.json()["run"]["id"]
    run = daemon.wait_for_run(run_id, "succeeded", "failed")
    assert run["status"] == "succeeded"
    with daemon.client() as client:
        lines = client.get(f"/api/v1/runs/{run_id}/logs").json()["lines"]
    assert [line["text"] for line in lines] == ["Hello, Ada!"]
    assert f"listening on {daemon.url}" in daemon.log()


def test_a_signed_webhook_runs_its_workflow(start_daemon: StartDaemon) -> None:
    daemon = start_daemon().wait_ready()
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sign(HOOK_SECRET, body)}

    with daemon.client() as client:
        unsigned = client.post("/hooks/deploy", content=body, headers={"X-Hub-Signature-256": "x"})
        accepted = client.post("/hooks/deploy", content=body, headers=headers)
        replayed = client.post("/hooks/deploy", content=body, headers=headers)

    assert unsigned.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    run_id = accepted.json()["run_id"]
    assert replayed.status_code == 200
    assert replayed.json() == {"status": "duplicate", "run_id": run_id}
    run = daemon.wait_for_run(run_id, "succeeded", "failed")
    assert run["status"] == "succeeded"
    assert run["trigger"]["body"] == {"ref": "refs/heads/main"}
    with daemon.client() as client:
        lines = client.get(f"/api/v1/runs/{run_id}/logs").json()["lines"]
    assert [line["text"] for line in lines] == ["deploying refs/heads/main"]


def test_sigterm_during_a_step_cancels_the_run_and_exits_0(start_daemon: StartDaemon) -> None:
    daemon = start_daemon().wait_ready()
    with daemon.client() as client:
        run_id = client.post("/api/v1/workflows/nap/runs", json={}).json()["run"]["id"]
    pid_file = daemon.project.root / f"nap-{run_id}.pid"
    eventually(pid_file.exists)

    daemon.signal(signal.SIGTERM)
    code = daemon.wait_exit()

    assert code == 0  # not -SIGTERM: uvicorn didn't re-raise the signal
    assert _stored_run(daemon.settings.db_path, run_id) == ("cancelled", "shutdown")
    assert InstanceLock.probe(daemon.settings.state_dir) is None
    assert not daemon.settings.holder_path.exists()
    assert "shutting down; signal again to stop the runs at once" in daemon.log()
    assert daemon.log().rstrip().endswith("INFO treadle.daemon stopped")


def test_a_third_signal_exits_130_at_once(
    start_daemon: StartDaemon, daemon_project: ProjectFactory
) -> None:
    daemon_project.write_workflow("stubborn", STUBBORN)
    daemon = start_daemon().wait_ready()
    with daemon.client() as client:
        client.post("/api/v1/workflows/stubborn/runs", json={})
    eventually((daemon_project.root / "stubborn.pid").exists)

    for _ in range(3):
        daemon.signal(signal.SIGTERM)
        time.sleep(0.05)

    assert daemon.wait_exit() == 130
    assert "stopping the runs at once" in daemon.log()


def test_an_idle_daemon_exits_0_on_sigterm_and_on_sigint(start_daemon: StartDaemon) -> None:
    first = start_daemon().wait_ready()
    first.signal(signal.SIGTERM)
    assert first.wait_exit() == 0

    second = start_daemon().wait_ready()
    second.signal(signal.SIGINT)
    assert second.wait_exit() == 0


def test_after_sigkill_a_restart_interrupts_the_run_and_readmits_the_queued_one(
    start_daemon: StartDaemon,
) -> None:
    daemon = start_daemon().wait_ready()
    with daemon.client() as client:
        first = client.post("/api/v1/workflows/nap/runs", json={}).json()["run"]["id"]
        second = client.post("/api/v1/workflows/nap/runs", json={}).json()["run"]["id"]
    pid_file = daemon.project.root / f"nap-{first}.pid"
    eventually(pid_file.exists)
    assert daemon.wait_for_run(second, "queued")["status"] == "queued"

    kill_group(daemon.pid)
    daemon.wait_exit()
    (daemon.project.root / "fast").touch()
    restarted = start_daemon().wait_ready()

    interrupted = restarted.wait_for_run(first, "interrupted")
    readmitted = restarted.wait_for_run(second, "succeeded", "failed")
    assert interrupted["reason"] == "executor_stopped"
    assert interrupted["steps"][0]["status"] == "interrupted"
    assert readmitted["status"] == "succeeded"
    log = restarted.log()
    assert f"run={first} was interrupted" in log
    assert "re-admitted 1 queued runs" in log
    assert "still exists; Treadle doesn't signal it" in log  # the orphaned `sleep`
    kill_group(int(pid_file.read_text()))


def test_hot_reload_applies_valid_edits_and_keeps_the_last_good_version(
    start_daemon: StartDaemon, daemon_project: ProjectFactory
) -> None:
    daemon = start_daemon().wait_ready()

    def workflow() -> dict[str, Any]:
        with daemon.client() as client:
            return dict(client.get("/api/v1/workflows/hello").json())

    original = workflow()
    daemon_project.write_workflow(
        "hello", "name: Hello again\nsteps:\n  - id: a\n    run: 'true'\n"
    )
    daemon.signal(signal.SIGHUP)
    edited = eventually(lambda: (shown := workflow())["name"] == "Hello again" and shown)
    daemon_project.write_workflow("hello", "name: Broken\nstpes: []\n")
    daemon.signal(signal.SIGHUP)
    stale = eventually(lambda: (shown := workflow())["stale"] and shown)

    assert edited["digest"] != original["digest"]
    assert stale["valid"] is False
    assert stale["name"] == "Hello again"
    assert stale["digest"] == edited["digest"]
    assert {issue["code"] for issue in stale["issues"]} == {"E101", "E102"}


def test_a_port_in_use_exits_8(start_daemon: StartDaemon) -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        daemon = start_daemon(args=("--port", str(port)))
        code = daemon.wait_exit()

    assert code == 8
    assert f"error: port {port} on 127.0.0.1 is in use" in daemon.log()


def test_a_second_daemon_exits_8(start_daemon: StartDaemon) -> None:
    first = start_daemon().wait_ready()

    second = start_daemon()
    code = second.wait_exit()

    assert code == 8
    assert (
        "error: another Treadle process holds the state directory lock "
        f"(serve, pid {first.pid}, port {first.port})"
    ) in second.log()
