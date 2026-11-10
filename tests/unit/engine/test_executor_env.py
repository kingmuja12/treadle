"""Step environments, expression contexts, and what each attempt's runtime holds."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from tests.support.builders import make_step, make_workflow
from tests.support.fakes import Fail, FakeStep
from tests.unit.engine.conftest import API_TOKEN
from tests.unit.engine.helpers import Engine, step_of

from treadle.config import RunSettings
from treadle.models import Defaults, RetryPolicy, ShellSpec, StepStatus, Workflow
from treadle.schema import Obj
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepResult, StepRuntime
from treadle.types import Issue, JSONValue, YamlPath

INHERITED = {"PATH": "/usr/bin:/bin", "HOME": "/home/ada", "LANG": "C.UTF-8"}
ZSH: ShellSpec = ("zsh", "{0}")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ProbeStep:
    """A step type that records each runtime and inspects the attempt's files as it runs."""

    name = "probe"
    output_names: ClassVar[frozenset[str] | None] = None
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    def __init__(self) -> None:
        self.runtimes: list[StepRuntime] = []
        self.seen: dict[str, object] = {}

    @property
    def schema(self) -> Obj:
        return Obj("probe step", {})

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        self.runtimes.append(rt)
        self.seen = inspect(rt)
        return StepResult()


def inspect(rt: StepRuntime) -> dict[str, object]:
    """Return the modes of the run's files, the output file's size, and the event payload."""
    shared, event = Path(rt.env["TREADLE_RUN_TMP"]), Path(rt.env["TREADLE_EVENT_PATH"])
    return {
        "modes": {
            "run": mode(shared.parent),
            "shared": mode(shared),
            "attempt": mode(rt.attempt_dir),
            "output": mode(rt.output_path),
            "event": mode(event),
        },
        "output_size": rt.output_path.stat().st_size,
        "event": json.loads(event.read_text(encoding="utf-8")),
    }


async def test_the_step_environment_layers_process_workflow_step_and_treadle(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        env:
          WF: from-workflow
          SHARED: workflow
          HOME: /overridden
        steps:
          - id: build
            uses: fake
            env:
              STEP: from-step
              SHARED: step
        """
    )

    detail = await engine.execute(workflow)

    env = fake.calls_for("build")[0].env
    treadle = {name: value for name, value in env.items() if name.startswith("TREADLE_")}
    assert {name: value for name, value in env.items() if name not in treadle} == {
        **INHERITED,
        "HOME": "/overridden",
        "WF": "from-workflow",
        "SHARED": "step",
        "STEP": "from-step",
    }
    run_dir = engine.settings.tmp_dir / f"run-{detail.run.id}"
    assert treadle == {
        "TREADLE_RUN_ID": str(detail.run.id),
        "TREADLE_RUN_ATTEMPT": "1",
        "TREADLE_WORKFLOW": "wf",
        "TREADLE_STEP_ID": "build",
        "TREADLE_STEP_ATTEMPT": "1",
        "TREADLE_TRIGGER": "manual",
        "TREADLE_OUTPUT": str(run_dir / "build-1" / "output"),
        "TREADLE_RUN_TMP": str(run_dir / "shared"),
        "TREADLE_EVENT_PATH": str(run_dir / "event.json"),
        "TREADLE_PROJECT_DIR": str(engine.settings.project_dir),
    }


async def test_treadle_variables_never_leak_from_the_process(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: a
            uses: fake
            with:
              data:
                token: ${{ env.TREADLE_TOKEN }}
                secret: ${{ env.TREADLE_SECRET_API_TOKEN }}
                hook: ${{ env.HOOK_SECRET }}
                path: ${{ env.PATH }}
        """
    )

    await engine.execute(workflow)

    call = fake.calls_for("a")[0]
    assert call.params == {
        "data": {"token": None, "secret": None, "hook": None, "path": "/usr/bin:/bin"}
    }
    assert {"TREADLE_TOKEN", "TREADLE_SECRET_API_TOKEN", "TREADLE_DEBUG", "HOOK_SECRET"}.isdisjoint(
        call.env
    )


async def test_workflow_env_entries_see_only_the_process_environment(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        env:
          FIRST: one
          SECOND: "[${{ env.FIRST }}]"
          HOME_COPY: ${{ env.HOME }}
          COUNT: 3
        steps:
          - id: a
            uses: fake
        """
    )

    await engine.execute(workflow)

    env = fake.calls_for("a")[0].env
    assert (env["FIRST"], env["SECOND"], env["HOME_COPY"], env["COUNT"]) == (
        "one",
        "[]",
        "/home/ada",
        "3",
    )


async def test_each_field_sees_the_env_of_its_layer(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        env:
          WF: w
        steps:
          - id: a
            uses: fake
            if: env.WF == 'w' && env.STEP == null
            env:
              STEP: "${{ env.WF }}-s"
              SEES_STEP: "${{ env.STEP }}"
            with:
              value: "${{ env.WF }} ${{ env.STEP }}"
        """
    )

    detail = await engine.execute(workflow)

    assert step_of(detail, "a").status is StepStatus.SUCCEEDED
    call = fake.calls_for("a")[0]
    assert (call.env["STEP"], call.env["SEES_STEP"]) == ("w-s", "")
    assert call.params == {"value": "w w-s"}


