from __future__ import annotations

import pytest
from tests.support.fakes import FakeStep

from treadle.steps.base import StepRegistry, StepResult


def test_registry_registers_and_looks_up_step_types() -> None:
    shell, fake = FakeStep("shell"), FakeStep("fake")
    registry = StepRegistry([shell])
    registry.register(fake)

    assert registry.get("shell") is shell
    assert registry.get("fake") is fake
    assert registry.get("missing") is None
    assert registry.names() == ["fake", "shell"]


def test_registry_rejects_duplicate_names() -> None:
    registry = StepRegistry([FakeStep("log")])

    with pytest.raises(ValueError, match="step type 'log' is already registered"):
        registry.register(FakeStep("log"))
    with pytest.raises(ValueError, match="already registered"):
        StepRegistry([FakeStep("x"), FakeStep("x")])


def test_an_empty_registry() -> None:
    assert StepRegistry().names() == []


def test_step_result_defaults() -> None:
    assert StepResult() == StepResult(outputs={}, exit_code=None)
    assert StepResult().outputs is not StepResult().outputs
