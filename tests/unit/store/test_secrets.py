from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from treadle.errors import ConfigError
from treadle.secrets import SECRET_PREFIX, SecretSnapshot, SecretStore, parse_dotenv

# The canary appears in every value that an error test feeds in; no message may contain it.
CANARY = "s3cr3t-canary"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", {}, id="empty file"),
        pytest.param("\n  \n\t\n", {}, id="blank lines"),
        pytest.param("# a comment\n   # an indented comment\n", {}, id="comments"),
        pytest.param("KEY=value", {"KEY": "value"}, id="no final newline"),
        pytest.param("export KEY=value\n", {"KEY": "value"}, id="export"),
        pytest.param("export   KEY=value", {"KEY": "value"}, id="export with spaces"),
        pytest.param("export=1", {"export": "1"}, id="export as a key"),
        pytest.param("  KEY  =  two words  ", {"KEY": "two words"}, id="plain values are trimmed"),
        pytest.param("KEY=value # note", {"KEY": "value"}, id="space hash starts a comment"),
        pytest.param("KEY=value\t# note", {"KEY": "value"}, id="tab hash starts a comment"),
        pytest.param("KEY=a#b", {"KEY": "a#b"}, id="hash inside a value"),
        pytest.param("KEY=", {"KEY": ""}, id="empty value"),
        pytest.param("KEY= # only a comment", {"KEY": ""}, id="empty value with a comment"),
        pytest.param("KEY=a=b", {"KEY": "a=b"}, id="equals inside a value"),
        pytest.param("KEY=$HOME/${USER}", {"KEY": "$HOME/${USER}"}, id="no interpolation"),
        pytest.param("_k9=x", {"_k9": "x"}, id="underscore and digits"),
        pytest.param(
            "KEY='literal $HOME \\n # kept'", {"KEY": "literal $HOME \\n # kept"}, id="single"
        ),
        pytest.param("KEY='quoted' # note", {"KEY": "quoted"}, id="single with a comment"),
        pytest.param("KEY=''", {"KEY": ""}, id="empty single"),
        pytest.param(
            'KEY="tab\\there\\nnew \\"quoted\\" back\\\\slash"',
            {"KEY": 'tab\there\nnew "quoted" back\\slash'},
            id="double escapes",
        ),
        pytest.param('KEY="keep \\x as is"', {"KEY": "keep \\x as is"}, id="other escapes stay"),
        pytest.param(
            'KEY="first\nsecond"\nNEXT=1', {"KEY": "first\nsecond", "NEXT": "1"}, id="multi-line"
        ),
        pytest.param('KEY="a # b"   # note', {"KEY": "a # b"}, id="double with a comment"),
        pytest.param('KEY="  padded  "', {"KEY": "  padded  "}, id="double keeps spaces"),
        pytest.param("KEY=first\nKEY=second", {"KEY": "second"}, id="the last value wins"),
        pytest.param("A=1\r\nB=2\r\n", {"A": "1", "B": "2"}, id="CRLF line endings"),
        pytest.param("﻿API_KEY=abc123\nB=2", {"API_KEY": "abc123", "B": "2"}, id="BOM"),
        pytest.param("﻿# a comment\nA=1", {"A": "1"}, id="BOM before a comment"),
    ],
)
def test_dotenv_grammar(text: str, expected: dict[str, str]) -> None:
    assert parse_dotenv(text, source=".env") == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            f'API_TOKEN="{CANARY}',
            ".env:1: the value of API_TOKEN has no closing quote, or text after it",
        ),
        (
            f"A=1\n\nAPI_TOKEN='{CANARY}\n'",
            ".env:3: the value of API_TOKEN has no closing quote, or text after it",
        ),
        (
            f'API_TOKEN="{CANARY}" trailing',
            ".env:1: the value of API_TOKEN has no closing quote, or text after it",
        ),
        (
            f"export API_TOKEN='{CANARY}' {CANARY}",
            ".env:1: the value of API_TOKEN has no closing quote, or text after it",
        ),
        (f"{CANARY}", ".env:1: expected KEY=VALUE, a comment, or a blank line"),
        (f"A=1\n9KEY={CANARY}", ".env:2: expected KEY=VALUE, a comment, or a blank line"),
        (f"export {CANARY}", ".env:1: expected KEY=VALUE, a comment, or a blank line"),
        (f"KEY-NAME={CANARY}", ".env:1: expected KEY=VALUE, a comment, or a blank line"),
        (f"A=1\n﻿B={CANARY}", ".env:2: expected KEY=VALUE, a comment, or a blank line"),
        (f"﻿﻿B={CANARY}", ".env:1: expected KEY=VALUE, a comment, or a blank line"),
    ],
)
def test_dotenv_errors_name_the_line_but_never_the_value(text: str, message: str) -> None:
    with pytest.raises(ConfigError) as raised:
        parse_dotenv(text, source=".env")

    assert str(raised.value) == message
    assert CANARY not in str(raised.value)


