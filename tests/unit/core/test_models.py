from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.support.builders import (
    FIXED_TIME,
    make_attempt,
    make_entry,
    make_input,
    make_run,
    make_step,
    make_step_record,
    make_watch,
    make_webhook,
    make_workflow,
)

from treadle.models import (
    CatchUp,
    CronSpec,
    Defaults,
    RetryPolicy,
    RunStatus,
    ScheduleTrigger,
    StepStatus,
    TriggerKind,
    trigger_kind,
)
from treadle.types import Issue, Severity

LATER = FIXED_TIME + timedelta(seconds=72, milliseconds=541)


def cron(hours: frozenset[int]) -> CronSpec:
    return CronSpec(
        "test", frozenset({0}), hours, frozenset(), frozenset(), frozenset(), False, False
    )


@pytest.mark.parametrize("status", list(RunStatus))
def test_run_status_terminal(status: RunStatus) -> None:
    assert status.terminal is (status not in {RunStatus.QUEUED, RunStatus.RUNNING})


@pytest.mark.parametrize("status", list(StepStatus))
def test_step_status_terminal(status: StepStatus) -> None:
    active = {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.RETRYING}
    assert status.terminal is (status not in active)


@pytest.mark.parametrize(
    ("hours", "interval_like"),
    [
        (frozenset(range(24)), True),
        (frozenset(range(23)), False),
        (frozenset({2}), False),
    ],
)
def test_cron_spec_interval_like(hours: frozenset[int], interval_like: bool) -> None:
    assert cron(hours).interval_like is interval_like


def test_trigger_kind() -> None:
    schedule = ScheduleTrigger("wf:schedule:x", cron(frozenset({2})), "UTC", CatchUp.LATEST)

    assert trigger_kind(schedule) is TriggerKind.SCHEDULE
    assert trigger_kind(make_watch(Path("/tmp/in"))) is TriggerKind.WATCH
    assert trigger_kind(make_webhook("push")) is TriggerKind.WEBHOOK


def test_workflow_lookups() -> None:
    hook = make_webhook("push")
    workflow = make_workflow(
        make_step("build"),
        make_step("test"),
        inputs=[make_input("name")],
        triggers=[make_watch(Path("/tmp/in")), hook],
    )

    assert workflow.step("test").position == 1
    assert workflow.input("name") == make_input("name")
    assert workflow.input("missing") is None
    assert workflow.webhooks() == (hook,)
    assert workflow.short_digest == workflow.digest[:12]
    assert len(workflow.short_digest) == 12
    with pytest.raises(KeyError):
        workflow.step("deploy")


def test_workflow_entry_valid_ignores_warnings() -> None:
    warning = Issue(Severity.WARNING, "W201", "secret isn't defined")
    error = Issue(Severity.ERROR, "E201", "bad expression")

    assert make_entry(make_workflow()).valid
    assert make_entry(make_workflow(), issues=[warning]).valid
    assert not make_entry(workflow_id="broken", issues=[warning, error]).valid


def test_durations_need_a_start_and_a_finish() -> None:
    assert make_run(started_at=FIXED_TIME, finished_at=LATER).duration_ms == 72541
    assert make_run(started_at=FIXED_TIME).duration_ms is None
    assert make_run(finished_at=LATER).duration_ms is None
    assert make_step_record(started_at=FIXED_TIME, finished_at=LATER).duration_ms == 72541
    assert make_step_record(started_at=FIXED_TIME).duration_ms is None
    assert make_attempt(finished_at=LATER).duration_ms == 72541
    assert make_attempt(finished_at=None).duration_ms is None


def test_defaults_match_the_documented_defaults() -> None:
    assert RetryPolicy() == RetryPolicy(attempts=1, delay=1.0, backoff=2.0, max_delay=300.0)
    assert RetryPolicy().jitter == 0.0
    defaults = Defaults()
    assert (defaults.timeout, defaults.retry, defaults.shell, defaults.cwd) == (
        1800.0,
        RetryPolicy(),
        "bash",
        ".",
    )
