"""`treadle serve` through `main()`, for the refusals that end before the daemon serves."""

from __future__ import annotations

import logging
import os
import signal
import socket
from types import FrameType
from typing import Any

import pytest
from tests.support.project import ProjectFactory

from treadle.cli import commands
from treadle.cli.main import main
from treadle.config import Settings
from treadle.types import ExitCode


def test_a_public_bind_without_a_token_exits_3(
    daemon_project: ProjectFactory,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TREADLE_TOKEN", raising=False)

    code = main(["-C", str(daemon_project.root), "serve", "--host", "192.0.2.10", "--port", "0"])

    assert code == ExitCode.CONFIG
    assert capsys.readouterr().err == (
        "error: set TREADLE_TOKEN (at least 32 characters) or bind 127.0.0.1\n"
    )


def test_a_busy_port_exits_8_and_frees_the_lock(
    daemon_project: ProjectFactory,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TREADLE_TOKEN", raising=False)
    root = str(daemon_project.root)

    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        code = main(["-C", root, "--debug", "serve", "--port", str(port), "--log-level", "error"])

    assert code == ExitCode.BUSY
    assert f"error: port {port} on 127.0.0.1 is in use" in capsys.readouterr().err
    assert not daemon_project.settings().holder_path.exists()
    assert root_logging.level == logging.ERROR  # --log-level wins over --debug


def test_serve_ignores_sighup_until_the_daemon_handles_it(
    daemon_project: ProjectFactory,
    root_logging: logging.Logger,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TREADLE_TOKEN", raising=False)
    received: list[int] = []

    def record(number: int, frame: FrameType | None) -> None:
        received.append(number)

    real_load = commands.load_settings

    def load_then_hang_up(*args: Any, **kwargs: Any) -> Settings:
        os.kill(os.getpid(), signal.SIGHUP)  # handled before `os.kill` returns
        return real_load(*args, **kwargs)

    monkeypatch.setattr(commands, "load_settings", load_then_hang_up)
    # The test's own handler stands in for the default action, which would end the test run.
    previous = signal.signal(signal.SIGHUP, record)
    try:
        code = main(["-C", str(daemon_project.root), "serve", "--host", "192.0.2.10"])
        after = signal.getsignal(signal.SIGHUP)
    finally:
        signal.signal(signal.SIGHUP, previous)

    assert code == ExitCode.CONFIG  # the daemon then refused the bind without a token
    assert received == []
    assert after is record
    assert "set TREADLE_TOKEN" in capsys.readouterr().err