def write_env(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_secret_variables_win_over_dotenv(tmp_path: Path) -> None:
    env_file = write_env(tmp_path / ".env", "API_KEY=from-dotenv\nONLY_DOTENV=dotenv-value\n")
    environ = {
        "TREADLE_SECRET_API_KEY": "from-environ",
        "TREADLE_SECRET_ONLY_ENV": "environ-value",
        "TREADLE_SECRET_": "has no name",
        "API_KEY": "plain variable",
    }

    snapshot = SecretStore(env_file, environ).load()

    assert SECRET_PREFIX == "TREADLE_SECRET_"
    assert dict(snapshot.secrets) == {
        "API_KEY": "from-environ",
        "ONLY_DOTENV": "dotenv-value",
        "ONLY_ENV": "environ-value",
    }
    assert snapshot.get("API_KEY") == "from-environ"
    assert snapshot.get("ONLY_DOTENV") == "dotenv-value"
    assert snapshot.get("MISSING") is None
    assert snapshot.get("") is None


def test_a_dotenv_file_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=abc123\nOTHER=x\n", encoding="utf-8-sig")

    snapshot = SecretStore(env_file, {}).load()

    assert dict(snapshot.secrets) == {"API_KEY": "abc123", "OTHER": "x"}


def test_lookup_env_reads_the_environment_then_dotenv(tmp_path: Path) -> None:
    env_file = write_env(tmp_path / ".env", "HOOK_SECRET=from-dotenv\nONLY_DOTENV=d-value\n")
    environ = {"HOOK_SECRET": "from-environ", "TREADLE_SECRET_PREFIXED": "p-value"}

    snapshot = SecretStore(env_file, environ).load()

    assert snapshot.lookup_env("HOOK_SECRET") == "from-environ"
    assert snapshot.lookup_env("ONLY_DOTENV") == "d-value"
    assert snapshot.lookup_env("PREFIXED") is None
    assert snapshot.lookup_env("MISSING") is None


def test_mask_values_holds_every_secret_and_each_hidden_variable() -> None:
    snapshot = SecretSnapshot(
        secrets={"A": "alpha-secret", "B": "beta-secret"},
        dotenv={"A": "alpha-secret", "TOKEN_FROM_DOTENV": "dotenv-token"},
        environ={"TREADLE_TOKEN": "a-token-of-32-or-more-characters", "PATH": "/bin"},
    )

    assert snapshot.mask_values() == {"alpha-secret", "beta-secret"}
    assert snapshot.mask_values(["TREADLE_TOKEN", "TOKEN_FROM_DOTENV", "UNSET"]) == {
        "alpha-secret",
        "beta-secret",
        "a-token-of-32-or-more-characters",
        "dotenv-token",
    }


def test_without_an_env_file_only_secret_variables_count() -> None:
    store = SecretStore(None, {"TREADLE_SECRET_X": "x-value"})

    assert dict(store.load().secrets) == {"X": "x-value"}
    assert not store.reload_if_changed()
    assert store.permission_warning() is None


def test_a_missing_env_file_has_no_entries(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / ".env", {})

    assert dict(store.load().secrets) == {}
    assert store.permission_warning() is None


def test_the_snapshot_before_load_holds_only_secret_variables(tmp_path: Path) -> None:
    env_file = write_env(tmp_path / ".env", "FROM_DOTENV=1\n")
    store = SecretStore(env_file, {"TREADLE_SECRET_FROM_ENV": "2"})

    assert dict(store.snapshot.secrets) == {"FROM_ENV": "2"}
    assert store.load() is store.snapshot
    assert dict(store.snapshot.secrets) == {"FROM_DOTENV": "1", "FROM_ENV": "2"}


def test_load_reports_a_file_that_isnt_utf8(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"KEY=caf\xe9\n")

    with pytest.raises(ConfigError, match="isn't valid UTF-8"):
        SecretStore(env_file, {}).load()


def test_load_reports_a_file_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()

    with pytest.raises(ConfigError, match=r"^cannot read .*\.env: "):
        SecretStore(tmp_path / ".env", {}).load()


def test_load_reports_a_syntax_error_with_the_path(tmp_path: Path) -> None:
    env_file = write_env(tmp_path / ".env", f'TOKEN="{CANARY}\n')

    with pytest.raises(ConfigError) as raised:
        SecretStore(env_file, {}).load()

    assert str(raised.value).startswith(f"{env_file}:1: the value of TOKEN has no closing quote")
    assert CANARY not in str(raised.value)


def bump(path: Path, text: str) -> None:
    """Rewrite `path` with a modification time that differs from the previous one."""
    previous = path.stat().st_mtime_ns if path.exists() else 0
    write_env(path, text)
    os.utime(path, ns=(previous + 1_000_000_000, previous + 1_000_000_000))


def test_reload_if_changed_follows_the_env_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env_file = write_env(tmp_path / ".env", "A=one\n")
    store = SecretStore(env_file, {})
    store.load()

    assert not store.reload_if_changed()

    bump(env_file, "A=two\n")
    assert store.reload_if_changed()
    assert store.snapshot.get("A") == "two"

    bump(env_file, f'A="{CANARY}\n')
    with caplog.at_level(logging.WARNING, logger="treadle.secrets"):
        assert not store.reload_if_changed()
        assert not store.reload_if_changed()
    assert store.snapshot.get("A") == "two"
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage().endswith("; keeping the previous secrets")
    assert CANARY not in caplog.text

    bump(env_file, "A=three\n")
    assert store.reload_if_changed()
    assert store.snapshot.get("A") == "three"

    env_file.unlink()
    assert store.reload_if_changed()
    assert store.snapshot.get("A") is None


def test_permission_warning_flags_an_env_file_others_can_read(tmp_path: Path) -> None:
    env_file = write_env(tmp_path / ".env", "A=1\n")
    store = SecretStore(env_file, {})
    expected = f"{env_file} is readable by other users; run `chmod 600 {env_file}`"

    assert store.permission_warning() is None
    env_file.chmod(0o640)
    assert store.permission_warning() == expected
    env_file.chmod(0o604)
    assert store.permission_warning() == expected
    env_file.chmod(0o620)
    assert store.permission_warning() is None
