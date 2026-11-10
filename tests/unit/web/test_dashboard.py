from __future__ import annotations

import dataclasses
import re
from datetime import timedelta
from typing import Any

import pytest
from tests.support.builders import (
    FIXED_TIME,
    make_attempt,
    make_detail,
    make_entry,
    make_input,
    make_run,
    make_step,
    make_step_record,
    make_workflow,
)
from tests.support.fakes import FakeRunControl, MemoryRunReader
from tests.unit.store.helpers import T0, Runs
from tests.unit.store.helpers import project_workflow as stored_workflow
from tests.unit.web.helpers import (
    FORM,
    TOKEN,
    MakeWeb,
    Web,
    csrf_field,
    elements,
    form_body,
    project_workflow,
    text,
    unescape,
)

from treadle.config import Settings
from treadle.errors import (
    ConflictError,
    InputError,
    NotFoundError,
    ShuttingDownError,
    StoreError,
    WorkflowInvalidError,
)
from treadle.models import (
    AttemptStatus,
    CancelOutcome,
    CancelReason,
    InputType,
    LogLine,
    LogStream,
    RunReason,
    RunRecord,
    RunStatus,
    SkipReason,
    StepStatus,
    TriggerKind,
    TriggerState,
    Workflow,
)
from treadle.store.async_store import AsyncStore
from treadle.store.repo import NewLogLine
from treadle.types import Issue, Position, Severity
from treadle.web.security import CSRF_COOKIE, SESSION_COOKIE

ERROR = Issue(Severity.ERROR, "E101", "unknown key 'on'", ("on",), Position(2, 1), "use triggers")
SOURCE = "name: Broken\non: push\n"
NEXT_FIRE = FIXED_TIME + timedelta(minutes=30)
SCHEDULE = TriggerState(
    "deploy", "deploy:schedule:1", TriggerKind.SCHEDULE, "cron 30 * * * * (UTC)", NEXT_FIRE, None
)
DEGRADED = TriggerState(
    "deploy", "deploy:watch:1", TriggerKind.WATCH, "watch ~/inbox", None, "degraded: path missing"
)
INPUTS = [
    make_input("name", description="Who to greet"),
    make_input("ratio", InputType.NUMBER, default=0.5),
    make_input("count", InputType.INTEGER, required=True),
    make_input("dry_run", InputType.BOOLEAN, default=True),
    make_input("mode", InputType.CHOICE, options=["fast", "full"], default="full"),
]


@pytest.fixture
def deploy(settings: Settings) -> Workflow:
    """A workflow with three steps and one input of each type."""
    return project_workflow(
        settings,
        make_step("check", name="Check drive"),
        make_step("archive", needs=["check"], condition="always()"),
        make_step("report"),
        workflow_id="deploy",
        name="Deploy",
        description="Ship the site.",
        inputs=INPUTS,
    )


@pytest.fixture
def site(make_web: MakeWeb, deploy: Workflow) -> Web:
    return make_web(workflows=[deploy])


async def test_workflows_page_lists_each_workflow(make_web: MakeWeb, deploy: Workflow) -> None:
    broken = make_entry(workflow_id="broken", issues=[ERROR], source=SOURCE)
    stale = make_entry(make_workflow(workflow_id="old"), issues=[ERROR], source=SOURCE, stale=True)
    reader = MemoryRunReader(make_run(5, workflow_id="deploy", status=RunStatus.FAILED))
    web = make_web(workflows=[deploy, broken, stale], reader=reader, triggers=[SCHEDULE, DEGRADED])

    response = await web.client.get("/")

    assert response.status_code == 200
    page = text(response.text)
    assert "Deploy deploy" in page
    assert "cron 30 * * * * (UTC) next 2026-11-09 12:30:00 UTC" in page
    assert "watch ~/inbox ⚠ degraded: path missing" in page
    assert "#5 ✗ failed" in page
    assert "never" in page
    assert elements(response.text, "a", href="/workflows/deploy")
    assert elements(response.text, "a", href="/runs/5")
    for state in ("✓ ok", "✗ invalid", "⚠ stale"):
        assert state in page
    assert len(elements(response.text, "pre", class_="issues")) == 2
    source = unescape(response.text)
    assert "/project/workflows/broken.yml:2:1: error[E101]: unknown key 'on'" in source
    assert "/project/workflows/old.yml:2:1: error[E101]" in source
    assert " 2 | on: push\n   | ^\n   = help: use triggers" in source


