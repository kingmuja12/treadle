"""The step graph: ancestors, depths, and the `treadle graph` formats."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from treadle.models import Step, Workflow
from treadle.types import JSONObject, JSONValue


def compute_ancestors(
    order: Sequence[str], needs: Mapping[str, tuple[str, ...]]
) -> dict[str, frozenset[str]]:
    """Return every step's direct and transitive dependencies.

    `order` is list order, in which `needs` names only earlier steps.
    """
    ancestors: dict[str, frozenset[str]] = {}
    for step_id in order:
        found: set[str] = set()
        for need in needs.get(step_id, ()):
            found.add(need)
            found.update(ancestors.get(need, ()))
        ancestors[step_id] = frozenset(found)
    return ancestors


def depths(workflow: Workflow) -> dict[str, int]:
    """Return each step's depth in the graph. Root steps are 0."""
    result: dict[str, int] = {}
    for step in workflow.steps:
        result[step.id] = 1 + max((result[need] for need in step.needs), default=-1)
    return result


def to_text(workflow: Workflow) -> str:
    """Return one line per step, ordered by depth and position, as `treadle graph` prints it.

    Each line reads `[depth] id  uses  needs: a, b  if: condition`, and implicit needs are
    marked `(implicit)`. A root step has no `needs:` part.
    """
    # The bracketed number is the depth, not the position: lines are ordered by depth, and
    # the position only breaks ties.
    level = depths(workflow)
    lines = []
    for step in sorted(workflow.steps, key=lambda step: (level[step.id], step.position)):
        parts = [f"[{level[step.id]}] {step.id}", step.uses]
        if step.needs:
            implicit = " (implicit)" if step.implicit_needs else ""
            parts.append(f"needs: {', '.join(step.needs)}{implicit}")
        parts.append(f"if: {_condition(step)}")
        lines.append("  ".join(parts))
    return "\n".join(lines)


def to_mermaid(workflow: Workflow) -> str:
    """Return a Mermaid `flowchart TD` block, with dashed edges for implicit needs.

    Nodes are `s0`, `s1`, and so on by position, labeled `"<id> · <uses>"`.
    """
    node = {step.id: f"s{step.position}" for step in workflow.steps}
    lines = ["flowchart TD"]
    for step in workflow.steps:
        label = f"{step.id} · {step.uses}".replace('"', "#quot;")
        lines.append(f'    {node[step.id]}["{label}"]')
    for step in workflow.steps:
        arrow = "-.->" if step.implicit_needs else "-->"
        lines.extend(f"    {node[need]} {arrow} {node[step.id]}" for need in step.needs)
    return "\n".join(lines)


def to_json(workflow: Workflow) -> JSONObject:
    """Return `{"workflow", "nodes", "edges"}` for `treadle graph --format json`.

    Nodes hold `id`, `name`, `uses`, `if`, and `depth`, in position order. Edges hold
    `from`, `to`, and `implicit`.
    """
    level = depths(workflow)
    nodes: list[JSONValue] = [
        {
            "id": step.id,
            "name": step.name,
            "uses": step.uses,
            "if": step.condition.source,
            "depth": level[step.id],
        }
        for step in workflow.steps
    ]
    edges: list[JSONValue] = [
        {"from": need, "to": step.id, "implicit": step.implicit_needs}
        for step in workflow.steps
        for need in step.needs
    ]
    return {"workflow": workflow.id, "nodes": nodes, "edges": edges}


def _condition(step: Step) -> str:
    """Return the condition on one line, with runs of whitespace collapsed."""
    return " ".join(step.condition.source.split())
