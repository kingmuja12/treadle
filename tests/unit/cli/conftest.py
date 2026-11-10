"""Fixtures for CLI tests.

The command tests call `main()`, which runs its own event loop with the threaded store, so
they run in real time. None of them starts a subprocess or opens a socket.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from tests.support.project import ProjectFactory
from tests.unit.cli.helpers import Cli

from treadle.cli import commands

# Loggers whose levels `treadle serve` sets when it configures logging.
_DAEMON_LOGGERS = ("uvicorn", "httpx", "httpcore")


@pytest.fixture
def cli(project: ProjectFactory, capsys: pytest.CaptureFixture[str]) -> Cli:
    """Run `treadle` in-process in the temporary project and capture its output."""
    return Cli(project, capsys)


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check runs and control requests more often than the CLI does, so tests end sooner."""
    monkeypatch.setattr(commands, "_CONTROL_POLL", 0.02)
    monkeypatch.setattr(commands, "_FOLLOW_POLL", 0.02)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo logging configuration that a command leaves behind, such as the daemon's."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    levels = {name: logging.getLogger(name).level for name in _DAEMON_LOGGERS}
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    for name, saved in levels.items():
        logging.getLogger(name).setLevel(saved)