async def test_workflows_page_without_workflows_says_how_to_add_one(web: Web) -> None:
    response = await web.client.get("/")

    assert "No workflows yet. Add a YAML file to workflows to create one." in text(response.text)


async def test_workflow_page_shows_the_definition_and_recent_runs(
    make_web: MakeWeb, deploy: Workflow
) -> None:
    reader = MemoryRunReader(*(make_run(n, workflow_id="deploy") for n in range(1, 26)))
    web = make_web(workflows=[deploy], reader=reader, triggers=[SCHEDULE])

    response = await web.client.get("/workflows/deploy")

    assert response.status_code == 200
    page = text(response.text)
    assert "Deploy" in page
    assert "Ship the site." in page
    assert "workflows/deploy.yml" in page
    assert "cron 30 * * * * (UTC)" in page
    assert "Check drive check fake none success()" in page
    assert "archive fake check always()" in page
    assert "report fake archive (implicit) success()" in page
    runs = [element.get("href") for element in elements(response.text, "a")]
    assert "/runs/25" in runs
    assert "/runs/6" in runs
    assert "/runs/5" not in runs
    assert "/runs?workflow=deploy" in runs


async def test_workflow_page_of_an_unknown_workflow_is_404(web: Web) -> None:
    response = await web.client.get("/workflows/nope")

    assert response.status_code == 404
    assert "Workflow 'nope' doesn't exist." in text(response.text)


async def test_an_invalid_workflow_shows_its_issues_and_no_run_form(make_web: MakeWeb) -> None:
    web = make_web(workflows=[make_entry(workflow_id="broken", issues=[ERROR], source=SOURCE)])

    response = await web.client.get("/workflows/broken")

    assert response.status_code == 200
    assert "error[E101]: unknown key 'on'" in unescape(response.text)
    assert "Run now" not in response.text
    assert not elements(response.text, "form", class_="run-form")


async def test_a_stale_workflow_explains_which_version_runs(
    make_web: MakeWeb, deploy: Workflow
) -> None:
    web = make_web(workflows=[make_entry(deploy, issues=[ERROR], stale=True)])

    response = await web.client.get("/workflows/deploy")

    assert "runs use the last good version" in text(response.text)
    assert elements(response.text, "form", class_="run-form")


async def test_the_run_form_has_a_control_for_each_input_type(site: Web) -> None:
    html = (await site.client.get("/workflows/deploy")).text

    [name] = elements(html, "input", id="input-name")
    assert (name.get("type"), name.get("value"), name.get("aria-describedby")) == (
        "text",
        "",
        "input-name-hint",
    )
    [ratio] = elements(html, "input", id="input-ratio")
    assert (ratio.get("type"), ratio.get("step"), ratio.get("value")) == ("number", "any", "0.5")
    [count] = elements(html, "input", id="input-count")
    assert (count.get("type"), count.get("step"), count.get("value")) == ("number", "1", "")
    assert "required" in count.attrs
    hidden, checkbox = elements(html, "input", name="input.dry_run")
    assert (hidden.get("type"), hidden.get("value")) == ("hidden", "false")
    assert (checkbox.get("type"), checkbox.get("value")) == ("checkbox", "true")
    assert "checked" in checkbox.attrs
    [mode] = elements(html, "select", id="input-mode")
    assert mode.get("name") == "input.mode"
    options = elements(html, "option")
    assert [option.get("value") for option in options] == ["", "fast", "full"]
    assert "selected" in options[2].attrs
    assert "Who to greet" in text(html)
    for field in ("name", "ratio", "count", "dry_run", "mode"):
        assert elements(html, "label", for_=f"input-{field}")


