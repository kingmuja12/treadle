from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from tests.support.builders import make_runtime, static_template
from tests.support.fakes import RecordingProcesses

from treadle.errors import StepConfigError
from treadle.models import ShellSpec
from treadle.schema import validate
from treadle.steps.shell import ShellStep, build_argv, parse_output_file
from treadle.types import Issue, JSONValue, Severity

SCRIPT = Path("/state/tmp/run-1/step-1/script")
BASH = ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(SCRIPT)]


def check(raw: dict[str, object], *, allow_templates: bool = False) -> list[tuple[str, str]]:
    _, issues = validate(ShellStep().schema, raw, path=("with",), allow_templates=allow_templates)
    return [(issue.code, issue.message) for issue in issues]


def test_the_schema_fills_defaults_but_leaves_shell_and_cwd_to_the_workflow() -> None:
    value, issues = validate(ShellStep().schema, {"run": "make"})

    assert issues == []
    assert value == {"run": "make", "capture_stdout": False, "success_codes": [0]}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            {"run": "a", "argv": ["b"]},
            [("E105", "'run' and 'argv' can't be used together")],
            id="run-and-argv",
        ),
        pytest.param({}, [("E106", "one of 'run' or 'argv' is required")], id="neither"),
        pytest.param({"argv": []}, [("E104", "must have at least 1 item")], id="empty-argv"),
        pytest.param(
            {"run": "a", "shell": "zsh"},
            [("E104", "must be one of: bash, sh, python")],
            id="unknown-shell",
        ),
        pytest.param(
            {"run": "a", "shell": []}, [("E104", "must have at least 1 item")], id="empty-shell"
        ),
        pytest.param(
            {"run": "a", "shell": ["sh", 1]},
            [("E103", "expected a string, got a number")],
            id="non-string-shell-item",
        ),
        pytest.param(
            {"run": "a", "shell": True},
            [("E103", "expected one of: bash, sh, python or a list, got a boolean")],
            id="shell-type",
        ),
        pytest.param(
            {"run": "a", "success_codes": [0, 256]},
            [("E104", "must be between 0 and 255")],
            id="exit-code-range",
        ),
        pytest.param(
            {"run": "a", "success_codes": []},
            [("E104", "must have at least 1 item")],
            id="no-success-codes",
        ),
        pytest.param(
            {"argv": ["sleep", 5]},
            [("E103", "expected a string, got a number")],
            id="non-string-argv",
        ),
    ],
)
def test_the_schema_rejects_bad_parameters(
    raw: dict[str, object], expected: list[tuple[str, str]]
) -> None:
    assert check(raw) == expected


def test_unknown_keys_get_a_suggestion() -> None:
    _, issues = validate(ShellStep().schema, {"run": "a", "capture": True}, path=("with",))

    assert issues == [
        Issue(
            Severity.ERROR,
            "E101",
            "unknown key 'capture' in shell step",
            ("with", "capture"),
            hint="did you mean 'capture_stdout'?",
        )
    ]


def test_a_misspelled_shell_gets_a_suggestion() -> None:
    _, [issue] = validate(ShellStep().schema, {"run": "a", "shell": "bsh"}, path=("with",))

    assert (issue.path, issue.hint) == (("with", "shell"), "did you mean 'bash'?")


def test_a_shell_list_is_kept_as_an_argv_template() -> None:
    value, issues = validate(ShellStep().schema, {"run": "a", "shell": ["zsh", "{0}"]})

    assert issues == []
    assert value == {
        "run": "a",
        "shell": ["zsh", "{0}"],
        "capture_stdout": False,
        "success_codes": [0],
    }


def test_the_shell_spec_describes_both_forms() -> None:
    spec = ShellStep().schema.fields["shell"].spec

    assert spec.describe() == "one of: bash, sh, python or a list"


def test_templates_pass_the_schema_at_load_time() -> None:
    raw = {
        "run": static_template("echo"),
        "cwd": static_template("~/x"),
        "shell": static_template("sh"),
    }

    assert check(raw, allow_templates=True) == []


def test_shell_with_argv_is_a_cross_field_error() -> None:
    step = ShellStep()

    assert step.check({"run": "a", "shell": "sh"}, ("steps", 0, "with")) == []
    assert step.check({"argv": ["a"]}, ("steps", 0, "with")) == []
    assert step.check({"argv": ["a"], "shell": "sh"}, ("steps", 0, "with")) == [
        Issue(
            Severity.ERROR,
            "E407",
            "'shell' applies only to 'run' scripts, not to 'argv'",
            ("steps", 0, "with", "shell"),
            hint="remove 'shell', or move the command into 'run'",
        )
    ]


@pytest.mark.parametrize(
    ("params", "default_shell", "expected"),
    [
        pytest.param({"run": "x"}, "bash", BASH, id="bash"),
        pytest.param({"run": "x"}, "sh", ["sh", "-e", str(SCRIPT)], id="sh"),
        pytest.param({"run": "x"}, "python", ["python3", "-u", str(SCRIPT)], id="python"),
        pytest.param({"run": "x", "shell": "sh"}, "bash", ["sh", "-e", str(SCRIPT)], id="override"),
        pytest.param(
            {"run": "x", "shell": ["zsh", "-e", "{0}", "{0}"]},
            "bash",
            ["zsh", "-e", str(SCRIPT), str(SCRIPT)],
            id="template",
        ),
        pytest.param(
            {"run": "x", "shell": ["sh", "-c", "cat {0}"]},
            "bash",
            ["sh", "-c", "cat {0}", str(SCRIPT)],
            id="template-appends",
        ),
        pytest.param({"run": "x"}, ("fish", "{0}"), ["fish", str(SCRIPT)], id="default-template"),
        pytest.param({"argv": ["echo", "$HOME"]}, "bash", ["echo", "$HOME"], id="argv"),
    ],
)
def test_build_argv(
    params: dict[str, JSONValue], default_shell: ShellSpec, expected: list[str]
) -> None:
    assert build_argv(params, script_path=SCRIPT, default_shell=default_shell) == expected


