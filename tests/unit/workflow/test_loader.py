from __future__ import annotations

import hashlib
import json
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.support.fakes import FakeStep

from treadle.config import Limits
from treadle.expr.ast import Expression, Template
from treadle.models import (
    CatchUp,
    Concurrency,
    ConcurrencyPolicy,
    Defaults,
    InputSpec,
    InputType,
    RetryPolicy,
    ScheduleTrigger,
    SignatureMode,
    WatchEvent,
    WatchTrigger,
    WebhookTrigger,
    Workflow,
)
from treadle.schema import Field, Obj, Str
from treadle.steps.base import StepRegistry
from treadle.steps.builtin import builtin_registry
from treadle.types import Issue, Position, Severity
from treadle.workflow.loader import (
    LoadResult,
    WorkflowLoader,
    cross_check,
    discover,
    workflow_id_for,
)

EXAMPLES = Path(__file__).parent / "examples"
PROJECT = Path("/project")


def make_loader(steps: StepRegistry | None = None, **kwargs: object) -> WorkflowLoader:
    options: dict[str, object] = {
        "project_dir": PROJECT,
        "default_timezone": "UTC",
        "limits": Limits(),
        "environment_checks": False,
    }
    options.update(kwargs)
    return WorkflowLoader(steps or builtin_registry(), **options)  # type: ignore[arg-type]


def load(text: str, name: str = "wf.yml", loader: WorkflowLoader | None = None) -> LoadResult:
    return (loader or make_loader()).load_text(textwrap.dedent(text), PROJECT / "workflows" / name)


def codes(result: LoadResult) -> list[tuple[str, str]]:
    return [(issue.code, issue.message) for issue in result.issues]


def only(result: LoadResult) -> Workflow:
    assert result.issues == ()
    assert result.workflow is not None
    return result.workflow


def schedules(workflow: Workflow) -> list[ScheduleTrigger]:
    return [trigger for trigger in workflow.triggers if isinstance(trigger, ScheduleTrigger)]


def watches(workflow: Workflow) -> list[WatchTrigger]:
    return [trigger for trigger in workflow.triggers if isinstance(trigger, WatchTrigger)]


@pytest.mark.parametrize("name", ["nightly-backup.yml", "deploy-on-push.yml", "sort-downloads.yml"])
def test_the_example_workflows_load_without_issues(name: str) -> None:
    result = make_loader().load_file(EXAMPLES / name)

    assert result.issues == ()
    assert result.ok
    assert result.workflow_id == name.removesuffix(".yml")


def test_omitted_keys_take_their_defaults() -> None:
    workflow = only(load("steps:\n  - id: greet\n    run: echo hi\n"))

    assert (workflow.id, workflow.name, workflow.description) == ("wf", "wf", "")
    assert (workflow.inputs, workflow.env, workflow.triggers) == ((), (), ())
    assert workflow.concurrency == Concurrency()
    assert workflow.defaults == Defaults()
    assert (workflow.timeout, workflow.cleanup_timeout, workflow.max_parallel) == (7200, 300, 4)
    step = workflow.steps[0]
    assert (step.id, step.name, step.position, step.uses) == ("greet", "greet", 0, "shell")
    assert step.params == {"run": "echo hi", "capture_stdout": False, "success_codes": [0]}
    assert (step.needs, step.implicit_needs) == ((), True)
    assert (step.condition.source, step.condition.uses_status) == ("success()", True)
    assert (step.env, step.outputs) == ((), ())
    assert (step.retry, step.timeout, step.continue_on_error) == (RetryPolicy(), 1800.0, False)
    assert step.location == Position(2, 5)
    assert workflow.ancestors == {"greet": frozenset()}


def test_written_keys_are_normalized() -> None:
    workflow = only(
        load(
            """\
            name: Release
            description: Ship it.
            concurrency: {policy: replace, max_queued: 3}
            timeout: 1h30m
            cleanup_timeout: 90
            max_parallel: 2
            defaults:
              timeout: 20m
              retry: {attempts: 4, delay: 10s, backoff: 1.5, max_delay: 1m, jitter: 0.5}
              shell: [zsh, -e, "{0}"]
              cwd: ~/src
            steps:
              - id: fetch
                name: Fetch the page
                uses: http
                timeout: 5m
                continue_on_error: true
                with:
                  method: post
                  url: https://example.net/
                  timeout: "10s"
                  max_response_bytes: 2MiB
            """
        )
    )

    assert (workflow.name, workflow.description) == ("Release", "Ship it.")
    assert workflow.concurrency == Concurrency(ConcurrencyPolicy.REPLACE, 3)
    assert (workflow.timeout, workflow.cleanup_timeout, workflow.max_parallel) == (5400, 90, 2)
    assert workflow.defaults == Defaults(  # noqa: S604 - a Defaults field, not a process
        timeout=1200.0,
        retry=RetryPolicy(attempts=4, delay=10.0, backoff=1.5, max_delay=60.0, jitter=0.5),
        shell=("zsh", "-e", "{0}"),
        cwd="~/src",
    )
    step = workflow.steps[0]
    assert (step.name, step.timeout, step.continue_on_error) == ("Fetch the page", 300.0, True)
    assert step.retry == workflow.defaults.retry
    assert step.params == {
        "method": "POST",
        "url": "https://example.net/",
        "headers": {},
        "query": {},
        "timeout": 10.0,
        "expect": ["2xx"],
        "follow_redirects": False,
        "max_response_bytes": 2 << 20,
    }


