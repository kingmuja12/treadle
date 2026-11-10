"""The JSON API under `/api/v1`."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection
from enum import StrEnum
from typing import Final, NoReturn, TypeAlias, TypeVar

from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from treadle.errors import InputError, NotFoundError, TreadleError, WorkflowInvalidError
from treadle.models import (
    CancelOutcome,
    CancelReason,
    Origin,
    RunDetail,
    RunFilter,
    RunRequest,
    RunStatus,
    Submission,
    TriggerKind,
    TriggerState,
    WorkflowEntry,
)
from treadle.serialize import (
    _display_path,
    _error_status,
    error_json,
    log_json,
    run_detail_json,
    run_json,
    workflow_detail_json,
    workflow_json,
)
from treadle.types import JSONObject, JSONValue
from treadle.web.security import WebContext, _media_type, read_body_capped

_Endpoint: TypeAlias = Callable[[Request], Awaitable[Response]]
_E = TypeVar("_E", bound=StrEnum)

_RETRY_AFTER: Final = {"Retry-After": "30"}
_MAX_ID: Final = 2**63 - 1  # SQLite's largest integer


class _ApiError(Exception):
    """A request that the API refuses, answered with the JSON error shape."""

    def __init__(self, status: int, code: str, message: str, details: JSONValue = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


def api_routes(ctx: WebContext) -> list[BaseRoute]:
    """Return the `/api/v1` routes."""
    api = _Api(ctx)
    return [
        api.route("/api/v1/health", api.health),
        api.route("/api/v1/status", api.status),
        api.route("/api/v1/workflows", api.workflows),
        api.route("/api/v1/workflows/{workflow_id}", api.workflow),
        api.route("/api/v1/workflows/{workflow_id}/runs", api.start_run, "POST"),
        api.route("/api/v1/runs", api.runs),
        api.route("/api/v1/runs/{run_id:int}", api.run),
        api.route("/api/v1/runs/{run_id:int}/logs", api.logs),
        api.route("/api/v1/runs/{run_id:int}/cancel", api.cancel, "POST"),
        api.route("/api/v1/runs/{run_id:int}/rerun", api.rerun, "POST"),
    ]


class _Api:
    """The API endpoints. Each one answers errors with the JSON error shape."""

    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx

    def route(self, path: str, endpoint: _Endpoint, method: str = "GET") -> Route:
        """Return a route whose endpoint maps refusals and Treadle errors to JSON errors."""

        async def guarded(request: Request) -> Response:
            try:
                return await endpoint(request)
            except _ApiError as exc:
                return _error_response(exc.status, exc.code, str(exc), exc.details)
            except TreadleError as exc:
                mapped = _error_status(exc)
                if mapped is None:
                    raise
                return _error_response(*mapped, str(exc), self._details(exc))

        return Route(path, guarded, methods=[method], name=endpoint.__name__)

    async def health(self, request: Request) -> Response:
        """`GET /api/v1/health`: 200, or 503 while the daemon drains."""
        draining = self.ctx.draining()
        body = {"status": "draining" if draining else "ok", "version": self.ctx.version}
        return JSONResponse(body, 503 if draining else 200)

    async def status(self, request: Request) -> Response:
        """`GET /api/v1/status`: the Status shape."""
        stats = self.ctx.runs.stats()
        entries = self.ctx.workflows.entries()
        valid = sum(entry.valid for entry in entries)
        # `uptime_s` counts whole seconds.
        uptime = (self.ctx.clock.now() - self.ctx.started_at).total_seconds()
        body: JSONObject = {
            "version": self.ctx.version,
            "schema_version": self.ctx.schema_version,
            "uptime_s": max(int(uptime), 0),
            "active_runs": stats.active,
            "queued_runs": stats.queued,
            "draining": self.ctx.draining(),
            "workflows": {"valid": valid, "invalid": len(entries) - valid},
        }
        return JSONResponse(body)

    async def workflows(self, request: Request) -> Response:
        """`GET /api/v1/workflows`: every workflow, with its triggers and last run."""
        last_runs = await self.ctx.reader.last_runs()
        states = _states_by_workflow(self.ctx.triggers.trigger_states())
        items: list[JSONValue] = [
            workflow_json(
                entry,
                project_dir=self.ctx.settings.project_dir,
                triggers=states.get(entry.workflow_id, []),
                last_run=last_runs.get(entry.workflow_id),
            )
            for entry in self.ctx.workflows.entries()
        ]
        return JSONResponse({"workflows": items})

    async def workflow(self, request: Request) -> Response:
        """`GET /api/v1/workflows/{id}`: the WorkflowDetail shape, or 404."""
        entry = self._entry(request.path_params["workflow_id"])
        last_runs = await self.ctx.reader.last_runs()
        body = workflow_detail_json(
            entry,
            project_dir=self.ctx.settings.project_dir,
            triggers=self.ctx.triggers.trigger_states(entry.workflow_id),
            last_run=last_runs.get(entry.workflow_id),
        )
        return JSONResponse(body)

    async def start_run(self, request: Request) -> Response:
        """`POST /api/v1/workflows/{id}/runs`: submit a manual run with JSON inputs."""
        body = await self._json_body(request, allowed=("inputs",))
        inputs = body.get("inputs", {})
        if not isinstance(inputs, dict):
            raise _ApiError(400, "bad_request", "'inputs' must be a JSON object")
        submission = await self.ctx.runs.submit(
            RunRequest(
                workflow_id=request.path_params["workflow_id"],
                kind=TriggerKind.MANUAL,
                trigger={"kind": TriggerKind.MANUAL.value, "origin": Origin.API.value},
                inputs=inputs,
            )
        )
        return _submission_response(submission)

    async def runs(self, request: Request) -> Response:
        """`GET /api/v1/runs`: one page of run history, newest first."""
        try:
            flt = _run_filter(request.query_params)
        except ValueError as exc:
            raise _ApiError(400, "bad_request", str(exc)) from exc
        page = await self.ctx.reader.list_runs(flt)
        runs: list[JSONValue] = [run_json(run) for run in page.runs]
        return JSONResponse({"runs": runs, "next_before_id": page.next_before_id})

    async def run(self, request: Request) -> Response:
        """`GET /api/v1/runs/{id}`: the RunDetail shape, or 404."""
        return JSONResponse(run_detail_json(await self._detail(request)))

    async def logs(self, request: Request) -> Response:
        """`GET /api/v1/runs/{id}/logs`: log lines after `after`, and whether more can come.

        `complete` is true when the run is terminal and no lines remain.
        """
        params = request.query_params
        try:
            attempt = _int_param(params, "attempt", 1)
            after = _int_param(params, "after", 0) or 0
            limit = _int_param(params, "limit", 1, 5000) or 1000
        except ValueError as exc:
            raise _ApiError(400, "bad_request", str(exc)) from exc
        detail = await self._detail(request)
        lines = await self.ctx.reader.logs(
            detail.run.id,
            step_id=params.get("step") or None,
            attempt=attempt,
            after_seq=after,
            limit=limit,
        )
        body: JSONObject = {
            "lines": [log_json(line) for line in lines],
            "next_after": lines[-1].seq if lines else after,
            "complete": detail.run.status.terminal and len(lines) < limit,
        }
        return JSONResponse(body)

    async def cancel(self, request: Request) -> Response:
        """`POST /api/v1/runs/{id}/cancel`: 202 with the CancelOutcome, or 409 when finished."""
        body = await self._json_body(request, allowed=("force",))
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise _ApiError(400, "bad_request", "'force' must be true or false")
        outcome = await self.ctx.runs.cancel(_run_id(request), CancelReason.USER, force=force)
        if not outcome.accepted:
            raise _ApiError(409, "already_finished", _refusal(outcome), _cancel_json(outcome))
        return JSONResponse(_cancel_json(outcome), 202)

    async def rerun(self, request: Request) -> Response:
        """`POST /api/v1/runs/{id}/rerun`: submit a finished run again."""
        await self._json_body(request, allowed=())
        return _submission_response(await self.ctx.runs.rerun(_run_id(request)))

    def _entry(self, workflow_id: str) -> WorkflowEntry:
        entry = self.ctx.workflows.get(workflow_id)
        if entry is None:
            raise _ApiError(404, "not_found", f"workflow '{workflow_id}' doesn't exist")
        return entry

    async def _detail(self, request: Request) -> RunDetail:
        detail = await self.ctx.reader.get_run(_run_id(request))
        if detail is None:
            raise _ApiError(404, "not_found", f"run {request.path_params['run_id']} doesn't exist")
        return detail

    async def _json_body(self, request: Request, *, allowed: Collection[str]) -> JSONObject:
        """Return the JSON object in the body; an empty body counts as `{}`.

        It raises 415 unless the body is `application/json`, and 400 for invalid JSON, a
        value that isn't an object, or a key outside `allowed`.
        """
        if _media_type(request.headers.get("content-type")) != "application/json":
            raise _ApiError(415, "unsupported_media_type", "send the body as application/json")
        body = await read_body_capped(request, self.ctx.settings.limits.api_body_bytes)
        # A POST without a body (curl -X POST) means "no options", like `{}`.
        if not body.strip():
            return {}
        try:
            value = json.loads(body, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            raise _ApiError(400, "bad_request", "the request body isn't valid JSON") from exc
        if not isinstance(value, dict):
            raise _ApiError(400, "bad_request", "the request body must be a JSON object")
        # Unknown keys are refused, as in workflow files and treadle.toml, so a typo such as
        # "input" fails loudly instead of running without inputs.
        unknown = sorted(set(value) - set(allowed))
        if unknown:
            raise _ApiError(400, "bad_request", f"unknown key '{unknown[0]}' in the request body")
        return value

    def _details(self, exc: TreadleError) -> JSONValue:
        """Return the `details` of an error: input problems, or a workflow's issues."""
        if isinstance(exc, InputError):
            return dict(exc.errors)
        if isinstance(exc, WorkflowInvalidError):
            entry = self.ctx.workflows.get(exc.workflow_id)
            path = exc.workflow_id
            if entry is not None:
                path = _display_path(entry.path, self.ctx.settings.project_dir)
            return [issue.to_json(path) for issue in exc.issues]
        return None


