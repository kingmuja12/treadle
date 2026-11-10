from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest
from tests.support.builders import (
    make_detail,
    make_entry,
    make_input,
    make_run,
    make_step_record,
)
from tests.support.fakes import FakeRunControl, MemoryRunReader
from tests.unit.store.helpers import T0, Runs, project_workflow
from tests.unit.web.helpers import MakeWeb, Web

from treadle.config import Settings
from treadle.errors import (
    BusyError,
    ConflictError,
    InputError,
    NotFoundError,
    ShuttingDownError,
    StoreError,
    TreadleError,
    WorkflowInvalidError,
)
from treadle.models import (
    CancelOutcome,
    CancelReason,
    InputType,
    LogStream,
    ManagerStats,
    RunRecord,
    RunStatus,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    Workflow,
)
from treadle.serialize import run_detail_json, run_json, workflow_detail_json, workflow_json
from treadle.store.async_store import AsyncStore
from treadle.store.repo import NewLogLine
from treadle.types import Issue, Position, Severity

JSON = {"Content-Type": "application/json"}
ERROR = Issue(Severity.ERROR, "E101", "unknown key 'on'", ("on",), Position(2, 1), "use triggers")
PUSH = TriggerState(
    "deploy", "deploy:webhook:push", TriggerKind.WEBHOOK, "webhook /hooks/push", None, None
)
OTHER = TriggerState(
    "other", "other:webhook:x", TriggerKind.WEBHOOK, "webhook /hooks/x", None, None
)


@pytest.fixture
def deploy(settings: Settings) -> Workflow:
    """The `deploy` workflow, stored inside the project, with one integer input."""
    return project_workflow(
        settings.project_dir,
        "deploy",
        name="Deploy",
        inputs=[make_input("count", InputType.INTEGER)],
    )


@pytest.fixture
async def history(store: AsyncStore, settings: Settings) -> dict[str, RunRecord]:
    """Four stored runs, IDs 1 to 4, with logs on the finished run and the running one."""
    deploy = project_workflow(settings.project_dir, "deploy")
    nightly = project_workflow(settings.project_dir, "nightly")

    def seed(repo: Any) -> dict[str, RunRecord]:
        runs = Runs(repo, deploy)
        stored = {
            "ok": runs.finished(RunStatus.SUCCEEDED, steps=["build"]),
            "broken": runs.finished(RunStatus.FAILED, error="step 'archive' failed: Disk FULL"),
            "scheduled": runs.queued(workflow=nightly, trigger_kind=TriggerKind.SCHEDULE),
            "active": runs.running(steps=["build"]),
        }
        lines = [
            NewLogLine(seq, T0 + seq, "build", 1 + seq // 4, LogStream.STDOUT, f"line {seq}")
            for seq in range(1, 6)
        ]
        repo.append_logs(stored["ok"].id, lines)
        repo.append_logs(stored["active"].id, lines[:2])
        return stored

    return await store.write(seed)


@pytest.fixture
async def stored(make_web: MakeWeb, store: AsyncStore, history: dict[str, RunRecord]) -> Web:
    """An application that reads the stored history."""
    return make_web(reader=store)


def error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


async def test_health_is_ok(web: Web) -> None:
    response = await web.client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_health_answers_503_while_draining(web: Web) -> None:
    web.draining = True

    response = await web.client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "draining", "version": "0.1.0"}


async def test_status_reports_runs_workflows_and_uptime(
    make_web: MakeWeb, deploy: Workflow
) -> None:
    stats = ManagerStats(active=2, queued=3, draining=False)
    broken = make_entry(workflow_id="broken", issues=[ERROR])
    web = make_web(workflows=[deploy, broken], runs=FakeRunControl(stats=stats))
    web.clock.jump(90.7)

    response = await web.client.get("/api/v1/status")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "schema_version": 1,
        "uptime_s": 90,
        "active_runs": 2,
        "queued_runs": 3,
        "draining": False,
        "workflows": {"valid": 1, "invalid": 1},
    }


