from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import pytest
from tests.support.clock import FakeClock
from tests.support.fakes import RecordingSink
from tests.support.project import ProjectFactory
from tests.unit.store.helpers import new_run
from tests.unit.triggers.helpers import running

from treadle.clock import inline_runner
from treadle.config import Settings
from treadle.models import RegistryChange, Workflow, WorkflowEntry
from treadle.protocols import TriggerSink
from treadle.registry import WorkflowRegistry
from treadle.secrets import SecretStore
from treadle.steps.builtin import builtin_registry
from treadle.store.async_store import AsyncStore

T = TypeVar("T")
needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads files regardless of their mode"
)

HELLO = """\
steps:
  - id: hello
    uses: log
    with:
      message: hi
"""
INVALID = """\
steps: []
"""


def with_hook(
    name: str,
    secret_env: str = "HOOK_SECRET",  # noqa: S107 - a variable name, not a secret
    message: str = "hi",
) -> str:
    return f"""\
triggers:
  webhook:
    name: {name}
    secret_env: {secret_env}
steps:
  - id: hello
    uses: log
    with:
      message: {message}
"""


_RECORD: Any = object()


class FlakySink:
    """A sink that fails its next `failures` changes, and records the versions it applied."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.applied: list[Workflow | None] = []

    def apply(self, change: RegistryChange) -> None:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the sink failed")
        self.applied.append(change.new)


class Harness:
    """A registry over a temporary project, recording what it emits and removes.

    `sinks` replaces the recording sink, and `on_removed` the recording callback.
    """

    def __init__(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        *,
        blocking: Any = inline_runner,
        sinks: Sequence[TriggerSink] | None = None,
        on_removed: Callable[[str], Awaitable[None]] | None = _RECORD,
    ) -> None:
        self.project = project
        self.sink = RecordingSink()
        self.removed: list[str] = []
        self.secrets = SecretStore(settings.env_file, {})
        self.secrets.load()
        self.registry = WorkflowRegistry(
            settings=settings,
            steps=builtin_registry(),
            secrets=self.secrets,
            store=store,
            sinks=[self.sink] if sinks is None else sinks,
            on_removed=self.record_removal if on_removed is _RECORD else on_removed,
            clock=clock,
            blocking=blocking,
        )

    async def record_removal(self, workflow_id: str) -> None:
        self.removed.append(workflow_id)

    def entry(self, workflow_id: str) -> WorkflowEntry:
        entry = self.registry.get(workflow_id)
        assert entry is not None
        return entry

    def current(self, workflow_id: str) -> Workflow:
        current = self.entry(workflow_id).current
        assert current is not None
        return current

    def codes(self, workflow_id: str) -> list[str]:
        return [issue.code for issue in self.entry(workflow_id).issues]


@pytest.fixture
def harness(
    project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
) -> Harness:
    return Harness(project, settings, store, clock)


class TestLoadAll:
    async def test_valid_files_become_current_and_reach_the_sinks(self, harness: Harness) -> None:
        harness.project.write_workflow("hello", HELLO)
        harness.project.write_workflow("broken", INVALID)
        harness.project.write_workflow("hook", with_hook("push"))

        changes = await harness.registry.load_all()

        assert [(c.workflow_id, c.old, c.new is not None) for c in changes] == [
            ("hello", None, True),
            ("hook", None, True),
        ]
        assert harness.sink.changes == changes
        entries = harness.registry.entries()
        assert [entry.workflow_id for entry in entries] == ["broken", "hello", "hook"]
        broken = entries[0]
        assert (broken.current, broken.stale, broken.valid) == (None, False, False)
        assert broken.source == INVALID
        assert [issue.code for issue in broken.issues] == ["E104"]
        assert harness.codes("hook") == ["W311"]
        assert harness.entry("hook").valid
        assert harness.current("hello").path == harness.project.workflows_dir / "hello.yml"

    async def test_hidden_and_non_yaml_files_are_ignored(self, harness: Harness) -> None:
        for name in (".hidden.yml", "_draft.yml", "notes.txt", "hello.yml~"):
            (harness.project.workflows_dir / name).write_text(HELLO, encoding="utf-8")
        (harness.project.workflows_dir / "nested").mkdir()
        (harness.project.workflows_dir / "nested" / "deep.yml").write_text(HELLO, encoding="utf-8")
        harness.project.write_workflow("real.yaml", HELLO)

        await harness.registry.load_all()

        assert [entry.workflow_id for entry in harness.registry.entries()] == ["real"]

    async def test_a_webhook_name_belongs_to_the_first_workflow_in_path_order(
        self, harness: Harness
    ) -> None:
        harness.project.write_workflow("alpha", with_hook("push"))
        harness.project.write_workflow("beta", with_hook("push"))

        changes = await harness.registry.load_all()

        assert [change.workflow_id for change in changes] == ["alpha"]
        assert harness.codes("beta") == ["E305", "W311"]
        assert harness.entry("beta").current is None

    async def test_duplicate_ids_keep_the_first_file(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness.project.write_workflow("twin.yaml", HELLO)
        harness.project.write_workflow("twin.yml", with_hook("push"))

        await harness.registry.load_all()

        assert harness.current("twin").path.name == "twin.yaml"
        assert "workflows/twin.yml: duplicate workflow ID 'twin': twin.yaml uses it" in caplog.text

    async def test_a_file_with_an_invalid_id_is_listed_by_its_stem(self, harness: Harness) -> None:
        harness.project.write_workflow("Not Valid.yml", HELLO)

        await harness.registry.load_all()

        [entry] = harness.registry.entries()
        assert entry.workflow_id == "Not Valid"
        assert entry.current is None
        assert harness.codes("Not Valid") == ["E057"]

    async def test_a_missing_workflows_directory_holds_no_workflows(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        shutil.rmtree(harness.project.workflows_dir)

        assert await harness.registry.load_all() == []
        assert harness.registry.entries() == []
        assert "can't scan the workflows directory (path missing:" in caplog.text

    async def test_loading_again_removes_workflows_whose_files_are_gone(
        self, harness: Harness
    ) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        old = harness.current("hello")
        path.unlink()

        changes = await harness.registry.load_all()

        assert changes == [RegistryChange("hello", old, None)]
        assert harness.removed == ["hello"]

    async def test_issues_are_logged_rendered(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        harness.project.write_workflow("broken", INVALID)
        harness.project.write_workflow("hook", with_hook("push"))

        await harness.registry.load_all()

        assert "workflows/broken.yml is invalid\nworkflows/broken.yml:1:" in caplog.text
        assert "error[E104]" in caplog.text
        assert "workflows/hook.yml has warnings\nworkflows/hook.yml:" in caplog.text


class TestHotReload:
    async def test_a_valid_edit_replaces_the_current_version(self, harness: Harness) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        old = harness.current("hello")

        path.write_text(HELLO.replace("hi", "hello again"), encoding="utf-8")
        changes = await harness.registry.poll()

        new = harness.current("hello")
        assert changes == [RegistryChange("hello", old, new)]
        assert harness.sink.changes[-1] == changes[0]
        assert new.digest != old.digest

    async def test_an_invalid_edit_keeps_the_last_good_version_and_marks_it_stale(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        good = harness.current("hello")

        path.write_text(INVALID, encoding="utf-8")
        assert await harness.registry.poll() == []

        entry = harness.entry("hello")
        assert (entry.current, entry.stale, entry.valid) == (good, True, False)
        assert entry.source == INVALID
        assert [issue.code for issue in entry.issues] == ["E104"]
        assert "workflows/hello.yml is invalid; keeping the last good version" in caplog.text

        path.write_text(HELLO.replace("hi", "fixed"), encoding="utf-8")
        [change] = await harness.registry.poll()
        assert change.old == good
        fixed = harness.entry("hello")
        assert (fixed.stale, fixed.valid, fixed.issues) == (False, True, ())

    async def test_a_new_file_is_loaded(self, harness: Harness) -> None:
        await harness.registry.load_all()
        harness.project.write_workflow("hello", HELLO)

        [change] = await harness.registry.poll()

        assert (change.workflow_id, change.old) == ("hello", None)

    async def test_a_deleted_file_is_removed_and_reported(self, harness: Harness) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        old = harness.current("hello")

        path.unlink()
        changes = await harness.registry.poll()

        assert changes == [RegistryChange("hello", old, None)]
        assert harness.registry.get("hello") is None
        assert harness.removed == ["hello"]

    async def test_deleting_an_invalid_file_removes_it_without_a_change(
        self, harness: Harness
    ) -> None:
        path = harness.project.write_workflow("broken", INVALID)
        await harness.registry.load_all()

        path.unlink()

        assert await harness.registry.poll() == []
        assert harness.registry.entries() == []
        assert harness.removed == ["broken"]

    async def test_rewriting_the_same_content_emits_nothing(self, harness: Harness) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        os.utime(path, ns=(1, 1))

        assert await harness.registry.poll() == []
        assert len(harness.sink.changes) == 1

    async def test_an_edit_that_takes_another_workflows_hook_name_is_invalid(
        self, harness: Harness
    ) -> None:
        harness.project.write_workflow("owner", with_hook("push"))
        path = harness.project.write_workflow("alpha", with_hook("pull"))
        await harness.registry.load_all()
        good = harness.current("alpha")

        path.write_text(with_hook("push"), encoding="utf-8")
        assert await harness.registry.poll() == []

        entry = harness.entry("alpha")
        assert (entry.current, entry.stale) == (good, True)
        [conflict] = [issue for issue in entry.issues if issue.code == "E305"]
        assert conflict.message == "webhook name 'push' is already used by workflow 'owner'"
        assert conflict.position is not None
        assert (conflict.position.line, conflict.position.column) == (3, 11)

    async def test_a_deleted_workflow_releases_its_hook_names(self, harness: Harness) -> None:
        old_path = harness.project.write_workflow("old", with_hook("push"))
        await harness.registry.load_all()

        old_path.unlink()
        harness.project.write_workflow("new", with_hook("push"))
        changes = await harness.registry.poll()

        assert [(c.workflow_id, c.new is not None) for c in changes] == [
            ("old", False),
            ("new", True),
        ]

    async def test_a_hook_moved_into_an_earlier_file_moves_in_one_edit(
        self, harness: Harness
    ) -> None:
        deploy = harness.project.write_workflow("deploy", with_hook("push"))
        await harness.registry.load_all()

        harness.project.write_workflow("build", with_hook("push"))
        deploy.write_text(HELLO, encoding="utf-8")
        changes = await harness.registry.poll()

        assert [change.workflow_id for change in changes] == ["deploy", "build"]
        assert harness.codes("build") == ["W311"]
        assert [hook.name for hook in harness.current("build").webhooks()] == ["push"]
        assert harness.current("deploy").webhooks() == ()

    async def test_a_hook_moved_in_two_edits_moves_once_its_owner_drops_it(
        self, harness: Harness
    ) -> None:
        deploy = harness.project.write_workflow("deploy", with_hook("push"))
        await harness.registry.load_all()
        harness.project.write_workflow("zeta", with_hook("push"))
        assert await harness.registry.poll() == []
        assert harness.codes("zeta") == ["E305", "W311"]

        deploy.write_text(HELLO, encoding="utf-8")
        changes = await harness.registry.poll()

        assert [change.workflow_id for change in changes] == ["deploy", "zeta"]
        assert harness.codes("zeta") == ["W311"]

    async def test_a_conflict_found_at_startup_clears_when_the_owner_drops_the_hook(
        self, harness: Harness
    ) -> None:
        alpha = harness.project.write_workflow("alpha", with_hook("push"))
        harness.project.write_workflow("beta", with_hook("push"))
        await harness.registry.load_all()
        assert harness.codes("beta") == ["E305", "W311"]

        alpha.write_text(HELLO, encoding="utf-8")
        changes = await harness.registry.poll()

        assert [(c.workflow_id, c.new is not None) for c in changes] == [
            ("alpha", True),
            ("beta", True),
        ]
        assert harness.codes("beta") == ["W311"]

    async def test_a_hook_name_still_in_use_keeps_the_conflict(self, harness: Harness) -> None:
        owner = harness.project.write_workflow("owner", with_hook("push"))
        harness.project.write_workflow("zeta", with_hook("push"))
        await harness.registry.load_all()

        owner.write_text(with_hook("push", message="edited"), encoding="utf-8")
        changes = await harness.registry.poll()

        assert [change.workflow_id for change in changes] == ["owner"]
        assert harness.codes("zeta") == ["E305", "W311"]
        assert harness.entry("zeta").current is None

    async def test_a_released_hook_name_goes_to_the_first_waiting_file_in_path_order(
        self, harness: Harness
    ) -> None:
        owner = harness.project.write_workflow("owner", with_hook("push"))
        harness.project.write_workflow("x-first", with_hook("push"))
        harness.project.write_workflow("y-second", with_hook("push"))
        await harness.registry.load_all()

        owner.write_text(HELLO, encoding="utf-8")
        await harness.registry.poll()

        assert harness.codes("x-first") == ["W311"]
        [conflict] = [issue for issue in harness.entry("y-second").issues if issue.code == "E305"]
        assert conflict.message == "webhook name 'push' is already used by workflow 'x-first'"

    async def test_a_vanished_file_counts_as_deleted(
        self, project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
    ) -> None:
        doomed = project.write_workflow("doomed", HELLO)

        async def vanish_first(fn: Callable[..., T], /, *args: Any) -> T:
            if args == (doomed,):
                doomed.unlink(missing_ok=True)
            return fn(*args)

        harness = Harness(project, settings, store, clock, blocking=vanish_first)
        assert await harness.registry.load_all() == []
        assert harness.registry.entries() == []

    async def test_a_new_file_that_vanishes_before_loading_is_ignored(
        self, project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
    ) -> None:
        doomed = project.workflows_dir / "doomed.yml"

        async def vanish(fn: Callable[..., T], /, *args: Any) -> T:
            if args == (doomed,):
                doomed.unlink(missing_ok=True)
            return fn(*args)

        harness = Harness(project, settings, store, clock, blocking=vanish)
        await harness.registry.load_all()
        project.write_workflow("doomed", HELLO)

        assert await harness.registry.poll() == []
        assert harness.registry.entries() == []
        assert harness.removed == []

    @needs_permissions
    async def test_an_unreadable_file_counts_as_deleted(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        path.chmod(0)
        os.utime(path, ns=(1, 1))
        try:
            [change] = await harness.registry.poll()
        finally:
            path.chmod(0o644)

        assert change.new is None
        assert "can't read workflows/hello.yml" in caplog.text
        assert "treating it as deleted" in caplog.text

    async def test_a_missing_workflows_directory_keeps_the_loaded_workflows(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="treadle.registry")
        harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        moved = harness.project.root / "moved"
        harness.project.workflows_dir.rename(moved)

        assert await harness.registry.poll() == []
        assert await harness.registry.poll() == []
        assert [entry.workflow_id for entry in harness.registry.entries()] == ["hello"]
        assert caplog.text.count("can't scan the workflows directory") == 1

        moved.rename(harness.project.workflows_dir)
        assert await harness.registry.poll() == []
        assert "the workflows directory can be scanned again" in caplog.text

    async def test_a_removal_callback_failure_is_logged(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def broken(workflow_id: str) -> None:
            raise RuntimeError("the run manager is gone")

        harness = Harness(project, settings, store, clock, on_removed=broken)
        path = project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        path.unlink()

        [change] = await harness.registry.poll()

        assert change.new is None
        assert "workflow hello: cancelling its queued runs failed" in caplog.text

    async def test_without_a_removal_callback_deletion_still_works(
        self, project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
    ) -> None:
        harness = Harness(project, settings, store, clock, on_removed=None)
        path = project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        path.unlink()

        [change] = await harness.registry.poll()
        assert change.new is None

    async def test_a_duplicate_takes_over_when_the_first_file_goes(self, harness: Harness) -> None:
        first = harness.project.write_workflow("twin.yaml", HELLO)
        harness.project.write_workflow("twin.yml", HELLO.replace("hi", "second"))
        await harness.registry.load_all()

        first.unlink()
        [change] = await harness.registry.poll()

        assert change.new is not None
        assert change.new.path.name == "twin.yml"


class TestFailures:
    async def test_a_file_that_fails_to_load_doesnt_hold_back_the_others(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        beta = project.workflows_dir / "beta.yml"
        failures = 1

        async def flaky(fn: Callable[..., T], /, *args: Any) -> T:
            nonlocal failures
            if args == (beta,) and failures:
                failures -= 1
                raise RuntimeError("the loader broke")
            return fn(*args)

        harness = Harness(project, settings, store, clock, blocking=flaky)
        await harness.registry.load_all()
        for name in ("alpha", "beta", "gamma"):
            project.write_workflow(name, HELLO)

        changes = await harness.registry.poll()
        assert [change.workflow_id for change in changes] == ["alpha", "gamma"]
        assert "workflows/beta.yml: reloading failed; trying again" in caplog.text
        assert "RuntimeError: the loader broke" in caplog.text

        [change] = await harness.registry.poll()
        assert change.workflow_id == "beta"

    async def test_a_file_that_keeps_failing_is_logged_once_per_file_state(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        beta = project.workflows_dir / "beta.yml"

        async def broken(fn: Callable[..., T], /, *args: Any) -> T:
            if args == (beta,):
                raise RuntimeError("the loader broke")
            return fn(*args)

        harness = Harness(project, settings, store, clock, blocking=broken)
        await harness.registry.load_all()
        project.write_workflow("beta", HELLO)

        for _ in range(4):
            assert await harness.registry.poll() == []
        assert caplog.text.count("workflows/beta.yml: reloading failed; trying again") == 1
        assert caplog.text.count("reloading failed again; waiting for the file to change") == 1
        assert caplog.text.count("RuntimeError: the loader broke") == 1

        project.write_workflow("beta", HELLO.replace("hi", "edited"))
        await harness.registry.poll()
        assert caplog.text.count("workflows/beta.yml: reloading failed; trying again") == 2

    async def test_a_change_that_a_sink_fails_to_apply_is_sent_again(
        self, project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
    ) -> None:
        sink = FlakySink()
        harness = Harness(project, settings, store, clock, sinks=[sink])
        path = project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        good = harness.current("hello")

        path.write_text(HELLO.replace("hi", "edited"), encoding="utf-8")
        sink.failures = 1
        assert await harness.registry.poll() == []
        assert harness.current("hello") is good  # the entry waits until every sink has it

        [change] = await harness.registry.poll()
        assert change.old is good
        assert sink.applied == [good, harness.current("hello")]

    async def test_a_removal_that_a_sink_fails_to_apply_is_sent_again(
        self, project: ProjectFactory, settings: Settings, store: AsyncStore, clock: FakeClock
    ) -> None:
        sink = FlakySink()
        harness = Harness(project, settings, store, clock, sinks=[sink])
        path = project.write_workflow("hello", HELLO)
        await harness.registry.load_all()

        path.unlink()
        sink.failures = 1
        assert await harness.registry.poll() == []
        assert harness.registry.get("hello") is not None

        [change] = await harness.registry.poll()
        assert change.new is None
        assert harness.registry.get("hello") is None
        assert harness.removed == ["hello"]


class TestSideFiles:
    async def test_an_env_change_reloads_the_secrets_for_new_loads(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="treadle.registry")
        path = harness.project.write_workflow("hook", with_hook("push"))
        await harness.registry.load_all()
        assert harness.codes("hook") == ["W311"]

        harness.project.write_env({"HOOK_SECRET": "a-sixteen-char-secret"})
        os.utime(harness.project.env_path, ns=(5, 5))
        path.write_text(with_hook("push", message="again"), encoding="utf-8")
        await harness.registry.poll()

        assert harness.secrets.snapshot.lookup_env("HOOK_SECRET") == "a-sixteen-char-secret"
        assert harness.codes("hook") == []
        assert "reloaded the secrets from" in caplog.text

    async def test_a_config_change_asks_for_a_restart_once(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        await harness.registry.load_all()
        harness.project.write_config('timezone = "Europe/London"\n')

        await harness.registry.poll()
        await harness.registry.poll()

        assert caplog.text.count("treadle.toml changed; restart Treadle to apply it") == 1

    async def test_a_deleted_config_file_also_asks_for_a_restart(
        self, harness: Harness, caplog: pytest.LogCaptureFixture
    ) -> None:
        await harness.registry.load_all()
        harness.project.config_path.unlink()

        await harness.registry.poll()

        assert "treadle.toml changed; restart Treadle to apply it" in caplog.text

    async def test_files_outside_the_project_are_shown_with_absolute_paths(
        self,
        project: ProjectFactory,
        store: AsyncStore,
        clock: FakeClock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "broken.yml").write_text(INVALID, encoding="utf-8")
        outside = project.settings(workflows_dir=elsewhere)
        harness = Harness(project, outside, store, clock)

        await harness.registry.load_all()

        assert f"{elsewhere / 'broken.yml'} is invalid" in caplog.text

    async def test_without_a_config_file_nothing_is_checked(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bare = dataclasses.replace(settings, config_path=None)
        harness = Harness(project, bare, store, clock)
        await harness.registry.load_all()
        project.write_config('timezone = "Europe/London"\n')

        assert await harness.registry.poll() == []
        assert "restart Treadle" not in caplog.text


class TestLookups:
    async def test_version_returns_the_current_version_by_digest(self, harness: Harness) -> None:
        harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        current = harness.current("hello")

        assert await harness.registry.version(current.digest) is current

    async def test_version_parses_a_stored_version_again(
        self, harness: Harness, store: AsyncStore
    ) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()
        old = harness.current("hello")
        await store.write(lambda repo: repo.create_run(new_run(old)))
        path.write_text(HELLO.replace("hi", "edited"), encoding="utf-8")
        await harness.registry.poll()

        stored = await harness.registry.version(old.digest)

        assert stored is not None
        assert stored is not old
        assert (stored.id, stored.digest, stored.source) == ("hello", old.digest, HELLO)

    async def test_version_of_identical_files_is_the_requested_workflow(
        self, harness: Harness
    ) -> None:
        harness.project.write_workflow("backup-docs", HELLO)
        harness.project.write_workflow("backup-photos", HELLO)
        await harness.registry.load_all()
        photos = harness.current("backup-photos")
        assert harness.current("backup-docs").digest == photos.digest

        assert await harness.registry.version(photos.digest, workflow_id="backup-photos") is photos
        assert await harness.registry.version(photos.digest, workflow_id="other") is None

    async def test_version_parses_a_stored_version_as_the_requested_workflow(
        self, harness: Harness, store: AsyncStore
    ) -> None:
        docs = harness.project.write_workflow("backup-docs", HELLO)
        photos = harness.project.write_workflow("backup-photos", HELLO)
        await harness.registry.load_all()
        old = harness.current("backup-docs")
        await store.write(lambda repo: repo.create_run(new_run(old)))
        for path in (docs, photos):
            path.write_text(HELLO.replace("hi", path.stem), encoding="utf-8")
        await harness.registry.poll()

        stored = await harness.registry.version(old.digest, workflow_id="backup-photos")

        assert stored is not None
        assert (stored.id, stored.path.name, stored.digest) == (
            "backup-photos",
            "backup-photos.yml",
            old.digest,
        )

    async def test_version_of_an_unknown_digest_is_none(self, harness: Harness) -> None:
        assert await harness.registry.version("0" * 64) is None

    async def test_version_that_no_longer_loads_is_none(
        self, harness: Harness, store: AsyncStore, load_workflow: Callable[..., Workflow]
    ) -> None:
        fake = load_workflow("steps:\n  - id: a\n    uses: fake\n")
        await store.write(lambda repo: repo.create_run(new_run(fake)))

        assert await harness.registry.version(fake.digest) is None

    async def test_hidden_env_names_the_token_and_every_current_hook_secret(
        self, harness: Harness
    ) -> None:
        harness.project.write_workflow("a", with_hook("one", secret_env="ONE_SECRET"))
        harness.project.write_workflow("b", with_hook("two", secret_env="TWO_SECRET"))
        harness.project.write_workflow("c", INVALID)
        await harness.registry.load_all()

        assert harness.registry.hidden_env() == {"TREADLE_TOKEN", "ONE_SECRET", "TWO_SECRET"}

    async def test_current_digests_cover_every_current_version(self, harness: Harness) -> None:
        harness.project.write_workflow("a", HELLO)
        harness.project.write_workflow("b", HELLO.replace("hi", "bee"))
        harness.project.write_workflow("c", INVALID)
        await harness.registry.load_all()

        expected = {harness.current("a").digest, harness.current("b").digest}
        assert harness.registry.current_digests() == expected


class TestRun:
    async def test_changes_apply_after_the_debounce(self, harness: Harness) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()

        async with running(harness.registry.run):
            await asyncio.sleep(0.1)
            path.write_text(HELLO.replace("hi", "one"), encoding="utf-8")
            await asyncio.sleep(1.1)
            assert len(harness.sink.changes) == 1
            await asyncio.sleep(0.5)
            assert len(harness.sink.changes) == 2

    async def test_request_reload_polls_at_once(self, harness: Harness) -> None:
        path = harness.project.write_workflow("hello", HELLO)
        await harness.registry.load_all()

        async with running(harness.registry.run):
            await asyncio.sleep(0.1)
            path.write_text(HELLO.replace("hi", "now"), encoding="utf-8")
            harness.registry.request_reload()
            await asyncio.sleep(0.01)
            assert len(harness.sink.changes) == 2

    async def test_a_failing_change_is_logged_and_applied_again(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        sink = FlakySink(failures=1)
        harness = Harness(project, settings, store, clock, sinks=[sink])
        await harness.registry.load_all()

        async with running(harness.registry.run):
            await asyncio.sleep(0.5)
            harness.project.write_workflow("a", HELLO)
            await asyncio.sleep(3)
            harness.project.write_workflow("b", HELLO)
            await asyncio.sleep(3)

        assert "workflows/a.yml: reloading failed; trying again" in caplog.text
        assert sink.calls == 3
        assert [workflow.id for workflow in sink.applied if workflow] == ["a", "b"]

    async def test_a_failing_poll_is_logged_and_polling_goes_on(
        self,
        project: ProjectFactory,
        settings: Settings,
        store: AsyncStore,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        scans = 0

        async def flaky(fn: Callable[..., T], /, *args: Any) -> T:
            nonlocal scans
            if isinstance(fn, functools.partial):  # the scan of the workflows directory
                scans += 1
                if scans == 2:
                    raise RuntimeError("the thread pool is gone")
            return fn(*args)

        harness = Harness(project, settings, store, clock, blocking=flaky)
        await harness.registry.load_all()

        async with running(harness.registry.run):
            await asyncio.sleep(0.5)
            harness.project.write_workflow("a", HELLO)
            await asyncio.sleep(3)

        assert "reloading the workflows failed; trying again" in caplog.text
        assert [change.workflow_id for change in harness.sink.changes] == ["a"]
