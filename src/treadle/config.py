"""Project settings: finding and validating `treadle.toml`, precedence, and path resolution.

Precedence, from highest to lowest: CLI flags, then `--config` or `TREADLE_CONFIG` (which only
select the file), then `treadle.toml` found by searching upward, then built-in defaults.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo

from treadle.errors import ConfigError
from treadle.schema import Bool, Duration, Field, Int, ListOf, Obj, Size, Str, validate

CONFIG_FILENAME: Final = "treadle.toml"
_DAY: Final = 86400.0
_KIB: Final = 1024
_MIB: Final = 1024**2
_LOCALTIME: Final = Path("/etc/localtime")
_DEFAULT_DIRS: Final = {"workflows_dir": "workflows", "state_dir": ".treadle"}
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """The `[server]` table. `session_ttl` is in seconds."""

    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "TREADLE_TOKEN"  # noqa: S105 - a variable name, not a secret
    allowed_hosts: tuple[str, ...] = ()
    secure_cookies: bool = False
    session_ttl: float = 43200.0

    @property
    def is_loopback(self) -> bool:
        """True when `host` is a loopback address or "localhost"."""
        return is_loopback_host(self.host)


@dataclass(frozen=True, slots=True)
class RunSettings:
    """The `[runs]` table. Durations are in seconds."""

    max_concurrent: int = 4
    kill_grace: float = 5.0
    shutdown_grace: float = 15.0


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """The `[retention]` table. Durations are in seconds."""

    max_age: float = 2_592_000.0
    max_runs_per_workflow: int = 500
    interval: float = 3600.0


@dataclass(frozen=True, slots=True)
class Limits:
    """The `[limits]` table, in bytes except `watch_max_entries`."""

    step_log_bytes: int = 1 << 20
    run_log_bytes: int = 16 << 20
    line_bytes: int = 16 << 10
    output_file_bytes: int = 1 << 20
    outputs_bytes: int = 1 << 20
    stdout_capture_bytes: int = 64 << 10
    webhook_body_bytes: int = 1 << 20
    api_body_bytes: int = 64 << 10
    watch_max_entries: int = 10_000


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    """The `[security]` table. `file_roots` are as written: "~" and relative paths allowed."""

    file_roots: tuple[str, ...] = (".", "~")


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """The `[logging]` table."""

    level: Literal["debug", "info", "warning", "error"] = "info"


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved settings. Every path is absolute, and `timezone` is an IANA name."""

    project_dir: Path
    config_path: Path | None
    workflows_dir: Path
    state_dir: Path
    env_file: Path | None
    timezone: str
    server: ServerSettings = ServerSettings()
    runs: RunSettings = RunSettings()
    retention: RetentionSettings = RetentionSettings()
    limits: Limits = Limits()
    security: SecuritySettings = SecuritySettings()
    logging: LoggingSettings = LoggingSettings()

    @property
    def db_path(self) -> Path:
        """The SQLite database, `state_dir/treadle.db`."""
        return self.state_dir / "treadle.db"

    @property
    def lock_path(self) -> Path:
        """The instance lock file, `state_dir/treadle.lock`."""
        return self.state_dir / "treadle.lock"

    @property
    def holder_path(self) -> Path:
        """The lock holder record, `state_dir/holder.json`."""
        return self.state_dir / "holder.json"

    @property
    def secret_key_path(self) -> Path:
        """The key that signs sessions and CSRF tokens, `state_dir/secret.key`."""
        return self.state_dir / "secret.key"

    @property
    def tmp_dir(self) -> Path:
        """Per-run scratch directories, `state_dir/tmp`."""
        return self.state_dir / "tmp"

    @property
    def backups_dir(self) -> Path:
        """Pre-migration database copies, `state_dir/backups`."""
        return self.state_dir / "backups"

    def file_roots(self) -> tuple[Path, ...]:
        """The `file` step roots: "~" expanded, relative to `project_dir`, symlinks resolved."""
        return tuple(_resolve(self.project_dir, root) for root in self.security.file_roots)

    def protected_paths(self) -> tuple[Path, ...]:
        """The state, workflows, and project directories and each file root, resolved.

        The `file` step never modifies the state directory, and never deletes or moves any of
        these paths.
        """
        own = (self.state_dir, self.workflows_dir, self.project_dir)
        return tuple(dict.fromkeys([*(path.resolve() for path in own), *self.file_roots()]))