async def test_workflows_lists_every_entry(make_web: MakeWeb, deploy: Workflow) -> None:
    last = make_run(5, workflow_id="deploy")
    broken = make_entry(workflow_id="broken", issues=[ERROR])
    web = make_web(
        workflows=[deploy, broken],
        reader=MemoryRunReader(make_run(4, workflow_id="deploy"), last),
        triggers=[PUSH, OTHER],
    )
    project_dir = web.ctx.settings.project_dir

    response = await web.client.get("/api/v1/workflows")

    assert response.status_code == 200
    assert response.json() == {
        "workflows": [
            workflow_json(broken, project_dir=project_dir, triggers=[], last_run=None),
            workflow_json(
                make_entry(deploy), project_dir=project_dir, triggers=[PUSH], last_run=last
            ),
        ]
    }
    assert response.json()["workflows"][1]["path"] == "workflows/deploy.yml"


async def test_workflow_detail(make_web: MakeWeb, deploy: Workflow) -> None:
    web = make_web(workflows=[deploy], triggers=[PUSH, OTHER])

    response = await web.client.get("/api/v1/workflows/deploy")

    assert response.status_code == 200
    assert response.json() == workflow_detail_json(
        make_entry(deploy), project_dir=web.ctx.settings.project_dir, triggers=[PUSH], last_run=None
    )


async def test_workflow_detail_of_an_unknown_workflow_is_404(web: Web) -> None:
    response = await web.client.get("/api/v1/workflows/nope")

    assert response.status_code == 404
    assert response.json() == error("not_found", "workflow 'nope' doesn't exist")


async def test_start_run_submits_a_manual_api_run(make_web: MakeWeb, deploy: Workflow) -> None:
    web = make_web(workflows=[deploy])

    response = await web.client.post("/api/v1/workflows/deploy/runs", json={"inputs": {"count": 3}})

    assert response.status_code == 202
    [request] = web.runs.requests
    assert request.workflow_id == "deploy"
    assert request.kind is TriggerKind.MANUAL
    assert request.trigger == {"kind": "manual", "origin": "api"}
    assert request.inputs == {"count": 3}
    assert request.inputs_are_strings is False
    assert response.json() == {
        "outcome": "queued",
        "run": run_json(make_run(1, workflow_id="deploy")),
    }


@pytest.mark.parametrize("body", [b"{}", b"", b"  \n", b'{"inputs": {}}'])
async def test_start_run_inputs_may_be_omitted(web: Web, body: bytes) -> None:
    response = await web.client.post("/api/v1/workflows/deploy/runs", content=body, headers=JSON)

    assert response.status_code == 202
    assert web.runs.requests[0].inputs == {}


async def test_start_run_reports_a_skipped_run(make_web: MakeWeb) -> None:
    web = make_web(runs=FakeRunControl(outcome=SubmitOutcome.SKIPPED))

    response = await web.client.post("/api/v1/workflows/deploy/runs", json={})

    assert response.status_code == 202
    assert response.json()["outcome"] == "skipped"
    assert response.json()["run"]["status"] == "skipped"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{", "the request body isn't valid JSON"),
        (b"\xff", "the request body isn't valid JSON"),
        (b'{"inputs": {"n": NaN}}', "the request body isn't valid JSON"),
        (b"[" * 20_000, "the request body isn't valid JSON"),
        (b"[]", "the request body must be a JSON object"),
        (b'"inputs"', "the request body must be a JSON object"),
        (b'{"inputs": [1]}', "'inputs' must be a JSON object"),
        (b'{"input": {}}', "unknown key 'input' in the request body"),
    ],
)
async def test_start_run_refuses_a_bad_body(web: Web, body: bytes, message: str) -> None:
    response = await web.client.post("/api/v1/workflows/deploy/runs", content=body, headers=JSON)

    assert response.status_code == 400
    assert response.json() == error("bad_request", message)
    assert web.runs.requests == []


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_api_posts_need_json(web: Web, content_type: str | None) -> None:
    headers = {"Content-Type": content_type} if content_type else {}

    for path in ("/api/v1/workflows/deploy/runs", "/api/v1/runs/1/cancel", "/api/v1/runs/1/rerun"):
        response = await web.client.post(path, content=b"{}", headers=headers)

        assert response.status_code == 415
        assert response.json() == error(
            "unsupported_media_type", "send the body as application/json"
        )
    assert web.runs.requests == web.runs.cancels == web.runs.reruns == []


async def test_api_posts_accept_json_with_a_charset(web: Web) -> None:
    headers = {"Content-Type": "Application/JSON; charset=utf-8"}

    response = await web.client.post(
        "/api/v1/workflows/deploy/runs", content=b"{}", headers=headers
    )

    assert response.status_code == 202


