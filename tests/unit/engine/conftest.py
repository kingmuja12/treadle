"""Fixtures for engine tests: an executor over the inline store, and the fake step it runs."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from tests.support.clock import FakeClock
from tests.support.fakes import FakeStep, FakeSteps
from tests.unit.engine.helpers import ENVIRON, Engine, Secrets

from treadle.config import Settings
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepType
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore

API_TOKEN = "s3cr3t-api-token"


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")


@pytest.fixture
def secrets(secret_snapshot: Callable[..., SecretSnapshot]) -> Secrets:
    """The secret `API_TOKEN`, and the hidden `HOOK_SECRET` variable of `ENVIRON`."""
    return Secrets(secret_snapshot(environ=ENVIRON, API_TOKEN=API_TOKEN))


@pytest.fixture
async def http(mock_http: Callable[..., httpx.AsyncClient]) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client that fails any request; the fake step never sends one."""
    client = mock_http(_refuse)
    yield client
    await client.aclose()


@pytest.fixture
def fake(fake_steps: FakeSteps) -> FakeStep:
    """The `fake` step type that the engine runs."""
    return fake_steps.step


@pytest.fixture
def make_engine(
    store: AsyncStore,
    settings: Settings,
    fake: FakeStep,
    clock: FakeClock,
    rng: Any,
    http: httpx.AsyncClient,
    secrets: Secrets,
) -> Callable[..., Engine]:
    """Return `make(*step_types, **sections) -> Engine`.

    The engine runs the built-in step types, `fake`, and `step_types` on the inline store.
    Keywords replace whole settings sections, such as `limits=Limits(...)`.
    """

    def make(*step_types: StepType, **sections: Any) -> Engine:
        steps = builtin_registry()
        for step_type in (fake, *step_types):
            steps.register(step_type)
        return Engine(
            store=store,
            settings=dataclasses.replace(settings, **sections),
            steps=steps,
            clock=clock,
            rng=rng,
            http=http,
            secrets=secrets,
        )

    return make


@pytest.fixture
def engine(make_engine: Callable[..., Engine]) -> Engine:
    """An engine with the project's settings."""
    return make_engine()