def _run_id(request: Request) -> int:
    """Return the `run_id` path parameter; an ID beyond SQLite's range doesn't exist."""
    value: int = request.path_params["run_id"]
    if value > _MAX_ID:
        raise NotFoundError(f"run {value} doesn't exist")
    return value


def _run_filter(params: QueryParams) -> RunFilter:
    """Build a run filter from the query parameters that the API and dashboard share.

    Raises ValueError with a message for a bad value.
    """
    trigger = params.get("trigger")
    limit = _int_param(params, "limit", 1, 200)
    return RunFilter(
        workflow_id=params.get("workflow") or None,
        statuses=frozenset(_choice(RunStatus, s, "status") for s in params.getlist("status") if s),
        trigger_kind=_choice(TriggerKind, trigger, "trigger") if trigger else None,
        text=params.get("q") or None,
        before_id=_int_param(params, "before", 1),
        limit=50 if limit is None else limit,
    )


def _int_param(params: QueryParams, name: str, low: int, high: int = _MAX_ID) -> int | None:
    """Return an integer query parameter in `[low, high]`, or None when it's absent or empty.

    Raises ValueError with a message for anything else.
    """
    raw = params.get(name)
    if not raw:
        return None
    if raw.isascii() and raw.isdigit() and len(raw) <= 18 and low <= int(raw) <= high:
        return int(raw)
    bounds = f"from {low} to {high}" if high < _MAX_ID else f"of at least {low}"
    raise ValueError(f"'{name}' must be an integer {bounds}")