@dataclass(frozen=True, slots=True)
class Overrides:
    """Settings from CLI flags. Relative paths resolve against the current directory."""

    config: Path | None = None
    workflows_dir: Path | None = None
    state_dir: Path | None = None
    host: str | None = None
    port: int | None = None
    log_level: str | None = None


def _table(name: str, **fields: Field) -> Field:
    return Field(Obj(f"[{name}]", fields))


# Keys have no defaults here: missing keys keep the dataclass defaults above.
_SCHEMA: Final = Obj(
    CONFIG_FILENAME,
    {
        "workflows_dir": Field(Str(min_len=1)),
        "state_dir": Field(Str(min_len=1)),
        "env_file": Field(Str()),
        "timezone": Field(Str(min_len=1)),
        "server": _table(
            "server",
            host=Field(Str(min_len=1)),
            port=Field(Int(min=0, max=65535)),
            token_env=Field(Str(pattern=r"[A-Za-z_][A-Za-z0-9_]*")),
            allowed_hosts=Field(ListOf(Str(min_len=1))),
            secure_cookies=Field(Bool()),
            session_ttl=Field(Duration(min=300, max=30 * _DAY)),
        ),
        "runs": _table(
            "runs",
            max_concurrent=Field(Int(min=1, max=64)),
            kill_grace=Field(Duration(min=0, max=60)),
            shutdown_grace=Field(Duration(min=1, max=600)),
        ),
        "retention": _table(
            "retention",
            max_age=Field(Duration(min=3600, max=3650 * _DAY)),
            max_runs_per_workflow=Field(Int(min=1, max=100_000)),
            interval=Field(Duration(min=60, max=7 * _DAY)),
        ),
        "limits": _table(
            "limits",
            step_log_bytes=Field(Size(min=64 * _KIB, max=64 * _MIB)),
            run_log_bytes=Field(Size(min=_MIB, max=1024 * _MIB)),
            line_bytes=Field(Size(min=_KIB, max=_MIB)),
            output_file_bytes=Field(Size(min=_KIB, max=16 * _MIB)),
            outputs_bytes=Field(Size(min=_KIB, max=16 * _MIB)),
            stdout_capture_bytes=Field(Size(min=_KIB, max=_MIB)),
            webhook_body_bytes=Field(Size(min=_KIB, max=10 * _MIB)),
            api_body_bytes=Field(Size(min=4 * _KIB, max=_MIB)),
            watch_max_entries=Field(Int(min=1, max=500_000)),
        ),
        "security": _table("security", file_roots=Field(ListOf(Str(min_len=1), min_len=1))),
        "logging": _table(
            "logging", level=Field(Str(choices=("debug", "info", "warning", "error")))
        ),
    },
)

_CONFIG_TEMPLATE: Final = """\
# Treadle project configuration. Every key is optional.
# Commented keys show the defaults. Relative paths resolve against this file's directory.

workflows_dir = "workflows"
state_dir = ".treadle"
env_file = ".env"           # "" disables it
timezone = "{timezone}"     # an IANA name, or "local" to detect it at startup

[server]
# host = "127.0.0.1"        # any other address requires TREADLE_TOKEN
# port = 8765               # 0 picks a free port
# token_env = "TREADLE_TOKEN"
# allowed_hosts = []
# secure_cookies = false
# session_ttl = "12h"

[runs]
# max_concurrent = 4
# kill_grace = "5s"
# shutdown_grace = "15s"

[retention]
# max_age = "30d"
# max_runs_per_workflow = 500
# interval = "1h"

[limits]
# step_log_bytes = "1MiB"
# run_log_bytes = "16MiB"
# line_bytes = "16KiB"
# output_file_bytes = "1MiB"
# outputs_bytes = "1MiB"
# stdout_capture_bytes = "64KiB"
# webhook_body_bytes = "1MiB"
# api_body_bytes = "64KiB"
# watch_max_entries = 10000

[security]
# file_roots = [".", "~"]

[logging]
# level = "info"            # debug, info, warning, or error
"""