def test_scalar_shorthands_set_the_primary_field() -> None:
    workflow = only(
        load(
            """\
            concurrency: skip
            defaults:
              retry: 3
            triggers:
              schedule: "0 2 * * *"
              watch: inbox
              webhook: {name: hook, secret_env: HOOK_SECRET}
            steps:
              - id: build
                run: make
                retry: 5
              - id: test
                needs: build
                run: make test
            """
        )
    )

    assert workflow.concurrency.policy is ConcurrencyPolicy.SKIP
    assert workflow.defaults.retry == RetryPolicy(attempts=3)
    assert workflow.steps[0].retry == RetryPolicy(attempts=5)
    assert workflow.steps[1].retry == RetryPolicy(attempts=3)
    assert (workflow.steps[1].needs, workflow.steps[1].implicit_needs) == (("build",), False)
    assert schedules(workflow)[0].cron.source == "0 2 * * *"
    assert watches(workflow)[0].paths == (PROJECT / "inbox",)
    assert [hook.name for hook in workflow.webhooks()] == ["hook"]


def test_a_step_retry_overrides_the_defaults_key_by_key() -> None:
    workflow = only(
        load(
            """\
            defaults:
              retry: {attempts: 3, delay: 30s, max_delay: 10m}
            steps:
              - id: a
                run: echo
                retry: {delay: 1m, jitter: 0.2}
            """
        )
    )

    assert workflow.steps[0].retry == RetryPolicy(
        attempts=3, delay=60.0, backoff=2.0, max_delay=600.0, jitter=0.2
    )


def test_the_run_shorthand_makes_a_shell_step() -> None:
    workflow = only(
        load(
            """\
            steps:
              - id: a
                run: echo ${{ run.id }}
                with: {cwd: /tmp, capture_stdout: true}
              - id: b
                uses: shell
                run: echo b
            """
        )
    )
    first, second = workflow.steps

    assert first.uses == second.uses == "shell"
    run = first.params["run"]
    assert isinstance(run, Template)
    assert run.source == "echo ${{ run.id }}"
    assert (first.params["cwd"], first.params["capture_stdout"]) == ("/tmp", True)
    assert second.params["run"] == "echo b"


def test_steps_depend_on_the_previous_step_unless_needs_is_written() -> None:
    workflow = only(
        load(
            """\
            steps:
              - {id: a, run: echo}
              - {id: b, run: echo}
              - {id: c, run: echo, needs: []}
              - {id: d, run: echo, needs: [a, c]}
              - {id: e, run: echo}
            """
        )
    )

    assert [(step.needs, step.implicit_needs) for step in workflow.steps] == [
        ((), True),
        (("a",), True),
        ((), False),
        (("a", "c"), False),
        (("d",), True),
    ]
    assert workflow.ancestors == {
        "a": frozenset(),
        "b": frozenset({"a"}),
        "c": frozenset(),
        "d": frozenset({"a", "c"}),
        "e": frozenset({"a", "c", "d"}),
    }


@pytest.mark.parametrize(
    ("written", "source", "expression", "uses_status"),
    [
        pytest.param(None, "success()", "success()", True, id="default"),
        pytest.param("failure()", "failure()", "failure()", True, id="status-function"),
        pytest.param("inputs.go", "inputs.go", "inputs.go", False, id="bare-expression"),
        pytest.param(
            "'${{ inputs.go }}'", "${{ inputs.go }}", " inputs.go ", False, id="unwrapped"
        ),
        pytest.param("true", "true", "true", False, id="boolean"),
    ],
)
def test_conditions(written: str | None, source: str, expression: str, uses_status: bool) -> None:
    condition = f"    if: {written}\n" if written else ""
    text = f"inputs:\n  go: {{type: boolean}}\nsteps:\n  - id: a\n    uses: log\n{condition}"
    text += "    with: {message: hi}\n"
    step = only(load(text)).steps[0]

    assert step.condition.source == source
    assert step.condition.expression.source == expression
    assert step.condition.uses_status is uses_status


def test_env_values_become_templates_with_display_strings() -> None:
    workflow = only(
        load(
            """\
            env:
              COUNT: 3
              RATIO: 2.0
              HALF: 0.5
              ENABLED: true
              PLAIN: text
            steps:
              - id: a
                env:
                  NAME: hello ${{ env.PLAIN }}
                run: echo
            """
        )
    )

    assert [(name, template.source) for name, template in workflow.env] == [
        ("COUNT", "3"),
        ("RATIO", "2"),
        ("HALF", "0.5"),
        ("ENABLED", "true"),
        ("PLAIN", "text"),
    ]
    assert all(template.is_static for _, template in workflow.env)
    name, template = workflow.steps[0].env[0]
    assert name == "NAME"
    assert isinstance(template.parts[1], Expression)


def test_outputs_keep_declaration_order() -> None:
    step = only(
        load(
            """\
            steps:
              - id: a
                run: echo
                outputs:
                  second: ${{ result.exit_code }}
                  first: fixed
            """
        )
    ).steps[0]

    assert [(name, template.source) for name, template in step.outputs] == [
        ("second", "${{ result.exit_code }}"),
        ("first", "fixed"),
    ]


def test_templates_compile_at_any_depth_of_with() -> None:
    step = only(
        load(
            """\
            steps:
              - id: a
                uses: http
                with:
                  url: https://example.net/
                  json:
                    items: [plain, "${{ run.id }}"]
                    nested: {value: "${{ run.attempt }}"}
            """
        )
    ).steps[0]
    body = step.params["json"]

    assert isinstance(body, dict)
    assert body["items"][0] == "plain"  # type: ignore[index]
    assert isinstance(body["items"][1], Template)  # type: ignore[index]
    assert isinstance(body["nested"]["value"], Template)  # type: ignore[index]


