"""The workflow loader: from file bytes to a validated `Workflow`.

Loading takes these steps, which the comments below refer to by number: (1) check the
file's size and encoding, and load the YAML; (2) validate the structure; (3) compile the
templates; (4) validate each step's parameters; (5) build the step graph; and (6) check the
expressions and triggers, then lint. Every problem is reported as an `Issue`, which gets its
file position last.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from treadle.config import Limits, is_time_zone, time_zone_hint
from treadle.cron import parse_cron
from treadle.errors import CronError, ExpressionSyntaxError, InputError
from treadle.expr.analysis import calls, references, tree_expressions
from treadle.expr.ast import Call, Condition, Expression, Template, TemplateTree
from treadle.expr.evaluator import to_display
from treadle.expr.functions import FUNCTIONS
from treadle.expr.template import compile_tree, parse_condition, parse_template
from treadle.globs import compile_glob
from treadle.models import (
    CatchUp,
    Concurrency,
    ConcurrencyPolicy,
    Defaults,
    InputSpec,
    InputType,
    RetryPolicy,
    ScheduleTrigger,
    SignatureMode,
    Step,
    Trigger,
    WatchEvent,
    WatchTrigger,
    WebhookTrigger,
    Workflow,
    trigger_kind,
)
from treadle.protocols import SecretLookup
from treadle.schema import (
    AnyOf,
    Bool,
    CheckContext,
    Duration,
    Field,
    Int,
    ListOf,
    MapOf,
    Num,
    Obj,
    Size,
    Spec,
    Str,
    kind_of,
    suggest,
    validate,
)
from treadle.steps.base import StepRegistry, StepType
from treadle.types import Issue, Position, Severity, YamlPath, format_path
from treadle.units import format_duration
from treadle.workflow.graph import compute_ancestors
from treadle.workflow.inputs import coerce_inputs
from treadle.workflow.lint import lint
from treadle.yamlio import MAX_FILE_BYTES, YamlDocument, load_yaml

WORKFLOW_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_MAX_ISSUES: Final = 50
_MAX_TRIGGERS: Final = 20
_MAX_STEPS: Final = 100
_DAY: Final = 86400.0
_STEP_ID: Final = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
_ENV_NAME: Final = r"[A-Za-z_][A-Za-z0-9_]*"
_HOOK_NAME: Final = r"[a-z0-9][a-z0-9_-]{0,63}"
# Every signature header that Git servers send matches, and a name with a space or a colon,
# which could never match a request header, doesn't.
_HEADER_NAME: Final = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_RESERVED_ENV: Final = "TREADLE_"
_OPEN: Final = "${{"
_CONTEXTS: Final = ("inputs", "env", "secrets", "trigger", "run", "workflow", "steps", "result")
_STEP_FIELDS: Final = ("outputs", "status")
_TEMPLATE_FIELDS: Final = frozenset({"if", "run", "env", "with", "outputs"})
_INPUT_TYPES: Final = frozenset(kind.value for kind in InputType)
_DEFAULT_CONDITION: Final = parse_condition("success()")
_BASE_DEFAULTS: Final = Defaults()
_SIGNATURE_HEADERS: Final = {
    SignatureMode.HMAC_SHA256: "x-hub-signature-256",
    SignatureMode.TOKEN: "x-gitlab-token",
}
_Site = Literal["workflow env", "if", "env", "with", "outputs"]

_TOP_LEVEL_HINTS: Final = {
    "on": "Treadle uses `triggers`. `on` is a YAML 1.1 boolean trap.",
    "jobs": "Treadle has one step graph per file. Move the steps under `steps`.",
    "runs-on": "Treadle runs on this machine. Remove the key.",
}
_TRIGGER_HINTS: Final = {
    "manual": "every workflow accepts manual runs, so there is no `manual` trigger",
    "workflow_dispatch": "every workflow accepts manual runs; remove the key",
    "cron": 'use `schedule`, such as schedule: "0 2 * * *"',
    "push": "use a `webhook` trigger that your Git server calls",
}
_STEP_HINTS: Final = {
    "shell": "set the shell under `with`, such as with: {shell: bash}",
    "working-directory": "set the working directory under `with`, such as with: {cwd: ~/src}",
}
_CONTEXT_HINTS: Final = {
    "github": "Treadle has no `github` context; use `trigger` for the event and `run` for the run",
    "needs": "read another step's outputs through steps.<id>.outputs",
    "vars": "use `env` for variables",
}
# Hints for a step that refers to itself through `steps`, by the field that holds the reference.
_OWN_RESULTS: Final = "read this step's own results through `result`"
_SELF_HINTS: Final[Mapping[_Site, str]] = {
    "if": "`if` decides whether the step runs, so the step has no status or outputs yet",
    "env": "the step has no outputs until it finishes; read them in a later step",
    "with": "the step has no outputs until it finishes; read them in a later step",
}
_SYMLINK_MESSAGE: Final = "the file is a symbolic link, so Treadle skips it"
_SYMLINK_HINT: Final = "copy the file into the workflows directory instead"


class _Raw:
    """Passes any value through; the loader validates it later, item by item."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        return value

    def describe(self) -> str:
        return "any value"


