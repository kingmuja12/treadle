"""The built-in step registry."""

from __future__ import annotations

from treadle.steps.base import StepRegistry
from treadle.steps.file import FileStep
from treadle.steps.http import HttpStep
from treadle.steps.log import LogStep
from treadle.steps.shell import ShellStep
from treadle.steps.sleep import SleepStep


def builtin_registry() -> StepRegistry:
    """Return a registry with the five built-in types: shell, http, file, log, and sleep.

    Each call returns a new registry, so callers can register more types without sharing.
    """
    return StepRegistry([ShellStep(), HttpStep(), FileStep(), LogStep(), SleepStep()])
