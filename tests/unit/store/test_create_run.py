from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from tests.support.builders import FIXED_TIME
from tests.unit.store.helpers import DAY, HOUR, MINUTE, SECOND, T0, Runs, new_run

from treadle.clock import from_ms
from treadle.config import RetentionSettings
from treadle.models import (
    Delivery,
    RunReason,
    RunRecord,
    RunStatus,
    ScheduleSlot,
    TriggerKind,
    Workflow,
)
from treadle.store.repo import CreateResult, NewRun, Repository, WorkflowVersion

KEY = "wf:schedule:3f9a1c0be2d4"
SLOT = T0 + MINUTE
HOOK = Delivery("blog-push", "9b74c9897bac770ffc029102a200c5de", window=HOUR / SECOND)


def created(result: CreateResult) -> RunRecord:
    assert result.outcome == "created"
    assert result.run is not None
    return result.run


def run_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


def deliveries(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = conn.execute("SELECT hook, digest, received_at, run_id FROM webhook_deliveries")
    return [tuple(row) for row in rows]


def scheduled(workflow: Workflow, slot: ScheduleSlot, **overrides: object) -> NewRun:
    return new_run(
        workflow,
        trigger_kind=TriggerKind.SCHEDULE,
        trigger_key=slot.key,
        trigger={"kind": "schedule", "cron": "* * * * *"},
        slot=slot,
        **overrides,
    )


def test_a_queued_run_is_stored_with_its_workflow_version(
    repo: Repository, workflow: Workflow
) -> None:
    result = repo.create_run(
        new_run(
            workflow,
            trigger={"kind": "manual", "origin": "api"},
            inputs={"name": "Ada", "count": 3, "tags": ["x"], "dry_run": False, "note": None},
            rerun_of=None,
        )
    )

    assert result == CreateResult(
        "created",
        RunRecord(
            id=1,
            workflow_id="wf",
            workflow_name="wf",
            digest=workflow.digest,
            status=RunStatus.QUEUED,
            reason=None,
            error=None,
            trigger_kind=TriggerKind.MANUAL,
            trigger_key=None,
            trigger={"kind": "manual", "origin": "api"},
            inputs={"name": "Ada", "count": 3, "tags": ["x"], "dry_run": False, "note": None},
            scheduled_for=None,
            attempt=1,
            rerun_of=None,
            replaced_by=None,
            created_at=FIXED_TIME,
            started_at=None,
            finished_at=None,
            cancel_requested_at=None,
        ),
    )
    assert repo.workflow_version(workflow.digest) == WorkflowVersion(
        workflow.digest, "wf", "workflows/wf.yml", workflow.source, FIXED_TIME
    )


def test_a_skipped_run_finishes_when_it_is_created(repo: Repository, workflow: Workflow) -> None:
    run = created(
        repo.create_run(new_run(workflow, status=RunStatus.SKIPPED, reason=RunReason.QUEUE_FULL))
    )

    assert (run.status, run.reason) == (RunStatus.SKIPPED, RunReason.QUEUE_FULL)
    assert run.finished_at == run.created_at == FIXED_TIME
    assert run.started_at is None


def test_a_rerun_records_its_lineage(repo: Repository, runs: Runs) -> None:
    original = runs.finished(RunStatus.FAILED)

    rerun = runs.queued(rerun_of=original.id, attempt=2)

    assert (rerun.rerun_of, rerun.attempt) == (original.id, 2)


def test_a_version_is_stored_once(repo: Repository, runs: Runs, workflow: Workflow) -> None:
    runs.queued()
    runs.queued(created_at_ms=T0 + DAY)

    version = repo.workflow_version(workflow.digest)
    assert version is not None
    assert version.created_at == FIXED_TIME


def test_a_workflow_outside_the_project_keeps_its_absolute_path(
    repo: Repository, workflow: Workflow
) -> None:
    elsewhere = dataclasses.replace(workflow, path=Path("/srv/flows/wf.yml"), digest="e" * 64)

    created(repo.create_run(new_run(elsewhere)))

    version = repo.workflow_version("e" * 64)
    assert version is not None
    assert version.path == "/srv/flows/wf.yml"


def test_a_slot_run_advances_the_schedule_in_the_same_transaction(
    repo: Repository, workflow: Workflow
) -> None:
    repo.init_schedule(KEY, "wf", T0, at_ms=T0)

    run = created(repo.create_run(scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0))))

    assert (run.trigger_key, run.scheduled_for) == (KEY, from_ms(SLOT))
    assert repo.schedule_slot(KEY) == SLOT


def test_a_stale_slot_is_taken_and_rolls_everything_back(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    repo.init_schedule(KEY, "wf", T0, at_ms=T0)
    created(repo.create_run(scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0))))

    # A second process still believes last_slot is T0.
    result = repo.create_run(
        scheduled(workflow, ScheduleSlot(KEY, SLOT + MINUTE, previous_ms=T0), delivery=HOOK)
    )

    assert result == CreateResult("slot_taken", None)
    assert run_count(conn) == 1
    assert repo.schedule_slot(KEY) == SLOT
    assert deliveries(conn) == []