@pytest.mark.parametrize(
    ("exc", "status", "code", "details"),
    [
        (NotFoundError("workflow 'deploy' doesn't exist"), 404, "not_found", None),
        (
            InputError({"count": "must be an integer"}),
            422,
            "invalid_inputs",
            {"count": "must be an integer"},
        ),
        (ShuttingDownError("Treadle is shutting down"), 503, "shutting_down", None),
        (BusyError("the run manager is busy"), 503, "busy", None),
        (ConflictError("run 1 is still running"), 409, "conflict", None),
    ],
)
async def test_start_run_maps_submit_errors(
    make_web: MakeWeb, exc: TreadleError, status: int, code: str, details: Any
) -> None:
    web = make_web(runs=FakeRunControl(error=exc))

    response = await web.client.post(
        "/api/v1/workflows/deploy/runs", json={"inputs": {"count": "x"}}
    )

    assert response.status_code == status
    assert response.json() == error(code, str(exc), details)
    assert ("retry-after" in response.headers) is (status == 503)
    if status == 503:
        assert response.headers["retry-after"] == "30"


async def test_start_run_of_an_invalid_workflow_is_409_with_its_issues(
    make_web: MakeWeb, settings: Settings
) -> None:
    entry = make_entry(workflow_id="deploy", issues=[ERROR])
    entry = dataclasses.replace(entry, path=settings.workflows_dir / "deploy.yml")
    web = make_web(
        workflows=[entry], runs=FakeRunControl(error=WorkflowInvalidError("deploy", [ERROR]))
    )

    response = await web.client.post("/api/v1/workflows/deploy/runs", json={})

    assert response.status_code == 409
    assert response.json() == error(
        "workflow_invalid",
        "workflow 'deploy' is invalid",
        [ERROR.to_json("workflows/deploy.yml")],
    )


async def test_an_invalid_workflow_without_an_entry_names_its_id(make_web: MakeWeb) -> None:
    web = make_web(runs=FakeRunControl(error=WorkflowInvalidError("gone", [ERROR])))

    response = await web.client.post("/api/v1/workflows/gone/runs", json={})

    assert response.json()["error"]["details"] == [ERROR.to_json("gone")]


async def test_an_unexpected_error_is_a_bare_500(make_web: MakeWeb) -> None:
    web = make_web(
        runs=FakeRunControl(error=StoreError("disk I/O error")), raise_app_exceptions=False
    )

    response = await web.client.post("/api/v1/workflows/deploy/runs", json={})

    assert response.status_code == 500
    assert response.json() == error("internal", "internal error")
    assert "disk" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


async def test_a_streamed_body_over_the_api_limit_is_413_json(web: Web) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(65):
            yield b" " * 1024

    response = await web.client.post(
        "/api/v1/workflows/deploy/runs", content=chunks(), headers=JSON
    )

    assert response.status_code == 413
    assert response.json() == error("payload_too_large", "the request body is larger than 64.0 KiB")
    assert web.runs.requests == []


async def test_a_declared_body_over_the_api_limit_is_413_json(web: Web) -> None:
    response = await web.client.post(
        "/api/v1/workflows/deploy/runs", content=b" " * (65 * 1024), headers=JSON
    )

    assert response.status_code == 413
    assert response.json() == error("payload_too_large", "the request body is larger than 64.0 KiB")
    assert web.runs.requests == []


async def test_runs_lists_history_newest_first(stored: Web, history: dict[str, RunRecord]) -> None:
    response = await stored.client.get("/api/v1/runs")

    assert response.status_code == 200
    assert response.json() == {
        "runs": [run_json(run) for run in reversed(history.values())],
        "next_before_id": None,
    }


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("workflow=nightly", ["scheduled"]),
        ("workflow=deploy&status=failed&status=succeeded", ["broken", "ok"]),
        ("status=running", ["active"]),
        ("trigger=schedule", ["scheduled"]),
        ("trigger=", ["active", "scheduled", "broken", "ok"]),
        ("q=disk+full", ["broken"]),
        ("q=line+3", ["ok"]),
        ("q=", ["active", "scheduled", "broken", "ok"]),
        ("before=3", ["broken", "ok"]),
        ("workflow=", ["active", "scheduled", "broken", "ok"]),
        ("workflow=unknown", []),
    ],
)
async def test_runs_filters(
    stored: Web, history: dict[str, RunRecord], query: str, names: list[str]
) -> None:
    response = await stored.client.get(f"/api/v1/runs?{query}")

    assert [run["id"] for run in response.json()["runs"]] == [history[name].id for name in names]


