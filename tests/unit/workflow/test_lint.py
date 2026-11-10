from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.fakes import FakeStep

from treadle.config import Limits
from treadle.models import Workflow
from treadle.secrets import SecretSnapshot
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.types import Position
from treadle.workflow.lint import lint
from treadle.workflow.loader import WorkflowLoader
from treadle.yamlio import load_yaml

SECRETS = SecretSnapshot(
    secrets={"TOKEN": "long-enough-token", "PIN": "123"},
    dotenv={},
    environ={"STRONG": "0123456789abcdef", "WEAK": "short"},
)


def workflow_from(text: str, *, project_dir: Path = Path("/project")) -> Workflow:
    """Load a valid workflow without lint's machine checks, which the tests run themselves."""
    loader = WorkflowLoader(
        builtin_registry(),
        project_dir=project_dir,
        default_timezone="UTC",
        limits=Limits(),
        environment_checks=False,
    )
    result = loader.load_text(textwrap.dedent(text), project_dir / "workflows" / "wf.yml")
    assert result.workflow is not None, result.issues
    return result.workflow


def warnings(workflow: Workflow, **kwargs: object) -> list[tuple[str, str]]:
    found = lint(workflow, builtin_registry(), **kwargs)  # type: ignore[arg-type]
    return [(issue.code, issue.message) for issue in found]


def script(expression: str) -> Workflow:
    return workflow_from(
        f"""\
        inputs:
          name: {{default: x}}
        steps:
          - id: fetch
            uses: http
            with: {{url: "https://example.net/"}}
          - id: prep
            run: echo
          - id: build
            run: echo "${{{{ {expression} }}}}"
        """
    )


@pytest.mark.parametrize(
    "reference",
    [
        "trigger.body.ref",
        "trigger.headers['x-event']",
        "trigger.query.page",
        "trigger.changes[0].path",
        "trigger.paths[0]",
        "toJSON(trigger)",
        "steps.fetch.outputs.body",
        "steps.fetch.outputs.json.version",
        "steps.fetch.outputs.headers",
        "steps.fetch.outputs.url",
        "toJSON(steps.fetch.outputs)",
        "toJSON(steps.fetch)",
    ],
)
def test_w101_flags_untrusted_data_in_a_run_script(reference: str) -> None:
    assert [code for code, _ in warnings(script(reference))] == ["W101"]


@pytest.mark.parametrize(
    "reference",
    ["inputs.name", "steps.fetch.outputs.status", "steps.prep.outputs.x", "toJSON(steps)"],
)
def test_w102_flags_inputs_and_step_outputs_in_a_run_script(reference: str) -> None:
    assert [code for code, _ in warnings(script(reference))] == ["W102"]


@pytest.mark.parametrize(
    "reference",
    ["trigger.kind", "trigger.hook", "steps.fetch.status", "env.HOME", "run.id", "workflow.id"],
)
def test_trusted_data_in_a_run_script_is_fine(reference: str) -> None:
    assert warnings(script(reference)) == []


def test_untrusted_data_wins_over_inputs_in_one_expression() -> None:
    found = warnings(script("format('{0}{1}', inputs.name, trigger.body.ref)"))

    assert found == [("W101", "`trigger.body.ref` puts untrusted data into the `run` script")]


def test_the_safe_pattern_passes_untrusted_data_through_env() -> None:
    workflow = workflow_from(
        """\
        steps:
          - id: good
            env:
              REF: ${{ trigger.body.ref }}
            run: echo "Deploying $REF"
        """
    )

    assert warnings(workflow) == []


def test_w103_flags_untrusted_data_in_paths_and_urls() -> None:
    workflow = workflow_from(
        """\
        steps:
          - id: fetch
            uses: http
            with:
              url: https://example.net/${{ trigger.body.page }}
          - id: copy
            uses: file
            with:
              action: copy
              source: ${{ steps.fetch.outputs.body }}
              path: ~/inbox/${{ trigger.changes[0].rel }}
          - id: build
            uses: shell
            with:
              argv: [make]
              cwd: ${{ trigger.query.dir }}
        """
    )

    assert warnings(workflow) == [
        ("W103", "`trigger.body.page` puts untrusted data into `url`"),
        ("W103", "`trigger.changes.0.rel` puts untrusted data into `path`"),
        ("W103", "`steps.fetch.outputs.body` puts untrusted data into `source`"),
        ("W103", "`trigger.query.dir` puts untrusted data into `cwd`"),
    ]