async def test_run_now_submits_a_dashboard_run_and_shows_it(site: Web) -> None:
    response = await site.post_form(
        "/workflows/deploy/run",
        [
            ("input.name", "Ada"),
            ("input.ratio", "1.5"),
            ("input.count", "3"),
            ("input.dry_run", "false"),
            ("input.dry_run", "true"),
            ("input.mode", "fast"),
        ],
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/runs/1"
    [request] = site.runs.requests
    assert request.workflow_id == "deploy"
    assert request.kind is TriggerKind.MANUAL
    assert request.trigger == {"kind": "manual", "origin": "dashboard"}
    assert request.inputs_are_strings is True
    assert request.inputs == {
        "name": "Ada",
        "ratio": "1.5",
        "count": "3",
        "dry_run": "true",
        "mode": "fast",
    }


async def test_an_unchecked_box_submits_false(site: Web) -> None:
    await site.post_form(
        "/workflows/deploy/run", [("input.dry_run", "false"), ("input.count", "1")]
    )

    assert site.runs.requests[0].inputs == {"dry_run": "false", "count": "1"}


async def test_an_empty_optional_field_is_not_supplied(site: Web) -> None:
    fields = [("input.name", ""), ("input.mode", ""), ("input.count", ""), ("input.extra", "")]

    await site.post_form("/workflows/deploy/run", fields)

    assert site.runs.requests[0].inputs == {"count": ""}


async def test_input_errors_show_the_form_again_with_422(
    make_web: MakeWeb, deploy: Workflow
) -> None:
    errors = {"count": "must be an integer", "": "unknown input 'zzz'"}
    web = make_web(workflows=[deploy], runs=FakeRunControl(error=InputError(errors)))

    response = await web.post_form(
        "/workflows/deploy/run", [("input.count", "three"), ("input.name", "Ada")]
    )

    assert response.status_code == 422
    html = response.text
    [summary] = elements(html, "div", class_="error-summary")
    assert summary.get("role") == "alert"
    assert elements(html, "a", href="#input-count")
    assert "count: must be an integer" in text(html)
    assert "unknown input 'zzz'" in text(html)
    [count] = elements(html, "input", id="input-count")
    assert count.get("value") == "three"
    assert count.get("aria-invalid") == "true"
    assert count.get("aria-describedby") == "input-count-error"
    assert elements(html, "p", id="input-count-error")
    [name] = elements(html, "input", id="input-name")
    assert name.get("value") == "Ada"
    assert name.get("aria-invalid") is None


@pytest.mark.parametrize(
    ("exc", "status", "message"),
    [
        (WorkflowInvalidError("deploy", [ERROR]), 409, "Workflow 'deploy' is invalid."),
        (ShuttingDownError("Treadle is shutting down"), 503, "Treadle is shutting down."),
        (NotFoundError("workflow 'deploy' doesn't exist"), 404, "Workflow 'deploy' doesn't exist."),
    ],
)
async def test_run_now_errors_show_a_page(
    make_web: MakeWeb, deploy: Workflow, exc: Exception, status: int, message: str
) -> None:
    web = make_web(workflows=[deploy], runs=FakeRunControl(error=exc))

    response = await web.post_form("/workflows/deploy/run", [("input.count", "1")])

    assert response.status_code == status
    assert message in text(response.text)
    assert (response.headers.get("retry-after") == "30") is (status == 503)


async def test_an_invalid_workflow_page_lists_the_issues(make_web: MakeWeb) -> None:
    entry = make_entry(workflow_id="broken", issues=[ERROR], source=SOURCE)
    web = make_web(
        workflows=[entry], runs=FakeRunControl(error=WorkflowInvalidError("broken", [ERROR]))
    )
    gone = make_web(runs=FakeRunControl(error=WorkflowInvalidError("gone", [ERROR])))

    response = await web.post_form("/workflows/broken/run")
    rerun = await gone.post_form("/runs/4/rerun")

    assert response.status_code == 409
    assert "/project/workflows/broken.yml:2:1: error[E101]" in unescape(response.text)
    assert rerun.status_code == 409
    assert "gone:2:1: error[E101]: unknown key 'on'" in unescape(rerun.text)


async def test_run_now_of_an_unknown_workflow_is_404(web: Web) -> None:
    response = await web.post_form("/workflows/nope/run")

    assert response.status_code == 404
    assert web.runs.requests == []


async def test_run_now_needs_a_valid_csrf_token(site: Web) -> None:
    await site.csrf("/workflows/deploy")

    response = await site.client.post(
        "/workflows/deploy/run", content=form_body(("input.count", "1")), headers=FORM
    )

    assert response.status_code == 403
    assert "This form expired. Reload the page and try again." in text(response.text)
    assert site.runs.requests == []


def step_logs(html: str) -> list[list[str]]:
    """Return the lines of each step's log excerpt on a run page."""
    blocks = re.findall(r'<pre class="log">(.*?)</pre>', unescape(html), re.DOTALL)
    return [block.splitlines() for block in blocks]


@pytest.fixture
async def history(store: AsyncStore, settings: Settings) -> dict[str, RunRecord]:
    """Stored runs 1 to 5 of two workflows; run 1 has 1,200 log lines over two steps."""
    deploy = stored_workflow(settings.project_dir, "deploy", name="Deploy")
    nightly = stored_workflow(settings.project_dir, "nightly")

    def seed(repo: Any) -> dict[str, RunRecord]:
        runs = Runs(repo, deploy)
        stored = {"logged": runs.finished(RunStatus.SUCCEEDED, steps=["build", "test"])}
        lines = [
            NewLogLine(
                seq,
                T0 + seq,
                "build" if seq <= 700 else "test",
                1 if seq <= 600 else 2,
                LogStream.STDOUT,
                f"line {seq}",
            )
            for seq in range(1, 1201)
        ]
        repo.append_logs(stored["logged"].id, lines)
        repo.append_logs(
            stored["logged"].id,
            [NewLogLine(1201, T0, None, None, LogStream.SYSTEM, "run finished")],
        )
        stored["failed"] = runs.finished(RunStatus.FAILED, error="disk full")
        stored["nightly"] = runs.queued(workflow=nightly, trigger_kind=TriggerKind.SCHEDULE)
        stored["cancelled"] = runs.finished(RunStatus.CANCELLED)
        stored["active"] = runs.running(steps=["build"])
        return stored

    return await store.write(seed)


@pytest.fixture
def stored(make_web: MakeWeb, store: AsyncStore, history: dict[str, RunRecord]) -> Web:
    return make_web(reader=store, workflows=[make_entry(workflow_id="deploy")])


async def test_runs_page_lists_runs_and_keeps_the_filters(stored: Web) -> None:
    response = await stored.client.get("/runs?status=failed&status=succeeded&q=disk&trigger=manual")

    assert response.status_code == 200
    html = response.text
    links = [element.get("href") for element in elements(html, "a")]
    assert "/runs/2" in links
    assert "/runs/1" not in links
    [failed] = elements(html, "input", id="filter-status-failed")
    [queued] = elements(html, "input", id="filter-status-queued")
    assert "checked" in failed.attrs
    assert "checked" not in queued.attrs
    assert elements(html, "input", id="filter-q", value="disk")
    [manual] = elements(html, "option", value="manual")
    assert "selected" in manual.attrs
    options = [option.get("value") for option in elements(html, "option")]
    assert options == ["", "deploy", "", "manual", "schedule", "watch", "webhook"]


async def test_runs_page_links_to_older_runs_with_the_same_filters(stored: Web) -> None:
    response = await stored.client.get("/runs?workflow=deploy&limit=2")

    html = response.text
    links = [element.get("href") for element in elements(html, "a")]
    assert "/runs/5" in links
    assert "/runs/4" in links
    assert "/runs/2" not in links
    assert "/runs?workflow=deploy&limit=2&before=4" in links
    last = await stored.client.get("/runs?workflow=deploy&limit=2&before=2")
    assert [run.get("href") for run in elements(last.text, "a", href="/runs/1")] == ["/runs/1"]
    assert "Older runs" not in text(last.text)


async def test_runs_page_selects_a_filtered_workflow_that_no_longer_exists(stored: Web) -> None:
    response = await stored.client.get("/runs?workflow=nightly")

    [option] = elements(response.text, "option", value="nightly")
    assert "selected" in option.attrs


async def test_runs_page_refuses_a_bad_filter(web: Web) -> None:
    response = await web.client.get("/runs?limit=999")

    assert response.status_code == 400
    assert "'limit' must be an integer from 1 to 200." in text(response.text)


async def test_runs_page_without_matches(web: Web) -> None:
    response = await web.client.get("/runs")

    assert "No runs match these filters." in text(response.text)


async def test_run_page_of_an_active_run_offers_cancel_and_live_updates(stored: Web) -> None:
    response = await stored.client.get("/runs/5")

    html = response.text
    [article] = elements(html, "article")
    assert article.get("data-run-active") == "true"
    assert article.get("data-run-id") == "5"
    assert article.get("data-after") == "0"
    [status] = elements(html, "p", id="run-status")
    assert status.get("aria-live") == "polite"
    [cancel] = elements(html, "form", action="/runs/5/cancel")
    assert cancel.get("data-confirm") == "Cancel run #5?"
    assert elements(html, "button", name="force", value="true")
    [pause] = elements(html, "button", id="pause-updates")
    assert "hidden" in pause.attrs
    assert elements(html, "pre", id="live-log")
    assert not elements(html, "form", action="/runs/5/rerun")
    assert elements(html, "a", href="/runs/5")


async def test_run_page_of_a_finished_run_offers_rerun(stored: Web) -> None:
    html = (await stored.client.get("/runs/2")).text

    assert elements(html, "article", data_run_active="false")
    assert elements(html, "form", action="/runs/2/rerun")
    assert not elements(html, "form", action="/runs/2/cancel")
    assert not elements(html, "pre", id="live-log")
    assert "✗ disk full" in text(html)


async def test_run_page_shows_the_last_500_lines_of_each_step(stored: Web) -> None:
    html = (await stored.client.get("/runs/1")).text

    assert elements(html, "article", data_after="1201")
    sections = [element.get("id") for element in elements(html, "details", class_="step-log")]
    assert sections == ["log-build", "log-test"]
    build, test = step_logs(html)
    assert len(build) == len(test) == 500
    assert build[0] == "12:00:00 #1 stdout | line 201"
    assert build[-1] == "12:00:00 #2 stdout | line 700"
    assert test[0] == "12:00:00 #2 stdout | line 701"
    assert test[-1] == "12:00:01 #2 stdout | line 1200"
    assert elements(html, "a", href="/runs/1/log.txt?step=build")
    assert elements(html, "a", href="/runs/1/log.txt")


async def test_run_page_shows_the_run_details(make_web: MakeWeb) -> None:
    run = make_run(
        9,
        workflow_id="deploy",
        workflow_name="Deploy",
        status=RunStatus.CANCELLED,
        reason=RunReason.USER,
        error="run cancelled (user)",
        trigger={"kind": "manual", "origin": "dashboard", "note": "<b>hi</b>"},
        inputs={"count": 3, "name": "Ada", "dry_run": None},
        started_at=FIXED_TIME,
        finished_at=FIXED_TIME + timedelta(seconds=68),
        rerun_of=7,
        replaced_by=10,
        attempt=2,
    )
    steps = [
        make_step_record(
            "archive",
            status=StepStatus.FAILED,
            tolerated=True,
            error="exit code 23",
            started_at=FIXED_TIME,
            finished_at=FIXED_TIME + timedelta(seconds=6.6),
            attempts=[make_attempt(1, status=AttemptStatus.FAILED), make_attempt(2)],
        ),
        make_step_record(
            "record", status=StepStatus.SKIPPED, skip_reason=SkipReason.UPSTREAM_FAILED
        ),
    ]
    log = LogLine(1, FIXED_TIME, "archive", 2, LogStream.STDERR, "<script>alert(1)</script>")
    reader = MemoryRunReader()
    reader.add(make_detail(run, steps), [log])
    web = make_web(reader=reader)

    html = (await web.client.get("/runs/9")).text

    page = text(html)
    assert "Run #9 of Deploy" in page
    assert "⊘ cancelled (user)" in page
    assert "✗ run cancelled (user)" in page
    assert "Attempt 2, re-run of #7" in page
    assert elements(html, "a", href="/runs/7")
    assert elements(html, "a", href="/runs/10")
    assert "Duration 1m08s" in page
    assert '"note": "<b>hi</b>"' in unescape(html)
    assert "&lt;b&gt;hi&lt;/b&gt;" in html
    assert 'Input Value count 3 name "Ada" dry_run null' in page
    assert "archive fake ✗ failed (tolerated) 2 6.6s exit code 23" in page
    assert "record fake ↷ skipped 0 - skipped: upstream failed" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html


async def test_run_page_of_an_unknown_run_is_404(web: Web) -> None:
    for run_id in ("3", "9" * 20):
        response = await web.client.get(f"/runs/{run_id}")

        assert response.status_code == 404
        assert f"Run {run_id} doesn't exist." in text(response.text)


async def test_log_text_streams_the_whole_log(stored: Web) -> None:
    response = await stored.client.get("/runs/1/log.txt")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    lines = response.text.splitlines()
    assert len(lines) == 1201
    assert lines[0] == "2026-11-09T12:00:00.001Z build#1 stdout | line 1"
    assert lines[1199] == "2026-11-09T12:00:01.200Z test#2 stdout | line 1200"
    assert lines[1200] == "2026-11-09T12:00:00.000Z - system | run finished"


async def test_log_text_filters_by_step_and_attempt(stored: Web) -> None:
    build = (await stored.client.get("/runs/1/log.txt?step=build")).text.splitlines()
    second = (await stored.client.get("/runs/1/log.txt?step=build&attempt=2")).text.splitlines()
    empty = await stored.client.get("/runs/2/log.txt")

    assert len(build) == 700
    assert second[0].endswith("line 601")
    assert len(second) == 100
    assert empty.text == ""


async def test_log_text_errors(web: Web) -> None:
    missing = await web.client.get("/runs/3/log.txt")
    bad = await web.client.get("/runs/3/log.txt?attempt=0")

    assert missing.status_code == 404
    assert bad.status_code == 400
    assert "'attempt' must be an integer of at least 1." in text(bad.text)


@pytest.mark.parametrize(("fields", "force"), [([], False), ([("force", "true")], True)])
async def test_cancel_requests_a_cancel_and_shows_the_run(
    web: Web, fields: list[tuple[str, str]], force: bool
) -> None:
    response = await web.post_form("/runs/4/cancel", fields)

    assert response.status_code == 303
    assert response.headers["location"] == "/runs/4"
    assert web.runs.cancels == [(4, CancelReason.USER, force)]


async def test_cancel_of_a_finished_run_is_409(make_web: MakeWeb) -> None:
    outcome = CancelOutcome(4, RunStatus.SUCCEEDED, accepted=False, forced=False)
    web = make_web(runs=FakeRunControl(cancel_outcome=outcome))

    response = await web.post_form("/runs/4/cancel")

    assert response.status_code == 409
    assert "Run 4 already finished (succeeded)." in text(response.text)


async def test_cancel_of_an_unknown_run_is_404(make_web: MakeWeb) -> None:
    web = make_web(runs=FakeRunControl(error=NotFoundError("run 4 doesn't exist")))

    response = await web.post_form("/runs/4/cancel")

    assert response.status_code == 404
    assert "Run 4 doesn't exist." in text(response.text)


async def test_rerun_submits_the_run_again_and_shows_the_new_one(web: Web) -> None:
    response = await web.post_form("/runs/4/rerun")

    assert response.status_code == 303
    assert response.headers["location"] == "/runs/1"
    assert web.runs.reruns == [4]


@pytest.mark.parametrize(
    ("exc", "status", "message"),
    [
        (ConflictError("run 4 isn't finished"), 409, "Run 4 isn't finished."),
        (ShuttingDownError("Treadle is shutting down"), 503, "Treadle is shutting down."),
        (InputError({"count": "is required"}), 422, "Input 'count' is required."),
        (NotFoundError("run 4 doesn't exist"), 404, "Run 4 doesn't exist."),
    ],
)
async def test_rerun_errors_show_a_page(
    make_web: MakeWeb, exc: Exception, status: int, message: str
) -> None:
    web = make_web(runs=FakeRunControl(error=exc))

    response = await web.post_form("/runs/4/rerun")

    assert response.status_code == status
    assert message in text(response.text)


@pytest.mark.parametrize("path", ["/runs/4/cancel", "/runs/4/rerun", "/workflows/deploy/run"])
async def test_actions_need_a_csrf_token(site: Web, path: str) -> None:
    response = await site.client.post(path, content="", headers=FORM)

    assert response.status_code == 403
    assert site.runs.cancels == site.runs.reruns == site.runs.requests == []


@pytest.mark.parametrize(
    ("name", "content_type", "marker"),
    [
        ("treadle.css", "text/css; charset=utf-8", ":root {"),
        ("treadle.js", "text/javascript; charset=utf-8", '"use strict";'),
    ],
)
async def test_static_files_are_served(web: Web, name: str, content_type: str, marker: str) -> None:
    response = await web.client.get(f"/static/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert marker in response.text


async def test_an_unknown_static_file_is_404(web: Web) -> None:
    for path in ("/static/nope.css", "/static/../treadle.db"):
        response = await web.client.get(path)

        assert response.status_code == 404


async def test_the_login_page_carries_a_safe_next(secured: Web) -> None:
    good = await secured.client.get("/login?next=/runs%3Fstatus%3Dfailed")
    evil = await secured.client.get("/login?next=//evil.example")

    assert good.status_code == 200
    assert elements(good.text, "input", name="next", value="/runs?status=failed")
    assert elements(evil.text, "input", name="next", value="/")
    [token] = elements(good.text, "input", id="token")
    assert token.get("type") == "password"
    assert elements(good.text, "label", for_="token")


async def test_login_starts_a_session_and_rotates_the_csrf_cookie(secured: Web) -> None:
    before = await secured.csrf("/login")

    response = await secured.login(next_url="/runs")

    assert response.status_code == 303
    assert response.headers["location"] == "/runs"
    cookies = response.headers.get_list("set-cookie")
    [session] = [cookie for cookie in cookies if cookie.startswith(f"{SESSION_COOKIE}=")]
    [csrf] = [cookie for cookie in cookies if cookie.startswith(f"{CSRF_COOKIE}=")]
    for attribute in ("HttpOnly", "Path=/", "SameSite=strict"):
        assert attribute in session
        assert attribute in csrf
    assert "Max-Age=43200" in session
    assert "Secure" not in session
    assert secured.client.cookies[CSRF_COOKIE] != before
    page = await secured.client.get("/runs")
    assert page.status_code == 200
    assert csrf_field(page.text) == secured.client.cookies[CSRF_COOKIE]
    assert elements(page.text, "form", action="/logout")


async def test_a_wrong_token_shows_the_form_again_with_401(secured: Web) -> None:
    response = await secured.login("not-the-token")

    assert response.status_code == 401
    assert "That token isn't valid." in text(response.text)
    assert elements(response.text, "input", id="token")
    assert SESSION_COOKIE not in secured.client.cookies


async def test_login_needs_a_valid_csrf_token(secured: Web) -> None:
    body = form_body(("token", TOKEN), ("next", "/"))

    response = await secured.client.post("/login", content=body, headers=FORM)

    assert response.status_code == 403
    assert SESSION_COOKIE not in secured.client.cookies


async def test_secure_cookies_adds_the_secure_attribute(
    make_web: MakeWeb, settings: Settings
) -> None:
    server = dataclasses.replace(settings.server, secure_cookies=True)
    web = make_web(token=TOKEN, settings=dataclasses.replace(settings, server=server))

    login_page = await web.client.get("/login")
    csrf = csrf_field(login_page.text)
    cookie = login_page.headers["set-cookie"]
    response = await web.client.post(
        "/login",
        content=form_body(("csrf_token", csrf), ("token", TOKEN), ("next", "/")),
        headers={**FORM, "Cookie": f"{CSRF_COOKIE}={csrf}"},
    )

    assert "Secure" in cookie
    assert all("Secure" in value for value in response.headers.get_list("set-cookie"))


async def test_logout_ends_the_session(secured: Web) -> None:
    await secured.login()

    response = await secured.post_form("/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert all("Max-Age=0" in value for value in response.headers.get_list("set-cookie"))
    after = await secured.client.get("/")
    assert after.status_code == 303


async def test_logout_needs_a_valid_csrf_token(secured: Web) -> None:
    await secured.login()

    response = await secured.client.post("/logout", content="", headers=FORM)

    assert response.status_code == 403
    assert (await secured.client.get("/")).status_code == 200


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/login"), ("POST", "/login"), ("POST", "/logout")]
)
async def test_sign_in_pages_are_404_without_a_token(web: Web, method: str, path: str) -> None:
    response = await web.client.request(method, path, content="", headers=FORM)

    assert response.status_code == 404
    assert "Sign-in is off" in text(response.text)


async def test_the_sign_out_button_appears_only_when_signed_in(web: Web, secured: Web) -> None:
    assert not elements((await web.client.get("/")).text, "form", action="/logout")
    await secured.login()
    assert elements((await secured.client.get("/")).text, "form", action="/logout")


async def test_pages_show_a_banner_while_draining(web: Web) -> None:
    web.draining = True

    response = await web.client.get("/")

    assert "Treadle is shutting down." in text(response.text)
    assert elements(response.text, "p", class_="banner", role="status")


async def test_an_unknown_dashboard_path_is_an_html_404(web: Web) -> None:
    response = await web.client.get("/nothing-here")

    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "Not found." in text(response.text)


async def test_a_wrong_method_on_a_page_is_405_with_allow(web: Web) -> None:
    response = await web.client.put("/runs")

    assert response.status_code == 405
    assert "Method not allowed." in text(response.text)
    assert "GET" in response.headers["allow"]


async def test_an_unexpected_error_on_a_page_is_a_500(make_web: MakeWeb) -> None:
    web = make_web(
        runs=FakeRunControl(error=StoreError("disk I/O error")), raise_app_exceptions=False
    )

    response = await web.post_form("/runs/4/rerun")

    assert response.status_code == 500
    assert response.text == "Internal server error."
