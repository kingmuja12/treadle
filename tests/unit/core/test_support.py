"""Tests for the shared test infrastructure: fakes, builders, projects, and fixtures."""

from __future__ import annotations

import asyncio
import random
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tests.support.builders import (
    FIXED_TIME,
    make_condition,
    make_detail,
    make_entry,
    make_run,
    make_runtime,
    make_schedule,
    make_step,
    make_step_record,
    make_watch,
    make_webhook,
    make_workflow,
    static_template,
)
from tests.support.clock import FakeClock
from tests.support.fakes import (
    Fail,
    FakeRunControl,
    FakeSecrets,
    FakeStep,
    FakeSteps,
    FakeSubmitter,
    FakeTriggerInfo,
    FakeWebhookHandler,
    FakeWorkflowSource,
    Hang,
    MemoryRunReader,
    Raise,
    RecordingLog,
    RecordingProcesses,
    RecordingSink,
    Sleep,
    Succeed,
)
from tests.support.project import ProjectFactory
from tests.support.vtime import current_loop

from treadle import __version__
from treadle.config import Settings
from treadle.errors import NotFoundError, StepError
from treadle.expr.ast import Call, Const
from treadle.models import (
    CancelOutcome,
    CancelReason,
    CronSpec,
    HookRequest,
    HookResponse,
    LogLine,
    LogStream,
    ManagerStats,
    RegistryChange,
    RunFilter,
    RunRequest,
    RunStatus,
    SignatureMode,
    StepStatus,
    Submission,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
)
from treadle.schema import validate
from treadle.secrets import SecretSnapshot
from treadle.types import Issue, Severity

MANUAL = RunRequest("wf", TriggerKind.MANUAL, {"kind": "manual", "origin": "cli"})


class TestFakeStep:
    async def test_succeeds_by_default_and_records_calls(self, tmp_path: Path) -> None:
        step = FakeStep()
        rt = make_runtime(tmp_path, step_id="build", env={"A": "1"})
        await asyncio.sleep(3)

        result = await step.run({"value": 1}, rt)

        assert result.outputs == {}
        assert [(c.step_id, c.attempt, c.params, c.env, c.time) for c in step.calls] == [
            ("build", 1, {"value": 1}, {"A": "1"}, 3.0)
        ]
        assert step.calls_for("other") == []

    async def test_scripts_attempts_and_repeats_the_last_behavior(self, tmp_path: Path) -> None:
        step = FakeStep().script("build", Fail("exit code 1", exit_code=1), Succeed({"tag": "v1"}))

        with pytest.raises(StepError, match="exit code 1") as caught:
            await step.run({}, make_runtime(tmp_path, step_id="build", attempt=1))
        second = await step.run({}, make_runtime(tmp_path, step_id="build", attempt=2))
        third = await step.run({}, make_runtime(tmp_path, step_id="build", attempt=3))

        assert (caught.value.retryable, caught.value.exit_code) == (True, 1)
        assert second.outputs == third.outputs == {"tag": "v1"}
        assert step.behavior("unscripted", 1) == Succeed()

    async def test_fail_writes_logs_and_carries_outputs(self, tmp_path: Path) -> None:
        log = RecordingLog()
        step = FakeStep(default=Fail("bad", retryable=False, outputs={"n": 1}, logs=("one",)))

        with pytest.raises(StepError) as caught:
            await step.run({}, make_runtime(tmp_path, log=log))

        assert (caught.value.retryable, caught.value.outputs) == (False, {"n": 1})
        assert log.lines == [(LogStream.STDOUT, "one")]

    async def test_succeed_writes_logs(self, tmp_path: Path) -> None:
        log = RecordingLog()

        await FakeStep(default=Succeed(logs=("a", "b"))).run({}, make_runtime(tmp_path, log=log))

        assert log.texts(LogStream.STDOUT) == ["a", "b"]

    async def test_raise_sleep_and_hang(self, tmp_path: Path) -> None:
        rt = make_runtime(tmp_path)
        with pytest.raises(TimeoutError):
            await FakeStep(default=Raise(TimeoutError())).run({}, rt)

        result = await FakeStep(default=Sleep(5, Succeed({"done": True}))).run({}, rt)
        assert (result.outputs, current_loop().time()) == ({"done": True}, 5.0)

        hung = asyncio.create_task(FakeStep(default=Hang()).run({}, rt))
        await asyncio.sleep(60)
        hung.cancel()
        with pytest.raises(asyncio.CancelledError):
            await hung

    def test_check_returns_the_scripted_issues_and_the_schema_accepts_params(self) -> None:
        issue = Issue(Severity.ERROR, "E407", "bad combination")
        step = FakeStep(issues=[issue])

        assert step.check({}, ("steps", 0, "with")) == [issue]
        assert validate(step.schema, {"value": [1], "message": "hi", "data": {}})[1] == []