def test_inputs_are_built_in_declaration_order() -> None:
    workflow = only(
        load(
            """\
            inputs:
              name:
                description: Who to greet
                default: world
              count:
                type: integer
                default: 5.0
              level:
                type: choice
                options: [low, high]
                default: low
              force:
                type: boolean
                required: true
            steps:
              - {id: a, run: echo}
            """
        )
    )

    assert workflow.inputs == (
        InputSpec("name", InputType.STRING, "Who to greet", False, "world", ()),
        InputSpec("count", InputType.INTEGER, "", False, 5, ()),
        InputSpec("level", InputType.CHOICE, "", False, "low", ("low", "high")),
        InputSpec("force", InputType.BOOLEAN, "", True, None, ()),
    )


SCHEDULES = """\
triggers:
  schedule:
    - {cron: "0 2 * * *"}
    - {cron: "30 4 * * 1", timezone: Europe/London, catch_up: all}
steps:
  - {id: a, run: echo}
"""


def test_schedule_triggers_fill_the_project_time_zone() -> None:
    first, second = schedules(only(load(SCHEDULES)))

    assert (first.timezone, first.catch_up) == ("UTC", CatchUp.LATEST)
    assert (second.timezone, second.catch_up) == ("Europe/London", CatchUp.ALL)
    assert first.key.startswith("wf:schedule:")
    assert len(first.key.removeprefix("wf:schedule:")) == 16
    assert second.cron.minutes == frozenset({30})


@pytest.mark.parametrize(
    ("zone", "hint"),
    [
        ("europe/london", "did you mean 'Europe/London'?"),
        ("utc", "did you mean 'UTC'?"),
        ("Mars/Olympus", "use an IANA name such as Europe/London"),
    ],
)
def test_a_schedule_time_zone_must_be_spelled_as_the_database_spells_it(
    zone: str, hint: str
) -> None:
    result = load(
        f'triggers:\n  schedule: {{cron: "0 2 * * *", timezone: {zone}}}\n'
        "steps:\n  - {id: a, run: x}\n"
    )

    assert [(issue.code, issue.message, issue.hint) for issue in result.issues] == [
        ("E303", f"unknown time zone '{zone}'", hint)
    ]


def test_trigger_keys_stay_stable_when_triggers_are_reordered() -> None:
    reordered = SCHEDULES.replace(
        '    - {cron: "0 2 * * *"}\n'
        '    - {cron: "30 4 * * 1", timezone: Europe/London, catch_up: all}',
        '    - {catch_up: all, timezone: Europe/London, cron: "30 4 * * 1"}\n'
        '    - {cron: "0 2 * * *", timezone: UTC, catch_up: latest}',
    )
    keys = {trigger.key for trigger in only(load(SCHEDULES)).triggers}

    assert reordered != SCHEDULES
    assert {trigger.key for trigger in only(load(reordered)).triggers} == keys


def test_trigger_keys_change_with_any_setting_and_the_workflow_id() -> None:
    base = schedules(only(load(SCHEDULES)))[0].key
    edited = SCHEDULES.replace('{cron: "0 2 * * *"}', '{cron: "0 2 * * *", catch_up: none}')

    assert schedules(only(load(edited)))[0].key != base
    assert schedules(only(load(SCHEDULES, "other.yml")))[0].key.startswith("other:schedule:")


def test_watch_triggers_resolve_paths_and_fill_defaults() -> None:
    workflow = only(
        load(
            """\
            triggers:
              watch:
                - paths: [~/inbox, data, /srv/../mnt/x]
                - paths: drop
                  include: ["*.pdf"]
                  exclude: []
                  recursive: true
                  events: [deleted, created]
                  debounce: 0
                  poll_interval: 500ms
                  initial_scan: true
            steps:
              - {id: a, run: echo}
            """
        )
    )
    first, second = watches(workflow)

    assert first.paths == (Path.home() / "inbox", PROJECT / "data", Path("/mnt/x"))
    assert (first.include, first.recursive, first.initial_scan) == (("*",), False, False)
    assert first.exclude == (".*", "*~", "*.tmp", "*.swp", "*.part", "*.crdownload")
    assert first.events == frozenset(WatchEvent)
    assert (first.debounce, first.poll_interval) == (2.0, 2.0)
    assert (second.include, second.exclude, second.recursive) == (("*.pdf",), (), True)
    assert second.events == frozenset({WatchEvent.CREATED, WatchEvent.DELETED})
    assert (second.debounce, second.poll_interval, second.initial_scan) == (0.0, 0.5, True)
    assert first.key.startswith("wf:watch:")


def test_watch_keys_ignore_the_order_of_events() -> None:
    def key(events: str) -> str:
        text = (
            f"triggers:\n  watch: {{paths: x, events: [{events}]}}\nsteps:\n  - {{id: a, run: x}}\n"
        )
        return watches(only(load(text)))[0].key

    assert key("created, modified") == key("modified, created")
    assert key("created") != key("modified")


def test_webhook_triggers_fill_defaults_by_signature_mode() -> None:
    workflow = only(
        load(
            """\
            triggers:
              webhook:
                - {name: github, secret_env: GITHUB_SECRET}
                - {name: gitlab, secret_env: GITLAB_SECRET, signature: token}
                - name: signed
                  secret_env: SIGNED_SECRET
                  signature_header: X-Signature
                  timestamp_header: X-Signature-Timestamp
                  tolerance: 10m
                  dedupe_window: 0
                  max_body: 64KiB
                  rate_limit: 0
            steps:
              - {id: a, run: echo}
            """
        ),
    )

    assert workflow.webhooks() == (
        WebhookTrigger(
            "wf:webhook:github",
            "github",
            SignatureMode.HMAC_SHA256,
            "GITHUB_SECRET",
            "x-hub-signature-256",
            None,
            300.0,
            86400.0,
            1 << 20,
            60,
        ),
        WebhookTrigger(
            "wf:webhook:gitlab",
            "gitlab",
            SignatureMode.TOKEN,
            "GITLAB_SECRET",
            "x-gitlab-token",
            None,
            300.0,
            86400.0,
            1 << 20,
            60,
        ),
        WebhookTrigger(
            "wf:webhook:signed",
            "signed",
            SignatureMode.HMAC_SHA256,
            "SIGNED_SECRET",
            "x-signature",
            "x-signature-timestamp",
            600.0,
            1200.0,
            64 << 10,
            0,
        ),
    )


