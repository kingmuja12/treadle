from __future__ import annotations

import pytest

from treadle.expr.ast import STATUS_FUNCTIONS, Expression, Name, Template

NAME = Expression("inputs", Name("inputs", 0, 6), offset=4)


@pytest.mark.parametrize(
    ("parts", "is_static", "single"),
    [
        ((), True, None),
        (("plain text",), True, None),
        ((NAME,), False, NAME),
        (("  ", NAME, "\n"), False, NAME),
        (("Hello ", NAME), False, None),
        ((NAME, " ", NAME), False, None),
    ],
)
def test_template_properties(
    parts: tuple[str | Expression, ...], is_static: bool, single: Expression | None
) -> None:
    template = Template("source", parts)

    assert template.is_static is is_static
    assert template.single == single


def test_status_functions() -> None:
    assert frozenset({"success", "failure", "always", "cancelled"}) == STATUS_FUNCTIONS