async def test_runs_pages_with_next_before_id(stored: Web) -> None:
    first = (await stored.client.get("/api/v1/runs?limit=3")).json()
    second = (
        await stored.client.get(f"/api/v1/runs?limit=3&before={first['next_before_id']}")
    ).json()

    assert [run["id"] for run in first["runs"]] == [4, 3, 2]
    assert first["next_before_id"] == 2
    assert [run["id"] for run in second["runs"]] == [1]
    assert second["next_before_id"] is None


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (
            "status=bogus",
            "'status' must be one of: queued, running, succeeded, failed, cancelled, "
            "timed_out, skipped, interrupted",
        ),
        ("trigger=cron", "'trigger' must be one of: manual, schedule, watch, webhook"),
        ("limit=0", "'limit' must be an integer from 1 to 200"),
        ("limit=201", "'limit' must be an integer from 1 to 200"),
        ("limit=ten", "'limit' must be an integer from 1 to 200"),
        ("limit=-1", "'limit' must be an integer from 1 to 200"),
        ("before=0", "'before' must be an integer of at least 1"),
        ("before=1.5", "'before' must be an integer of at least 1"),
        ("before=" + "9" * 19, "'before' must be an integer of at least 1"),
    ],
)
async def test_runs_refuses_bad_filters(web: Web, query: str, message: str) -> None:
    response = await web.client.get(f"/api/v1/runs?{query}")

    assert response.status_code == 400
    assert response.json() == error("bad_request", message)


async def test_run_detail(make_web: MakeWeb) -> None:
    detail = make_detail(make_run(7), [make_step_record("build")])
    web = make_web(reader=MemoryRunReader(detail))

    response = await web.client.get("/api/v1/runs/7")

    assert response.status_code == 200
    assert response.json() == run_detail_json(detail)


@pytest.mark.parametrize("run_id", ["8", "9" * 20])
async def test_run_detail_of_an_unknown_run_is_404(web: Web, run_id: str) -> None:
    response = await web.client.get(f"/api/v1/runs/{run_id}")

    assert response.status_code == 404
    assert response.json() == error("not_found", f"run {run_id} doesn't exist")


async def test_logs_of_a_finished_run_page_and_complete(
    stored: Web, history: dict[str, RunRecord]
) -> None:
    run_id = history["ok"].id

    everything = (await stored.client.get(f"/api/v1/runs/{run_id}/logs")).json()
    page = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?after=2&limit=2")).json()
    last = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?after=4&limit=1")).json()
    past = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?after=5")).json()

    assert [line["seq"] for line in everything["lines"]] == [1, 2, 3, 4, 5]
    assert everything["lines"][0] == {
        "seq": 1,
        "ts": "2026-11-09T12:00:00.001Z",
        "step": "build",
        "attempt": 1,
        "stream": "stdout",
        "text": "line 1",
    }
    assert (everything["next_after"], everything["complete"]) == (5, True)
    assert [line["seq"] for line in page["lines"]] == [3, 4]
    assert (page["next_after"], page["complete"]) == (4, False)
    assert (last["next_after"], last["complete"]) == (5, False)
    assert (past["lines"], past["next_after"], past["complete"]) == ([], 5, True)


async def test_logs_of_an_active_run_are_never_complete(
    stored: Web, history: dict[str, RunRecord]
) -> None:
    body = (await stored.client.get(f"/api/v1/runs/{history['active'].id}/logs")).json()

    assert [line["seq"] for line in body["lines"]] == [1, 2]
    assert body["complete"] is False


async def test_logs_filter_by_step_and_attempt(stored: Web, history: dict[str, RunRecord]) -> None:
    run_id = history["ok"].id

    attempt = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?step=build&attempt=2")).json()
    other = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?step=deploy")).json()
    blank = (await stored.client.get(f"/api/v1/runs/{run_id}/logs?step=&attempt=")).json()

    assert [line["seq"] for line in attempt["lines"]] == [4, 5]
    assert other["lines"] == []
    assert len(blank["lines"]) == 5