def test_the_webhook_body_limit_comes_from_the_limits() -> None:
    loader = make_loader(limits=Limits(webhook_body_bytes=4096))
    text = (
        "triggers:\n  webhook: {name: h, secret_env: S, max_body: 8KiB}\n"
        "steps:\n  - {id: a, run: x}\n"
    )

    assert codes(load(text, loader=loader)) == [("E104", "must be between 1.0 KiB and 4.0 KiB")]
    hook = only(load(text.replace(", max_body: 8KiB", ""), loader=loader)).webhooks()[0]
    assert hook.max_body == 4096


def test_the_digest_covers_the_file_bytes(tmp_path: Path) -> None:
    text = "steps:\r\n  - id: a\r\n    run: echo é\r\n"
    path = tmp_path / "wf.yml"
    path.write_bytes(text.encode())
    from_file = make_loader().load_file(path)
    from_text = make_loader().load_text(text, path)

    assert from_file.workflow is not None
    assert from_file.workflow.digest == hashlib.sha256(text.encode()).hexdigest()
    assert from_text.workflow is not None
    assert from_text.workflow.digest == from_file.workflow.digest
    assert from_file.source == from_file.workflow.source == text
    assert from_file.workflow.path == path


def test_the_workflow_path_is_absolute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("flows").mkdir()
    Path("flows/wf.yml").write_text("steps:\n  - {id: a, run: x}\n", encoding="utf-8")
    result = make_loader().load_file(Path("flows/../flows/wf.yml"))

    assert result.workflow is not None
    assert result.workflow.path == tmp_path.resolve() / "flows" / "wf.yml"
    assert result.path == Path("flows/../flows/wf.yml")


def test_a_large_file_is_rejected_without_reading_it_all(tmp_path: Path) -> None:
    path = tmp_path / "big.yml"
    path.write_bytes(b"#" * ((1 << 20) + 1))
    result = make_loader().load_file(path)

    assert codes(result) == [("E055", "the file is larger than 1 MiB")]
    assert (result.workflow, result.source) == (None, None)


def test_large_text_is_rejected_too() -> None:
    assert codes(load("#" * (1 << 20) + "\n")) == [("E055", "the file is larger than 1 MiB")]


def test_invalid_utf8_is_located_by_line_and_character(tmp_path: Path) -> None:
    path = tmp_path / "wf.yml"
    path.write_bytes("steps:\n  - id: é".encode() + b"\xff\n")
    result = make_loader().load_file(path)

    assert [(issue.code, issue.position) for issue in result.issues] == [("E056", Position(2, 10))]
    assert result.source is None


def test_text_with_a_lone_surrogate_isnt_utf8() -> None:
    assert [issue.code for issue in load("name: \ud800\n").issues] == ["E056"]


def test_an_escape_above_the_unicode_range_is_a_yaml_error() -> None:
    result = load('name: "\\U00110000"\nsteps:\n  - {id: a, run: x}\n')

    assert codes(result) == [
        ("E050", "invalid escape \\U00110000: the code point is above U+10FFFF")
    ]
    assert result.workflow is None


def test_a_workflow_that_json_dumps_wrote_keeps_its_characters() -> None:
    text = json.dumps({"name": "Deploy \U0001f680", "steps": [{"id": "a", "run": "echo \u00e9"}]})

    workflow = only(load(text))

    assert "\\ud83d\\ude80" in text
    assert workflow.name == "Deploy \U0001f680"
    assert workflow.steps[0].params["run"] == "echo \u00e9"


def test_a_nul_escape_in_an_env_value_is_a_yaml_error() -> None:
    result = load('env:\n  TAG: "v1\\0"\nsteps:\n  - {id: a, run: x}\n')

    assert codes(result) == [("E050", "invalid escape: a string can't hold U+0000 (NUL)")]


TOO_LONG_TO_PRINT = "0x" + "f" * 4000


@pytest.mark.parametrize(
    ("text", "path"),
    [
        pytest.param(
            f"env:\n  BIG: {TOO_LONG_TO_PRINT}\nsteps:\n  - {{id: a, run: x}}\n",
            ("env", "BIG"),
            id="env",
        ),
        pytest.param(
            f"inputs:\n  n: {{type: integer, default: {TOO_LONG_TO_PRINT}}}\n"
            "steps:\n  - {id: a, run: x}\n",
            ("inputs", "n", "default"),
            id="input-default",
        ),
        pytest.param(
            "steps:\n  - id: a\n    uses: http\n    with:\n      url: https://example.net/\n"
            f"      json: {{n: {TOO_LONG_TO_PRINT}}}\n",
            ("steps", 0, "with", "json", "n"),
            id="http-json",
        ),
    ],
)
def test_an_integer_too_long_to_print_isnt_a_finite_number(text: str, path: object) -> None:
    result = load(text)

    assert [(issue.code, issue.message, issue.path) for issue in result.issues] == [
        ("E104", "must be a finite number", path)
    ]


def test_a_long_hex_integer_that_prints_is_an_env_value() -> None:
    workflow = only(load(f"env:\n  BIG: 0x{'f' * 20}\nsteps:\n  - {{id: a, run: x}}\n"))

    assert [(name, template.source) for name, template in workflow.env] == [
        ("BIG", str(16**20 - 1))
    ]


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        make_loader().load_file(tmp_path / "missing.yml")


