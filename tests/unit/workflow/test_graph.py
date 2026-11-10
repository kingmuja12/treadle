from __future__ import annotations

from collections.abc import Callable

from tests.support.builders import make_step, make_workflow

from treadle.models import Workflow
from treadle.workflow.graph import compute_ancestors, depths, to_json, to_mermaid, to_text

BACKUP = make_workflow(
    make_step("check-drive", uses="shell"),
    make_step("archive", uses="shell"),
    make_step("record", uses="file"),
    make_step("alert", uses="http", needs=["archive"], condition="failure()"),
    workflow_id="nightly-backup",
)
FAN_OUT = make_workflow(
    make_step("a"),
    make_step("b", needs=[]),
    make_step("c", needs=["a"]),
    make_step("d", needs=[]),
    make_step("e", needs=["b", "c"]),
)


def test_ancestors_are_transitive() -> None:
    order = ["a", "b", "c", "d"]
    needs = {"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")}

    assert compute_ancestors(order, needs) == {
        "a": frozenset(),
        "b": frozenset({"a"}),
        "c": frozenset({"a"}),
        "d": frozenset({"a", "b", "c"}),
    }


def test_steps_without_needs_have_no_ancestors() -> None:
    assert compute_ancestors(["a", "b"], {}) == {"a": frozenset(), "b": frozenset()}


def test_depths_count_the_longest_chain_from_a_root() -> None:
    assert depths(FAN_OUT) == {"a": 0, "b": 0, "c": 1, "d": 0, "e": 2}


def test_text_lists_steps_by_depth_then_position() -> None:
    assert to_text(BACKUP) == (
        "[0] check-drive  shell  if: success()\n"
        "[1] archive  shell  needs: check-drive (implicit)  if: success()\n"
        "[2] record  file  needs: archive (implicit)  if: success()\n"
        "[2] alert  http  needs: archive  if: failure()"
    )
    assert [line.split()[1] for line in to_text(FAN_OUT).splitlines()] == [
        "a",
        "b",
        "d",
        "c",
        "e",
    ]


def test_text_puts_a_multi_line_condition_on_one_line(
    load_workflow: Callable[..., Workflow],
) -> None:
    workflow = load_workflow(
        """\
        steps:
          - id: only
            if: |
              success() &&
                true
            uses: fake
        """
    )

    assert to_text(workflow) == "[0] only  fake  if: success() && true"


def test_mermaid_uses_position_ids_and_dashed_implicit_edges() -> None:
    assert to_mermaid(BACKUP) == (
        "flowchart TD\n"
        '    s0["check-drive · shell"]\n'
        '    s1["archive · shell"]\n'
        '    s2["record · file"]\n'
        '    s3["alert · http"]\n'
        "    s0 -.-> s1\n"
        "    s1 -.-> s2\n"
        "    s1 --> s3"
    )


def test_mermaid_escapes_quotes_in_labels() -> None:
    workflow = make_workflow(make_step("quote", uses='say"hi'))

    assert to_mermaid(workflow).splitlines()[1] == '    s0["quote · say#quot;hi"]'


def test_json_lists_nodes_and_edges() -> None:
    assert to_json(BACKUP) == {
        "workflow": "nightly-backup",
        "nodes": [
            {
                "id": "check-drive",
                "name": "check-drive",
                "uses": "shell",
                "if": "success()",
                "depth": 0,
            },
            {"id": "archive", "name": "archive", "uses": "shell", "if": "success()", "depth": 1},
            {"id": "record", "name": "record", "uses": "file", "if": "success()", "depth": 2},
            {"id": "alert", "name": "alert", "uses": "http", "if": "failure()", "depth": 2},
        ],
        "edges": [
            {"from": "check-drive", "to": "archive", "implicit": True},
            {"from": "archive", "to": "record", "implicit": True},
            {"from": "archive", "to": "alert", "implicit": False},
        ],
    }
