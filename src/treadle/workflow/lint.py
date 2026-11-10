"""Lint warnings for loaded workflows (W101 to W106, W201, W202, W301, W302, W311, W312)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from treadle.expr.analysis import Reference, references, tree_expressions
from treadle.expr.ast import Expression, Template
from treadle.models import Defaults, Step, WatchTrigger, Workflow
from treadle.protocols import SecretLookup
from treadle.schema import suggest
from treadle.steps.base import StepRegistry
from treadle.types import Issue, Severity, YamlPath
from treadle.units import format_duration
from treadle.yamlio import YamlDocument

TAINTED_TRIGGER_FIELDS: Final[frozenset[str]] = frozenset(
    {"body", "headers", "query", "changes", "paths"}
)

# The outputs of an `http` step that carry what the server sent.
_HTTP_RESPONSE: Final = frozenset({"body", "headers", "json", "url"})
_MIN_MASKED_SECRET: Final = 4
_MIN_HOOK_SECRET: Final = 16
_Taint = Literal["untrusted", "data"]


@dataclass(frozen=True, slots=True)
class _Site:
    """One expression and where it sits.

    `step` is None in workflow `env`, and `param` names the `with` key that holds it.
    """

    path: YamlPath
    expression: Expression
    step: Step | None
    param: str | None = None


def lint(
    workflow: Workflow,
    steps: StepRegistry,
    *,
    secrets: SecretLookup | None = None,
    state_dir: Path | None = None,
    workflows_dir: Path | None = None,
    environment_checks: bool = True,
    document: YamlDocument | None = None,
) -> list[Issue]:
    """Return warnings only.

    With `environment_checks=False`, the checks that depend on this machine (W201, W202, W301,
    W302, W311, and W312) are skipped, so a file validates the same way everywhere. They are
    also skipped when there is nothing to check against: W201, W202, W311, and W312 need
    `secrets`, and W302 needs `state_dir` or `workflows_dir`. With the workflow's `document`,
    each warning carries its file position.
    """
    linter = _Linter(workflow, steps, document)
    linter.expressions(secrets if environment_checks else None)
    linter.timeouts()
    if environment_checks:
        linter.watch_paths(state_dir, workflows_dir)
        if secrets is not None:
            linter.webhook_secrets(secrets)
    return linter.issues


class _Linter:
    """Collects the warnings of one workflow."""

    def __init__(
        self, workflow: Workflow, steps: StepRegistry, document: YamlDocument | None
    ) -> None:
        self.workflow = workflow
        self.registry = steps
        self.document = document
        self.issues: list[Issue] = []
        self.by_id = {step.id: step for step in workflow.steps}

    def warn(
        self,
        code: str,
        message: str,
        path: YamlPath,
        *,
        hint: str | None = None,
        offset: int | None = None,
    ) -> None:
        position = None
        if self.document is not None:
            position = (
                self.document.position(path)
                if offset is None
                else self.document.offset_position(path, offset)
            )
        self.issues.append(Issue(Severity.WARNING, code, message, path, position, hint))

    def expressions(self, secrets: SecretLookup | None) -> None:
        """W101 to W104 and W106 for every expression, and W201 and W202 with `secrets`."""
        reported: set[str] = set()
        for site in self._sites():
            refs = references(site.expression)
            for ref in refs:
                offset = site.expression.offset + ref.start
                self._output_name(site, ref, offset)
                name = ref.path[1] if ref.path[0] == "secrets" and len(ref.path) > 1 else None
                if secrets is not None and name is not None and name not in reported:
                    reported.add(name)
                    self._secret(site, name, secrets, offset)
            self._taint(site, refs)

    def timeouts(self) -> None:
        """W105: a step timeout longer than the workflow timeout.

        Steps that keep the default step timeout share one warning, at `defaults.timeout`
        when the file sets it and at the workflow `timeout` otherwise.
        """
        limit, default = self.workflow.timeout, self.workflow.defaults.timeout
        for step in self.workflow.steps:
            if step.timeout > limit and step.timeout != default:
                self._timeout_warning("step", step.timeout, ("steps", step.position, "timeout"))
        if default > limit and any(step.timeout == default for step in self.workflow.steps):
            written = default != Defaults().timeout
            where: YamlPath = ("defaults", "timeout") if written else ("timeout",)
            self._timeout_warning("default step", default, where)

    def _timeout_warning(self, label: str, timeout: float, where: YamlPath) -> None:
        message = (
            f"the {label} timeout ({format_duration(timeout)}) is longer than the workflow "
            f"timeout ({format_duration(self.workflow.timeout)})"
        )
        hint = "the run times out first; lower the step timeout or raise `timeout`"
        self.warn("W105", message, where, hint=hint)

    def watch_paths(self, state_dir: Path | None, workflows_dir: Path | None) -> None:
        """W301 for missing watch paths and W302 for paths that hold Treadle's directories."""
        watches = [
            trigger for trigger in self.workflow.triggers if isinstance(trigger, WatchTrigger)
        ]
        for index, trigger in enumerate(watches):
            for number, path in enumerate(trigger.paths):
                where = ("triggers", "watch", index, "paths", number)
                if not os.path.exists(path):
                    hint = "the trigger stays degraded until the path exists"
                    self.warn("W301", f"the watch path {path} doesn't exist", where, hint=hint)
                owned = [
                    label
                    for label, directory in (("state", state_dir), ("workflows", workflows_dir))
                    if directory is not None and _contains(path, directory)
                ]
                if owned:
                    plural = "directories" if len(owned) > 1 else "directory"
                    message = f"the watch path {path} contains the {' and '.join(owned)} {plural}"
                    hint = "Treadle writes there, so the workflow could trigger itself"
                    self.warn("W302", message, where, hint=hint)

    def webhook_secrets(self, secrets: SecretLookup) -> None:
        """W311 and W312: a webhook secret that is unset or too short disables the hook."""
        for index, hook in enumerate(self.workflow.webhooks()):
            where = ("triggers", "webhook", index, "secret_env")
            value = secrets.lookup_env(hook.secret_env)
            if value is None:
                self.warn(
                    "W311",
                    f"{hook.secret_env} isn't set, so the webhook '{hook.name}' is disabled",
                    where,
                    hint=f"set {hook.secret_env} in the environment or in .env",
                )
            elif len(value) < _MIN_HOOK_SECRET:
                self.warn(
                    "W312",
                    f"{hook.secret_env} is shorter than {_MIN_HOOK_SECRET} characters, so the "
                    f"webhook '{hook.name}' is disabled",
                    where,
                    hint=f"use at least {_MIN_HOOK_SECRET} random characters",
                )

    def _sites(self) -> Iterator[_Site]:
        for name, template in self.workflow.env:
            for expression in _parts(template):
                yield _Site(("env", name), expression, None)
        for step in self.workflow.steps:
            base = ("steps", step.position)
            yield _Site((*base, "if"), step.condition.expression, step)
            for name, template in step.env:
                for expression in _parts(template):
                    yield _Site((*base, "env", name), expression, step)
            for name, tree in step.params.items():
                for path, expression in tree_expressions(tree, self._param_path(step, name)):
                    yield _Site(path, expression, step, name)
            for name, template in step.outputs:
                for expression in _parts(template):
                    yield _Site((*base, "outputs", name), expression, step)

    def _param_path(self, step: Step, name: str) -> YamlPath:
        """Return where a `with` value is written; a step-level `run` stands for with.run."""
        shorthand = ("steps", step.position, "run")
        written = self.document is not None and shorthand in self.document.value_positions
        return shorthand if name == "run" and written else ("steps", step.position, "with", name)

    def _taint(self, site: _Site, refs: list[Reference]) -> None:
        """W101 to W104: untrusted data, inputs, or secrets where they are dangerous."""
        step = site.step
        if step is None or site.param is None:
            return
        step_type = self.registry.get(step.uses)
        sink = step_type.taint_sinks.get(site.param) if step_type is not None else None
        if step.uses == "shell" and site.param == "argv":
            secret = next((ref for ref in refs if ref.path[0] == "secrets"), None)
            if secret is not None:
                message = (
                    f"the secret `{_text(secret)}` is in `argv`, where process listings can show it"
                )
                hint = "pass it through env, or read it in a `run` script"
                self._warn_at("W104", message, site, secret, hint)
        if sink is None:
            return
        kinds = [(self._taint_of(ref.path), ref) for ref in refs]
        untrusted = next((ref for kind, ref in kinds if kind == "untrusted"), None)
        data = next((ref for kind, ref in kinds if kind == "data"), None)
        if untrusted is not None and sink == "code":
            message = f"`{_text(untrusted)}` puts untrusted data into the `{site.param}` script"
            self._warn_at("W101", message, site, untrusted, _env_hint(untrusted))
        elif untrusted is not None:
            message = f"`{_text(untrusted)}` puts untrusted data into `{site.param}`"
            hint = f"check the value first, or build the {sink} from trusted parts"
            self._warn_at("W103", message, site, untrusted, hint)
        elif data is not None and sink == "code":
            message = f"`{_text(data)}` is pasted into the `{site.param}` script as code"
            self._warn_at("W102", message, site, data, _env_hint(data))

    def _warn_at(self, code: str, message: str, site: _Site, ref: Reference, hint: str) -> None:
        """Warn at the position of `ref` inside the site's expression."""
        offset = site.expression.offset + ref.start
        self.warn(code, message, site.path, hint=hint, offset=offset)

    def _taint_of(self, path: tuple[str, ...]) -> _Taint | None:
        """Classify a reference: untrusted (trigger payloads, HTTP responses) or data."""
        root = path[0]
        if root == "trigger":
            return "untrusted" if len(path) == 1 or path[1] in TAINTED_TRIGGER_FIELDS else None
        if root == "inputs":
            return "data"
        if root != "steps" or (len(path) > 2 and path[2] != "outputs"):
            return None
        source = self.by_id.get(path[1]) if len(path) > 1 else None
        response = len(path) < 4 or path[3] in _HTTP_RESPONSE
        return "untrusted" if source is not None and source.uses == "http" and response else "data"

    def _output_name(self, site: _Site, ref: Reference, offset: int) -> None:
        """W106: an output that a step type with fixed outputs never sets."""
        path = ref.path
        if path[0] == "steps" and len(path) > 3 and path[2] == "outputs":
            source, name = self.by_id.get(path[1]), path[3]
            declared = {output for output, _ in source.outputs} if source else set()
        elif path[0] == "result" and len(path) > 1 and site.step is not None:
            source, name, declared = site.step, path[1], set()
        else:
            return
        step_type = self.registry.get(source.uses) if source is not None else None
        fixed = step_type.output_names if step_type is not None else None
        if source is None or fixed is None or name in fixed or name in declared:
            return
        names = sorted(fixed | declared)
        close = suggest(name, names)
        hint = (
            f"did you mean '{close}'?"
            if close
            else (f"its outputs are: {', '.join(names)}" if names else "it has no outputs")
        )
        message = f"step '{source.id}' has no output '{name}'"
        self.warn("W106", message, site.path, hint=hint, offset=offset)

    def _secret(self, site: _Site, name: str, secrets: SecretLookup, offset: int) -> None:
        """W201 for an undefined secret and W202 for one too short to mask."""
        value = secrets.get(name)
        if value is None:
            hint = f"set TREADLE_SECRET_{name} in the environment, or {name} in .env"
            self.warn("W201", f"secret '{name}' isn't defined", site.path, hint=hint, offset=offset)
        elif len(value) < _MIN_MASKED_SECRET:
            self.warn(
                "W202",
                f"secret '{name}' is shorter than {_MIN_MASKED_SECRET} characters, so masking "
                "ignores it",
                site.path,
                hint="use a longer secret",
                offset=offset,
            )


def _parts(template: Template) -> Iterator[Expression]:
    return (part for part in template.parts if isinstance(part, Expression))


def _text(ref: Reference) -> str:
    return ".".join(ref.path)


def _env_hint(ref: Reference) -> str:
    return (
        "pass it through env instead: set `env: {VALUE: ${{ " + _text(ref) + " }}}`, "
        'then quote "$VALUE" in the script'
    )


def _contains(outer: Path, inner: Path) -> bool:
    """True when `inner` is `outer` or inside it, comparing resolved paths."""
    try:
        return inner.resolve().is_relative_to(outer.resolve())
    except (OSError, RuntimeError):  # a symlink loop
        return False