def test_a_bad_file_name_keeps_the_other_checks_but_no_workflow() -> None:
    result = load("steps:\n  - {id: a, run: x, timeout: 0s}\n", "Not Valid.yml")

    assert [issue.code for issue in result.issues] == ["E057", "E104"]
    assert (result.workflow_id, result.workflow, result.ok) == (None, None, False)


def test_issues_are_sorted_by_position_with_file_issues_first() -> None:
    result = load("steps:\n  - {id: a, uses: nope}\nname: ''\n", "Bad.yml")

    assert [issue.code for issue in result.issues] == ["E057", "E401", "E104"]


def test_at_most_50_issues_are_kept() -> None:
    steps = "".join(f"  - {{id: s{index}, uses: nope}}\n" for index in range(60))
    result = load(f"steps:\n{steps}")

    assert len(result.issues) == 50
    assert result.issues[-1].position == Position(51, 21)


def test_e210_replaces_the_schema_error_at_the_same_path() -> None:
    result = load("timeout: ${{ inputs.t }}\nsteps:\n  - {id: a, run: x}\n")

    assert codes(result) == [("E210", "expressions aren't allowed here")]


def test_warnings_alone_keep_the_workflow() -> None:
    result = load("timeout: 10s\nsteps:\n  - {id: a, run: x}\n")

    assert [issue.code for issue in result.issues] == ["W105"]
    assert result.ok
    assert result.workflow is not None


def test_ok_needs_a_workflow_and_no_errors() -> None:
    warning = Issue(Severity.WARNING, "W105", "slow")
    error = Issue(Severity.ERROR, "E104", "bad")
    workflow = only(load("steps:\n  - {id: a, run: x}\n"))

    assert LoadResult(PROJECT, "wf", workflow, "", (warning,)).ok
    assert not LoadResult(PROJECT, "wf", workflow, "", (error,)).ok
    assert not LoadResult(PROJECT, "wf", None, "", ()).ok


def test_an_invalid_key_is_located_at_the_key_and_a_value_problem_at_the_value() -> None:
    result = load(
        """\
        env:
          DEPLOY-TARGET:
            prod
          BAD-NULL: null
        inputs:
          bad name: {type: string}
        steps:
          - id: a
            run: x
            outputs:
              out put: x
        """
    )

    assert [(issue.code, issue.position) for issue in result.issues] == [
        ("E104", Position(2, 3)),
        ("E104", Position(4, 3)),
        ("E103", Position(4, 13)),
        ("E104", Position(6, 3)),
        ("E104", Position(11, 7)),
    ]


def test_non_string_keys_are_reported_and_dropped() -> None:
    result = load(
        """\
        env:
          1: one
        steps:
          - id: a
            uses: http
            with:
              url: https://example.net/
              headers: {200: ok}
        """
    )

    assert [(issue.code, issue.message, issue.position) for issue in result.issues] == [
        ("E103", "expected a string key, got a number", Position(2, 3)),
        ("E103", "expected a string key, got a number", Position(8, 17)),
    ]


def test_a_step_type_check_runs_on_normalized_parameters() -> None:
    issue = Issue(Severity.ERROR, "E407", "bad combination", ("steps", 0, "with", "value"))
    step = FakeStep(issues=[issue])
    result = load(
        "steps:\n  - id: a\n    uses: fake\n    with: {value: 1}\n",
        loader=make_loader(StepRegistry([step])),
    )

    assert [(found.code, found.position) for found in result.issues] == [("E407", Position(4, 19))]


def test_issues_about_with_run_point_at_the_step_level_run() -> None:
    issue = Issue(Severity.ERROR, "E407", "bad script", ("steps", 0, "with", "run"))
    shell = FakeStep("shell", schema=Obj("shell step", {"run": Field(Str())}), issues=[issue])
    result = load(
        "steps:\n  - id: a\n    run: make\n",
        loader=make_loader(StepRegistry([shell])),
    )

    assert [(found.path, found.position) for found in result.issues] == [
        (("steps", 0, "run"), Position(3, 10))
    ]


def test_a_shorthand_run_without_a_shell_type_is_an_unknown_type() -> None:
    result = load(
        "steps:\n  - id: a\n    run: make\n",
        loader=make_loader(StepRegistry([FakeStep()])),
    )

    assert [(found.code, found.position) for found in result.issues] == [("E401", Position(3, 10))]


def test_custom_reserved_outputs_are_enforced() -> None:
    step = FakeStep(reserved_outputs=frozenset({"value"}))
    result = load(
        "steps:\n  - id: a\n    uses: fake\n    outputs: {value: x}\n",
        loader=make_loader(StepRegistry([step])),
    )

    assert codes(result) == [("E406", "output 'value' reuses an output name of the fake step type")]


def test_expression_syntax_errors_point_into_with_and_run() -> None:
    result = load(
        """\
        steps:
          - id: a
            run: echo ${{ ) }}
          - id: b
            uses: http
            with:
              url: https://example.net/
              json: {list: [ok, "${{ ( }}"]}
        """
    )

    assert [(issue.code, issue.path) for issue in result.issues] == [
        ("E201", ("steps", 0, "run")),
        ("E201", ("steps", 1, "with", "json", "list", 1)),
    ]


def test_retry_delays_must_fit_max_delay() -> None:
    result = load(
        """\
        defaults:
          retry: {delay: 20s, max_delay: 10s}
        steps:
          - id: a
            run: x
            retry: {delay: 10m}
        """
    )

    assert [(issue.message, issue.path, issue.hint) for issue in result.issues] == [
        ("must be at least delay (20s)", ("defaults", "retry", "max_delay"), None),
        (
            "must be at most max_delay (10s)",
            ("steps", 0, "retry", "delay"),
            "set max_delay as well, such as max_delay: 10m",
        ),
    ]


