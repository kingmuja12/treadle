from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from tests.support.builders import (
    FIXED_TIME,
    PROJECT_DIR,
    make_attempt,
    make_detail,
    make_entry,
    make_input,
    make_run,
    make_step,
    make_step_record,
    make_workflow,
)

from treadle.models import (
    AttemptStatus,
    Concurrency,
    ConcurrencyPolicy,
    InputType,
    LogLine,
    LogStream,
    RunReason,
    RunStatus,
    SkipReason,
    StepStatus,
    TriggerKind,
    TriggerState,
)
from treadle.serialize import (
    error_json,
    log_json,
    run_detail_json,
    run_json,
    step_json,
    trigger_json,
    workflow_detail_json,
    workflow_json,
)
from treadle.types import Issue, Position, Severity

LATER = FIXED_TIME + timedelta(seconds=72, milliseconds=541)
WARNING = Issue(Severity.WARNING, "W105", "too long", ("steps", 0), Position(3, 5), "lower it")
NIGHTLY = TriggerState(
    "nightly", "nightly:schedule:abc", TriggerKind.SCHEDULE, "cron 30 2 * * * (UTC)", LATER, None
)


def test_run_json_has_the_section_8_shape_in_order() -> None:
    run = make_run(
        42,
        workflow_id="nightly-backup",
        workflow_name="Nightly backup",
        digest="3f9a1c0be2d4" + "0" * 52,
        status=RunStatus.FAILED,
        error="step 'archive' failed after 3 attempts: exit code 23",
        trigger_kind=TriggerKind.SCHEDULE,
        trigger={"kind": "schedule", "cron": "30 2 * * *"},
        inputs={"dry_run": False},
        scheduled_for=FIXED_TIME,
        created_at=FIXED_TIME,
        started_at=FIXED_TIME + timedelta(milliseconds=10),
        finished_at=LATER,
        rerun_of=40,
        replaced_by=43,
        attempt=2,
    )

    assert run_json(run) == {
        "id": 42,
        "workflow": "nightly-backup",
        "workflow_name": "Nightly backup",
        "digest": "3f9a1c0be2d4" + "0" * 52,
        "status": "failed",
        "reason": None,
        "error": "step 'archive' failed after 3 attempts: exit code 23",
        "trigger_kind": "schedule",
        "trigger": {"kind": "schedule", "cron": "30 2 * * *"},
        "inputs": {"dry_run": False},
        "attempt": 2,
        "rerun_of": 40,
        "replaced_by": 43,
        "scheduled_for": "2026-11-09T12:00:00.000Z",
        "created_at": "2026-11-09T12:00:00.000Z",
        "started_at": "2026-11-09T12:00:00.010Z",
        "finished_at": "2026-11-09T12:01:12.541Z",
        "duration_ms": 72531,
    }
    assert list(run_json(run)) == [
        "id",
        "workflow",
        "workflow_name",
        "digest",
        "status",
        "reason",
        "error",
        "trigger_kind",
        "trigger",
        "inputs",
        "attempt",
        "rerun_of",
        "replaced_by",
        "scheduled_for",
        "created_at",
        "started_at",
        "finished_at",
        "duration_ms",
    ]


def test_run_json_uses_null_for_missing_values_and_plain_strings_for_enums() -> None:
    body = run_json(make_run(7, status=RunStatus.CANCELLED, reason=RunReason.USER))

    assert body["reason"] == "user"
    assert type(body["status"]) is str
    assert type(body["reason"]) is str
    for key in ("scheduled_for", "started_at", "finished_at", "duration_ms", "error"):
        assert body[key] is None
    assert json.loads(json.dumps(body)) == body


