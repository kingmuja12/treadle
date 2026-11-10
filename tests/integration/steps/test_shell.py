"""The `shell` step with real `sh`, `bash`, and `python3` processes."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import os
import signal
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from tests.support.builders import make_runtime
from tests.support.fakes import RecordingLog, RecordingProcesses

from treadle.config import Limits
from treadle.errors import StepConfigError, StepError
from treadle.models import LogStream
from treadle.schema import validate
from treadle.steps.base import StepResult, StepRuntime
from treadle.steps.shell import ShellStep
from treadle.types import JSONValue


class CappedLog(RecordingLog):
    """Keeps lines up to `cap` bytes and drops the rest, as the engine's attempt log does."""

    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap
        self.kept = 0
        self.seen: dict[LogStream, int] = {LogStream.STDOUT: 0, LogStream.STDERR: 0}

    def write(self, stream: LogStream, text: str) -> None:
        self.seen[stream] += 1
        size = len(text.encode()) + 1
        if self.kept + size <= self.cap:
            self.kept += size
            super().write(stream, text)


def runtime(tmp_path: Path, *, env: dict[str, str] | None = None, **kwargs: Any) -> StepRuntime:
    """A runtime with the test's PATH whose TREADLE_OUTPUT names the attempt's output file."""
    rt = make_runtime(tmp_path, **kwargs)
    base = {"PATH": os.environ["PATH"], "TREADLE_OUTPUT": str(rt.output_path)}
    return dataclasses.replace(rt, env={**base, **(env or {})})


async def run_shell(rt: StepRuntime, raw: Mapping[str, object]) -> StepResult:
    """Validate and check `with` parameters as the executor does, then run the step."""
    value, issues = validate(ShellStep().schema, dict(raw))
    assert issues == []
    params = cast(dict[str, JSONValue], value)
    assert ShellStep().check(params, ()) == []
    return await ShellStep().run(params, rt)


def script_file(rt: StepRuntime) -> tuple[int, str]:
    """Return the mode and the text of the attempt's script file."""
    path = rt.attempt_dir / "script"
    return stat.S_IMODE(path.stat().st_mode), path.read_text(encoding="utf-8")


def make_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    return path


def real(path: Path) -> str:
    return os.path.realpath(path)


def only_group(processes: RecordingProcesses) -> int:
    """Return the one process group that was tracked and then released."""
    pgid = processes.history[0][1]
    assert processes.history == [("add", pgid), ("discard", pgid)]
    return pgid


async def group_gone(pgid: int) -> bool:
    """Wait up to 2 seconds for every member of a process group to be gone and reaped."""
    for _ in range(40):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # macOS reports zombies that way until they are reaped
            pass
        await asyncio.sleep(0.05)
    return False


@pytest.mark.parametrize(
    ("shell", "script", "expected"),
    [
        pytest.param(
            "bash",
            'echo "$0"; case $- in *e*) echo errexit;; esac; shopt -qo pipefail && echo pipefail',
            ["{script}", "errexit", "pipefail"],
            id="bash",
        ),
        pytest.param(
            "sh", 'echo "$0"; case $- in *e*) echo errexit;; esac', ["{script}", "errexit"], id="sh"
        ),
        pytest.param(
            "python",
            "import sys; print(sys.argv[0]); print(sys.stdout.write_through)",
            ["{script}", "True"],
            id="python",
        ),
    ],
)
async def test_each_shell_runs_the_script_file_with_its_flags(
    tmp_path: Path, shell: str, script: str, expected: list[str]
) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    result = await run_shell(rt, {"run": script, "shell": shell})

    path = str(rt.attempt_dir / "script")
    assert result == StepResult({"exit_code": 0}, exit_code=0)
    assert log.texts(LogStream.STDOUT) == [line.format(script=path) for line in expected]