def find_config(start: Path) -> Path | None:
    """Return the nearest `treadle.toml` in `start` or one of its parents, or None."""
    start = start.resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_settings(
    overrides: Overrides,
    *,
    cwd: Path,
    environ: Mapping[str, str],
    require_project: bool = True,
) -> Settings:
    """Load, validate, and resolve the settings for a command started in `cwd`.

    Without a config file, `require_project=True` raises ConfigError with the hint to run
    `treadle init`; otherwise `cwd` is the project directory and defaults apply. Every problem
    in the file is reported in one ConfigError whose issues carry the key paths.
    """
    cwd = cwd.resolve()
    config_path = _config_path(overrides, cwd, environ)
    if config_path is None and require_project:
        raise ConfigError(f"no {CONFIG_FILENAME} found in {cwd} or its parents; run `treadle init`")
    raw = _read_toml(config_path) if config_path is not None else {}
    _apply_overrides(raw, overrides)
    data, issues = validate(_SCHEMA, raw)
    if issues:
        source = config_path or "command-line options"
        raise ConfigError(f"invalid configuration in {source}", issues=issues)
    values = cast(dict[str, Any], data)
    project_dir = config_path.parent if config_path is not None else cwd
    env_file = values.get("env_file", ".env")
    return Settings(
        project_dir=project_dir,
        config_path=config_path,
        workflows_dir=_choose_dir(
            overrides.workflows_dir, values, "workflows_dir", cwd, project_dir
        ),
        state_dir=_choose_dir(overrides.state_dir, values, "state_dir", cwd, project_dir),
        env_file=_resolve(project_dir, env_file) if env_file else None,
        timezone=resolve_timezone(values.get("timezone", "local"), environ),
        server=ServerSettings(**_table_values(values, "server")),
        runs=RunSettings(**_table_values(values, "runs")),
        retention=RetentionSettings(**_table_values(values, "retention")),
        limits=Limits(**_table_values(values, "limits")),
        security=SecuritySettings(**_table_values(values, "security")),
        logging=LoggingSettings(**_table_values(values, "logging")),
    )


def resolve_timezone(name: str, environ: Mapping[str, str], *, localtime: Path = _LOCALTIME) -> str:
    """Return an IANA zone name. "local" detects the zone, falling back to UTC with a warning.

    Any other name must load with `zoneinfo.ZoneInfo`, or ConfigError is raised. `localtime`
    is the link that detection reads after `TZ`.
    """
    if name != "local":
        if not _is_zone(name):
            raise ConfigError(f"unknown time zone {name!r}; use an IANA name such as Europe/London")
        return name
    detected = detect_timezone(environ, localtime=localtime)
    if detected is None:
        _log.warning("could not detect the local time zone; using UTC")
        return "UTC"
    return detected


def detect_timezone(environ: Mapping[str, str], *, localtime: Path = _LOCALTIME) -> str | None:
    """Return the local IANA zone name from `TZ`, then the `/etc/localtime` link, or None."""
    candidates = [environ.get("TZ", "").removeprefix(":")]
    with contextlib.suppress(OSError):
        candidates.append(str(localtime.readlink()))
    for candidate in candidates:
        name = candidate.rpartition("zoneinfo/")[2]
        if name and _is_zone(name):
            return name
    return None


def is_loopback_host(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 (with or without brackets), and "localhost"."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def default_config_text(timezone: str) -> str:
    """Return the commented `treadle.toml` that `treadle init` writes, with `timezone` set."""
    return _CONFIG_TEMPLATE.replace("{timezone}", timezone)


def _config_path(overrides: Overrides, cwd: Path, environ: Mapping[str, str]) -> Path | None:
    explicit = overrides.config or environ.get("TREADLE_CONFIG")
    if not explicit:
        return find_config(cwd)
    path = _resolve(cwd, explicit)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError) as exc:  # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _apply_overrides(raw: dict[str, Any], overrides: Overrides) -> None:
    """Merge CLI flags into the raw TOML data, so the schema validates them too."""
    flags = (
        ("server", "host", overrides.host),
        ("server", "port", overrides.port),
        ("logging", "level", overrides.log_level),
    )
    for table, key, value in flags:
        section = raw.setdefault(table, {})
        if value is not None and isinstance(section, dict):
            section[key] = value


def _choose_dir(
    flag: Path | None, values: dict[str, Any], key: str, cwd: Path, project_dir: Path
) -> Path:
    if flag is not None:
        return _resolve(cwd, flag)
    return _resolve(project_dir, values.get(key, _DEFAULT_DIRS[key]))


def _table_values(values: dict[str, Any], table: str) -> dict[str, Any]:
    """Return a validated table as constructor arguments, with lists turned into tuples."""
    entries = values.get(table, {}).items()
    return {key: tuple(value) if isinstance(value, list) else value for key, value in entries}


def _resolve(base: Path, raw: str | Path) -> Path:
    return (base / Path(raw).expanduser()).resolve()


def _is_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return False
    return True