async def test_logs_of_an_unknown_run_is_404(web: Web) -> None:
    response = await web.client.get("/api/v1/runs/3/logs")

    assert response.status_code == 404
    assert response.json() == error("not_found", "run 3 doesn't exist")


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("limit=5001", "'limit' must be an integer from 1 to 5000"),
        ("limit=0", "'limit' must be an integer from 1 to 5000"),
        ("attempt=0", "'attempt' must be an integer of at least 1"),
        ("after=x", "'after' must be an integer of at least 0"),
    ],
)
async def test_logs_refuses_bad_parameters(stored: Web, query: str, message: str) -> None:
    response = await stored.client.get(f"/api/v1/runs/1/logs?{query}")

    assert response.status_code == 400
    assert response.json() == error("bad_request", message)


@pytest.mark.parametrize(("body", "force"), [({}, False), ({"force": True}, True)])
async def test_cancel_accepts_a_request(web: Web, body: dict[str, Any], force: bool) -> None:
    response = await web.client.post("/api/v1/runs/4/cancel", json=body)

    assert response.status_code == 202
    assert response.json() == {"run_id": 4, "status": "running", "accepted": True, "forced": force}
    assert web.runs.cancels == [(4, CancelReason.USER, force)]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (RunStatus.SUCCEEDED, "run 4 already finished (succeeded)"),
        (RunStatus.RUNNING, "run 4 is already finishing"),
    ],
)
async def test_cancel_of_a_finished_run_is_409(
    make_web: MakeWeb, status: RunStatus, message: str
) -> None:
    outcome = CancelOutcome(4, status, accepted=False, forced=False)
    web = make_web(runs=FakeRunControl(cancel_outcome=outcome))

    response = await web.client.post("/api/v1/runs/4/cancel", json={})

    assert response.status_code == 409
    details = {"run_id": 4, "status": status.value, "accepted": False, "forced": False}
    assert response.json() == error("already_finished", message, details)


async def test_cancel_of_an_unknown_run_is_404(make_web: MakeWeb) -> None:
    web = make_web(runs=FakeRunControl(error=NotFoundError("run 4 doesn't exist")))

    for run_id in ("4", "9" * 20):
        response = await web.client.post(f"/api/v1/runs/{run_id}/cancel", json={})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("body", [{"force": "yes"}, {"force": 1}, {"forced": True}])
async def test_cancel_refuses_bad_options(web: Web, body: dict[str, Any]) -> None:
    response = await web.client.post("/api/v1/runs/4/cancel", json=body)

    assert response.status_code == 400
    assert web.runs.cancels == []


async def test_rerun_submits_the_run_again(web: Web) -> None:
    response = await web.client.post("/api/v1/runs/4/rerun", json={})

    assert response.status_code == 202
    assert response.json()["outcome"] == "queued"
    assert response.json()["run"]["rerun_of"] == 4
    assert web.runs.reruns == [4]


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (NotFoundError("run 4 doesn't exist"), 404, "not_found"),
        (ConflictError("run 4 is still running"), 409, "conflict"),
        (WorkflowInvalidError("wf", [ERROR]), 409, "workflow_invalid"),
        (InputError({"count": "is required"}), 422, "invalid_inputs"),
        (ShuttingDownError("Treadle is shutting down"), 503, "shutting_down"),
    ],
)
async def test_rerun_maps_errors(
    make_web: MakeWeb, exc: TreadleError, status: int, code: str
) -> None:
    web = make_web(runs=FakeRunControl(error=exc))

    response = await web.client.post("/api/v1/runs/4/rerun", json={})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_rerun_takes_no_options(web: Web) -> None:
    response = await web.client.post("/api/v1/runs/4/rerun", json={"force": True})

    assert response.status_code == 400
    assert web.runs.reruns == []


async def test_an_unknown_api_path_is_404_json(web: Web) -> None:
    response = await web.client.get("/api/v1/nothing-here")

    assert response.status_code == 404
    assert response.json() == error("not_found", "not found")


async def test_a_wrong_method_is_405_json(web: Web) -> None:
    response = await web.client.delete("/api/v1/runs/4")

    assert response.status_code == 405
    assert response.json() == error("bad_request", "method not allowed")
    assert set(response.headers["allow"].split(", ")) == {"GET", "HEAD"}
