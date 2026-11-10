"""Fixtures for daemon tests: real `treadle serve` processes in temporary projects."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.integration.daemon.helpers import Daemon
from tests.support.project import ProjectFactory

HOOK_SECRET = "0123456789abcdef-hook-secret"

HELLO = """\
name: Hello
inputs:
  name:
    default: world
steps:
  - id: greet
    env:
      NAME: ${{ inputs.name }}
    run: echo "Hello, $NAME!"
"""

DEPLOY = """\
triggers:
  webhook:
    name: deploy
    secret_env: DEPLOY_SECRET
steps:
  - id: show
    env:
      REF: ${{ trigger.body.ref }}
    run: echo "deploying $REF"
"""

# The first run sleeps until it's stopped, and records its process ID so that cleanup can
# find it. A run that finds `fast` in the project directory ends at once.
NAP = """\
steps:
  - id: nap
    run: |
      if [ -f "$TREADLE_PROJECT_DIR/fast" ]; then exit 0; fi
      echo $$ > "$TREADLE_PROJECT_DIR/nap-$TREADLE_RUN_ID.pid"
      sleep 30
"""

# A step that ignores SIGTERM, so only a SIGKILL ends it.
STUBBORN = """\
steps:
  - id: hold
    run: |
      trap '' TERM
      echo $$ > "$TREADLE_PROJECT_DIR/stubborn.pid"
      sleep 30
"""


@pytest.fixture
def daemon_project(tmp_path: Path) -> ProjectFactory:
    """A project with the daemon test workflows and the webhook secret in `.env`."""
    project = ProjectFactory(tmp_path / "project")
    project.write_workflow("hello", HELLO)
    project.write_workflow("deploy", DEPLOY)
    project.write_workflow("nap", NAP)
    project.write_env({"DEPLOY_SECRET": HOOK_SECRET})
    return project


@pytest.fixture
def start_daemon(daemon_project: ProjectFactory) -> Iterator[Callable[..., Daemon]]:
    """Start daemons in the project; every one is killed, with its steps, afterward."""
    started: list[Daemon] = []

    def start(**options: Any) -> Daemon:
        daemon = Daemon(daemon_project, name=f"daemon-{len(started)}", **options).start()
        started.append(daemon)
        return daemon

    yield start
    for daemon in started:
        daemon.cleanup()


@pytest.fixture
def root_logging() -> Iterator[logging.Logger]:
    """The root logger; its handlers and the levels that `serve` configures come back after."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    names = ("uvicorn", "httpx", "httpcore")
    levels = {name: logging.getLogger(name).level for name in names}
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, saved in levels.items():
        logging.getLogger(name).setLevel(saved)
