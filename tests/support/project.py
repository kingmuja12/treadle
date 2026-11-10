"""Temporary Treadle projects for tests."""

from __future__ import annotations

import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from treadle.config import CONFIG_FILENAME, Overrides, Settings, load_settings

DEFAULT_CONFIG = 'timezone = "UTC"\n'
_ESCAPES = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"})


class ProjectFactory:
    """A project directory with `treadle.toml`, `workflows/`, and an empty `.env` (mode 0600).

    The config pins `timezone = "UTC"`, so settings don't depend on the machine. `environ` is
    the environment that `settings()` passes to `load_settings`.
    """

    def __init__(self, root: Path, *, environ: Mapping[str, str] | None = None) -> None:
        self.root = root
        self.environ: dict[str, str] = dict(environ or {})
        self.config_path = root / CONFIG_FILENAME
        self.workflows_dir = root / "workflows"
        self.state_dir = root / ".treadle"
        self.env_path = root / ".env"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.write_config(DEFAULT_CONFIG)
        self.write_env({})

    def write_config(self, text: str) -> Path:
        """Replace `treadle.toml` with `text`, dedented."""
        self.config_path.write_text(textwrap.dedent(text), encoding="utf-8")
        return self.config_path

    def write_workflow(self, name: str, text: str) -> Path:
        """Write `workflows/<name>`, adding `.yml` when `name` has no YAML suffix."""
        filename = name if name.endswith((".yml", ".yaml")) else f"{name}.yml"
        path = self.workflows_dir / filename
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def write_env(self, values: Mapping[str, str]) -> Path:
        """Replace `.env` with double-quoted `KEY="value"` lines, mode 0600."""
        lines = [f'{key}="{value.translate(_ESCAPES)}"\n' for key, value in values.items()]
        self.env_path.write_text("".join(lines), encoding="utf-8")
        self.env_path.chmod(0o600)
        return self.env_path

    def settings(self, **overrides: Any) -> Settings:
        """Load the project's settings; keyword arguments become `Overrides` fields."""
        return load_settings(Overrides(**overrides), cwd=self.root, environ=self.environ)
