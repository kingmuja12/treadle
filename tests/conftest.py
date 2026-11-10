"""Fixtures that every test package shares."""

from __future__ import annotations

import random
import textwrap
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import httpx
import pytest

from tests.support.clock import FakeClock
from tests.support.fakes import FakeStep, FakeSteps
from tests.support.project import ProjectFactory
from treadle.config import Settings
from treadle.models import Workflow
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.steps.http import shared_client
from treadle.store.async_store import AsyncStore
from treadle.workflow.loader import WorkflowLoader


@pytest.fixture
def project(tmp_path: Path) -> ProjectFactory:
    """A temporary project in `tmp_path/project`."""
    return ProjectFactory(tmp_path / "project")


@pytest.fixture
def settings(project: ProjectFactory) -> Settings:
    """The temporary project's settings."""
    return project.settings()


@pytest.fixture
def clock() -> FakeClock:
    """A fake clock that starts at 2026-11-09 12:00 UTC and follows loop time."""
    return FakeClock()


@pytest.fixture
def rng() -> random.Random:
    """A seeded random source."""
    return random.Random(1234)


@pytest.fixture
async def store(settings: Settings) -> AsyncIterator[AsyncStore]:
    """An inline owner store on the project's state directory, closed after the test."""
    opened = await AsyncStore.open(settings, inline=True)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
def steps_registry() -> StepRegistry:
    """The built-in step registry."""
    return builtin_registry()


@pytest.fixture
def fake_steps() -> FakeSteps:
    """A registry with one FakeStep named `fake`, and the FakeStep itself."""
    step = FakeStep()
    return FakeSteps(StepRegistry([step]), step)


@pytest.fixture
def load_workflow(settings: Settings, fake_steps: FakeSteps) -> Callable[..., Workflow]:
    """Return `load(text, *, workflow_id="wf", steps=None) -> Workflow`.

    It loads dedented YAML with the real loader (the built-in step types plus the `fake` step
    by default, and no machine-specific checks) and fails the test with the rendered issues
    when the workflow isn't valid.
    """

    def load(text: str, *, workflow_id: str = "wf", steps: StepRegistry | None = None) -> Workflow:
        if steps is None:
            steps = builtin_registry()
            steps.register(fake_steps.step)
        loader = WorkflowLoader(
            steps,
            project_dir=settings.project_dir,
            default_timezone=settings.timezone,
            limits=settings.limits,
            state_dir=settings.state_dir,
            workflows_dir=settings.workflows_dir,
            environment_checks=False,
        )
        path = settings.workflows_dir / f"{workflow_id}.yml"
        result = loader.load_text(textwrap.dedent(text), path)
        if not result.ok or result.workflow is None:
            rendered = "\n".join(issue.render(str(path), result.source) for issue in result.issues)
            pytest.fail(f"the workflow didn't load:\n{rendered}", pytrace=False)
        return result.workflow

    return load


@pytest.fixture
def mock_http() -> Callable[..., httpx.AsyncClient]:
    """Return `build(handler) -> httpx.AsyncClient` over `httpx.MockTransport(handler)`.

    The client comes from `treadle.steps.http.shared_client`, so tests exercise the
    production client's redirect limit, User-Agent, and cookie policy.
    """

    def build(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
        return shared_client(transport=httpx.MockTransport(handler))

    return build


@pytest.fixture
def secret_snapshot() -> Callable[..., SecretSnapshot]:
    """Return `build(*, environ=None, dotenv=None, **secrets) -> SecretSnapshot`.

    Keyword values become the secrets, and the `.env` entries unless `dotenv` is given.
    """

    def build(
        *,
        environ: Mapping[str, str] | None = None,
        dotenv: Mapping[str, str] | None = None,
        **secrets: str,
    ) -> SecretSnapshot:
        return SecretSnapshot(
            secrets=dict(secrets),
            dotenv=dict(secrets if dotenv is None else dotenv),
            environ=dict(environ or {}),
        )

    return build