def test_step_json_includes_every_attempt() -> None:
    step = make_step_record(
        "archive",
        name="Archive",
        position=1,
        uses="shell",
        status=StepStatus.FAILED,
        tolerated=True,
        started_at=FIXED_TIME,
        finished_at=LATER,
        outputs={"exit_code": "23"},
        error="exit code 23",
        attempts=[
            make_attempt(1, status=AttemptStatus.FAILED, exit_code=23, error="exit code 23"),
            make_attempt(
                2,
                status=AttemptStatus.RUNNING,
                finished_at=None,
                delay_before_ms=30_000,
                log_bytes=120,
                log_truncated=True,
            ),
        ],
    )

    assert step_json(step) == {
        "id": "archive",
        "name": "Archive",
        "position": 1,
        "uses": "shell",
        "status": "failed",
        "tolerated": True,
        "skip_reason": None,
        "started_at": "2026-11-09T12:00:00.000Z",
        "finished_at": "2026-11-09T12:01:12.541Z",
        "duration_ms": 72541,
        "outputs": {"exit_code": "23"},
        "error": "exit code 23",
        "attempts": [
            {
                "attempt": 1,
                "status": "failed",
                "exit_code": 23,
                "error": "exit code 23",
                "delay_before_ms": 0,
                "started_at": "2026-11-09T12:00:00.000Z",
                "finished_at": "2026-11-09T12:00:00.000Z",
                "duration_ms": 0,
                "log_bytes": 0,
                "log_truncated": False,
            },
            {
                "attempt": 2,
                "status": "running",
                "exit_code": None,
                "error": None,
                "delay_before_ms": 30_000,
                "started_at": "2026-11-09T12:00:00.000Z",
                "finished_at": None,
                "duration_ms": None,
                "log_bytes": 120,
                "log_truncated": True,
            },
        ],
    }


def test_step_json_names_the_skip_reason() -> None:
    step = make_step_record("record", status=StepStatus.SKIPPED, skip_reason=SkipReason.CONDITION)

    assert step_json(step)["skip_reason"] == "condition"


def test_run_detail_json_is_a_run_plus_its_steps() -> None:
    run = make_run(3, status=RunStatus.SUCCEEDED)
    steps = [make_step_record("a"), make_step_record("b", position=1)]

    body = run_detail_json(make_detail(run, steps))

    assert body == {**run_json(run), "steps": [step_json(step) for step in steps]}


def test_log_json_has_the_log_shape() -> None:
    line = LogLine(17, FIXED_TIME, "archive", 1, LogStream.STDOUT, "sending incremental file list")
    system = LogLine(18, FIXED_TIME, None, None, LogStream.SYSTEM, "run started")

    assert log_json(line) == {
        "seq": 17,
        "ts": "2026-11-09T12:00:00.000Z",
        "step": "archive",
        "attempt": 1,
        "stream": "stdout",
        "text": "sending incremental file list",
    }
    assert log_json(system)["step"] is None
    assert log_json(system)["stream"] == "system"


def test_trigger_json_has_the_trigger_shape() -> None:
    degraded = TriggerState(
        "sort", "sort:watch:1", TriggerKind.WATCH, "watch ~/Downloads", None, "degraded: gone"
    )

    assert trigger_json(NIGHTLY) == {
        "kind": "schedule",
        "key": "nightly:schedule:abc",
        "summary": "cron 30 2 * * * (UTC)",
        "next_fire": "2026-11-09T12:01:12.541Z",
        "error": None,
    }
    assert trigger_json(degraded)["next_fire"] is None
    assert trigger_json(degraded)["error"] == "degraded: gone"


def test_workflow_json_has_the_workflow_shape() -> None:
    workflow = make_workflow(
        workflow_id="nightly",
        name="Nightly backup",
        description="Back up the drive.",
        concurrency=Concurrency(ConcurrencyPolicy.SKIP, 5),
    )
    entry = make_entry(workflow, issues=[WARNING])
    last = make_run(9, workflow_id="nightly")

    body = workflow_json(entry, project_dir=PROJECT_DIR, triggers=[NIGHTLY], last_run=last)

    assert body == {
        "id": "nightly",
        "name": "Nightly backup",
        "description": "Back up the drive.",
        "path": "workflows/nightly.yml",
        "valid": True,
        "stale": False,
        "digest": workflow.digest,
        "issues": [WARNING.to_json("workflows/nightly.yml")],
        "concurrency": {"policy": "skip", "max_queued": 5},
        "triggers": [trigger_json(NIGHTLY)],
        "last_run": run_json(last),
    }


