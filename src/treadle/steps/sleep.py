"""The `sleep` step type: waits for a duration. Cancellation interrupts it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import ClassVar, Final, Literal, cast

from treadle.schema import Duration, Field, Obj
from treadle.steps.base import StepResult, StepRuntime
from treadle.types import Issue, JSONValue, YamlPath

_SCHEMA: Final = Obj("sleep step", {"duration": Field(Duration(max=86400.0), required=True)})


class SleepStep:
    """Sleeps for `duration` (0 to 24 hours). It has no outputs."""

    name = "sleep"
    output_names: ClassVar[frozenset[str] | None] = frozenset()
    reserved_outputs: ClassVar[frozenset[str]] = frozenset()
    taint_sinks: ClassVar[Mapping[str, Literal["code", "path", "url"]]] = {}

    @property
    def schema(self) -> Obj:
        """The `with` keys: duration (required)."""
        return _SCHEMA

    def check(self, params: Mapping[str, object], path: YamlPath) -> list[Issue]:
        """Return no issues; the schema covers every rule."""
        return []

    async def run(self, params: Mapping[str, JSONValue], rt: StepRuntime) -> StepResult:
        """Sleep with `asyncio.sleep`."""
        await asyncio.sleep(cast(float, params["duration"]))
        return StepResult()
