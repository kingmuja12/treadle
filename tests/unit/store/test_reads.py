from __future__ import annotations

from typing import Any

import pytest
from tests.support.builders import FIXED_TIME
from tests.unit.store.helpers import MINUTE, T0, Runs

from treadle.clock import from_ms
from treadle.models import (
    ControlRequest,
    LogStream,
    RunDetail,
    RunFilter,
    RunRecord,
    RunStatus,
    TriggerKind,
    Workflow,
)
from treadle.store.repo import NewLogLine, Repository

FAILED_OR_QUEUED = frozenset({RunStatus.FAILED, RunStatus.QUEUED})


@pytest.fixture
def logged_run(repo: Repository, runs: Runs) -> RunRecord:
    """A running run with five log lines across two steps and two attempts."""
    run = runs.running(steps=["build", "test"])
    repo.append_logs(
        run.id,
        [
            NewLogLine(1, T0, None, None, LogStream.SYSTEM, "starting"),
            NewLogLine(2, T0 + 1, "build", 1, LogStream.STDOUT, "compiling"),
            NewLogLine(3, T0 + 2, "build", 1, LogStream.STDERR, "warning: unused import"),
            NewLogLine(4, T0 + 3, "build", 2, LogStream.STDOUT, "compiling again"),
            NewLogLine(5, T0 + 4, "test", 1, LogStream.STDOUT, "ok"),
        ],
    )
    return run


@pytest.mark.parametrize(
    ("query", "seqs"),
    [
        ({}, [1, 2, 3, 4, 5]),
        ({"after_seq": 2}, [3, 4, 5]),
        ({"limit": 2}, [1, 2]),
        ({"after_seq": 2, "limit": 2}, [3, 4]),
        ({"after_seq": 5}, []),
        ({"limit": 0}, []),
        ({"step_id": "build"}, [2, 3, 4]),
        ({"step_id": "build", "attempt": 1}, [2, 3]),
        ({"attempt": 1}, [2, 3, 5]),
        ({"tail": True, "limit": 2}, [4, 5]),
        ({"tail": True, "limit": 2, "step_id": "build"}, [3, 4]),
        ({"tail": True, "after_seq": 3}, [4, 5]),
        ({"tail": True, "limit": 50}, [1, 2, 3, 4, 5]),
    ],
)
def test_logs_filter_and_page(
    repo: Repository, logged_run: RunRecord, query: dict[str, Any], seqs: list[int]
) -> None:
    assert [line.seq for line in repo.logs(logged_run.id, **query)] == seqs


def test_logs_of_a_run_without_lines_are_empty(repo: Repository, runs: Runs) -> None:
    assert repo.logs(runs.running().id) == []
    assert repo.logs(999) == []


@pytest.fixture
def history(repo: Repository, runs: Runs, other_workflow: Workflow) -> dict[str, RunRecord]:
    """Four runs, IDs 1 to 4, across two workflows, statuses, and trigger kinds."""
    stored = {
        "ok": runs.finished(RunStatus.SUCCEEDED),
        "broken": runs.finished(RunStatus.FAILED, error="step 'archive' failed: Disk FULL"),
        "nightly": runs.queued(
            workflow=other_workflow,
            trigger_kind=TriggerKind.SCHEDULE,
            trigger_key="other:schedule:1",
            trigger={"kind": "schedule"},
        ),
        "hooked": runs.running(
            workflow=other_workflow,
            steps=["upload"],
            trigger_kind=TriggerKind.WEBHOOK,
            trigger={"kind": "webhook"},
        ),
    }
    repo.append_logs(
        stored["hooked"].id,
        [NewLogLine(1, T0, "upload", 1, LogStream.STDOUT, "Uploading report.pdf to S3")],
    )
    return stored


@pytest.mark.parametrize(
    ("flt", "names"),
    [
        (RunFilter(), ["hooked", "nightly", "broken", "ok"]),
        (RunFilter(workflow_id="wf"), ["broken", "ok"]),
        (RunFilter(workflow_id="missing"), []),
        (RunFilter(statuses=FAILED_OR_QUEUED), ["nightly", "broken"]),
        (RunFilter(trigger_kind=TriggerKind.SCHEDULE), ["nightly"]),
        (RunFilter(trigger_kind=TriggerKind.MANUAL), ["broken", "ok"]),
        (RunFilter(text="disk full"), ["broken"]),
        (RunFilter(text="UPLOADING report"), ["hooked"]),
        (RunFilter(text=""), ["hooked", "nightly", "broken", "ok"]),
        (RunFilter(text="no such text"), []),
        (RunFilter(before_id=3), ["broken", "ok"]),
        (RunFilter(workflow_id="other", statuses=frozenset({RunStatus.RUNNING})), ["hooked"]),
    ],
)
def test_list_runs_applies_every_filter_field(
    repo: Repository, history: dict[str, RunRecord], flt: RunFilter, names: list[str]
) -> None:
    page = repo.list_runs(flt)

    assert list(page.runs) == [history[name] for name in names]
    assert page.next_before_id is None


def test_list_runs_pages_newest_first(repo: Repository, runs: Runs) -> None:
    ids = [runs.queued().id for _ in range(5)]

    first = repo.list_runs(RunFilter(limit=2))
    second = repo.list_runs(RunFilter(limit=2, before_id=first.next_before_id))
    last = repo.list_runs(RunFilter(limit=2, before_id=second.next_before_id))

    assert ([r.id for r in first.runs], first.next_before_id) == ([ids[4], ids[3]], ids[3])
    assert ([r.id for r in second.runs], second.next_before_id) == ([ids[2], ids[1]], ids[1])
    assert ([r.id for r in last.runs], last.next_before_id) == ([ids[0]], None)


