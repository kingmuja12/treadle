from __future__ import annotations

import logging
import re
import sys
import tomllib
from pathlib import Path

import pytest
from tests.support.project import ProjectFactory

from treadle.config import (
    CONFIG_FILENAME,
    Limits,
    LoggingSettings,
    Overrides,
    RetentionSettings,
    RunSettings,
    SecuritySettings,
    ServerSettings,
    default_config_text,
    detect_timezone,
    find_config,
    is_loopback_host,
    is_time_zone,
    load_settings,
    resolve_timezone,
    time_zone_hint,
)
from treadle.errors import ConfigError
from treadle.types import ExitCode

NO_LINK = Path("/nonexistent/localtime")


def config_problems(
    project: ProjectFactory, text: str
) -> list[tuple[str, str, tuple[object, ...]]]:
    project.write_config(text)
    with pytest.raises(ConfigError) as caught:
        project.settings()
    assert str(caught.value) == f"invalid configuration in {project.config_path.resolve()}"
    return [(issue.code, issue.message, issue.path) for issue in caught.value.issues]


def test_find_config_searches_upward(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / CONFIG_FILENAME).write_text("", encoding="utf-8")

    assert find_config(nested) == (root / CONFIG_FILENAME).resolve()
    assert find_config(root) == (root / CONFIG_FILENAME).resolve()
    assert find_config(tmp_path) is None


def test_defaults_resolve_against_the_project(project: ProjectFactory) -> None:
    settings = project.settings()
    root = project.root.resolve()

    assert settings.project_dir == root
    assert settings.config_path == root / CONFIG_FILENAME
    assert settings.workflows_dir == root / "workflows"
    assert settings.state_dir == root / ".treadle"
    assert settings.env_file == root / ".env"
    assert settings.timezone == "UTC"
    assert (settings.server, settings.runs, settings.retention) == (
        ServerSettings(),
        RunSettings(),
        RetentionSettings(),
    )
    assert (settings.limits, settings.security, settings.logging) == (
        Limits(),
        SecuritySettings(),
        LoggingSettings(),
    )


def test_settings_paths(project: ProjectFactory) -> None:
    settings = project.settings()
    state = settings.state_dir

    assert settings.db_path == state / "treadle.db"
    assert settings.lock_path == state / "treadle.lock"
    assert settings.holder_path == state / "holder.json"
    assert settings.secret_key_path == state / "secret.key"
    assert settings.tmp_dir == state / "tmp"
    assert settings.backups_dir == state / "backups"


def test_relative_paths_in_the_file_resolve_against_its_directory(project: ProjectFactory) -> None:
    project.write_config(
        """
        workflows_dir = "flows"
        state_dir = "../state"
        env_file = "secrets/.env"
        timezone = "Europe/London"
        """
    )
    nested = project.root / "sub"
    nested.mkdir()

    settings = load_settings(Overrides(), cwd=nested, environ={})
    root = project.root.resolve()

    assert settings.project_dir == root
    assert settings.workflows_dir == root / "flows"
    assert settings.state_dir == root.parent / "state"
    assert settings.env_file == root / "secrets" / ".env"
    assert settings.timezone == "Europe/London"


def test_an_empty_env_file_disables_it(project: ProjectFactory) -> None:
    project.write_config('env_file = ""\ntimezone = "UTC"\n')

    assert project.settings().env_file is None


def test_cli_flags_win_and_their_paths_resolve_against_cwd(project: ProjectFactory) -> None:
    project.write_config(
        """
        timezone = "UTC"
        [server]
        host = "0.0.0.0"
        port = 9000
        [logging]
        level = "warning"
        """
    )
    cwd = project.root / "here"
    cwd.mkdir()
    overrides = Overrides(
        workflows_dir=Path("w"),
        state_dir=Path("s"),
        host="127.0.0.2",
        port=0,
        log_level="debug",
    )

    settings = load_settings(overrides, cwd=cwd, environ={})

    assert settings.workflows_dir == cwd.resolve() / "w"
    assert settings.state_dir == cwd.resolve() / "s"
    assert (settings.server.host, settings.server.port) == ("127.0.0.2", 0)
    assert settings.logging.level == "debug"