def test_an_invalid_retry_value_doesnt_hide_the_other_keys() -> None:
    result = load("steps:\n  - id: a\n    run: x\n    retry: {attempts: 0, delay: 1s}\n")

    assert codes(result) == [("E104", "must be between 1 and 20")]


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        pytest.param(
            "pick: {type: choice}",
            [("E102", "missing required key 'options'")],
            id="choice-without-options",
        ),
        pytest.param(
            "name: {options: [a]}",
            [("E104", "'options' applies only to choice inputs")],
            id="options-on-a-string",
        ),
        pytest.param(
            "name: {required: true, default: x}",
            [("E104", "a required input can't have a default")],
            id="required-with-default",
        ),
        pytest.param(
            "count: {type: integer, default: many}",
            [("E103", "must be an integer")],
            id="default-of-the-wrong-type",
        ),
        pytest.param(
            "flag: {type: boolean, default: 1}",
            [("E103", "must be true or false")],
            id="number-isnt-a-boolean",
        ),
        pytest.param(
            "pick: {type: choice, options: [a, b], default: c}",
            [("E104", "must be one of: a, b")],
            id="default-not-an-option",
        ),
        pytest.param(
            "pick: {type: color}",
            [("E104", "must be one of: string, number, integer, boolean, choice")],
            id="unknown-type",
        ),
        pytest.param(
            "list: {default: [1]}",
            [("E103", "expected a string, number, or boolean, got a list")],
            id="default-not-a-scalar",
        ),
        pytest.param("note: {default: null}", [], id="null-default"),
    ],
)
def test_input_declarations_are_checked(inputs: str, expected: list[tuple[str, str]]) -> None:
    assert codes(load(f"inputs:\n  {inputs}\nsteps:\n  - {{id: a, run: x}}\n")) == expected


def test_an_input_default_of_infinity_is_rejected() -> None:
    result = load("inputs:\n  n: {type: number, default: .inf}\nsteps:\n  - {id: a, run: x}\n")

    assert codes(result) == [("E104", "must be a finite number")]


def test_triggers_are_capped_at_20_items() -> None:
    hours = ", ".join(f'"0 {hour} * * *"' for hour in range(21))
    result = load(f"triggers:\n  schedule: [{hours}]\nsteps:\n  - {{id: a, run: x}}\n")

    assert codes(result) == [("E104", "must have at most 20 trigger items, got 21")]


def test_a_trigger_with_schema_errors_isnt_built() -> None:
    result = load(
        "triggers:\n  watch: {paths: [], include: [5]}\n  schedule: null\n"
        "steps:\n  - {id: a, run: x}\n"
    )

    assert codes(result) == [
        ("E104", "must have at least 1 item"),
        ("E103", "expected a string, got a number"),
        ("E103", "expected a string or a schedule trigger mapping, got null"),
    ]


def test_a_webhook_with_timestamps_needs_hmac() -> None:
    result = load(
        "triggers:\n  webhook: {name: h, secret_env: S, signature: token, timestamp_header: X-T}\n"
        "steps:\n  - {id: a, run: x}\n"
    )

    assert codes(result) == [("E104", "'timestamp_header' applies only to hmac-sha256")]


def test_different_webhooks_in_one_workflow_are_fine() -> None:
    workflow = only(
        load(
            "triggers:\n  webhook: [{name: a, secret_env: S}, {name: b, secret_env: S}]\n"
            "steps:\n  - {id: a, run: x}\n"
        )
    )

    assert [hook.key for hook in workflow.webhooks()] == ["wf:webhook:a", "wf:webhook:b"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", [("E059", "the file is empty")], id="empty"),
        pytest.param(
            "just text\n",
            [("E059", "the top level must be a mapping of workflow keys, got a string")],
            id="scalar",
        ),
    ],
)
def test_the_top_level_must_be_a_mapping(text: str, expected: list[tuple[str, str]]) -> None:
    assert codes(load(text)) == expected


def test_invalid_steps_dont_stop_the_other_checks() -> None:
    result = load(
        """\
        steps:
          - 5
          - id: bad id
            run: echo ${{ steps.x.outputs.y }}
            if: 7
            outputs: {name: 5}
          - id: next
            run: echo
        """
    )

    assert [issue.code for issue in result.issues] == ["E103", "E104", "E208", "E103", "E103"]


def test_a_duplicate_step_keeps_the_graph_of_the_first() -> None:
    result = load(
        """\
        steps:
          - {id: a, run: x}
          - {id: b, run: x}
          - {id: a, run: x}
          - {id: c, run: "${{ steps.a.status }}"}
        """
    )

    assert codes(result) == [("E403", "duplicate step ID 'a'")]


def test_an_unknown_step_type_still_checks_its_expressions() -> None:
    result = load("steps:\n  - id: a\n    uses: nope\n    with: {x: '${{ nope.y }}'}\n")

    assert [issue.code for issue in result.issues] == ["E401", "E202"]


def test_a_function_name_used_as_a_context_gets_a_hint() -> None:
    result = load("steps:\n  - id: a\n    if: success\n    run: x\n")

    assert [(issue.code, issue.hint) for issue in result.issues] == [
        ("E202", "'success' is a function; call it as success()")
    ]


def test_a_variadic_function_takes_any_number_of_arguments() -> None:
    result = load(
        "steps:\n  - id: a\n    env: {X: \"${{ format('{0}{1}', 1, 2) }}\"}\n    run: x\n"
    )

    assert result.issues == ()


def test_format_needs_at_least_its_pattern() -> None:
    result = load("steps:\n  - id: a\n    env: {X: '${{ format() }}'}\n    run: x\n")

    assert codes(result) == [
        ("E206", "wrong number of arguments for format(): expected at least 1, got 0")
    ]