def test_inputs_in_paths_and_urls_are_fine() -> None:
    workflow = workflow_from(
        """\
        inputs:
          page: {default: index}
        steps:
          - id: fetch
            uses: http
            with:
              url: https://example.net/${{ inputs.page }}
              headers: {X-Body: "${{ trigger.body.x }}"}
        """
    )

    assert warnings(workflow) == []


def test_w104_flags_secrets_in_argv_only() -> None:
    workflow = workflow_from(
        """\
        steps:
          - id: upload
            uses: shell
            env:
              TOKEN: ${{ secrets.TOKEN }}
            with:
              argv: [curl, -u, "${{ secrets.TOKEN }}", "${{ toJSON(secrets) }}"]
          - id: script
            run: curl -u "${{ secrets.TOKEN }}" https://example.net/
        """
    )

    assert warnings(workflow) == [
        ("W104", "the secret `secrets.TOKEN` is in `argv`, where process listings can show it"),
        ("W104", "the secret `secrets` is in `argv`, where process listings can show it"),
    ]


def test_w105_flags_steps_that_outlast_the_run() -> None:
    workflow = workflow_from(
        """\
        timeout: 10m
        steps:
          - {id: quick, run: x, timeout: 5m}
          - {id: slow, run: x, timeout: 1h}
          - {id: inherits, run: x}
          - {id: also, run: x}
        """
    )

    assert warnings(workflow) == [
        ("W105", "the step timeout (1h) is longer than the workflow timeout (10m)"),
        ("W105", "the default step timeout (30m) is longer than the workflow timeout (10m)"),
    ]
    assert [issue.path for issue in lint(workflow, builtin_registry())] == [
        ("steps", 1, "timeout"),
        ("timeout",),
    ]


def test_w105_points_at_a_written_default_timeout() -> None:
    workflow = workflow_from("defaults: {timeout: 3h}\nsteps:\n  - {id: a, run: x}\n")

    assert [issue.path for issue in lint(workflow, builtin_registry())] == [("defaults", "timeout")]


def test_w105_ignores_a_default_that_no_step_uses() -> None:
    workflow = workflow_from("timeout: 10m\nsteps:\n  - {id: a, run: x, timeout: 1m}\n")

    assert warnings(workflow) == []


def test_w106_flags_unknown_outputs_of_fixed_output_steps() -> None:
    workflow = workflow_from(
        """\
        steps:
          - id: probe
            uses: http
            with: {url: "https://example.net/"}
            outputs:
              healthy: ${{ result.status == 200 && result.statuscode }}
          - id: note
            uses: log
            with: {message: hi}
          - id: script
            run: echo
          - id: report
            uses: log
            with:
              message: >-
                ${{ steps.probe.outputs.healthy }} ${{ steps.probe.outputs.stauts }}
                ${{ steps.probe.outputs.zzz }} ${{ steps.note.outputs.anything }}
                ${{ steps.script.outputs.anything }}
        """
    )

    assert [(issue.message, issue.hint) for issue in lint(workflow, builtin_registry())] == [
        ("step 'probe' has no output 'statuscode'", "did you mean 'status'?"),
        ("step 'probe' has no output 'stauts'", "did you mean 'status'?"),
        (
            "step 'probe' has no output 'zzz'",
            "its outputs are: body, elapsed_ms, headers, healthy, json, status, url",
        ),
        ("step 'note' has no output 'anything'", "it has no outputs"),
    ]


def test_w106_needs_a_known_step_type() -> None:
    step = FakeStep(output_names=frozenset({"value"}))
    registry = StepRegistry([step])
    loader = WorkflowLoader(
        registry, project_dir=Path("/project"), default_timezone="UTC", limits=Limits()
    )
    text = (
        "steps:\n  - {id: a, uses: fake}\n  - id: b\n    uses: fake\n"
        "    with: {message: '${{ steps.a.outputs.other }}'}\n"
    )
    result = loader.load_text(text, Path("/project/workflows/wf.yml"))

    assert result.workflow is not None
    assert [issue.code for issue in result.issues] == ["W106"]
    assert lint(result.workflow, StepRegistry()) == []


def test_custom_code_sinks_get_script_warnings() -> None:
    step = FakeStep(taint_sinks={"message": "code"})
    loader = WorkflowLoader(
        StepRegistry([step]), project_dir=Path("/project"), default_timezone="UTC", limits=Limits()
    )
    text = "steps:\n  - id: a\n    uses: fake\n    with: {message: '${{ trigger.body.x }}'}\n"
    result = loader.load_text(text, Path("/project/workflows/wf.yml"))

    assert [(issue.code, issue.message) for issue in result.issues] == [
        ("W101", "`trigger.body.x` puts untrusted data into the `message` script")
    ]


