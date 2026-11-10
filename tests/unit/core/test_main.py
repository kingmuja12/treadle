from __future__ import annotations

import runpy
from collections.abc import Sequence

import pytest


def test_python_dash_m_treadle_exits_with_the_cli_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Sequence[str] | None] = []

    def fake_main(argv: Sequence[str] | None = None) -> int:
        calls.append(argv)
        return 7

    monkeypatch.setattr("treadle.cli.main.main", fake_main)

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("treadle", run_name="__main__")

    assert caught.value.code == 7
    assert calls == [None]