@pytest.mark.parametrize(
    ("field", "hint"),
    [
        pytest.param(
            "outputs: {code: '${{ steps.a.outputs.status }}'}",
            "read this step's own results through `result`, such as result.status",
            id="outputs",
        ),
        pytest.param(
            "outputs: {state: '${{ steps.a.status }}'}",
            "read this step's own results through `result`",
            id="outputs-status",
        ),
        pytest.param(
            "if: steps.a.status == 'success'",
            "`if` decides whether the step runs, so the step has no status or outputs yet",
            id="if",
        ),
        pytest.param(
            "env: {VALUE: '${{ steps.a.outputs.value }}'}",
            "the step has no outputs until it finishes; read them in a later step",
            id="env",
        ),
    ],
)
def test_a_step_that_refers_to_itself_gets_a_hint_it_can_follow(field: str, hint: str) -> None:
    result = load(f"steps:\n  - id: a\n    run: x\n    {field}\n")

    assert [(issue.code, issue.message, issue.hint) for issue in result.issues] == [
        ("E203", "step 'a' can't refer to itself through `steps`", hint)
    ]


def test_a_reference_to_all_steps_is_allowed_in_steps_only() -> None:
    result = load(
        """\
        env:
          ALL: ${{ toJSON(steps) }}
        steps:
          - id: a
            env:
              ALL: ${{ toJSON(steps) }}
            run: x
        """
    )

    assert [(issue.code, issue.path) for issue in result.issues] == [("E212", ("env", "ALL"))]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("backup.yml", "backup"),
        ("backup.yaml", "backup"),
        ("0-nightly_backup.yml", "0-nightly_backup"),
        ("a" * 64 + ".yml", "a" * 64),
        ("a" * 65 + ".yml", None),
        ("Backup.yml", None),
        ("_backup.yml", None),
        ("back.up.yml", None),
        ("back up.yml", None),
    ],
)
def test_workflow_ids_come_from_the_file_stem(name: str, expected: str | None) -> None:
    assert workflow_id_for(Path("/flows") / name) == expected


def test_discover_lists_workflow_files_in_sorted_order(tmp_path: Path) -> None:
    for name in [
        "b.yml",
        "a.yaml",
        ".hidden.yml",
        "_draft.yml",
        "notes.txt",
        "c.yml~",
        "Upper.yml",
    ]:
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / "dir.yml").mkdir()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "d.yml").write_text("", encoding="utf-8")

    assert [path.name for path in discover(tmp_path)] == ["Upper.yml", "a.yaml", "b.yml"]


def test_discover_skips_symlinked_workflow_files_with_a_warning(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "linked.yml").write_text("", encoding="utf-8")
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "real.yml").write_text("", encoding="utf-8")
    (workflows / "linked.yml").symlink_to(shared / "linked.yml")
    (workflows / "dangling.yaml").symlink_to(tmp_path / "missing.yml")
    (workflows / "folder.yml").symlink_to(shared)
    (workflows / "_draft.yml").symlink_to(shared / "linked.yml")
    (workflows / "notes.txt").symlink_to(shared / "linked.yml")
    skipped: list[LoadResult] = []

    found = discover(workflows, skipped=skipped)

    assert [path.name for path in found] == ["real.yml"]
    assert [result.path for result in skipped] == [
        workflows / "dangling.yaml",
        workflows / "folder.yml",
        workflows / "linked.yml",
    ]
    assert skipped[0] == LoadResult(
        workflows / "dangling.yaml",
        None,
        None,
        None,
        (
            Issue(
                Severity.WARNING,
                "W060",
                "the file is a symbolic link, so Treadle skips it",
                hint="copy the file into the workflows directory instead",
            ),
        ),
    )
    assert not skipped[0].ok


def test_discover_skips_symlinks_without_a_list_for_them(tmp_path: Path) -> None:
    (tmp_path / "real.yml").write_text("", encoding="utf-8")
    (tmp_path / "linked.yml").symlink_to(tmp_path / "real.yml")

    assert discover(tmp_path) == [tmp_path / "real.yml"]


