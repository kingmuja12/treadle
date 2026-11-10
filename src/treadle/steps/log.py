"""The `log` step type: writes one system line, `[level] message`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final, Literal

from treadle.schema import Field, Obj, Str
from treadle.steps.base import StepResult, StepRuntime
from treadle.types import Issue, JSONValue, YamlPath

_SCHEMA: Final = Obj(
    "log step",
    {
        "message": Field(Str(), required=True),
        "level": Field(Str(choices=("info", "warning", "error")), default="info"),
    },
)


class LogStep:
    """Writes `message` at `level` (info, warning, or error). It has no outputs."""

    name = "log"
    output_names: ClassVar[frozenset[str] | None] = frozenset()
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    @property
    def schema(self) -> Obj:
        """The `with` keys: message (required) and level."""
        return _SCHEMA

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return no issues; the schema covers every rule."""
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Write the system line."""
        rt.log.system(f"[{params['level']}] {params['message']}")
        return StepResult()