def test_a_slot_without_schedule_state_is_taken(repo: Repository, workflow: Workflow) -> None:
    result = repo.create_run(scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0)))

    assert result == CreateResult("slot_taken", None)
    assert repo.workflow_version(workflow.digest) is None


def test_the_unique_slot_index_refuses_a_second_run_for_one_slot(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    repo.init_schedule(KEY, "wf", T0, at_ms=T0)
    created(repo.create_run(scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0))))
    # Even if last_slot moved back, so that the compare-and-set passes, the index holds.
    assert repo.advance_schedule(KEY, SLOT, T0, at_ms=T0)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        repo.create_run(scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0)))

    assert run_count(conn) == 1
    assert repo.schedule_slot(KEY) == T0
    assert not conn.in_transaction


def test_a_delivery_inside_its_window_is_a_duplicate_of_the_first_run(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    first = created(repo.create_run(new_run(workflow, delivery=HOOK)))

    again = repo.create_run(new_run(workflow, delivery=HOOK, created_at_ms=T0 + HOUR - 1))

    assert again == CreateResult("duplicate", first)
    assert run_count(conn) == 1
    assert deliveries(conn) == [(HOOK.hook, HOOK.digest, T0, first.id)]


def test_a_delivery_after_its_window_creates_a_new_run(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    first = created(repo.create_run(new_run(workflow, delivery=HOOK)))

    second = created(repo.create_run(new_run(workflow, delivery=HOOK, created_at_ms=T0 + HOUR)))

    assert second.id != first.id
    assert deliveries(conn) == [(HOOK.hook, HOOK.digest, T0 + HOUR, second.id)]


def test_the_same_digest_on_another_hook_is_a_new_delivery(
    repo: Repository, workflow: Workflow
) -> None:
    created(repo.create_run(new_run(workflow, delivery=HOOK)))

    other_hook = dataclasses.replace(HOOK, hook="other-hook")
    assert repo.create_run(new_run(workflow, delivery=other_hook)).outcome == "created"


def test_a_duplicate_of_a_pruned_run_has_no_run(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    run = created(repo.create_run(new_run(workflow, delivery=HOOK)))
    assert repo.start_run(run.id, [], at_ms=T0, pid=1)
    assert repo.finish_run(run.id, RunStatus.SUCCEEDED, at_ms=T0, reason=None, error=None)
    assert repo.prune_batch(RetentionSettings(max_age=1.0), now_ms=T0 + MINUTE) == 1

    again = repo.create_run(new_run(workflow, delivery=HOOK, created_at_ms=T0 + 2 * MINUTE))

    assert again == CreateResult("duplicate", None)
    assert deliveries(conn) == [(HOOK.hook, HOOK.digest, T0, None)]


def test_a_zero_window_never_deduplicates(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    no_window = dataclasses.replace(HOOK, window=0.0)

    first = created(repo.create_run(new_run(workflow, delivery=no_window)))
    second = created(repo.create_run(new_run(workflow, delivery=no_window)))

    assert first.id != second.id
    assert deliveries(conn) == []


def test_replace_cancels_queued_runs_and_links_the_active_run(
    repo: Repository, runs: Runs, workflow: Workflow
) -> None:
    active = runs.running()
    waiting = [runs.queued(), runs.queued()]
    done = runs.finished()

    replacement = created(
        repo.create_run(
            new_run(
                workflow,
                cancel_queued=(waiting[0].id, waiting[1].id, done.id),
                replaced_active=active.id,
                created_at_ms=T0 + 5 * SECOND,
            )
        )
    )

    for run in waiting:
        cancelled = runs.get(run.id)
        assert (cancelled.status, cancelled.reason) == (RunStatus.CANCELLED, RunReason.REPLACED)
        assert cancelled.replaced_by == replacement.id
        assert cancelled.finished_at == from_ms(T0 + 5 * SECOND)
    assert runs.get(done.id) == done
    still_running = runs.get(active.id)
    assert (still_running.status, still_running.replaced_by) == (RunStatus.RUNNING, replacement.id)


def test_slot_taken_inside_an_enclosing_transaction_undoes_only_create_run(
    repo: Repository, conn: sqlite3.Connection, workflow: Workflow
) -> None:
    with repo.transaction():
        request_id = repo.enqueue_control("run", {"workflow": "wf"}, at_ms=T0)
        result = repo.create_run(
            scheduled(workflow, ScheduleSlot(KEY, SLOT, previous_ms=T0), delivery=HOOK)
        )

    assert result.outcome == "slot_taken"
    assert deliveries(conn) == []
    assert [request.id for request in repo.claim_controls(at_ms=T0)] == [request_id]