def test_build_argv_needs_a_script_path_for_a_script() -> None:
    with pytest.raises(ValueError, match="script path"):
        build_argv({"run": "x"}, script_path=None, default_shell="bash")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("a=1\nb=two words\n", {"a": "1", "b": "two words"}, id="pairs"),
        pytest.param("url=https://x/?a=b=c", {"url": "https://x/?a=b=c"}, id="equals-in-value"),
        pytest.param("empty=\n", {"empty": ""}, id="empty-value"),
        pytest.param("\n  \n\t\na=1\n\n", {"a": "1"}, id="blank-lines"),
        pytest.param(
            "text<<EOF\nline 1\n\nline 3\nEOF\nnext=2\n",
            {"text": "line 1\n\nline 3", "next": "2"},
            id="heredoc",
        ),
        pytest.param("x<<END\nEND", {"x": ""}, id="empty-heredoc"),
        pytest.param("x<<a=b\nhi\na=b\n", {"x": "hi"}, id="heredoc-wins-when-first"),
        pytest.param("a=b<<c", {"a": "b<<c"}, id="pair-wins-when-first"),
        pytest.param("a=1\r\nb<<E\r\nv\r\nE\r\n", {"a": "1", "b": "v"}, id="crlf"),
        pytest.param("a=1\na=2\n", {"a": "2"}, id="last-wins"),
        pytest.param("my-key_2=x\n_x=y", {"my-key_2": "x", "_x": "y"}, id="name-characters"),
        pytest.param("", {}, id="empty-file"),
    ],
)
def test_parse_output_file(text: str, expected: dict[str, str]) -> None:
    assert parse_output_file(text) == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("no equals", "line 1 of the output file isn't KEY=value", id="malformed"),
        pytest.param("x<<", "isn't KEY=value or KEY<<DELIMITER", id="no-delimiter"),
        pytest.param(
            "a=1\n\n1abc=x",
            "line 3 of the output file has an invalid output name '1abc'",
            id="bad-name",
        ),
        pytest.param("=x", "invalid output name ''", id="empty-name"),
        pytest.param("key = value", "invalid output name 'key '", id="spaces"),
        pytest.param(f"{'k' * 65}=v", "invalid output name", id="long-name"),
        pytest.param("exit_code=3", "sets the reserved output 'exit_code'", id="reserved-pair"),
        pytest.param("stdout<<E\nx\nE", "sets the reserved output 'stdout'", id="reserved-heredoc"),
        pytest.param(
            "a=1\nnote<<EOF\nnever closed\n",
            "the heredoc on line 2 of the output file never ends with 'EOF'",
            id="unterminated",
        ),
    ],
)
def test_parse_output_file_rejects_malformed_content(text: str, message: str) -> None:
    with pytest.raises(StepConfigError, match=message) as caught:
        parse_output_file(text)

    assert caught.value.retryable is False


REFUSED = "which the operating system doesn't accept"


@pytest.mark.parametrize(
    ("raw", "env", "message"),
    [
        pytest.param(
            {"run": "true"},
            {"REF": "main\0"},
            f"the environment variable REF holds a NUL character, {REFUSED}",
            id="env-nul",
        ),
        pytest.param(
            {"run": "true"},
            {"REF": "main\ud800"},
            f"the environment variable REF holds text that isn't valid Unicode, {REFUSED}",
            id="env-surrogate",
        ),
        pytest.param(
            {"argv": ["echo", "a\0b"]}, {}, f"argv[1] holds a NUL character, {REFUSED}", id="argv"
        ),
        pytest.param(
            {"argv": ["\ud800"]},
            {},
            f"argv[0] holds text that isn't valid Unicode, {REFUSED}",
            id="program",
        ),
        pytest.param(
            {"run": "true", "cwd": "dir\0"}, {}, f"cwd holds a NUL character, {REFUSED}", id="cwd"
        ),
        pytest.param(
            {"run": "true", "cwd": "~user\0"},
            {},
            f"cwd holds a NUL character, {REFUSED}",
            id="cwd-with-a-user",
        ),
    ],
)
async def test_a_value_that_a_process_cant_receive_is_a_config_error(
    tmp_path: Path, raw: dict[str, object], env: dict[str, str], message: str
) -> None:
    processes = RecordingProcesses()
    rt = make_runtime(tmp_path, env={"PATH": os.defpath, **env}, processes=processes)
    value, issues = validate(ShellStep().schema, raw)
    assert issues == []

    with pytest.raises(StepConfigError) as caught:
        await ShellStep().run(cast(dict[str, JSONValue], value), rt)

    assert caught.value.message == message
    assert caught.value.retryable is False
    assert processes.history == []


def test_the_step_type_attributes() -> None:
    step = ShellStep()

    assert step.name == "shell"
    assert step.output_names is None
    assert step.reserved_outputs == {"exit_code", "stdout"}
    assert step.taint_sinks == {"run": "code", "cwd": "path"}
