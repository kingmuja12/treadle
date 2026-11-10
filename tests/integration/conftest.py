"""Integration tests run on a real asyncio event loop, with real time, threads, and processes."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run async tests on asyncio. Treadle uses asyncio APIs directly."""
    return "asyncio"
