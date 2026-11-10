from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from tests.support.builders import DEFAULT_LIMITS, make_run, make_watch, make_workflow
from tests.support.fakes import FakeSubmitter
from tests.unit.triggers.helpers import running, write

from treadle.clock import inline_runner
from treadle.config import Limits
from treadle.errors import WorkflowInvalidError
from treadle.fswatch import Change
from treadle.models import (
    RunRequest,
    Submission,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    WatchEvent,
    WatchTrigger,
)
from treadle.triggers.watch import MAX_PAYLOAD_CHANGES, WatchService, watch_payload

T = TypeVar("T")
CREATED, MODIFIED, DELETED = WatchEvent.CREATED, WatchEvent.MODIFIED, WatchEvent.DELETED
needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads directories regardless of their mode"
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "inbox"
    path.mkdir()
    return path


def watch(*paths: Path, **kwargs: Any) -> WatchTrigger:
    kwargs.setdefault("debounce", 2.0)
    kwargs.setdefault("poll_interval", 1.0)
    return make_watch(*paths, **kwargs)


def service_for(
    *triggers: WatchTrigger,
    submitter: FakeSubmitter | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> tuple[WatchService, FakeSubmitter]:
    chosen = submitter or FakeSubmitter()
    service = WatchService(submitter=chosen, limits=limits, blocking=inline_runner)
    service.update("wf", make_workflow(triggers=triggers))
    return service, chosen


def events(request: RunRequest) -> list[tuple[str, str]]:
    changes: Any = request.trigger["changes"]
    return [(change["event"], change["rel"]) for change in changes]


class TestPayload:
    def test_the_payload_lists_each_change_and_path(self, root: Path) -> None:
        trigger = watch(root)
        changes = [
            Change(CREATED, str(root / "a.pdf"), str(root), "a.pdf"),
            Change(DELETED, str(root / "b/c.pdf"), str(root), "b/c.pdf"),
        ]
        assert watch_payload(trigger, changes) == {
            "kind": "watch",
            "paths": [str(root / "a.pdf"), str(root / "b/c.pdf")],
            "changes": [
                {
                    "event": "created",
                    "path": str(root / "a.pdf"),
                    "root": str(root),
                    "rel": "a.pdf",
                },
                {
                    "event": "deleted",
                    "path": str(root / "b/c.pdf"),
                    "root": str(root),
                    "rel": "b/c.pdf",
                },
            ],
            "total": 2,
            "truncated": False,
        }

    def test_the_payload_keeps_only_the_watched_events(self, root: Path) -> None:
        trigger = watch(root, events=[CREATED])
        changes = [Change(event, f"/r/{event}", "/r", event.value) for event in WatchEvent]
        payload = watch_payload(trigger, changes)
        assert payload["paths"] == ["/r/created"]
        assert payload["total"] == 1

    def test_the_payload_holds_at_most_a_thousand_changes(self) -> None:
        trigger = watch(Path("/r"))
        changes = [Change(CREATED, f"/r/{n:05}", "/r", f"{n:05}") for n in range(1500)]
        payload: Any = watch_payload(trigger, changes)
        assert len(payload["changes"]) == len(payload["paths"]) == MAX_PAYLOAD_CHANGES
        assert payload["paths"][-1] == "/r/00999"
        assert (payload["total"], payload["truncated"]) == (1500, True)


class TestPolling:
    async def test_the_first_scan_is_a_silent_baseline(self, root: Path) -> None:
        write(root / "old.txt")
        service, submitter = service_for(watch(root))

        async with running(service.run):
            await asyncio.sleep(10)
            assert submitter.requests == []
            write(root / "new.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert request.kind is TriggerKind.WATCH
        assert request.workflow_id == "wf"
        assert request.trigger_key == service.states()[0].key
        assert request.trigger["changes"] == [
            {"event": "created", "path": str(root / "new.txt"), "root": str(root), "rel": "new.txt"}
        ]

    async def test_initial_scan_reports_existing_files_as_created(self, root: Path) -> None:
        write(root / "a.txt")
        write(root / "b.txt")
        service, submitter = service_for(watch(root, initial_scan=True))

        async with running(service.run):
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "a.txt"), ("created", "b.txt")]

    async def test_modifications_deletions_and_renames_are_reported(self, root: Path) -> None:
        write(root / "edit.txt")
        write(root / "gone.txt")
        write(root / "old-name.txt")
        service, submitter = service_for(watch(root))

        async with running(service.run):
            await asyncio.sleep(5)
            write(root / "edit.txt", "a longer text")
            (root / "gone.txt").unlink()
            (root / "old-name.txt").rename(root / "new-name.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [
            ("modified", "edit.txt"),
            ("deleted", "gone.txt"),
            ("created", "new-name.txt"),
            ("deleted", "old-name.txt"),
        ]

    async def test_changes_within_the_debounce_coalesce_into_one_run(self, root: Path) -> None:
        service, submitter = service_for(watch(root, debounce=5.0))

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "draft.txt")
            write(root / "brief.txt")
            await asyncio.sleep(2)
            write(root / "draft.txt", "edited")
            (root / "brief.txt").unlink()
            await asyncio.sleep(20)

        [request] = submitter.requests
        assert events(request) == [("created", "draft.txt")]

    async def test_constant_churn_fires_at_the_maximum_wait(self, root: Path) -> None:
        loop = asyncio.get_running_loop()
        fired: list[float] = []

        def record(request: RunRequest) -> Submission:
            fired.append(loop.time())
            return Submission(SubmitOutcome.QUEUED, make_run())

        submitter = FakeSubmitter(handler=record)
        service, _ = service_for(watch(root, debounce=5.0), submitter=submitter)
        target = write(root / "log.txt")

        async with running(service.run):
            await asyncio.sleep(0.5)
            start = loop.time()
            for second in range(90):
                target.write_text("x" * (second + 2))
                await asyncio.sleep(1)

        assert 60 <= fired[0] - start <= 62

    async def test_a_batch_fires_at_its_deadline_between_polls(self, root: Path) -> None:
        loop = asyncio.get_running_loop()
        fired: list[float] = []

        def record(request: RunRequest) -> Submission:
            fired.append(loop.time())
            return Submission(SubmitOutcome.QUEUED, make_run())

        trigger = watch(root, debounce=1.5, poll_interval=10.0)
        service, _ = service_for(trigger, submitter=FakeSubmitter(handler=record))
        start = loop.time()

        async with running(service.run):
            await asyncio.sleep(5)
            write(root / "a.txt")
            await asyncio.sleep(20)

        assert fired == [start + 11.5]

    async def test_unwatched_events_never_start_a_run(self, root: Path) -> None:
        write(root / "keep.txt")
        write(root / "drop.txt")
        service, submitter = service_for(watch(root, events=[CREATED]))

        async with running(service.run):
            await asyncio.sleep(5)
            write(root / "keep.txt", "changed")
            (root / "drop.txt").unlink()
            await asyncio.sleep(10)
            assert submitter.requests == []
            write(root / "fresh.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "fresh.txt")]

    async def test_a_big_batch_is_capped_in_the_payload(self, root: Path) -> None:
        service, submitter = service_for(watch(root))

        async with running(service.run):
            await asyncio.sleep(3)
            for number in range(1200):
                write(root / f"{number:04}.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert request.trigger["total"] == 1200
        assert request.trigger["truncated"] is True
        assert len(events(request)) == 1000

    async def test_several_roots_report_together(self, root: Path, tmp_path: Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        service, submitter = service_for(watch(root, other))

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            write(other / "b.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        roots: Any = request.trigger["changes"]
        assert [(change["root"], change["rel"]) for change in roots] == [
            (str(root), "a.txt"),
            (str(other), "b.txt"),
        ]


class TestDegradedRoots:
    async def test_a_vanished_root_degrades_without_deletes_and_rebaselines_silently(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="treadle.triggers.watch")
        write(root / "a.txt")
        service, submitter = service_for(watch(root, initial_scan=True))

        async with running(service.run):
            await asyncio.sleep(5)
            assert len(submitter.requests) == 1
            shutil.rmtree(root)
            await asyncio.sleep(10)
            [state] = service.states()
            assert state.error == f"degraded: path missing: {root}"
            write(root / "a.txt")
            write(root / "b.txt")
            await asyncio.sleep(10)
            assert service.states()[0].error is None
            assert len(submitter.requests) == 1
            write(root / "c.txt")
            await asyncio.sleep(10)

        assert [events(request) for request in submitter.requests[1:]] == [[("created", "c.txt")]]
        assert caplog.text.count("degraded: path missing") == 1
        assert f"{root} is available again" in caplog.text

    async def test_a_root_that_appears_later_gets_its_initial_scan(self, tmp_path: Path) -> None:
        later = tmp_path / "later"
        service, submitter = service_for(watch(later, initial_scan=True))

        async with running(service.run):
            await asyncio.sleep(5)
            assert service.states()[0].error == f"degraded: path missing: {later}"
            write(later / "a.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "a.txt")]

    async def test_too_many_entries_degrades_and_rebaselines_silently(self, root: Path) -> None:
        write(root / "a.txt")
        limits = Limits(watch_max_entries=3)
        service, submitter = service_for(watch(root), limits=limits)

        async with running(service.run):
            await asyncio.sleep(3)
            for name in ("b", "c", "d"):
                write(root / f"{name}.txt")
            await asyncio.sleep(5)
            assert service.states()[0].error == f"degraded: more than 3 entries in {root}"
            (root / "c.txt").unlink()
            (root / "d.txt").unlink()
            await asyncio.sleep(5)
            assert service.states()[0].error is None
            write(root / "e.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "e.txt")]

    async def test_one_degraded_root_doesnt_silence_the_others(
        self, root: Path, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing"
        service, submitter = service_for(watch(missing, root))

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "a.txt")]
        assert service.states()[0].error == f"degraded: path missing: {missing}"

    @needs_permissions
    async def test_unreadable_subdirectories_are_logged_once_per_trigger(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        locked = root / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            service, _ = service_for(watch(root, recursive=True))
            async with running(service.run):
                await asyncio.sleep(10)
        finally:
            locked.chmod(0o755)

        assert caplog.text.count("skipping unreadable directories, such as") == 1
        assert str(locked) in caplog.text


class TestResilience:
    async def test_a_failed_submit_is_logged_and_watching_goes_on(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        submitter = FakeSubmitter(error=WorkflowInvalidError("wf", []))
        service, _ = service_for(watch(root), submitter=submitter)

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            await asyncio.sleep(10)
            submitter.error = None
            write(root / "b.txt")
            await asyncio.sleep(10)

        assert "the run wasn't created: workflow 'wf' is invalid" in caplog.text
        assert [events(request) for request in submitter.requests] == [
            [("created", "a.txt")],
            [("created", "b.txt")],
        ]

    async def test_a_failed_scan_is_logged_and_retried(
        self, root: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls = 0

        async def flaky(fn: Callable[..., T], /, *args: Any) -> T:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("the thread pool is gone")
            return fn(*args)

        submitter = FakeSubmitter()
        service = WatchService(submitter=submitter, limits=DEFAULT_LIMITS, blocking=flaky)
        service.update("wf", make_workflow(triggers=[watch(root)]))

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            await asyncio.sleep(10)

        assert "the scan failed" in caplog.text
        [request] = submitter.requests
        assert events(request) == [("created", "a.txt")]


class TestReload:
    async def test_an_equal_key_keeps_its_watcher_and_baseline(self, root: Path) -> None:
        trigger = watch(root)
        service, submitter = service_for(trigger)

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            service.update("wf", make_workflow(triggers=[trigger], source="# edited\n"))
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "a.txt")]

    async def test_a_changed_trigger_gets_a_new_watcher_and_a_silent_baseline(
        self, root: Path
    ) -> None:
        service, submitter = service_for(watch(root))

        async with running(service.run):
            await asyncio.sleep(3)
            write(root / "a.txt")
            edited = watch(root, debounce=3.0, key="wf:watch:edited")
            service.update("wf", make_workflow(triggers=[edited]))
            await asyncio.sleep(10)
            assert submitter.requests == []
            write(root / "b.txt")
            await asyncio.sleep(10)

        [request] = submitter.requests
        assert events(request) == [("created", "b.txt")]
        assert request.trigger_key == "wf:watch:edited"

    async def test_removing_the_workflow_stops_its_watches(self, root: Path) -> None:
        service, submitter = service_for(watch(root))

        async with running(service.run):
            await asyncio.sleep(3)
            service.update("wf", None)
            await asyncio.sleep(1)
            write(root / "a.txt")
            await asyncio.sleep(10)

        assert submitter.requests == []
        assert service.states() == []

    async def test_a_watch_registered_while_running_starts_polling(self, root: Path) -> None:
        submitter = FakeSubmitter()
        service = WatchService(submitter=submitter, limits=DEFAULT_LIMITS, blocking=inline_runner)

        async with running(service.run):
            await asyncio.sleep(3)
            service.update("wf", make_workflow(triggers=[watch(root)]))
            await asyncio.sleep(3)
            write(root / "a.txt")
            await asyncio.sleep(10)

        assert len(submitter.requests) == 1


class TestStates:
    def test_states_summarize_paths_with_a_home_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        trigger = watch(tmp_path / "Downloads", tmp_path, Path("/srv/drop"), key="wf:watch:k")
        service, _ = service_for(trigger)
        assert service.states() == [
            TriggerState(
                "wf", "wf:watch:k", TriggerKind.WATCH, "watch ~/Downloads, ~, /srv/drop", None, None
            )
        ]

    def test_states_are_ordered_by_workflow_and_position(self, root: Path) -> None:
        service = WatchService(submitter=FakeSubmitter(), limits=DEFAULT_LIMITS)
        second, first = watch(root, key="b:watch:2"), watch(root, key="b:watch:1")
        service.update("b", make_workflow(workflow_id="b", triggers=[second, first]))
        service.update("a", make_workflow(workflow_id="a", triggers=[watch(root, key="a:watch:1")]))
        assert [state.key for state in service.states()] == ["a:watch:1", "b:watch:2", "b:watch:1"]
        assert [state.key for state in service.states("b")] == ["b:watch:2", "b:watch:1"]