def test_list_runs_clamps_the_limit_to_1_through_200(repo: Repository, runs: Runs) -> None:
    with repo.transaction():
        for _ in range(201):
            runs.queued()

    smallest = repo.list_runs(RunFilter(limit=0))
    largest = repo.list_runs(RunFilter(limit=1000))

    assert (len(smallest.runs), smallest.next_before_id) == (1, 201)
    assert (len(largest.runs), largest.next_before_id) == (200, 2)


def test_last_runs_and_latest_run_id(
    repo: Repository, runs: Runs, other_workflow: Workflow
) -> None:
    assert repo.last_runs() == {}
    assert repo.latest_run_id() is None

    runs.queued()
    other = runs.queued(workflow=other_workflow)
    newest = runs.queued()

    assert repo.last_runs() == {"wf": newest, "other": other}
    assert repo.latest_run_id() == newest.id
    assert repo.latest_run_id("wf") == newest.id
    assert repo.latest_run_id("other") == other.id
    assert repo.latest_run_id("missing") is None


def test_queued_runs_are_listed_in_id_order(repo: Repository, runs: Runs) -> None:
    first = runs.queued()
    runs.running()
    third = runs.queued()

    assert repo.queued_runs() == [first, third]


def test_run_detail_lists_steps_by_position(repo: Repository, runs: Runs) -> None:
    run = runs.running(steps=["zeta", "alpha"])

    detail = repo.get_run_detail(run.id)

    assert detail is not None
    assert detail.run == run
    assert [(step.step_id, step.position) for step in detail.steps] == [("zeta", 0), ("alpha", 1)]


def test_missing_records_read_as_none(repo: Repository) -> None:
    assert repo.get_run(1) is None
    assert repo.get_run_detail(1) is None
    assert repo.workflow_version("0" * 64) is None
    assert repo.control_result(1) is None
    assert repo.schedule_slot("wf:schedule:x") is None


def test_run_detail_reads_a_run_with_no_steps(repo: Repository, runs: Runs) -> None:
    run = runs.queued()

    assert repo.get_run_detail(run.id) == RunDetail(run, ())


def test_init_schedule_keeps_an_existing_slot(repo: Repository) -> None:
    assert repo.init_schedule("wf:schedule:a", "wf", T0, at_ms=T0) == T0
    assert repo.init_schedule("wf:schedule:a", "wf", T0 + MINUTE, at_ms=T0 + MINUTE) == T0
    assert repo.schedule_slot("wf:schedule:a") == T0


def test_advance_schedule_is_a_compare_and_set(repo: Repository) -> None:
    repo.init_schedule("wf:schedule:a", "wf", T0, at_ms=T0)

    assert repo.advance_schedule("wf:schedule:a", T0, T0 + MINUTE, at_ms=T0 + MINUTE)
    assert not repo.advance_schedule("wf:schedule:a", T0, T0 + 2 * MINUTE, at_ms=T0)
    assert not repo.advance_schedule("wf:schedule:missing", T0, T0 + MINUTE, at_ms=T0)
    assert repo.schedule_slot("wf:schedule:a") == T0 + MINUTE


def test_prune_schedules_drops_removed_workflows_and_stale_keys(repo: Repository) -> None:
    for key, workflow_id in [
        ("gone:schedule:a", "gone"),
        ("wf:schedule:old", "wf"),
        ("wf:schedule:new", "wf"),
        ("broken:schedule:a", "broken"),
    ]:
        repo.init_schedule(key, workflow_id, T0, at_ms=T0)

    deleted = repo.prune_schedules(
        keep={"wf:schedule:new"}, valid_workflows={"wf"}, known_workflows={"wf", "broken"}
    )

    assert deleted == 2
    assert repo.schedule_slot("gone:schedule:a") is None
    assert repo.schedule_slot("wf:schedule:old") is None
    assert repo.schedule_slot("wf:schedule:new") == T0
    assert repo.schedule_slot("broken:schedule:a") == T0  # an invalid file keeps its state


def test_control_requests_are_claimed_in_order_and_completed(repo: Repository) -> None:
    run_payload: dict[str, Any] = {"workflow": "hello", "inputs": {"name": "Ada"}, "origin": "cli"}
    first = repo.enqueue_control("run", run_payload, at_ms=T0)
    second = repo.enqueue_control("cancel", {"run_id": 42, "force": False}, at_ms=T0 + 1)

    assert repo.claim_controls(at_ms=T0 + 5, limit=1) == [
        ControlRequest(first, "run", run_payload, FIXED_TIME)
    ]
    assert repo.claim_controls(at_ms=T0 + 6) == [
        ControlRequest(second, "cancel", {"run_id": 42, "force": False}, from_ms(T0 + 1))
    ]
    assert repo.claim_controls(at_ms=T0 + 7) == []
    assert repo.control_result(first) is None

    repo.complete_control(first, {"run_id": 43, "outcome": "queued"}, at_ms=T0 + 8)

    assert repo.control_result(first) == {"run_id": 43, "outcome": "queued"}


def test_completing_an_unclaimed_request_consumes_it(repo: Repository) -> None:
    request_id = repo.enqueue_control("cancel", {"run_id": 1, "force": True}, at_ms=T0)

    repo.complete_control(request_id, {"error": {"code": "not_found"}}, at_ms=T0 + 1)

    assert repo.claim_controls(at_ms=T0 + 2) == []
    assert repo.control_result(request_id) == {"error": {"code": "not_found"}}