class TestSubmitters:
    async def test_submitter_records_requests_and_returns_queued_runs(self) -> None:
        submitter = FakeSubmitter()

        first = await submitter.submit(MANUAL)
        second = await submitter.submit(MANUAL)

        assert submitter.requests == [MANUAL, MANUAL]
        assert first.outcome is SubmitOutcome.QUEUED
        assert first.run is not None
        assert second.run is not None
        assert (first.run.id, second.run.id, first.run.status) == (1, 2, RunStatus.QUEUED)

    @pytest.mark.parametrize(
        ("outcome", "status"),
        [(SubmitOutcome.SKIPPED, RunStatus.SKIPPED), (SubmitOutcome.SLOT_TAKEN, None)],
    )
    async def test_submitter_outcomes(
        self, outcome: SubmitOutcome, status: RunStatus | None
    ) -> None:
        submission = await FakeSubmitter(outcome=outcome).submit(MANUAL)

        assert submission.outcome is outcome
        assert (submission.run.status if submission.run else None) == status

    async def test_submitter_errors_and_handlers(self) -> None:
        with pytest.raises(NotFoundError):
            await FakeSubmitter(error=NotFoundError("no such workflow")).submit(MANUAL)
        handled = Submission(SubmitOutcome.DUPLICATE, None)

        assert await FakeSubmitter(handler=lambda _: handled).submit(MANUAL) is handled

    async def test_run_control_records_cancels_and_reruns(self) -> None:
        control = FakeRunControl(stats=ManagerStats(1, 2, False))

        cancelled = await control.cancel(7, force=True)
        rerun = await control.rerun(7)

        assert control.cancels == [(7, CancelReason.USER, True)]
        assert cancelled == CancelOutcome(7, RunStatus.RUNNING, True, True)
        assert control.reruns == [7]
        assert rerun.run is not None
        assert rerun.run.rerun_of == 7
        assert control.stats() == ManagerStats(1, 2, False)

    async def test_run_control_scripted_outcomes_and_errors(self) -> None:
        finished = CancelOutcome(3, RunStatus.SUCCEEDED, False, False)

        assert await FakeRunControl(cancel_outcome=finished).cancel(3) is finished
        failing = FakeRunControl(error=NotFoundError("no run"))
        with pytest.raises(NotFoundError):
            await failing.cancel(1)
        with pytest.raises(NotFoundError):
            await failing.rerun(1)


async def test_workflow_source() -> None:
    first, second = make_workflow(workflow_id="b"), make_workflow(workflow_id="a")
    source = FakeWorkflowSource(first, make_entry(workflow_id="broken"))
    source.add(second)

    assert [entry.workflow_id for entry in source.entries()] == ["a", "b", "broken"]
    assert source.get("b") == make_entry(first)
    assert await source.version(first.digest) == first
    assert await source.version(first.digest, workflow_id="b") == first
    assert await source.version(first.digest, workflow_id="a") is None
    source.remove("b")
    assert source.get("b") is None
    assert await source.version(first.digest) == first