def test_discover_treats_a_missing_directory_as_empty(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("", encoding="utf-8")

    assert discover(tmp_path / "missing") == []
    assert discover(tmp_path / "file") == []


HOOK = "triggers:\n  webhook: {{name: {name}, secret_env: S}}\nsteps:\n  - {{id: a, run: x}}\n"


def test_cross_check_flags_the_second_file_with_an_id() -> None:
    loader = make_loader()
    first = loader.load_text("steps:\n  - {id: a, run: x}\n", PROJECT / "x.yml")
    second = loader.load_text("steps:\n  - {id: a, run: x}\n", PROJECT / "x.yaml")
    checked = cross_check([first, second])

    assert checked[1] is second
    assert [(issue.code, issue.message) for issue in checked[0].issues] == [
        ("E058", "duplicate workflow ID 'x': x.yaml uses it")
    ]
    assert not checked[0].ok


def test_cross_check_gives_a_webhook_name_to_the_first_valid_workflow() -> None:
    loader = make_loader()
    invalid = loader.load_text(HOOK.format(name="push") + "bogus: 1\n", PROJECT / "a.yml")
    owner = loader.load_text(HOOK.format(name="push"), PROJECT / "b.yml")
    other = loader.load_text(HOOK.format(name="push"), PROJECT / "c.yml")
    unnamed = loader.load_text(HOOK.format(name="push"), PROJECT / "Bad.yml")
    checked = cross_check([other, unnamed, owner, invalid])

    assert checked[1:] == [unnamed, owner, invalid]
    assert [(issue.code, issue.message, issue.position) for issue in checked[0].issues] == [
        ("E305", "webhook name 'push' is already used by workflow 'b'", Position(2, 19))
    ]


def hooks(*names: str) -> str:
    items = ", ".join(f"{{name: {name}, secret_env: S}}" for name in names)
    return f"triggers:\n  webhook: [{items}]\nsteps:\n  - {{id: a, run: x}}\n"


def test_a_workflow_that_loses_one_webhook_name_claims_none_of_them() -> None:
    loader = make_loader()
    first = loader.load_text(hooks("deploy"), PROJECT / "a.yml")
    second = loader.load_text(hooks("deploy", "notify"), PROJECT / "b.yml")
    third = loader.load_text(hooks("notify"), PROJECT / "c.yml")

    checked = cross_check([first, second, third])

    assert (checked[0], checked[2]) == (first, third)
    assert codes(checked[1]) == [("E305", "webhook name 'deploy' is already used by workflow 'a'")]


def test_every_conflicting_webhook_name_of_a_workflow_is_reported() -> None:
    loader = make_loader()
    owner = loader.load_text(hooks("deploy", "notify"), PROJECT / "a.yml")
    other = loader.load_text(hooks("notify", "build", "deploy"), PROJECT / "b.yml")
    later = loader.load_text(hooks("build"), PROJECT / "c.yml")

    checked = cross_check([owner, other, later])

    assert codes(checked[1]) == [
        ("E305", "webhook name 'notify' is already used by workflow 'a'"),
        ("E305", "webhook name 'deploy' is already used by workflow 'a'"),
    ]
    assert checked[2] is later


def test_cross_check_changes_nothing_without_conflicts() -> None:
    loader = make_loader()
    results = [
        loader.load_text(HOOK.format(name=name), PROJECT / f"{name}.yml") for name in ["a", "b"]
    ]

    assert cross_check(results) == results


def test_cross_check_keeps_issues_sorted() -> None:
    loader = make_loader()
    text = "timeout: 1s\n" + HOOK.format(name="push")
    first = loader.load_text(text, PROJECT / "a.yml")
    second = loader.load_text(text, PROJECT / "b.yml")
    issues = cross_check([first, second])[1].issues

    assert [issue.code for issue in issues] == ["W105", "E305"]


def test_cross_check_survives_a_result_without_source() -> None:
    loader = make_loader()
    first = loader.load_text(HOOK.format(name="push"), PROJECT / "a.yml")
    second = loader.load_text(HOOK.format(name="push"), PROJECT / "b.yml")
    stripped = LoadResult(second.path, second.workflow_id, second.workflow, None, second.issues)

    assert cross_check([first, stripped])[1].issues[0].position is None


def test_load_workflow_fixture_uses_this_loader(load_workflow: Callable[..., Workflow]) -> None:
    workflow = load_workflow("steps:\n  - id: a\n    uses: fake\n    with: {value: 1}\n")

    assert workflow.steps[0].params == {"value": 1}


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        pytest.param("sh", [], id="name"),
        pytest.param("zsh", [("E104", "must be one of: bash, sh, python")], id="unknown-name"),
        pytest.param("[]", [("E104", "must have at least 1 item")], id="empty-list"),
        pytest.param(
            "5",
            [("E103", "expected one of: bash, sh, python or a list, got a number")],
            id="wrong-type",
        ),
    ],
)
def test_the_default_shell_is_a_name_or_a_list(shell: str, expected: list[tuple[str, str]]) -> None:
    result = load(f"defaults: {{shell: {shell}}}\nsteps:\n  - {{id: a, run: x}}\n")

    assert codes(result) == expected


def test_invalid_defaults_fall_back_to_the_built_in_ones() -> None:
    result = load(
        "defaults: 5\nsteps:\n  - {id: a, uses: http, timeout: 1s, with: {url: 'https://x.net/'}}\n"
    )

    assert codes(result) == [("E103", "expected a defaults mapping, got a number")]


def test_a_boolean_key_is_described_as_a_boolean() -> None:
    result = load("true: 1\nsteps:\n  - {id: a, run: x}\n")

    assert codes(result) == [("E103", "expected a string key, got a boolean")]


def test_repeated_list_items_are_reported_once_each() -> None:
    result = load(
        """\
        inputs:
          level: {type: choice, options: [low, low, high]}
        triggers:
          watch: {paths: x, events: [created, created, created]}
        steps:
          - {id: a, run: x}
          - {id: b, run: x, needs: [a, a]}
        """
    )

    assert [(issue.message, issue.path) for issue in result.issues] == [
        ("duplicate value 'low'", ("inputs", "level", "options", 1)),
        ("duplicate value 'created'", ("triggers", "watch", "events", 1)),
        ("duplicate value 'created'", ("triggers", "watch", "events", 2)),
        ("duplicate value 'a'", ("steps", 1, "needs", 1)),
    ]


def test_steps_after_the_100th_arent_checked() -> None:
    steps = "".join(f"  - {{id: s{index}, run: x}}\n" for index in range(100))
    result = load(f"steps:\n{steps}  - {{id: extra, uses: nope}}\n")

    assert codes(result) == [("E104", "must have at most 100 items")]


def test_trigger_items_after_the_20th_arent_checked() -> None:
    hours = ", ".join(f'"0 {hour % 24} * * {hour // 24}"' for hour in range(20))
    text = f"triggers:\n  schedule: [{hours}, bad]\nsteps:\n  - {{id: a, run: x}}\n"

    assert codes(load(text)) == [("E104", "must have at most 20 trigger items, got 21")]


def test_a_unique_list_ignores_items_of_the_wrong_type() -> None:
    result = load("steps:\n  - {id: a, run: x}\n  - {id: b, run: x, needs: [1, 1, a]}\n")

    assert codes(result) == [
        ("E103", "expected a string, got a number"),
        ("E103", "expected a string, got a number"),
    ]