def test_workflow_json_of_a_stale_entry_describes_the_last_good_version() -> None:
    workflow = make_workflow(workflow_id="wf")
    error = Issue(Severity.ERROR, "E101", "unknown key 'on'", ("on",), Position(1, 1))
    entry = make_entry(workflow, issues=[error], stale=True)

    body = workflow_json(entry, project_dir=PROJECT_DIR, triggers=[], last_run=None)

    assert body["valid"] is False
    assert body["stale"] is True
    assert body["digest"] == workflow.digest
    assert body["last_run"] is None


def test_workflow_json_without_a_current_version_uses_nulls() -> None:
    error = Issue(Severity.ERROR, "E059", "the file must hold a mapping")
    entry = make_entry(workflow_id="broken", issues=[error])

    body = workflow_json(entry, project_dir=PROJECT_DIR, triggers=[], last_run=None)

    assert body["id"] == "broken"
    assert body["valid"] is False
    for key in ("name", "description", "digest", "concurrency"):
        assert body[key] is None
    assert body["issues"] == [error.to_json("workflows/broken.yml")]


def test_workflow_json_keeps_an_absolute_path_outside_the_project() -> None:
    entry = make_entry(make_workflow(workflow_id="wf"))

    body = workflow_json(entry, project_dir=Path("/elsewhere"), triggers=[], last_run=None)

    assert body["path"] == "/project/workflows/wf.yml"


def test_workflow_detail_json_adds_inputs_and_steps() -> None:
    workflow = make_workflow(
        make_step("check"),
        make_step("archive", needs=["check"], condition="always()"),
        make_step("report"),
        inputs=[
            make_input("dry_run", InputType.BOOLEAN, default=False, description="Only print"),
            make_input("mode", InputType.CHOICE, required=True, options=["fast", "full"]),
        ],
    )
    entry = make_entry(workflow)

    body = workflow_detail_json(entry, project_dir=PROJECT_DIR, triggers=[], last_run=None)

    assert body == {
        **workflow_json(entry, project_dir=PROJECT_DIR, triggers=[], last_run=None),
        "inputs": [
            {
                "name": "dry_run",
                "type": "boolean",
                "description": "Only print",
                "required": False,
                "default": False,
                "options": [],
            },
            {
                "name": "mode",
                "type": "choice",
                "description": "",
                "required": True,
                "default": None,
                "options": ["fast", "full"],
            },
        ],
        "steps": [
            {
                "id": "check",
                "name": "check",
                "uses": "fake",
                "needs": [],
                "implicit_needs": True,
                "if": "success()",
            },
            {
                "id": "archive",
                "name": "archive",
                "uses": "fake",
                "needs": ["check"],
                "implicit_needs": False,
                "if": "always()",
            },
            {
                "id": "report",
                "name": "report",
                "uses": "fake",
                "needs": ["archive"],
                "implicit_needs": True,
                "if": "success()",
            },
        ],
    }


def test_workflow_detail_json_without_a_current_version_has_empty_lists() -> None:
    entry = make_entry(workflow_id="broken")

    body = workflow_detail_json(entry, project_dir=PROJECT_DIR, triggers=[], last_run=None)

    assert body["inputs"] == []
    assert body["steps"] == []


def test_error_json_has_the_error_shape() -> None:
    assert error_json("not_found", "run 4 doesn't exist") == {
        "error": {"code": "not_found", "message": "run 4 doesn't exist", "details": None}
    }
    details = {"count": "must be an integer"}
    assert error_json("invalid_inputs", "bad", details)["error"] == {
        "code": "invalid_inputs",
        "message": "bad",
        "details": details,
    }