async def test_secrets_reach_a_step_only_through_explicit_templates(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        steps:
          - id: a
            uses: fake
            env:
              TOKEN: ${{ secrets.API_TOKEN }}
            with:
              message: "Bearer ${{ secrets.API_TOKEN }}"
          - id: b
            uses: fake
        """
    )

    await engine.execute(workflow)

    assert fake.calls_for("a")[0].env["TOKEN"] == API_TOKEN
    assert fake.calls_for("a")[0].params == {"message": f"Bearer {API_TOKEN}"}
    assert API_TOKEN not in fake.calls_for("b")[0].env.values()


async def test_templates_see_the_run_workflow_trigger_and_inputs(
    engine: Engine, fake: FakeStep, load_workflow: Callable[..., Workflow]
) -> None:
    workflow = load_workflow(
        """
        name: Context check
        inputs:
          who: {type: string}
        steps:
          - id: a
            uses: fake
            with:
              data:
                run: ${{ run }}
                workflow: ${{ workflow }}
                ref: ${{ trigger.body.ref }}
                who: ${{ inputs.who }}
        """
    )
    trigger: dict[str, JSONValue] = {"kind": "webhook", "body": {"ref": "refs/heads/main"}}

    detail = await engine.execute(workflow, inputs={"who": "Ada"}, trigger=trigger)

    data = fake.calls_for("a")[0].params["data"]
    assert isinstance(data, dict)
    assert data["run"] == {
        "id": detail.run.id,
        "attempt": 1,
        "started_at": "2026-11-09T12:00:00.000Z",
        "workflow": "wf",
        "trigger": "manual",
        "rerun_of": None,
    }
    assert data["workflow"] == {
        "id": "wf",
        "name": "Context check",
        "description": "",
        "path": "workflows/wf.yml",
        "digest": workflow.digest[:12],
    }
    assert (data["ref"], data["who"]) == ("refs/heads/main", "Ada")


async def test_each_attempt_gets_fresh_private_files(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    probe = ProbeStep()
    engine = make_engine(probe)
    trigger: dict[str, JSONValue] = {"kind": "webhook", "body": {"token": API_TOKEN, "n": 1}}
    workflow = make_workflow(make_step("look", uses="probe"))

    detail = await engine.execute(workflow, trigger=trigger)

    assert step_of(detail, "look").status is StepStatus.SUCCEEDED
    assert probe.seen == {
        "modes": {"run": 0o700, "shared": 0o700, "attempt": 0o700, "output": 0o600, "event": 0o600},
        "output_size": 0,
        "event": {"kind": "webhook", "body": {"token": "***", "n": 1}},
    }
    assert not (engine.settings.tmp_dir / f"run-{detail.run.id}").exists()


async def test_the_event_file_masks_a_number_that_holds_a_secret(
    make_engine: Callable[..., Engine], secret_snapshot: Callable[..., SecretSnapshot]
) -> None:
    probe = ProbeStep()
    engine = make_engine(probe)
    engine.secrets.snapshot = secret_snapshot(CHAT_ID="834512907")
    trigger: dict[str, JSONValue] = {"kind": "webhook", "body": {"chat": 834512907, "n": 1}}

    await engine.execute(make_workflow(make_step("look", uses="probe")), trigger=trigger)

    assert probe.seen["event"] == {"kind": "webhook", "body": {"chat": "***", "n": 1}}


async def test_the_runtime_carries_the_settings_and_workflow_defaults(
    make_engine: Callable[..., Engine],
) -> None:
    probe = ProbeStep()
    engine = make_engine(probe, runs=RunSettings(kill_grace=1.5))
    settings = engine.settings
    defaults = Defaults(shell=ZSH, cwd="sub/dir")
    workflow = make_workflow(make_step("look", uses="probe"), defaults=defaults)

    await engine.execute(workflow)

    [rt] = probe.runtimes
    assert (rt.step_id, rt.attempt, rt.workflow_id) == ("look", 1, "wf")
    assert rt.attempt_dir == settings.tmp_dir / f"run-{rt.run_id}" / "look-1"
    assert rt.output_path == rt.attempt_dir / "output"
    assert (rt.default_shell, rt.default_cwd) == (("zsh", "{0}"), settings.project_dir / "sub/dir")
    assert (rt.project_dir, rt.state_dir) == (settings.project_dir, settings.state_dir)
    assert (rt.file_roots, rt.protected_paths) == (
        settings.file_roots(),
        settings.protected_paths(),
    )
    assert (rt.kill_grace, rt.limits) == (1.5, settings.limits)
    assert rt.http is engine.deps.http


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [("~/work", Path("~/work").expanduser()), ("/srv/app", Path("/srv/app"))],
)
async def test_the_default_cwd_expands_home_and_keeps_absolute_paths(
    make_engine: Callable[..., Engine], cwd: str, expected: Path
) -> None:
    probe = ProbeStep()
    engine = make_engine(probe)

    await engine.execute(make_workflow(make_step("look", uses="probe"), defaults=Defaults(cwd=cwd)))

    assert probe.runtimes[0].default_cwd == expected


async def test_retries_get_their_own_attempt_directories(
    make_engine: Callable[..., Engine], fake: FakeStep
) -> None:
    engine = make_engine()
    fake.script("a", Fail(), Fail(), Fail())

    await engine.execute(make_workflow(make_step("a", retry=RetryPolicy(attempts=3, delay=0))))

    outputs = [call.env["TREADLE_OUTPUT"] for call in fake.calls_for("a")]
    assert [Path(output).parent.name for output in outputs] == ["a-1", "a-2", "a-3"]
    assert [call.env["TREADLE_STEP_ATTEMPT"] for call in fake.calls_for("a")] == ["1", "2", "3"]