def test_config_flag_and_environment_select_the_file(tmp_path: Path) -> None:
    first = ProjectFactory(tmp_path / "first")
    second = ProjectFactory(tmp_path / "second")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    environ = {"TREADLE_CONFIG": str(second.config_path)}

    by_flag = load_settings(Overrides(config=first.config_path), cwd=elsewhere, environ=environ)
    by_env = load_settings(Overrides(), cwd=elsewhere, environ=environ)
    relative = load_settings(
        Overrides(config=Path("../first/treadle.toml")), cwd=elsewhere, environ={}
    )

    assert by_flag.project_dir == first.root.resolve()
    assert by_env.project_dir == second.root.resolve()
    assert relative.config_path == first.config_path.resolve()


def test_a_missing_selected_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(Overrides(config=Path("nope.toml")), cwd=tmp_path, environ={})


def test_commands_that_need_a_project_explain_how_to_create_one(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError, match=r"no treadle\.toml found .* run `treadle init`"
    ) as caught:
        load_settings(Overrides(), cwd=tmp_path, environ={})

    assert caught.value.exit_code is ExitCode.CONFIG


def test_without_a_project_the_current_directory_and_defaults_apply(tmp_path: Path) -> None:
    settings = load_settings(
        Overrides(), cwd=tmp_path, environ={"TZ": "Europe/Paris"}, require_project=False
    )

    assert settings.project_dir == tmp_path.resolve()
    assert settings.config_path is None
    assert settings.workflows_dir == tmp_path.resolve() / "workflows"
    assert settings.timezone == "Europe/Paris"


def test_invalid_flags_without_a_project_name_the_command_line(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigError, match="invalid configuration in command-line options"
    ) as caught:
        load_settings(Overrides(port=70000), cwd=tmp_path, environ={}, require_project=False)

    assert [(i.code, i.path) for i in caught.value.issues] == [("E104", ("server", "port"))]


def test_values_are_parsed_and_lists_become_tuples(project: ProjectFactory) -> None:
    project.write_config(
        """
        timezone = "UTC"
        [server]
        allowed_hosts = ["treadle.local"]
        session_ttl = "1h"
        secure_cookies = true
        [runs]
        kill_grace = 2
        [retention]
        max_age = "7d"
        [limits]
        api_body_bytes = "128KiB"
        watch_max_entries = 50
        [security]
        file_roots = ["/srv/data"]
        """
    )

    settings = project.settings()

    assert settings.server.allowed_hosts == ("treadle.local",)
    assert (settings.server.session_ttl, settings.server.secure_cookies) == (3600.0, True)
    assert settings.runs.kill_grace == 2.0
    assert settings.retention.max_age == 7 * 86400.0
    assert (settings.limits.api_body_bytes, settings.limits.watch_max_entries) == (131072, 50)
    assert settings.security.file_roots == ("/srv/data",)


@pytest.mark.parametrize(
    "host",
    [
        "treadle.local",
        "Treadle.Example.COM",
        "localhost",
        "my_host",
        "example.com.",
        "192.168.1.10",
        "[::1]",
        "[2001:db8::1]",
        "*",
        "*.example.com",
    ],
)
def test_allowed_hosts_accept_the_forms_that_the_host_check_matches(
    project: ProjectFactory, host: str
) -> None:
    project.write_config(f'timezone = "UTC"\n[server]\nallowed_hosts = ["{host}"]\n')

    assert project.settings().server.allowed_hosts == (host,)


@pytest.mark.parametrize(
    "host",
    [
        "foo*",
        "*foo.com",
        "*.",
        "a.*.com",
        "**.example.com",
        "example.com:8765",
        "http://example.com",
        "::1",
        "[::1]:8765",
        "[not-an-address]",
        "[1.2.3.4]",
        "two words",
        "",
    ],
)
def test_allowed_hosts_reject_other_forms(project: ProjectFactory, host: str) -> None:
    project.write_config(f'timezone = "UTC"\n[server]\nallowed_hosts = ["ok.local", "{host}"]\n')

    with pytest.raises(ConfigError) as caught:
        project.settings()

    assert [(i.code, i.message, i.path, i.hint) for i in caught.value.issues] == [
        (
            "E104",
            "must be a host name or address, '*', or '*.' followed by a domain",
            ("server", "allowed_hosts", 1),
            "such as treadle.local, 192.168.1.10, [::1], or *.example.com, without a scheme "
            "or a port",
        )
    ]


