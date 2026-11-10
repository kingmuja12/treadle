"""Unit tests run on the virtual-time event loop, so sleeps and timeouts are instant."""

from __future__ import annotations

import pytest
from tests.support.vtime import VirtualTimeLoop


@pytest.fixture(scope="module")
def anyio_backend() -> tuple[str, dict[str, object]]:
    """Run async tests on asyncio with the virtual-time loop."""
    return ("asyncio", {"loop_factory": VirtualTimeLoop})
