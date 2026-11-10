"""Fixtures for end-to-end tests, which drive the installed `treadle` console script."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from tests.integration.daemon.helpers import child_environ

Treadle = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="session")
def treadle_script() -> Path:
    """The console script installed next to this interpreter."""
    found = shutil.which("treadle", path=str(Path(sys.executable).parent)) or shutil.which(
        "treadle"
    )
    if found is None:
        pytest.fail("the `treadle` console script isn't installed; run `uv sync`")
    return Path(found)


@pytest.fixture(scope="session")
def treadle(treadle_script: Path) -> Treadle:
    """Run `treadle ARGS...` to completion and return the finished process, with text output."""

    def run(*args: str, cwd: Path, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(treadle_script), *args],
            cwd=cwd,
            env=child_environ({"TZ": "UTC"}),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return run


def start_treadle(script: Path, args: Sequence[str], *, cwd: Path) -> subprocess.Popen[str]:
    """Start `treadle ARGS...` in its own process group, with piped text output."""
    return subprocess.Popen(
        [str(script), *args],
        cwd=cwd,
        env=child_environ({"TZ": "UTC"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