def test_allowed_hosts_must_be_strings(project: ProjectFactory) -> None:
    text = 'timezone = "UTC"\n[server]\nallowed_hosts = [5]\n'

    assert config_problems(project, text) == [
        ("E103", "expected a string, got a number", ("server", "allowed_hosts", 0))
    ]


def test_unknown_keys_get_suggestions(project: ProjectFactory) -> None:
    project.write_config('timezne = "UTC"\n[server]\nprot = 1\n[extras]\n')
    with pytest.raises(ConfigError) as caught:
        project.settings()

    assert [(i.code, i.message, i.path, i.hint) for i in caught.value.issues] == [
        ("E101", "unknown key 'timezne' in treadle.toml", ("timezne",), "did you mean 'timezone'?"),
        ("E101", "unknown key 'extras' in treadle.toml", ("extras",), None),
        ("E101", "unknown key 'prot' in [server]", ("server", "prot"), "did you mean 'port'?"),
    ]


@pytest.mark.parametrize(
    ("table", "key", "value", "message"),
    [
        ("server", "port", "70000", "must be between 0 and 65535"),
        ("server", "token_env", "'1BAD'", "must match the pattern [A-Za-z_][A-Za-z0-9_]*"),
        ("server", "session_ttl", "'1m'", "must be between 5m and 30d"),
        ("runs", "max_concurrent", "0", "must be between 1 and 64"),
        ("runs", "kill_grace", "'2m'", "must be between 0ms and 1m"),
        ("runs", "shutdown_grace", "'20m'", "must be between 1s and 10m"),
        ("retention", "max_age", "'30m'", "must be between 1h and 3650d"),
        ("retention", "max_runs_per_workflow", "0", "must be between 1 and 100000"),
        ("retention", "interval", "'10s'", "must be between 1m and 7d"),
        ("limits", "step_log_bytes", "'1KiB'", "must be between 64.0 KiB and 64.0 MiB"),
        ("limits", "run_log_bytes", "'2GiB'", "must be between 1.0 MiB and 1.0 GiB"),
        ("limits", "api_body_bytes", "'2MiB'", "must be between 4.0 KiB and 1.0 MiB"),
        ("limits", "watch_max_entries", "0", "must be between 1 and 500000"),
        ("security", "file_roots", "[]", "must have at least 1 item"),
        ("logging", "level", "'verbose'", "must be one of: debug, info, warning, error"),
    ],
)
def test_out_of_range_values(
    project: ProjectFactory, table: str, key: str, value: str, message: str
) -> None:
    text = f'timezone = "UTC"\n[{table}]\n{key} = {value}\n'

    assert config_problems(project, text) == [("E104", message, (table, key))]


def test_wrong_types(project: ProjectFactory) -> None:
    text = 'timezone = "UTC"\nserver = 5\n[runs]\nmax_concurrent = "4"\n'

    assert config_problems(project, text) == [
        ("E103", "expected a [server] mapping, got a number", ("server",)),
        ("E103", "expected an integer, got a string", ("runs", "max_concurrent")),
    ]


def test_invalid_toml_is_a_config_error(project: ProjectFactory) -> None:
    project.write_config("timezone = \n")

    with pytest.raises(ConfigError, match="cannot read"):
        project.settings()


def test_an_unknown_time_zone_is_a_config_error(project: ProjectFactory) -> None:
    project.write_config('timezone = "Mars/Olympus_Mons"\n')

    with pytest.raises(ConfigError, match="unknown time zone 'Mars/Olympus_Mons'"):
        project.settings()


def test_an_invalid_log_level_flag_is_reported(project: ProjectFactory) -> None:
    with pytest.raises(ConfigError) as caught:
        project.settings(log_level="loud")

    assert [(i.code, i.path) for i in caught.value.issues] == [("E104", ("logging", "level"))]


UNKNOWN_USER = "~nosuchuser4242"
NO_HOME = f"can't expand '{UNKNOWN_USER}' to a home directory"
NUL = "the path holds a NUL character"