class _Scalar:
    """A string, a finite number, or a boolean: an `env` value or an input default."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            ctx.issues.append(Issue(Severity.ERROR, "E104", "must be a finite number", path))
        elif not isinstance(value, (str, int, float, bool)):
            message = f"expected {self.describe()}, got {kind_of(value)}"
            ctx.issues.append(Issue(Severity.ERROR, "E103", message, path))
        return value

    def describe(self) -> str:
        return "a string, number, or boolean"


_SHELL_NAME: Final = Str(choices=("bash", "sh", "python"))
_SHELL_ARGV: Final = ListOf(Str(), min_len=1)
_SHELL_EITHER: Final = AnyOf(_SHELL_NAME, _SHELL_ARGV)


class _Shell:
    """`bash`, `sh`, `python`, or an argv list, checked by the form it takes."""

    def check(self, value: object, path: YamlPath, ctx: CheckContext) -> object:
        if isinstance(value, str):
            return _SHELL_NAME.check(value, path, ctx)
        if isinstance(value, list):
            return _SHELL_ARGV.check(value, path, ctx)
        return _SHELL_EITHER.check(value, path, ctx)

    def describe(self) -> str:
        return _SHELL_EITHER.describe()


_RAW: Final = _Raw()
_RETRY: Final = Obj(
    "retry",
    {
        "attempts": Field(Int(min=1, max=20)),
        "delay": Field(Duration(max=3600.0)),
        "backoff": Field(Num(min=1.0, max=10.0)),
        "max_delay": Field(Duration(max=6 * 3600.0)),
        "jitter": Field(Num(min=0.0, max=1.0)),
    },
    shorthand="attempts",
)
_INPUT: Final = Obj(
    "input",
    {
        "type": Field(Str(choices=[kind.value for kind in InputType]), default="string"),
        "description": Field(Str(), default=""),
        "required": Field(Bool(), default=False),
        "default": Field(_Scalar(), default=None),
        "options": Field(ListOf(Str(), min_len=1, max_len=100, unique=True)),
    },
)
_ENV: Final = MapOf(_Scalar(), key_pattern=_ENV_NAME)
_STEP: Final = Obj(
    "step",
    {
        "id": Field(Str(pattern=_STEP_ID), required=True),
        "name": Field(Str(min_len=1)),
        "uses": Field(Str(min_len=1)),
        "run": Field(Str()),
        "with": Field(MapOf(_RAW)),
        "needs": Field(ListOf(Str(pattern=_STEP_ID), scalar_ok=True, unique=True)),
        "if": Field(AnyOf(Str(), Bool())),
        "env": Field(_ENV),
        "retry": Field(_RETRY),
        "timeout": Field(Duration(min=1.0, max=_DAY)),
        "continue_on_error": Field(Bool(), default=False),
        "outputs": Field(MapOf(Str(), key_pattern=_STEP_ID)),
    },
    hints=_STEP_HINTS,
    require_one=[("uses", "run")],
)
_SCHEDULE: Final = Obj(
    "schedule trigger",
    {
        "cron": Field(Str(min_len=1), required=True),
        "timezone": Field(Str(min_len=1)),
        "catch_up": Field(Str(choices=[mode.value for mode in CatchUp]), default="latest"),
    },
    shorthand="cron",
)
_WATCH: Final = Obj(
    "watch trigger",
    {
        "paths": Field(
            ListOf(Str(min_len=1), min_len=1, max_len=20, scalar_ok=True), required=True
        ),
        "include": Field(ListOf(Str(min_len=1)), default=["*"]),
        "exclude": Field(
            ListOf(Str(min_len=1)),
            default=[".*", "*~", "*.tmp", "*.swp", "*.part", "*.crdownload"],
        ),
        "recursive": Field(Bool(), default=False),
        "events": Field(
            ListOf(Str(choices=[event.value for event in WatchEvent]), min_len=1, unique=True),
            default=[event.value for event in WatchEvent],
        ),
        "debounce": Field(Duration(max=600.0), default=2.0),
        "poll_interval": Field(Duration(min=0.5, max=3600.0), default=2.0),
        "initial_scan": Field(Bool(), default=False),
    },
    shorthand="paths",
)
_WORKFLOW: Final = Obj(
    "workflow",
    {
        "name": Field(Str(min_len=1, max_len=100)),
        "description": Field(Str(max_len=2000), default=""),
        "inputs": Field(MapOf(_INPUT, key_pattern=_STEP_ID, max_len=50), default={}),
        "env": Field(_ENV, default={}),
        "triggers": Field(
            Obj(
                "triggers",
                dict.fromkeys(("schedule", "watch", "webhook"), Field(_RAW)),
                hints=_TRIGGER_HINTS,
            ),
            default={},
        ),
        "concurrency": Field(
            Obj(
                "concurrency",
                {
                    "policy": Field(Str(choices=[mode.value for mode in ConcurrencyPolicy])),
                    "max_queued": Field(Int(min=1, max=1000)),
                },
                shorthand="policy",
            ),
            default={},
        ),
        "defaults": Field(
            Obj(
                "defaults",
                {
                    "timeout": Field(Duration(min=1.0, max=_DAY)),
                    "retry": Field(_RETRY),
                    "shell": Field(_Shell()),
                    "cwd": Field(Str()),
                },
            ),
            default={},
        ),
        "timeout": Field(Duration(min=1.0, max=7 * _DAY), default=7200.0),
        "cleanup_timeout": Field(Duration(min=1.0, max=3600.0), default=300.0),
        "max_parallel": Field(Int(min=1, max=32), default=4),
        "steps": Field(
            ListOf(_RAW, min_len=1, max_len=_MAX_STEPS),
            required=True,
            help="add a `steps` list with at least one step",
        ),
    },
    hints=_TOP_LEVEL_HINTS,
)


def _webhook_spec(max_body: int) -> Obj:
    """The webhook trigger schema, whose `max_body` limit comes from `[limits]`."""
    return Obj(
        "webhook trigger",
        {
            "name": Field(Str(pattern=_HOOK_NAME), required=True),
            "secret_env": Field(Str(pattern=_ENV_NAME), required=True),
            "signature": Field(
                Str(choices=[mode.value for mode in SignatureMode]), default="hmac-sha256"
            ),
            "signature_header": Field(Str(pattern=_HEADER_NAME)),
            "timestamp_header": Field(Str(pattern=_HEADER_NAME), default=None),
            "tolerance": Field(Duration(min=10.0, max=3600.0), default=300.0),
            "dedupe_window": Field(Duration(max=7 * _DAY), default=_DAY),
            "max_body": Field(Size(min=1024, max=max_body)),
            "rate_limit": Field(Int(min=0, max=6000), default=60),
        },
    )


def workflow_id_for(path: Path) -> str | None:
    """Return the workflow ID (the file stem), or None when the stem isn't a valid ID."""
    return path.stem if WORKFLOW_ID_RE.fullmatch(path.stem) else None


