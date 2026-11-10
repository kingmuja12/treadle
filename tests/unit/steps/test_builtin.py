from __future__ import annotations

from tests.support.fakes import FakeStep

from treadle.schema import Obj
from treadle.steps.builtin import builtin_registry
from treadle.steps.file import FileStep
from treadle.steps.http import HttpStep
from treadle.steps.log import LogStep
from treadle.steps.shell import ShellStep
from treadle.steps.sleep import SleepStep


def test_the_registry_holds_exactly_the_five_built_in_types() -> None:
    registry = builtin_registry()

    assert registry.names() == ["file", "http", "log", "shell", "sleep"]
    for name, kind in [
        ("file", FileStep),
        ("http", HttpStep),
        ("log", LogStep),
        ("shell", ShellStep),
        ("sleep", SleepStep),
    ]:
        step_type = registry.get(name)
        assert isinstance(step_type, kind)
        assert step_type.name == name
        assert isinstance(step_type.schema, Obj)


def test_each_call_returns_an_independent_registry() -> None:
    first = builtin_registry()
    first.register(FakeStep())

    assert first.get("fake") is not None
    assert builtin_registry().get("fake") is None


def test_only_shell_has_dynamic_outputs() -> None:
    registry = builtin_registry()
    steps = [registry.get(name) for name in registry.names()]

    assert [step.name for step in steps if step and step.output_names is None] == ["shell"]