@pytest.mark.parametrize(
    ("text", "key", "message"),
    [
        pytest.param(
            f'workflows_dir = "{UNKNOWN_USER}/workflows"\n',
            ("workflows_dir",),
            NO_HOME,
            id="workflows-dir-of-an-unknown-user",
        ),
        pytest.param(
            f'state_dir = "{UNKNOWN_USER}/state"\n',
            ("state_dir",),
            NO_HOME,
            id="state-dir-of-an-unknown-user",
        ),
        pytest.param(
            f'env_file = "{UNKNOWN_USER}/.env"\n',
            ("env_file",),
            NO_HOME,
            id="env-file-of-an-unknown-user",
        ),
        pytest.param('state_dir = "a\\u0000b"\n', ("state_dir",), NUL, id="state-dir-with-nul"),
        pytest.param('env_file = "a\\u0000b"\n', ("env_file",), NUL, id="env-file-with-nul"),
        pytest.param(
            f'[security]\nfile_roots = [".", "{UNKNOWN_USER}/data"]\n',
            ("security", "file_roots", 1),
            NO_HOME,
            id="file-root-of-an-unknown-user",
        ),
        pytest.param(
            '[security]\nfile_roots = ["a\\u0000b"]\n',
            ("security", "file_roots", 0),
            NUL,
            id="file-root-with-nul",
        ),
    ],
)
def test_a_path_that_cant_be_resolved_is_reported_at_its_key(
    project: ProjectFactory, text: str, key: tuple[object, ...], message: str
) -> None:
    assert config_problems(project, f'timezone = "UTC"\n{text}') == [("E104", message, key)]


def test_an_unknown_user_gets_a_hint(project: ProjectFactory) -> None:
    project.write_config(f'timezone = "UTC"\nstate_dir = "{UNKNOWN_USER}/state"\n')

    with pytest.raises(ConfigError) as caught:
        project.settings()

    assert [issue.hint for issue in caught.value.issues] == [
        "check the user name after `~`, or write the full path"
    ]


def test_every_path_that_cant_be_resolved_is_reported(project: ProjectFactory) -> None:
    project.write_config(
        f'timezone = "UTC"\nstate_dir = "{UNKNOWN_USER}/s"\nenv_file = "a\\u0000"\n'
    )

    with pytest.raises(ConfigError) as caught:
        project.settings()

    assert [issue.path for issue in caught.value.issues] == [("state_dir",), ("env_file",)]


def test_a_path_flag_that_cant_be_resolved_is_a_config_error(project: ProjectFactory) -> None:
    with pytest.raises(ConfigError) as caught:
        project.settings(state_dir=Path(f"{UNKNOWN_USER}/state"))

    assert [(issue.code, issue.message, issue.path) for issue in caught.value.issues] == [
        ("E104", NO_HOME, ("state_dir",))
    ]


def test_a_config_path_that_cant_be_resolved_is_a_config_error(tmp_path: Path) -> None:
    overrides = Overrides(config=Path(f"{UNKNOWN_USER}/treadle.toml"))

    with pytest.raises(ConfigError) as caught:
        load_settings(overrides, cwd=tmp_path, environ={})

    assert str(caught.value) == f"can't use the config file {UNKNOWN_USER}/treadle.toml: {NO_HOME}"


@pytest.mark.skipif(
    sys.version_info >= (3, 13), reason="Path.resolve() raises for a symlink loop before 3.13 only"
)
def test_a_symlink_loop_is_reported_with_the_system_error(project: ProjectFactory) -> None:
    loop = project.root / "loop"
    loop.symlink_to(loop)
    project.write_config('timezone = "UTC"\nstate_dir = "loop/state"\n')

    with pytest.raises(ConfigError) as caught:
        project.settings()

    [issue] = caught.value.issues
    assert (issue.code, issue.path) == ("E104", ("state_dir",))
    assert issue.message.startswith("can't resolve the path: ")