def _refusal(outcome: CancelOutcome) -> str:
    """Explain why a cancel request wasn't accepted."""
    if outcome.status.terminal:
        return f"run {outcome.run_id} already finished ({outcome.status})"
    return f"run {outcome.run_id} is already finishing"


def _states_by_workflow(states: list[TriggerState]) -> dict[str, list[TriggerState]]:
    """Group trigger states by workflow ID, keeping their order."""
    grouped: dict[str, list[TriggerState]] = {}
    for state in states:
        grouped.setdefault(state.workflow_id, []).append(state)
    return grouped


def _error_response(status: int, code: str, message: str, details: JSONValue = None) -> Response:
    headers = _RETRY_AFTER if status == 503 else None
    return JSONResponse(error_json(code, message, details), status, headers)


def _submission_response(submission: Submission) -> Response:
    run = run_json(submission.run) if submission.run else None
    return JSONResponse({"outcome": submission.outcome.value, "run": run}, 202)


def _cancel_json(outcome: CancelOutcome) -> JSONObject:
    return {
        "run_id": outcome.run_id,
        "status": outcome.status.value,
        "accepted": outcome.accepted,
        "forced": outcome.forced,
    }


def _choice(kind: type[_E], value: str, name: str) -> _E:
    try:
        return kind(value)
    except ValueError:
        choices = ", ".join(member.value for member in kind)
        raise ValueError(f"'{name}' must be one of: {choices}") from None


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} isn't valid JSON")