async def test_bash_is_the_default_and_fails_a_broken_pipeline(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    with pytest.raises(StepError) as caught:
        await run_shell(rt, {"run": "false | true\necho unreachable"})

    assert (caught.value.message, caught.value.exit_code) == ("exit code 1", 1)
    assert log.texts(LogStream.STDOUT) == []


async def test_the_script_is_a_private_file_in_the_attempt_directory(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)
    script = "import os, stat, sys\nprint(oct(stat.S_IMODE(os.stat(sys.argv[0]).st_mode)))\n"

    await run_shell(rt, {"run": script, "shell": "python"})

    assert log.texts(LogStream.STDOUT) == ["0o600"]
    assert script_file(rt) == (0o600, script)


async def test_a_planted_script_file_fails_the_attempt(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("untouched")
    (rt.attempt_dir / "script").symlink_to(decoy)

    with pytest.raises(StepError, match=r"^cannot write the script file .*: File exists$"):
        await run_shell(rt, {"run": "echo hi"})

    assert decoy.read_text() == "untouched"


@pytest.mark.parametrize(
    ("shell", "script", "expected"),
    [
        pytest.param(
            ["sh", "-c", '. "$1"', "treadle", "{0}"],
            'echo "sourced as $0"',
            ["sourced as treadle"],
            id="replaced",
        ),
        pytest.param(
            ["python3", "-c", "import sys; print(open(sys.argv[1]).read().upper())"],
            "hello",
            ["HELLO"],
            id="appended",
        ),
    ],
)
async def test_a_shell_list_is_an_argv_template(
    tmp_path: Path, shell: list[str], script: str, expected: list[str]
) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    await run_shell(rt, {"run": script, "shell": shell})

    assert log.texts(LogStream.STDOUT) == expected


async def test_the_default_shell_can_be_a_template(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = dataclasses.replace(runtime(tmp_path, log=log), default_shell=("sh", "-e", "{0}"))

    with pytest.raises(StepError, match=r"^exit code 1$"):
        await run_shell(rt, {"run": "echo first\nfalse\necho unreachable"})

    assert log.texts(LogStream.STDOUT) == ["first"]


async def test_argv_runs_the_program_without_a_shell(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log, env={"HOME": "/home/nobody"})

    result = await run_shell(rt, {"argv": ["printf", "%s|", "$HOME", "a b", "*"]})

    assert result == StepResult({"exit_code": 0}, exit_code=0)
    assert log.texts(LogStream.STDOUT) == ["$HOME|a b|*|"]
    assert not (rt.attempt_dir / "script").exists()


async def test_the_output_file_becomes_outputs(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    script = (
        'echo "version=1.2.3" >> "$TREADLE_OUTPUT"\n'
        "cat >> \"$TREADLE_OUTPUT\" <<'OUT'\n"
        "notes<<EOF\n"
        "line one\n"
        "\n"
        "line three\n"
        "EOF\n"
        "\n"
        "query=a=b&c=d\n"
        "OUT\n"
    )

    result = await run_shell(rt, {"run": script})

    assert result.outputs == {
        "exit_code": 0,
        "version": "1.2.3",
        "notes": "line one\n\nline three",
        "query": "a=b&c=d",
    }


async def test_a_deleted_output_file_has_no_entries(tmp_path: Path) -> None:
    rt = runtime(tmp_path)

    result = await run_shell(rt, {"run": 'rm "$TREADLE_OUTPUT"'})

    assert result == StepResult({"exit_code": 0}, exit_code=0)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(
            "just text", "line 1 of the output file isn't KEY=value or KEY<<DELIMITER", id="text"
        ),
        pytest.param(
            "exit_code=0",
            "line 1 of the output file sets the reserved output 'exit_code'",
            id="reserved-exit-code",
        ),
        pytest.param(
            "stdout<<EOF\nx\nEOF",
            "line 1 of the output file sets the reserved output 'stdout'",
            id="reserved-stdout",
        ),
        pytest.param(
            "ok=1\nopen<<EOF\nnever closed",
            "the heredoc on line 2 of the output file never ends with 'EOF'",
            id="unterminated",
        ),
    ],
)
async def test_a_malformed_output_file_is_a_config_error(
    tmp_path: Path, content: str, message: str
) -> None:
    rt = runtime(tmp_path, env={"CONTENT": content})

    with pytest.raises(StepConfigError) as caught:
        await run_shell(rt, {"run": 'printf "%s\\n" "$CONTENT" > "$TREADLE_OUTPUT"'})

    assert caught.value.message == message
    assert caught.value.retryable is False
    assert (caught.value.outputs, caught.value.exit_code) == ({"exit_code": 0}, 0)


def output_of_size(size: int) -> str:
    """Return a Python script that writes one `k=vvv...` entry of exactly `size` bytes."""
    entry = f"'k=' + 'v' * {size - 3} + '\\n'"
    return f"import os\nopen(os.environ['TREADLE_OUTPUT'], 'w').write({entry})\n"


async def test_an_output_file_at_the_size_cap_is_read(tmp_path: Path) -> None:
    rt = runtime(tmp_path, limits=Limits(output_file_bytes=1024))

    result = await run_shell(rt, {"run": output_of_size(1024), "shell": "python"})

    assert result.outputs["k"] == "v" * 1021


async def test_an_output_file_over_the_size_cap_is_a_config_error(tmp_path: Path) -> None:
    rt = runtime(tmp_path, limits=Limits(output_file_bytes=1024))

    with pytest.raises(StepConfigError) as caught:
        await run_shell(rt, {"run": output_of_size(1025), "shell": "python"})

    assert caught.value.message == (
        "the output file is larger than 1.0 KiB (limits.output_file_bytes)"
    )


async def test_a_failed_exit_ignores_a_malformed_output_file(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    with pytest.raises(StepError) as caught:
        await run_shell(rt, {"run": 'echo oops > "$TREADLE_OUTPUT"; exit 3'})

    assert type(caught.value) is StepError
    assert (caught.value.message, caught.value.retryable) == ("exit code 3", True)
    assert caught.value.outputs == {"exit_code": 3}
    assert log.texts(LogStream.SYSTEM) == [
        "ignored the output file: line 1 of the output file isn't KEY=value or KEY<<DELIMITER"
    ]


async def test_success_codes_decide_the_outcome(tmp_path: Path) -> None:
    script = 'echo "partial=yes" >> "$TREADLE_OUTPUT"; exit "$CODE"'
    accepted = runtime(tmp_path, attempt=1, env={"CODE": "3"})
    rejected = runtime(tmp_path, attempt=2, env={"CODE": "4"})

    result = await run_shell(accepted, {"run": script, "success_codes": [0, 3]})
    with pytest.raises(StepError) as caught:
        await run_shell(rejected, {"run": script, "success_codes": [0, 3]})

    assert result == StepResult({"exit_code": 3, "partial": "yes"}, exit_code=3)
    error = caught.value
    assert (error.message, error.retryable, error.exit_code) == ("exit code 4", True, 4)
    assert error.outputs == {"exit_code": 4, "partial": "yes"}


async def test_capture_stdout_keeps_the_tail_without_one_trailing_newline(tmp_path: Path) -> None:
    rt = runtime(tmp_path, limits=Limits(stdout_capture_bytes=1024))
    script = "for n in range(1000):\n    print(f'line {n:04}')\nprint('end')\nprint()\n"

    result = await run_shell(rt, {"run": script, "shell": "python", "capture_stdout": True})

    printed = "".join(f"line {n:04}\n" for n in range(1000)) + "end\n\n"
    assert result.outputs["stdout"] == printed[-1024:].removesuffix("\n")
    assert str(result.outputs["stdout"]).endswith("line 0999\nend\n")


async def test_add_mask_registers_the_value_and_hides_the_line(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    await run_shell(rt, {"run": 'echo "::add-mask::tok-12345"; echo next'})

    assert log.masks == ["tok-12345"]
    assert log.texts(LogStream.STDOUT) == ["::add-mask::***", "next"]


async def test_stdout_and_stderr_are_logged_as_separate_streams(tmp_path: Path) -> None:
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    await run_shell(rt, {"run": "echo to-out; echo to-err >&2; echo more-out"})

    assert log.texts(LogStream.STDOUT) == ["to-out", "more-out"]
    assert log.texts(LogStream.STDERR) == ["to-err"]


async def test_the_step_gets_exactly_the_runtime_env_and_its_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREADLE_TEST_PARENT_ONLY", "leaked")
    log = RecordingLog()
    rt = runtime(tmp_path, log=log, env={"GREETING": "hi"})
    sub = make_dir(rt.project_dir / "sub")

    await run_shell(
        rt, {"run": 'echo "$GREETING|${TREADLE_TEST_PARENT_ONLY-unset}"; pwd -P', "cwd": "sub"}
    )

    assert log.texts(LogStream.STDOUT) == ["hi|unset", real(sub)]


async def test_cwd_defaults_to_defaults_cwd_and_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_dir(tmp_path / "home")
    elsewhere = make_dir(tmp_path / "elsewhere")
    monkeypatch.setenv("HOME", str(home))
    log = RecordingLog()
    rt = runtime(tmp_path, log=log)

    await run_shell(rt, {"argv": ["pwd", "-P"], "cwd": "~"})
    await run_shell(dataclasses.replace(rt, default_cwd=elsewhere), {"argv": ["pwd", "-P"]})

    assert log.texts(LogStream.STDOUT) == [real(home), real(elsewhere)]


async def test_a_missing_cwd_fails_the_attempt(tmp_path: Path) -> None:
    rt = runtime(tmp_path)

    with pytest.raises(StepError) as caught:
        await run_shell(rt, {"run": "true", "cwd": "missing"})

    assert caught.value.message == (
        f"cannot start bash: {os.strerror(errno.ENOENT)} ({rt.project_dir / 'missing'})"
    )
    assert caught.value.retryable is True


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["term", "kill"])
async def test_a_signal_exit_reports_128_plus_the_signal(
    tmp_path: Path, sig: signal.Signals
) -> None:
    rt = runtime(tmp_path)

    with pytest.raises(StepError) as caught:
        await run_shell(rt, {"run": f"kill -{sig.name.removeprefix('SIG')} $$"})

    code = 128 + sig
    assert (caught.value.message, caught.value.exit_code) == (f"exit code {code}", code)
    assert caught.value.outputs == {"exit_code": code}


async def test_a_timeout_kills_every_process_of_the_step(tmp_path: Path) -> None:
    processes = RecordingProcesses()
    rt = runtime(tmp_path, processes=processes, kill_grace=0.5)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await run_shell(rt, {"argv": ["sh", "-c", "sleep 100 & sleep 100"]})

    assert await group_gone(only_group(processes))


async def test_a_step_that_writes_5_mib_finishes_under_a_small_log_cap(tmp_path: Path) -> None:
    log = CappedLog(cap=64 * 1024)
    rt = runtime(tmp_path, log=log)
    script = (
        "import sys\n"
        "line = b'y' * 1023 + b'\\n'\n"
        "for _ in range(5 * 1024):\n"
        "    sys.stdout.buffer.write(line)\n"
        "    sys.stderr.buffer.write(line)\n"
        "print('done')\n"
    )

    async with asyncio.timeout(60):
        result = await run_shell(rt, {"run": script, "shell": "python"})

    assert result.exit_code == 0
    assert log.seen == {LogStream.STDOUT: 5 * 1024 + 1, LogStream.STDERR: 5 * 1024}
    assert log.kept <= 64 * 1024