def test_file_roots_expand_home_and_resolve_against_the_project(
    project: ProjectFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project.write_config('timezone = "UTC"\n[security]\nfile_roots = [".", "~", "data", "."]\n')
    settings = project.settings()
    root = project.root.resolve()

    assert settings.file_roots() == (root, home.resolve(), root / "data", root)
    assert settings.protected_paths() == (
        root / ".treadle",
        root / "workflows",
        root,
        home.resolve(),
        root / "data",
    )


def test_resolve_timezone() -> None:
    assert resolve_timezone("America/New_York", {}) == "America/New_York"
    assert resolve_timezone("local", {"TZ": "Asia/Tokyo"}) == "Asia/Tokyo"
    with pytest.raises(ConfigError, match="unknown time zone"):
        resolve_timezone("Nowhere/Special", {})
    with pytest.raises(ConfigError, match="unknown time zone"):
        resolve_timezone("", {})


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("utc", "unknown time zone 'utc'; did you mean 'UTC'?"),
        ("europe/london", "unknown time zone 'europe/london'; did you mean 'Europe/London'?"),
        (
            "Mars/Olympus",
            "unknown time zone 'Mars/Olympus'; use an IANA name such as Europe/London",
        ),
    ],
)
def test_a_time_zone_name_must_be_spelled_as_the_database_spells_it(
    name: str, message: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_timezone(name, {})

    assert str(caught.value) == message


def test_a_wrong_case_time_zone_in_the_file_is_a_config_error(project: ProjectFactory) -> None:
    project.write_config('timezone = "utc"\n')

    with pytest.raises(ConfigError, match="did you mean 'UTC'"):
        project.settings()


@pytest.mark.parametrize(
    ("name", "known"),
    [
        ("UTC", True),
        ("Europe/London", True),
        ("America/Argentina/Buenos_Aires", True),
        ("utc", False),
        ("EUROPE/LONDON", False),
        ("Europe/Londn", False),
        ("", False),
        ("/UTC", False),
    ],
)
def test_is_time_zone(name: str, known: bool) -> None:
    assert is_time_zone(name) is known


def test_time_zone_hint_suggests_the_right_spelling() -> None:
    assert time_zone_hint("america/new_york") == "did you mean 'America/New_York'?"
    assert time_zone_hint("Nowhere") == "use an IANA name such as Europe/London"


def test_detect_timezone_ignores_a_wrong_case_tz() -> None:
    assert detect_timezone({"TZ": "utc"}, localtime=NO_LINK) is None


def test_local_time_zone_falls_back_to_utc_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="treadle.config"):
        assert resolve_timezone("local", {}, localtime=NO_LINK) == "UTC"

    assert "could not detect the local time zone" in caplog.text


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("America/New_York", "America/New_York"),
        (":Europe/London", "Europe/London"),
        ("/usr/share/zoneinfo/Asia/Tokyo", "Asia/Tokyo"),
        ("UTC", "UTC"),
        ("Not/AZone", None),
        ("", None),
    ],
)
def test_detect_timezone_from_tz(tz: str, expected: str | None) -> None:
    assert detect_timezone({"TZ": tz}, localtime=NO_LINK) == expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/var/db/timezone/zoneinfo/America/New_York", "America/New_York"),
        ("../usr/share/zoneinfo/Europe/London", "Europe/London"),
        ("/usr/share/zoneinfo/Not/AZone", None),
        ("/etc/alternatives/localtime", None),
    ],
)
def test_detect_timezone_from_the_localtime_link(
    tmp_path: Path, target: str, expected: str | None
) -> None:
    link = tmp_path / "localtime"
    link.symlink_to(target)

    assert detect_timezone({}, localtime=link) == expected
    assert detect_timezone({"TZ": "bogus"}, localtime=link) == expected


def test_detect_timezone_needs_a_link(tmp_path: Path) -> None:
    regular = tmp_path / "localtime"
    regular.write_bytes(b"TZif")

    assert detect_timezone({}, localtime=regular) is None
    assert detect_timezone({}, localtime=NO_LINK) is None


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LocalHost", True),
        ("0.0.0.0", False),  # noqa: S104 - a value under test, not a bind
        ("192.168.1.10", False),
        ("::", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback
    assert ServerSettings(host=host).is_loopback is loopback


@pytest.mark.parametrize("zone", ["UTC", "Europe/London", "America/Argentina/ComodRivadavia"])
def test_default_config_text_lines_up_its_comments_for_any_zone(zone: str) -> None:
    text = default_config_text(zone)
    columns = {
        match.start(1)
        for line in text.splitlines()
        if (match := re.search(r"\S\s+(# )", line)) is not None
    }

    assert f'timezone = "{zone}"\n' in text
    assert len(columns) == 1


def test_default_config_text_loads_as_the_defaults(project: ProjectFactory) -> None:
    text = default_config_text("Europe/London")
    project.write_config(text)
    settings = project.settings()

    assert tomllib.loads(text)["timezone"] == "Europe/London"
    assert settings.timezone == "Europe/London"
    assert (settings.server, settings.runs, settings.limits) == (
        ServerSettings(),
        RunSettings(),
        Limits(),
    )
    assert settings.workflows_dir == project.root.resolve() / "workflows"