def discover(workflows_dir: Path, *, skipped: list[LoadResult] | None = None) -> list[Path]:
    """Return the `*.yml` and `*.yaml` regular files directly in `workflows_dir`, sorted.

    Names that start with `.` or `_`, or end with `~`, are skipped. A missing directory
    holds no workflows. A symbolic link with a workflow file's name is skipped too, because
    the daemon never follows one. When `skipped` is given, a result for each such link is
    appended to it in sorted order, with no workflow and one W060 warning.
    """
    try:
        entries = sorted(workflows_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    found: list[Path] = []
    for entry in entries:
        if not _is_workflow_name(entry.name):
            continue
        if entry.is_symlink():
            if skipped is not None:
                warning = Issue(Severity.WARNING, "W060", _SYMLINK_MESSAGE, hint=_SYMLINK_HINT)
                skipped.append(LoadResult(entry, None, None, None, (warning,)))
        elif entry.is_file():
            found.append(entry)
    return found


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One loaded file. `issues` are sorted by position and capped at 50."""

    path: Path
    workflow_id: str | None
    workflow: Workflow | None
    source: str | None
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        """True when a workflow was built and no issue is an error."""
        return self.workflow is not None and not _has_errors(self.issues)


class WorkflowLoader:
    """Loads workflow files with one step registry and project context.

    A loader holds no per-file state, so one instance may load files from several threads.
    """

    def __init__(
        self,
        steps: StepRegistry,
        *,
        project_dir: Path,
        default_timezone: str,
        limits: Limits,
        secrets: SecretLookup | None = None,
        state_dir: Path | None = None,
        workflows_dir: Path | None = None,
        environment_checks: bool = True,
    ) -> None:
        self._steps = steps
        self._project_dir = project_dir
        self._timezone = default_timezone
        self._limits = limits
        self._secrets = secrets
        self._state_dir = state_dir
        self._workflows_dir = workflows_dir
        self._environment_checks = environment_checks
        self._webhook = _webhook_spec(limits.webhook_body_bytes)

    def load_file(self, path: Path) -> LoadResult:
        """Read and load one file, checking its size (E055) and encoding (E056).

        Raises OSError when the file can't be read.
        """
        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
        return self._load(data, path)

    def load_text(self, text: str, path: Path) -> LoadResult:
        """Load workflow text as if it were the file at `path`.

        The digest covers the UTF-8 encoding of `text`, so loading a file's text gives the
        same digest as loading the file.
        """
        return self._load(text.encode("utf-8", "surrogatepass"), path)

    def _load(self, data: bytes, path: Path) -> LoadResult:
        workflow_id = workflow_id_for(path)
        issues: list[Issue] = []
        if workflow_id is None:
            issues.append(
                _file_issue(
                    "E057",
                    f"the file name '{path.name}' isn't a valid workflow ID",
                    "rename the file: the stem must match ^[a-z0-9][a-z0-9_-]{0,63}$",
                )
            )
        if len(data) > MAX_FILE_BYTES:
            hint = "move long scripts into files that a step runs"
            issues.append(_file_issue("E055", "the file is larger than 1 MiB", hint))
            return LoadResult(path, workflow_id, None, None, _sorted_issues(issues))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            issues.append(_encoding_issue(data, exc.start))
            return LoadResult(path, workflow_id, None, None, _sorted_issues(issues))
        document, yaml_issues = load_yaml(text)
        issues.extend(yaml_issues)
        workflow = None
        if document is not None:
            digest = hashlib.sha256(data).hexdigest()
            build = _Build(self, document, workflow_id or path.stem, path, digest)
            workflow = build.workflow()
            issues.extend(build.issues())
        if workflow_id is None or _has_errors(issues):
            workflow = None
        return LoadResult(path, workflow_id, workflow, text, _sorted_issues(issues))


def cross_check(results: Sequence[LoadResult]) -> list[LoadResult]:
    """Add the checks across files: duplicate workflow IDs (E058) and webhook names (E305).

    Results are taken in path order. Every file after the first with a workflow ID gets
    E058. The first valid workflow that uses a webhook name keeps it, and every later valid
    workflow that uses it gets E305 for that name. A workflow with E305 claims none of its
    names, because it can't load. The results come back in their original order.
    """
    added: dict[int, list[Issue]] = {}
    owners: dict[str, Path] = {}
    hooks: dict[str, str] = {}
    for index in sorted(range(len(results)), key=lambda number: str(results[number].path)):
        result = results[index]
        workflow_id, workflow = result.workflow_id, result.workflow
        if workflow_id is None:
            continue
        if workflow_id in owners:
            message = f"duplicate workflow ID '{workflow_id}': {owners[workflow_id].name} uses it"
            hint = "rename or remove one of the two files"
            added.setdefault(index, []).append(_file_issue("E058", message, hint))
            continue
        owners[workflow_id] = result.path
        if workflow is None or not result.ok:
            continue
        names = [hook.name for hook in workflow.webhooks()]
        conflicts = [
            _hook_conflict(result, number, hooks[name])
            for number, name in enumerate(names)
            if name in hooks
        ]
        if conflicts:
            added.setdefault(index, []).extend(conflicts)
        else:
            hooks.update(dict.fromkeys(names, workflow_id))
    return [
        dataclasses.replace(result, issues=_sorted_issues([*result.issues, *added[index]]))
        if index in added
        else result
        for index, result in enumerate(results)
    ]


@dataclass(frozen=True, slots=True)
class _Found:
    """An issue and how to place it: by its key, or by an offset inside its string."""

    issue: Issue
    key: bool = False
    offset: int | None = None


@dataclass(frozen=True, slots=True)
class _Draft:
    """What the checks need from one step before the model exists."""

    index: int
    item: Mapping[str, object]  # the schema-normalized step mapping
    id: str | None
    uses: str | None
    shorthand: bool  # the step-level `run` became `with.run`
    condition: Condition
    env: tuple[tuple[str, Template], ...]
    params: dict[str, TemplateTree]
    outputs: tuple[tuple[str, Template], ...]
    needs: tuple[str, ...] | None  # None: implicit
    retry: RetryPolicy
    timeout: float

    @property
    def label(self) -> str:
        return self.id or f"steps[{self.index}]"


class _Build:
    """The checks and the model of one parsed document, in the order of the loading steps."""

    def __init__(
        self,
        loader: WorkflowLoader,
        document: YamlDocument,
        workflow_id: str,
        path: Path,
        digest: str,
    ) -> None:
        self.loader = loader
        self.document = document
        self.workflow_id = workflow_id
        self.path = Path(os.path.abspath(path))
        self.digest = digest
        self.found: list[_Found] = []
        self.first_step: dict[str, int] = {}
        self.declared: set[str] = set()

    def error(
        self,
        code: str,
        message: str,
        path: YamlPath,
        *,
        hint: str | None = None,
        key: bool = False,
        offset: int | None = None,
    ) -> None:
        self.found.append(
            _Found(Issue(Severity.ERROR, code, message, path, hint=hint), key, offset)
        )

    def extend(self, issues: Iterable[Issue], keys: Iterable[Issue] = ()) -> None:
        """Add issues to place later. Those that are also in `keys` concern a mapping key."""
        at_keys = set(keys)
        self.found.extend(_Found(issue, key=issue in at_keys) for issue in issues)

    def validated(
        self, spec: Spec, value: object, path: YamlPath = ()
    ) -> tuple[object, list[Issue]]:
        """Validate `value` at `path` and add the issues; return the normalized value and them."""
        keys: list[Issue] = []
        normalized, issues = validate(spec, value, path=path, key_issues=keys)
        self.extend(issues, keys)
        return normalized, issues

    def issues(self) -> list[Issue]:
        """Return every issue with its position. E210 replaces schema errors at its path."""
        templated = {found.issue.path for found in self.found if found.issue.code == "E210"}
        placed = []
        for found in self.found:
            issue = found.issue
            if issue.code.startswith("E1") and issue.path in templated:
                continue
            if issue.position is None:
                position = (
                    self.document.position(issue.path, key=found.key)
                    if found.offset is None
                    else self.document.offset_position(issue.path, found.offset)
                )
                issue = dataclasses.replace(issue, position=position)
            placed.append(issue)
        return placed

    def workflow(self) -> Workflow | None:
        """Run every check and return the workflow, or None when there is an error."""
        data = self.document.data
        if not isinstance(data, dict):
            message = (
                f"the top level must be a mapping of workflow keys, got {kind_of(data)}"
                if data is not None
                else "the file is empty"
            )
            self.error(
                "E059", message, (), hint="start the file with keys such as name: and steps:"
            )
            return None
        data = cast(dict[str, object], self._string_keys(data, ()))
        self._reject_expressions(data, ())
        normalized, _ = self.validated(_WORKFLOW, data)
        top = cast(dict[str, object], normalized)
        inputs = self._inputs(top.get("inputs"))
        env = self._env(top.get("env"), ("env",))
        defaults = self._defaults(top.get("defaults"))
        drafts = self._steps(top.get("steps"), defaults)
        needs, ancestors = self._graph(drafts)
        self._check_expressions(env, drafts, ancestors)
        triggers = self._triggers(top.get("triggers"), inputs)
        if any(found.issue.severity is Severity.ERROR for found in self.found):
            return None
        workflow = Workflow(
            id=self.workflow_id,
            name=cast(str, top.get("name", self.workflow_id)),
            description=cast(str, top["description"]),
            path=self.path,
            digest=self.digest,
            source=self.document.source,
            inputs=tuple(inputs),
            env=env,
            triggers=tuple(triggers),
            concurrency=_concurrency(cast(Mapping[str, object], top["concurrency"])),
            defaults=defaults,
            timeout=cast(float, top["timeout"]),
            cleanup_timeout=cast(float, top["cleanup_timeout"]),
            max_parallel=cast(int, top["max_parallel"]),
            steps=tuple(self._step(draft, needs) for draft in drafts),
            ancestors=ancestors,
        )
        loader = self.loader
        warnings = lint(
            workflow,
            loader._steps,
            secrets=loader._secrets,
            state_dir=loader._state_dir,
            workflows_dir=loader._workflows_dir,
            environment_checks=loader._environment_checks,
            document=self.document,
        )
        self.extend(warnings)
        return workflow

    # Step 2: structure.

    def _string_keys(self, value: object, path: YamlPath) -> object:
        """Copy `value` without the non-string keys, which are E103 (step 2 needs strings)."""
        if isinstance(value, list):
            return [self._string_keys(item, (*path, index)) for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        result: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(key, str):
                result[key] = self._string_keys(item, (*path, key))
            else:
                message = f"expected a string key, got {kind_of(key)}"
                self.error("E103", message, (*path, str(key)), key=True, hint="quote the key")
        return result

    def _inputs(self, value: object) -> list[InputSpec]:
        specs: list[InputSpec] = []
        for name, item in value.items() if isinstance(value, dict) else ():
            spec = self._input(name, item, ("inputs", name))
            if spec is not None:
                specs.append(spec)
                self.declared.add(name)
        return specs

    def _input(self, name: str, item: object, path: YamlPath) -> InputSpec | None:
        if not isinstance(item, dict) or item.get("type") not in _INPUT_TYPES:
            return None
        kind = InputType(item["type"])
        options = item.get("options")
        choices = (
            tuple(o for o in options if isinstance(o, str)) if isinstance(options, list) else ()
        )
        if kind is InputType.CHOICE and options is None:
            hint = "a choice input lists its options, such as options: [low, high]"
            self.error("E102", "missing required key 'options'", path, hint=hint)
        elif kind is not InputType.CHOICE and options is not None:
            hint = "remove it, or set type: choice"
            self.error(
                "E104",
                "'options' applies only to choice inputs",
                (*path, "options"),
                key=True,
                hint=hint,
            )
        required = item.get("required") is True
        default = item.get("default")
        if required and default is not None:
            hint = "remove `default`, or set required: false"
            self.error(
                "E104",
                "a required input can't have a default",
                (*path, "default"),
                key=True,
                hint=hint,
            )
        description = item.get("description")
        spec = InputSpec(
            name, kind, description if isinstance(description, str) else "", required, None, choices
        )
        if not _is_scalar(default) or (kind is InputType.CHOICE and not choices):
            return spec
        try:
            value = coerce_inputs([spec], {name: default}, from_strings=False)[name]
        except InputError as exc:
            code = "E104" if kind is InputType.CHOICE else "E103"
            self.error(code, exc.errors[name], (*path, "default"))
            return spec
        return dataclasses.replace(spec, default=cast("str | int | float | bool", value))

    def _defaults(self, value: object) -> Defaults:
        if not isinstance(value, dict):
            return _BASE_DEFAULTS
        timeout, shell, cwd = value.get("timeout"), value.get("shell"), value.get("cwd")
        if isinstance(shell, list):
            shell = tuple(shell)
        elif not isinstance(shell, str):
            shell = _BASE_DEFAULTS.shell
        return Defaults(
            timeout=timeout if isinstance(timeout, float) else _BASE_DEFAULTS.timeout,
            retry=self._retry(value.get("retry"), _BASE_DEFAULTS.retry, ("defaults", "retry")),
            shell=shell,
            cwd=cwd if isinstance(cwd, str) else _BASE_DEFAULTS.cwd,
        )

    def _retry(self, value: object, base: RetryPolicy, path: YamlPath) -> RetryPolicy:
        """Override `base` key by key, and check that max_delay is at least delay."""
        if not isinstance(value, dict):
            return base
        policy = dataclasses.replace(base, **{k: v for k, v in value.items() if _is_number(v)})
        if policy.max_delay < policy.delay and ("delay" in value or "max_delay" in value):
            # An inherited or default max_delay below the delay written here is an error too,
            # because the policy as a whole is invalid.
            if "max_delay" in value:
                message = f"must be at least delay ({format_duration(policy.delay)})"
                self.error("E104", message, (*path, "max_delay"))
            else:
                message = f"must be at most max_delay ({format_duration(policy.max_delay)})"
                hint = f"set max_delay as well, such as max_delay: {format_duration(policy.delay)}"
                self.error("E104", message, (*path, "delay"), hint=hint)
        return policy

    # Steps 3 and 4: templates and step parameters.

    def _reject_expressions(self, value: object, path: YamlPath) -> None:
        """E210 for `${{` in keys and in every field that doesn't take expressions."""
        if isinstance(value, dict):
            for key, item in value.items():
                if _OPEN in key:
                    self.error("E210", "expressions aren't allowed in keys", (*path, key), key=True)
                self._reject_expressions(item, (*path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._reject_expressions(item, (*path, index))
        elif isinstance(value, str) and _OPEN in value and not _takes_expressions(path):
            message, offset = "expressions aren't allowed here", value.index(_OPEN)
            hint = (
                "Treadle needs this value before a run starts; expressions work only in env, "
                "with, outputs, run, and if"
            )
            self.error("E210", message, path, hint=hint, offset=offset)

    def _syntax(self, error: ExpressionSyntaxError, path: YamlPath) -> None:
        self.error("E201", error.message, path, hint=error.hint, offset=error.offset)

    def _template(self, text: str, path: YamlPath) -> Template | None:
        try:
            return parse_template(text)
        except ExpressionSyntaxError as exc:
            self._syntax(exc, path)
            return None

    def _compile(self, value: object, path: YamlPath) -> TemplateTree:
        errors: list[tuple[YamlPath, ExpressionSyntaxError]] = []
        tree = compile_tree(value, errors=errors)
        for inner, error in errors:
            self._syntax(error, (*path, *inner))
        return tree

    def _condition(self, value: object, path: YamlPath) -> Condition:
        if not isinstance(value, (str, bool)):
            return _DEFAULT_CONDITION
        try:
            return parse_condition(value)
        except ExpressionSyntaxError as exc:
            self._syntax(exc, path)
            return _DEFAULT_CONDITION

    def _env(self, value: object, path: YamlPath) -> tuple[tuple[str, Template], ...]:
        entries: list[tuple[str, Template]] = []
        for name, raw in value.items() if isinstance(value, dict) else ():
            if name.startswith(_RESERVED_ENV):
                message = (
                    f"'{name}' is reserved: names that start with {_RESERVED_ENV} are Treadle's"
                )
                self.error("E405", message, (*path, name), key=True, hint="rename the variable")
            if _is_scalar(raw):
                text = raw if isinstance(raw, str) else to_display(cast("int | float | bool", raw))
                template = self._template(text, (*path, name))
                if template is not None:
                    entries.append((name, template))
        return tuple(entries)

    def _steps(self, value: object, defaults: Defaults) -> list[_Draft]:
        # Steps after the 100th aren't checked. The file already fails with E104, and the
        # bound keeps the ancestor sets of an oversized file small.
        drafts = []
        for index, item in enumerate(value[:_MAX_STEPS] if isinstance(value, list) else ()):
            normalized, _ = self.validated(_STEP, item, ("steps", index))
            if isinstance(normalized, dict):
                drafts.append(self._draft(index, normalized, defaults))
        return drafts

    def _draft(self, index: int, item: dict[str, object], defaults: Defaults) -> _Draft:
        path: YamlPath = ("steps", index)
        step_id, run, uses = _text(item.get("id")), _text(item.get("run")), _text(item.get("uses"))
        if step_id is not None and not re.fullmatch(_STEP_ID, step_id):
            step_id = None
        if run is not None and uses not in (None, "shell"):
            hint = "remove `uses`, or move the script into its own shell step"
            message = f"'run' needs a shell step, but this step uses '{uses}'"
            self.error("E404", message, (*path, "run"), key=True, hint=hint)
            run = None
        uses = uses or ("shell" if run is not None else None)
        step_type = self.loader._steps.get(uses) if uses is not None else None
        if uses is not None and step_type is None:
            self._unknown_type(uses, (*path, "uses" if "uses" in item else "run"))
        written = item.get("with")
        raw_with = cast(dict[str, object], written) if isinstance(written, dict) else {}
        params = cast(dict[str, TemplateTree], self._compile(raw_with, (*path, "with")))
        shorthand = run is not None and not self._clashes(raw_with, path)
        if shorthand:
            params = {"run": self._compile(run, (*path, "run")), **params}
        timeout = item.get("timeout")
        step_timeout = timeout if isinstance(timeout, float) else defaults.timeout
        if step_type is not None:
            params = self._params(step_type, params, path, shorthand)
            self._request_timeout(step_type, params, raw_with, path, step_timeout)
        return _Draft(
            index=index,
            item=item,
            id=step_id,
            uses=uses,
            shorthand=shorthand,
            condition=self._condition(item["if"], (*path, "if"))
            if "if" in item
            else _DEFAULT_CONDITION,
            env=self._env(item.get("env"), (*path, "env")),
            params=params,
            outputs=self._outputs(item.get("outputs"), (*path, "outputs"), step_type),
            needs=_explicit_needs(item),
            retry=self._retry(item.get("retry"), defaults.retry, (*path, "retry")),
            timeout=step_timeout,
        )

    def _clashes(self, raw_with: Mapping[str, object], path: YamlPath) -> bool:
        """E105 for a step-level `run` together with `with.run` or `with.argv`."""
        clashes = [key for key in ("run", "argv") if key in raw_with]
        for key in clashes:
            message = f"'run' and 'with.{key}' can't be used together"
            self.error("E105", message, (*path, "with", key), key=True, hint="keep one of them")
        return bool(clashes)

    def _unknown_type(self, uses: str, path: YamlPath) -> None:
        names = self.loader._steps.names()
        close = suggest(uses, names)
        hint = f"did you mean '{close}'?" if close else f"use one of: {', '.join(names)}"
        self.error("E401", f"unknown step type '{uses}'", path, hint=hint)

    def _params(
        self, step_type: StepType, params: dict[str, TemplateTree], path: YamlPath, shorthand: bool
    ) -> dict[str, TemplateTree]:
        """Validate `with` with the step type's schema, then its own checks (step 4)."""
        where = (*path, "with")
        keys: list[Issue] = []
        normalized, issues = validate(
            step_type.schema, params, path=where, allow_templates=True, key_issues=keys
        )
        result = cast(dict[str, TemplateTree], normalized)
        issues.extend(step_type.check(result, where))
        if shorthand:
            issues, keys = _moved(issues, path), _moved(keys, path)
        self.extend(issues, keys)
        return result

    def _request_timeout(
        self,
        step_type: StepType,
        params: Mapping[str, object],
        raw_with: Mapping[str, object],
        path: YamlPath,
        step_timeout: float,
    ) -> None:
        """E407 for a static `http` request timeout longer than the step timeout."""
        # Only a written `with.timeout` counts; otherwise the default request timeout (30s)
        # would reject every http step with a step timeout under 30s.
        request = params.get("timeout")
        written = step_type.name == "http" and "timeout" in raw_with
        if not (written and isinstance(request, float) and request > step_timeout):
            return
        message = (
            f"the request timeout ({format_duration(request)}) is longer than the step "
            f"timeout ({format_duration(step_timeout)})"
        )
        hint = "lower with.timeout, or raise the step's timeout"
        self.error("E407", message, (*path, "with", "timeout"), hint=hint)

    def _outputs(
        self, value: object, path: YamlPath, step_type: StepType | None
    ) -> tuple[tuple[str, Template], ...]:
        reserved = step_type.reserved_outputs if step_type is not None else frozenset()
        entries: list[tuple[str, Template]] = []
        for name, raw in value.items() if isinstance(value, dict) else ():
            if step_type is not None and name in reserved:
                message = f"output '{name}' reuses an output name of the {step_type.name} step type"
                self.error("E406", message, (*path, name), key=True, hint="rename the output")
            template = self._template(raw, (*path, name)) if isinstance(raw, str) else None
            if template is not None:
                entries.append((name, template))
        return tuple(entries)

    # Step 5: the graph.

    def _graph(
        self, drafts: Sequence[_Draft]
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
        for draft in drafts:
            if draft.id is not None and draft.id in self.first_step:
                hint = f"steps[{self.first_step[draft.id]}] already uses this ID"
                message = f"duplicate step ID '{draft.id}'"
                self.error("E403", message, ("steps", draft.index, "id"), hint=hint)
            elif draft.id is not None:
                self.first_step[draft.id] = draft.index
        needs: dict[str, tuple[str, ...]] = {}
        previous: str | None = None
        for draft in drafts:
            resolved: tuple[str, ...] = ()
            if draft.needs is None:
                resolved = (previous,) if previous is not None else ()
            else:
                resolved = tuple(
                    name
                    for number, name in enumerate(draft.needs)
                    if self._need(draft, number, name)
                )
            if draft.id is not None and self.first_step[draft.id] == draft.index:
                needs[draft.id] = resolved
            previous = draft.id
        return needs, compute_ancestors(list(needs), needs)

    def _need(self, draft: _Draft, number: int, name: str) -> bool:
        """E402 unless `needs` entry `number` of `draft` names an earlier step."""
        where = ("steps", draft.index, "needs", number)
        target = self.first_step.get(name)
        if target is None:
            self.error(
                "E402", f"unknown step '{name}'", where, hint=_did_you_mean(name, self.first_step)
            )
        elif target == draft.index:
            self.error(
                "E402", f"step '{name}' can't need itself", where, hint="remove it from needs"
            )
        elif target > draft.index:
            hint = f"move step '{name}' above step '{draft.label}'"
            self.error("E402", f"step '{name}' comes after step '{draft.label}'", where, hint=hint)
        else:
            return True
        return False

    # Step 6: expressions and triggers.

    def _check_expressions(
        self,
        env: Sequence[tuple[str, Template]],
        drafts: Sequence[_Draft],
        ancestors: Mapping[str, frozenset[str]],
    ) -> None:
        """E202 to E212 for every expression, in the context of the field that holds it."""
        for name, template in env:
            for expression in _parts(template):
                self._expression(expression, ("env", name), "workflow env", None, frozenset())
        for draft in drafts:
            scope = ancestors.get(draft.id, frozenset()) if draft.id is not None else frozenset()
            path: YamlPath = ("steps", draft.index)
            self._expression(draft.condition.expression, (*path, "if"), "if", draft, scope)
            for name, template in draft.env:
                for expression in _parts(template):
                    self._expression(expression, (*path, "env", name), "env", draft, scope)
            for where, expression in self._param_expressions(draft):
                self._expression(expression, where, "with", draft, scope)
            for name, template in draft.outputs:
                for expression in _parts(template):
                    self._expression(expression, (*path, "outputs", name), "outputs", draft, scope)

    def _param_expressions(self, draft: _Draft) -> Iterator[tuple[YamlPath, Expression]]:
        for name, tree in draft.params.items():
            if draft.shorthand and name == "run":
                yield from tree_expressions(tree, ("steps", draft.index, "run"))
            else:
                yield from tree_expressions(tree, ("steps", draft.index, "with", name))

    def _expression(
        self,
        expression: Expression,
        path: YamlPath,
        site: _Site,
        draft: _Draft | None,
        scope: frozenset[str],
    ) -> None:
        for ref in references(expression):
            problem = self._reference(ref.path, site, draft, scope)
            if problem is not None:
                code, message, hint = problem
                offset = expression.offset + ref.start
                self.error(code, message, path, hint=hint, offset=offset)
        for call in calls(expression):
            problem = _call_problem(call, site)
            if problem is not None:
                code, message, hint = problem
                offset = expression.offset + call.start
                self.error(code, message, path, hint=hint, offset=offset)

    def _reference(
        self, path: tuple[str, ...], site: _Site, draft: _Draft | None, scope: frozenset[str]
    ) -> tuple[str, str, str | None] | None:
        """Return (code, message, hint) for a reference that its field can't use."""
        root = path[0]
        if root not in _CONTEXTS:
            hint = _CONTEXT_HINTS.get(root) or _did_you_mean(root, _CONTEXTS)
            if root in FUNCTIONS:
                hint = f"'{root}' is a function; call it as {root}()"
            return "E202", f"unknown context '{root}'", hint
        if root == "result" and site != "outputs":
            return "E211", "`result` is only available in step outputs", None
        if root == "inputs" and len(path) > 1 and path[1] not in self.declared:
            hint = _did_you_mean(path[1], self.declared) or "declare it under `inputs`"
            return "E207", f"undeclared input '{path[1]}'", hint
        if root != "steps":
            return None
        if site == "workflow env" or draft is None:
            hint = "workflow env is rendered before any step runs; use the step's env instead"
            return "E212", "`steps` isn't available in workflow env", hint
        if len(path) < 2:
            return None
        target = path[1]
        if target not in self.first_step:
            return "E208", f"unknown step '{target}'", _did_you_mean(target, self.first_step)
        if target == draft.id:
            return (
                "E203",
                f"step '{target}' can't refer to itself through `steps`",
                _self_hint(path, site),
            )
        if target not in scope:
            later = self.first_step[target] >= draft.index
            hint = (
                f"move step '{target}' above step '{draft.label}' and add it to needs"
                if later
                else f"add '{target}' to the needs of step '{draft.label}'"
            )
            return "E203", f"step '{target}' isn't an ancestor of step '{draft.label}'", hint
        if len(path) > 2 and path[2] not in _STEP_FIELDS:
            hint = _did_you_mean(path[2], _STEP_FIELDS) or "use `outputs` or `status`"
            return "E209", f"unknown step field '{path[2]}'", hint
        return None

    def _triggers(self, value: object, inputs: Sequence[InputSpec]) -> list[Trigger]:
        """Validate and build each trigger item, then E304, the count, and E301."""
        triggers: list[Trigger] = []
        first: dict[str, YamlPath] = {}
        count = 0
        specs = (("schedule", _SCHEDULE), ("watch", _WATCH), ("webhook", self.loader._webhook))
        for kind, spec in specs:
            if not isinstance(value, dict) or kind not in value:
                continue
            items = value[kind]
            listed = isinstance(items, list)
            for index, item in enumerate(items if isinstance(items, list) else [items]):
                count += 1
                if count > _MAX_TRIGGERS:
                    continue  # the file already fails with E104, so the rest isn't checked
                path: YamlPath = ("triggers", kind, index) if listed else ("triggers", kind)
                normalized, issues = self.validated(spec, item, path)
                if issues:
                    continue
                trigger = self._trigger(kind, cast(dict[str, object], normalized), path)
                if trigger is not None and trigger.key in first:
                    self._duplicate(trigger, path, first[trigger.key])
                elif trigger is not None:
                    first[trigger.key] = path
                    triggers.append(trigger)
        if count > _MAX_TRIGGERS:
            message = f"must have at most {_MAX_TRIGGERS} trigger items, got {count}"
            self.error("E104", message, ("triggers",))
        required = [spec.name for spec in inputs if spec.required] if count else []
        for name in required:
            message = (
                f"input '{name}' is required, but schedule, watch, and webhook triggers "
                "can't supply inputs"
            )
            where = ("inputs", name, "required")
            self.error("E301", message, where, key=True, hint="give the input a default instead")
        return triggers

    def _trigger(self, kind: str, item: dict[str, object], path: YamlPath) -> Trigger | None:
        if kind == "schedule":
            return self._schedule(item, path)
        if kind == "watch":
            return self._watch(item, path)
        return self._hook(item, path)

    def _schedule(self, item: dict[str, object], path: YamlPath) -> ScheduleTrigger | None:
        text = cast(str, item["cron"])
        try:
            spec = parse_cron(text)
        except CronError as exc:
            self.error("E302", str(exc), (*path, "cron"), offset=exc.offset)
            return None
        zone = cast(str, item.get("timezone", self.loader._timezone))
        if not is_time_zone(zone):
            hint = time_zone_hint(zone)
            self.error("E303", f"unknown time zone '{zone}'", (*path, "timezone"), hint=hint)
            return None
        catch_up = CatchUp(cast(str, item["catch_up"]))
        canonical = {"cron": text, "timezone": zone, "catch_up": catch_up.value}
        return ScheduleTrigger(self._key("schedule", canonical), spec, zone, catch_up)

    def _watch(self, item: dict[str, object], path: YamlPath) -> WatchTrigger | None:
        valid = True
        for field in ("include", "exclude"):
            for index, pattern in enumerate(cast(list[str], item[field])):
                try:
                    compile_glob(pattern)
                except ValueError as exc:
                    self.error(
                        "E306", f"invalid glob pattern '{pattern}': {exc}", (*path, field, index)
                    )
                    valid = False
        if not valid:
            return None
        project = str(self.loader._project_dir)
        paths = tuple(
            Path(os.path.abspath(os.path.join(project, os.path.expanduser(raw))))
            for raw in cast(list[str], item["paths"])
        )
        events = cast(list[str], item["events"])
        canonical = {**item, "paths": [str(entry) for entry in paths], "events": sorted(events)}
        return WatchTrigger(
            key=self._key("watch", canonical),
            paths=paths,
            include=tuple(cast(list[str], item["include"])),
            exclude=tuple(cast(list[str], item["exclude"])),
            recursive=cast(bool, item["recursive"]),
            events=frozenset(WatchEvent(event) for event in events),
            debounce=cast(float, item["debounce"]),
            poll_interval=cast(float, item["poll_interval"]),
            initial_scan=cast(bool, item["initial_scan"]),
        )

    def _hook(self, item: dict[str, object], path: YamlPath) -> WebhookTrigger | None:
        signature = SignatureMode(cast(str, item["signature"]))
        timestamp = cast("str | None", item["timestamp_header"])
        if timestamp is not None and signature is SignatureMode.TOKEN:
            hint = "remove it, or use signature: hmac-sha256"
            message = "'timestamp_header' applies only to hmac-sha256"
            self.error("E104", message, (*path, "timestamp_header"), key=True, hint=hint)
            return None
        tolerance = cast(float, item["tolerance"])
        window = cast(float, item["dedupe_window"])
        if timestamp is not None:
            # With a timestamp header, a replay can only arrive within the tolerance on either
            # side, so the window is at least 2 x tolerance, even when 0 turns deduplication off.
            window = max(window, 2 * tolerance)
        name = cast(str, item["name"])
        header = cast(str, item.get("signature_header", _SIGNATURE_HEADERS[signature]))
        return WebhookTrigger(
            key=f"{self.workflow_id}:webhook:{name}",
            name=name,
            signature=signature,
            secret_env=cast(str, item["secret_env"]),
            signature_header=header.lower(),
            timestamp_header=timestamp.lower() if timestamp is not None else None,
            tolerance=tolerance,
            dedupe_window=window,
            max_body=cast(int, item.get("max_body", self.loader._limits.webhook_body_bytes)),
            rate_limit=cast(int, item["rate_limit"]),
        )

    def _key(self, kind: str, canonical: Mapping[str, object]) -> str:
        text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return f"{self.workflow_id}:{kind}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"

    def _duplicate(self, trigger: Trigger, path: YamlPath, first: YamlPath) -> None:
        message = (
            f"webhook '{trigger.name}' is declared twice"
            if isinstance(trigger, WebhookTrigger)
            else f"duplicate {trigger_kind(trigger).value} trigger"
        )
        self.error("E304", message, path, hint=f"it repeats {format_path(first)}")

    # The model.

    def _step(self, draft: _Draft, needs: Mapping[str, tuple[str, ...]]) -> Step:
        step_id = cast(str, draft.id)
        return Step(
            id=step_id,
            name=cast(str, draft.item.get("name", step_id)),
            position=draft.index,
            uses=cast(str, draft.uses),
            params=draft.params,
            needs=needs[step_id],
            implicit_needs=draft.needs is None,
            condition=draft.condition,
            env=draft.env,
            retry=draft.retry,
            timeout=draft.timeout,
            continue_on_error=cast(bool, draft.item["continue_on_error"]),
            outputs=draft.outputs,
            location=self.document.position(("steps", draft.index)),
        )


def _takes_expressions(path: YamlPath) -> bool:
    """True inside workflow `env` and inside a step's `if`, `run`, `env`, `with`, or `outputs`."""
    if path[:1] == ("env",):
        return True
    return (
        len(path) > 2
        and path[0] == "steps"
        and isinstance(path[1], int)
        and path[2] in _TEMPLATE_FIELDS
    )


def _call_problem(call: Call, site: _Site) -> tuple[str, str, str | None] | None:
    """Return (code, message, hint) for an unknown function, a misplaced status function,
    or a wrong number of arguments."""
    spec = FUNCTIONS.get(call.name)
    if spec is None:
        folded = {name.lower(): name for name in FUNCTIONS}
        exact = folded.get(call.name.lower())
        hint = (
            f"did you mean '{exact}'? Function names are case-sensitive."
            if exact
            else _did_you_mean(call.name, FUNCTIONS)
        )
        return "E204", f"unknown function '{call.name}'", hint
    if spec.status and site != "if":
        hint = "use steps.<id>.status to read a step's status outside `if`"
        return "E205", f"status function '{call.name}()' is only allowed in `if`", hint
    problem = spec.arity_problem(len(call.args))
    return None if problem is None else ("E206", problem, None)


def _self_hint(path: tuple[str, ...], site: _Site) -> str:
    """Return the hint for a step that refers to itself through `steps` in the field `site`."""
    if site != "outputs":
        return _SELF_HINTS[site]
    if len(path) > 3 and path[2] == "outputs":
        return f"{_OWN_RESULTS}, such as result.{path[3]}"
    return _OWN_RESULTS


def _moved(issues: Iterable[Issue], step_path: YamlPath) -> list[Issue]:
    """Point issues about `with.run` at the step-level `run` that it came from."""
    source = (*step_path, "with", "run")
    return [
        dataclasses.replace(issue, path=(*step_path, "run", *issue.path[len(source) :]))
        if issue.path[: len(source)] == source
        else issue
        for issue in issues
    ]


def _concurrency(value: Mapping[str, object]) -> Concurrency:
    base = Concurrency()
    return Concurrency(
        policy=ConcurrencyPolicy(cast(str, value.get("policy", base.policy))),
        max_queued=cast(int, value.get("max_queued", base.max_queued)),
    )


def _hook_conflict(result: LoadResult, number: int, owner: str) -> Issue:
    """E305 at the name of webhook `number` of `result`, located by parsing its source again."""
    workflow = cast(Workflow, result.workflow)
    name = workflow.webhooks()[number].name
    path = ("triggers", "webhook", number, "name")
    document = load_yaml(result.source)[0] if result.source is not None else None
    position = document.position(path) if document is not None else None
    message = f"webhook name '{name}' is already used by workflow '{owner}'"
    hint = "webhook names are unique across workflows; rename one of them"
    return Issue(Severity.ERROR, "E305", message, path, position, hint)


def _file_issue(code: str, message: str, hint: str) -> Issue:
    return Issue(Severity.ERROR, code, message, hint=hint)


def _encoding_issue(data: bytes, start: int) -> Issue:
    line_start = data.rfind(b"\n", 0, start) + 1
    line = data.count(b"\n", 0, line_start) + 1
    column = len(data[line_start:start].decode("utf-8", "replace")) + 1
    hint = "save the file as UTF-8"
    return Issue(
        Severity.ERROR, "E056", "the file isn't valid UTF-8", (), Position(line, column), hint
    )


def _sorted_issues(issues: Iterable[Issue]) -> tuple[Issue, ...]:
    """Sort by position, issues about the whole file first, and keep at most 50."""

    def order(issue: Issue) -> tuple[int, int, int]:
        position = issue.position
        return (0, 0, 0) if position is None else (1, position.line, position.column)

    return tuple(sorted(issues, key=order)[:_MAX_ISSUES])


def _is_workflow_name(name: str) -> bool:
    """True for a `*.yml` or `*.yaml` name that doesn't start with `.` or `_`.

    A backup name that ends with `~` doesn't end with a YAML suffix, so it's never one.
    """
    return name.endswith((".yml", ".yaml")) and not name.startswith((".", "_"))


def _has_errors(issues: Iterable[Issue]) -> bool:
    return any(issue.severity is Severity.ERROR for issue in issues)


def _parts(template: Template) -> Iterator[Expression]:
    return (part for part in template.parts if isinstance(part, Expression))


def _explicit_needs(item: Mapping[str, object]) -> tuple[str, ...] | None:
    """Return the step IDs that `needs` names, or None when the step omits `needs`."""
    if "needs" not in item:
        return None
    needs = item["needs"]
    return tuple(name for name in needs if isinstance(name, str)) if isinstance(needs, list) else ()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_scalar(value: object) -> bool:
    """True for a string, a boolean, or a finite number."""
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, int, bool))


def _did_you_mean(word: str, candidates: Iterable[str]) -> str | None:
    close = suggest(word, candidates)
    return f"did you mean '{close}'?" if close else None
