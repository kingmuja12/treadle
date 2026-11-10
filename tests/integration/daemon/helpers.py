"""A real `treadle serve` process for daemon tests, and polling helpers.

Each daemon runs in its own process group, binds port 0, and is found through `holder.json`.
Cleanup kills the daemon's group and every step process group it recorded, so a failed test
leaves nothing running.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

import httpx
from tests.support.project import ProjectFactory

T = TypeVar("T")

READY_WITHIN = 20.0


def eventually(check: Callable[[], T | None], *, within: float = 20.0, every: float = 0.05) -> T:
    """Poll `check` until it returns a truthy value, and return it; fail after `within` s."""
    deadline = time.monotonic() + within
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"the condition didn't hold within {within}s")
        time.sleep(every)


def child_environ(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a Treadle child process, without this machine's Treadle settings."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("TREADLE_") and name not in {"COVERAGE_PROCESS_START"}
    }
    environ["NO_COLOR"] = "1"
    environ.update(extra or {})
    return environ


def kill_group(pgid: int) -> None:
    """SIGKILL a process group, ignoring one that is already gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def recorded_process_groups(db_path: Path) -> list[int]:
    """Return the step process groups that the database recorded."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT pgid FROM step_attempts WHERE pgid IS NOT NULL")
        return [row[0] for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def sign(secret: str, body: bytes) -> str:
    """Return a GitHub-style `X-Hub-Signature-256` value."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Daemon:
    """`python -m treadle serve --port 0` in a project, with its output in log files."""

    def __init__(
        self,
        project: ProjectFactory,
        *,
        args: Sequence[str] = ("--port", "0"),
        environ: Mapping[str, str] | None = None,
        name: str = "daemon",
    ) -> None:
        self.project = project
        self.settings = project.settings()
        self._args = list(args)
        self._environ = child_environ(environ)
        self._stdout = project.root.parent / f"{name}.out"
        self._stderr = project.root.parent / f"{name}.err"
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None

    def start(self) -> Daemon:
        """Start the daemon; `wait_ready` waits until it listens."""
        with self._stdout.open("ab") as out, self._stderr.open("ab") as err:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "treadle",
                    "-C",
                    str(self.project.root),
                    "serve",
                    *self._args,
                ],
                stdout=out,
                stderr=err,
                env=self._environ,
                start_new_session=True,
            )
        return self

    def wait_ready(self) -> Daemon:
        """Wait until `holder.json` names this process and its bound port."""
        holder = eventually(self._holder, within=READY_WITHIN)
        self.port = int(holder["port"])
        return self

    def wait_exit(self, within: float = 20.0) -> int:
        """Wait for the daemon to exit and return its exit status."""
        assert self.process is not None
        return self.process.wait(timeout=within)

    def signal(self, number: int) -> None:
        assert self.process is not None
        self.process.send_signal(number)

    @property
    def pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    @property
    def url(self) -> str:
        assert self.port is not None
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=10.0)

    def log(self) -> str:
        """The daemon's stderr so far."""
        return self._stderr.read_text(encoding="utf-8", errors="replace")

    def run_status(self, run_id: int) -> dict[str, Any] | None:
        """Return a run from the API, or None while it doesn't exist."""
        with self.client() as client:
            response = client.get(f"/api/v1/runs/{run_id}")
        return response.json() if response.status_code == 200 else None

    def wait_for_run(self, run_id: int, *statuses: str) -> dict[str, Any]:
        """Wait until the run has one of `statuses`, through the API, and return it."""

        def reached() -> dict[str, Any] | None:
            run = self.run_status(run_id)
            return run if run is not None and run["status"] in statuses else None

        return eventually(reached)

    def cleanup(self) -> None:
        """Kill the daemon's process group and every step group it started."""
        if self.process is not None and self.process.poll() is None:
            kill_group(self.process.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
        for pgid in recorded_process_groups(self.settings.db_path):
            kill_group(pgid)

    def _holder(self) -> dict[str, Any] | None:
        assert self.process is not None
        if self.process.poll() is not None:
            raise AssertionError(f"the daemon exited with {self.process.returncode}:\n{self.log()}")
        try:
            holder = json.loads(self.settings.holder_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if holder.get("pid") == self.process.pid and holder.get("port"):
            return dict(holder)
        return None