SECRET_USES = """\
env:
  A: ${{ secrets.MISSING }}
steps:
  - id: a
    env:
      B: ${{ secrets.MISSING }}
      C: ${{ secrets.PIN }}
      D: ${{ secrets.TOKEN }}
    run: echo
"""


def test_w201_and_w202_check_each_secret_once() -> None:
    assert warnings(workflow_from(SECRET_USES), secrets=SECRETS) == [
        ("W201", "secret 'MISSING' isn't defined"),
        ("W202", "secret 'PIN' is shorter than 4 characters, so masking ignores it"),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [{"secrets": SECRETS, "environment_checks": False}, {}],
    ids=["checks-off", "no-secrets"],
)
def test_secret_checks_need_secrets_and_environment_checks(kwargs: dict[str, object]) -> None:
    assert warnings(workflow_from(SECRET_USES), **kwargs) == []


def test_w301_and_w302_check_watch_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "workflows").mkdir(parents=True)
    (project / "inbox").mkdir()
    workflow = workflow_from(
        """\
        triggers:
          watch:
            - paths: [inbox, missing]
            - paths: [., workflows]
        steps:
          - {id: a, run: x}
        """,
        project_dir=project,
    )
    found = warnings(workflow, state_dir=project / ".treadle", workflows_dir=project / "workflows")

    assert found == [
        ("W301", f"the watch path {project / 'missing'} doesn't exist"),
        (
            "W302",
            f"the watch path {project} contains the state and workflows directories",
        ),
        ("W302", f"the watch path {project / 'workflows'} contains the workflows directory"),
    ]
    assert warnings(workflow) == [("W301", f"the watch path {project / 'missing'} doesn't exist")]
    assert warnings(workflow, environment_checks=False, state_dir=project) == []


def test_w311_and_w312_check_webhook_secrets() -> None:
    workflow = workflow_from(
        """\
        triggers:
          webhook:
            - {name: unset, secret_env: UNSET}
            - {name: weak, secret_env: WEAK}
            - {name: strong, secret_env: STRONG}
        steps:
          - {id: a, run: x}
        """
    )

    assert warnings(workflow, secrets=SECRETS) == [
        ("W311", "UNSET isn't set, so the webhook 'unset' is disabled"),
        ("W312", "WEAK is shorter than 16 characters, so the webhook 'weak' is disabled"),
    ]
    assert warnings(workflow) == []
    assert warnings(workflow, secrets=SECRETS, environment_checks=False) == []


def test_a_document_gives_each_warning_its_position() -> None:
    workflow = workflow_from(
        """\
        steps:
          - id: a
            run: |
              echo start
              echo ${{ inputs.name }}
            timeout: 3h
        inputs:
          name: {default: x}
        """
    )
    document, _ = load_yaml(workflow.source)
    found = lint(workflow, builtin_registry(), document=document)

    assert [(issue.code, issue.path, issue.position) for issue in found] == [
        ("W102", ("steps", 0, "run"), Position(5, 16)),
        ("W105", ("steps", 0, "timeout"), Position(6, 14)),
    ]
    assert [issue.position for issue in lint(workflow, builtin_registry())] == [None, None]
    assert [issue.path[:3] for issue in lint(workflow, builtin_registry())] == [
        ("steps", 0, "with"),
        ("steps", 0, "timeout"),
    ]


def test_lint_returns_warnings_from_the_loader_too(
    load_workflow: Callable[..., Workflow],
) -> None:
    workflow = load_workflow("steps:\n  - id: a\n    run: echo ${{ trigger.body }}\n")

    assert warnings(workflow) == [
        ("W101", "`trigger.body` puts untrusted data into the `run` script")
    ]


def test_argv_without_secrets_is_fine() -> None:
    workflow = workflow_from(
        """\
        inputs:
          name: {default: x}
        steps:
          - id: greet
            uses: shell
            with:
              argv: [echo, "${{ inputs.name }}"]
        """
    )

    assert warnings(workflow) == []


def test_a_watch_path_in_a_symlink_loop_never_contains_anything(tmp_path: Path) -> None:
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    workflow = workflow_from(
        "triggers:\n  watch: a/inner\nsteps:\n  - {id: a, run: x}\n", project_dir=tmp_path
    )

    assert warnings(workflow, state_dir=tmp_path / "a" / "inner" / ".treadle") == [
        ("W301", f"the watch path {tmp_path / 'a' / 'inner'} doesn't exist")
    ]