class TestMemoryRunReader:
    RUNS = (
        make_run(1, workflow_id="backup", status=RunStatus.SUCCEEDED),
        make_run(2, workflow_id="deploy", status=RunStatus.FAILED, error="exit code 23"),
        make_run(
            3, workflow_id="backup", status=RunStatus.FAILED, trigger_kind=TriggerKind.SCHEDULE
        ),
        make_run(4, workflow_id="backup", status=RunStatus.RUNNING),
    )

    def reader(self) -> MemoryRunReader:
        reader = MemoryRunReader(*self.RUNS)
        reader.add(make_detail(self.RUNS[0]), [self.line(1, "rsync: Connection refused")])
        return reader

    @staticmethod
    def line(seq: int, text: str, step_id: str = "archive", attempt: int = 1) -> LogLine:
        return LogLine(seq, FIXED_TIME, step_id, attempt, LogStream.STDOUT, text)

    @pytest.mark.parametrize(
        ("flt", "ids"),
        [
            (RunFilter(), [4, 3, 2, 1]),
            (RunFilter(workflow_id="backup"), [4, 3, 1]),
            (RunFilter(statuses=frozenset({RunStatus.FAILED})), [3, 2]),
            (RunFilter(trigger_kind=TriggerKind.SCHEDULE), [3]),
            (RunFilter(text="EXIT CODE"), [2]),
            (RunFilter(text="connection refused"), [1]),
            (RunFilter(before_id=3), [2, 1]),
        ],
    )
    async def test_list_runs_filters(self, flt: RunFilter, ids: list[int]) -> None:
        page = await self.reader().list_runs(flt)

        assert [run.id for run in page.runs] == ids
        assert page.next_before_id is None

    async def test_list_runs_pages(self) -> None:
        page = await self.reader().list_runs(RunFilter(limit=3))

        assert ([run.id for run in page.runs], page.next_before_id) == ([4, 3, 2], 2)

    async def test_logs_filter_and_tail(self) -> None:
        reader = MemoryRunReader(self.RUNS[1])
        reader.add(
            self.RUNS[1],
            [self.line(seq, f"line {seq}", attempt=1 + seq // 3) for seq in range(1, 7)],
        )

        assert [line.seq for line in await reader.logs(2, after_seq=4)] == [5, 6]
        assert [line.seq for line in await reader.logs(2, attempt=2)] == [3, 4, 5]
        assert [line.seq for line in await reader.logs(2, limit=2)] == [1, 2]
        assert [line.seq for line in await reader.logs(2, limit=2, tail=True)] == [5, 6]
        assert await reader.logs(2, step_id="other") == []
        assert await reader.logs(99) == []

    async def test_lookups(self) -> None:
        reader = self.reader()

        assert (await reader.get_run(1)) == make_detail(self.RUNS[0])
        assert await reader.get_run(99) is None
        assert {key: run.id for key, run in (await reader.last_runs()).items()} == {
            "backup": 4,
            "deploy": 2,
        }
        assert await reader.latest_run_id() == 4
        assert await reader.latest_run_id("deploy") == 2
        assert await reader.latest_run_id("missing") is None


async def test_small_fakes_record_what_they_receive() -> None:
    hooks = FakeWebhookHandler(precheck=HookResponse(404, {"error": {"code": "not_found"}}))
    request = HookRequest("POST", {}, {}, b"{}", "application/json")
    state = TriggerState("wf", "wf:webhook:push", TriggerKind.WEBHOOK, "webhook", None, None)
    sink, processes, log = RecordingSink(), RecordingProcesses(), RecordingLog()

    assert hooks.precheck("push") == HookResponse(404, {"error": {"code": "not_found"}})
    assert (await hooks.handle("push", request)).status == 202
    assert (hooks.prechecks, hooks.requests) == (["push"], [("push", request)])
    assert FakeTriggerInfo(state).trigger_states("other") == []
    assert FakeTriggerInfo(state).trigger_states() == [state]
    assert (FakeSecrets({"A": "1"}).get("A"), FakeSecrets(env={"B": "2"}).lookup_env("B")) == (
        "1",
        "2",
    )
    sink.apply(RegistryChange("wf", None, None))
    assert sink.changes == [RegistryChange("wf", None, None)]
    processes.add(10)
    processes.discard(10)
    assert (processes.live, processes.history) == (set(), [("add", 10), ("discard", 10)])
    log.system("starting")
    log.write(LogStream.STDERR, "oops")
    log.add_mask("hunter22")
    assert (log.texts(), log.texts(LogStream.SYSTEM), log.masks) == (
        ["starting", "oops"],
        ["starting"],
        ["hunter22"],
    )


class TestBuilders:
    def test_make_workflow_resolves_implicit_needs_and_ancestors(self) -> None:
        workflow = make_workflow(
            make_step("check"),
            make_step("archive"),
            make_step("record"),
            make_step("alert", needs=["archive"], condition="failure()"),
            make_step("root", needs=[]),
        )

        assert [(s.id, s.position, s.needs, s.implicit_needs) for s in workflow.steps] == [
            ("check", 0, (), True),
            ("archive", 1, ("check",), True),
            ("record", 2, ("archive",), True),
            ("alert", 3, ("archive",), False),
            ("root", 4, (), False),
        ]
        assert workflow.ancestors == {
            "check": frozenset(),
            "archive": frozenset({"check"}),
            "record": frozenset({"archive", "check"}),
            "alert": frozenset({"archive", "check"}),
            "root": frozenset(),
        }
        assert len(make_workflow().steps) == 1

    @pytest.mark.parametrize("name", ["success", "failure", "always", "cancelled"])
    def test_make_condition_builds_status_calls(self, name: str) -> None:
        condition = make_condition(f"{name}()")

        assert condition.uses_status
        assert condition.expression.root == Call(name, (), 0, len(name) + 2)

    def test_make_condition_builds_booleans_and_rejects_the_rest(self) -> None:
        assert make_condition("true").expression.root == Const(True, 0, 4)
        assert not make_condition("false").uses_status
        with pytest.raises(ValueError, match="load the workflow instead"):
            make_condition("steps.a.status == 'failed'")

    def test_make_step_wraps_env_and_outputs_in_templates(self) -> None:
        template = static_template("kept")
        step = make_step(env={"A": "1"}, outputs={"tag": template})

        assert step.env == (("A", static_template("1")),)
        assert step.outputs == (("tag", template),)
        assert static_template("").parts == ()

    def test_make_entry_and_records(self) -> None:
        with pytest.raises(ValueError, match="needs a workflow or a workflow_id"):
            make_entry()
        record = make_step_record("build", status=StepStatus.FAILED, error="exit code 1")
        assert (record.step_id, record.name, record.status, record.outputs) == (
            "build",
            "build",
            StepStatus.FAILED,
            {},
        )
        assert make_run(trigger_kind=TriggerKind.WEBHOOK).trigger == {"kind": "webhook"}
        assert make_run().trigger == {"kind": "manual", "origin": "cli"}

    def test_trigger_builders(self) -> None:
        cron = CronSpec(
            "@hourly",
            frozenset({0}),
            frozenset(range(24)),
            frozenset(range(1, 32)),
            frozenset(range(1, 13)),
            frozenset(range(7)),
            False,
            False,
        )

        assert make_schedule(cron).key == "wf:schedule:@hourly"
        assert make_watch(Path("/in")).paths == (Path("/in"),)
        assert make_webhook("push").signature_header == "x-hub-signature-256"
        token_hook = make_webhook("lab", signature=SignatureMode.TOKEN, timestamp_header="X-Time")
        assert (token_hook.signature_header, token_hook.timestamp_header) == (
            "x-gitlab-token",
            "x-time",
        )

    async def test_make_runtime_creates_the_attempt_files(self, tmp_path: Path) -> None:
        rt = make_runtime(tmp_path, step_id="build", attempt=2, run_id=9)

        assert rt.attempt_dir == tmp_path / "project" / ".treadle" / "tmp" / "run-9" / "build-2"
        assert stat.S_IMODE(rt.attempt_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(rt.output_path.stat().st_mode) == 0o600
        assert rt.output_path.read_text() == ""
        with pytest.raises(AssertionError, match="unexpected HTTP request"):
            await rt.http.get("https://example.com/")


class TestProjectFactory:
    def test_creates_a_project(self, project: ProjectFactory) -> None:
        assert project.config_path.read_text(encoding="utf-8") == 'timezone = "UTC"\n'
        assert project.workflows_dir.is_dir()
        assert stat.S_IMODE(project.env_path.stat().st_mode) == 0o600

    def test_writes_workflows_and_env(self, project: ProjectFactory) -> None:
        assert project.write_workflow("hello", "  steps: []\n").name == "hello.yml"
        assert project.write_workflow("other.yaml", "x: 1\n").read_text() == "x: 1\n"
        assert (project.workflows_dir / "hello.yml").read_text() == "steps: []\n"
        project.write_env({"TOKEN": 'a "quoted"\\value\n', "PLAIN": "x"})
        assert project.env_path.read_text() == 'TOKEN="a \\"quoted\\"\\\\value\\n"\nPLAIN="x"\n'

    def test_settings_accept_overrides(self, project: ProjectFactory) -> None:
        settings = project.settings(port=0, state_dir=Path("custom-state"))

        assert settings.server.port == 0
        assert settings.state_dir == project.root.resolve() / "custom-state"


class TestFixtures:
    def test_settings_clock_and_rng(
        self, settings: Settings, clock: FakeClock, rng: random.Random
    ) -> None:
        assert settings.timezone == "UTC"
        assert clock.now() == FIXED_TIME
        assert rng.random() == random.Random(1234).random()

    def test_fake_steps(self, fake_steps: FakeSteps) -> None:
        assert fake_steps.registry.get("fake") is fake_steps.step

    async def test_mock_http(self, mock_http: Callable[..., httpx.AsyncClient]) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        async with mock_http(handler) as client:
            response = await client.get("https://example.net/healthz")

        assert response.json() == {"ok": True}
        assert seen[0].headers["user-agent"] == f"treadle/{__version__}"
        assert client.max_redirects == 5

    async def test_mock_http_keeps_no_cookie(
        self, mock_http: Callable[..., httpx.AsyncClient]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Set-Cookie": "session=s1; Path=/"})

        async with mock_http(handler) as client:
            await client.get("https://example.net/login")
            response = await client.get("https://example.net/home")

        assert "cookie" not in response.request.headers
        assert len(client.cookies.jar) == 0

    def test_secret_snapshot(self, secret_snapshot: Callable[..., SecretSnapshot]) -> None:
        snapshot = secret_snapshot(NTFY_URL="https://ntfy.sh/x", environ={"HOME": "/h"})

        assert snapshot.secrets == {"NTFY_URL": "https://ntfy.sh/x"}
        assert snapshot.dotenv == {"NTFY_URL": "https://ntfy.sh/x"}
        assert snapshot.environ == {"HOME": "/h"}
        assert secret_snapshot(A="1", dotenv={}).dotenv == {}
