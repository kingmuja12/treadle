"""The server-rendered dashboard."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any, Final, TypeAlias
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import jinja2
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route
from starlette.templating import Jinja2Templates

from treadle.clock import iso_utc
from treadle.errors import InputError, TreadleError, WorkflowInvalidError
from treadle.expr.evaluator import to_display
from treadle.models import (
    CancelReason,
    InputSpec,
    LogLine,
    Origin,
    RunFilter,
    RunRequest,
    RunStatus,
    Submission,
    TriggerKind,
    WorkflowEntry,
)
from treadle.serialize import _display_path, _error_status
from treadle.types import JSONValue
from treadle.units import format_duration
from treadle.web.api import (
    _RETRY_AFTER,
    _int_param,
    _refusal,
    _run_filter,
    _run_id,
    _states_by_workflow,
)
from treadle.web.security import (
    CSRF_COOKIE,
    CSRF_FIELD,
    SESSION_COOKIE,
    WebContext,
    check_csrf,
    check_session,
    constant_time_equals,
    issue_csrf,
    issue_session,
    read_form,
    safe_next,
    session_binding,
)

_Endpoint: TypeAlias = Callable[[Request], Awaitable[Response]]
_Form: TypeAlias = dict[str, list[str]]

_STATIC_TYPES: Final = {
    "treadle.css": "text/css; charset=utf-8",
    "treadle.js": "text/javascript; charset=utf-8",
}
_STATUS_ICONS: Final = {
    "succeeded": "✓",
    "failed": "✗",
    "timed_out": "⏱",
    "cancelled": "⊘",
    "skipped": "↷",
    "interrupted": "⚠",
    "queued": "◷",
    "running": "▶",
    "retrying": "↻",
    "pending": "○",
    "ok": "✓",
    "invalid": "✗",
    "stale": "⚠",
}
_HEADINGS: Final = {
    400: "Bad request",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Request too large",
    422: "Invalid inputs",
    503: "Shutting down",
}
_FORM_EXPIRED: Final = "This form expired. Reload the page and try again."
_INPUT_PREFIX: Final = "input."
_RECENT_RUNS: Final = 20
_LOG_TAIL: Final = 500
_LOG_PAGE: Final = 1000


def make_templates() -> Jinja2Templates:
    """Return the dashboard templates, with autoescaping and `StrictUndefined`.

    They load through `PackageLoader("treadle", "web/templates")`.
    """
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("treadle", "web/templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(iso=iso_utc, localtime=_local_time, duration=_duration)
    env.globals["status_icon"] = _status_icon
    return Jinja2Templates(env=env)


def dashboard_routes(ctx: WebContext, templates: Jinja2Templates) -> list[BaseRoute]:
    """Return the dashboard, login, and static-file routes."""
    pages = _Pages(ctx, templates)
    ui = _Dashboard(ctx, pages)
    route = pages.route
    return [
        route("/static/{name}", ui.static),
        route("/login", ui.login_page),
        route("/login", ui.login, "POST"),
        route("/logout", ui.logout, "POST"),
        route("/", ui.workflows),
        route("/workflows/{workflow_id}", ui.workflow),
        route("/workflows/{workflow_id}/run", ui.run_workflow, "POST"),
        route("/runs", ui.runs),
        route("/runs/{run_id:int}", ui.run),
        route("/runs/{run_id:int}/log.txt", ui.log_text),
        route("/runs/{run_id:int}/cancel", ui.cancel, "POST"),
        route("/runs/{run_id:int}/rerun", ui.rerun, "POST"),
    ]


@dataclass(frozen=True, slots=True)
class _Field:
    """One control of the run-now form. `value` is the text to prefill."""

    name: str
    type: str
    description: str
    required: bool
    value: str
    options: tuple[str, ...]
    error: str | None

    @property
    def described_by(self) -> str:
        """The IDs of the hint and error that describe the control, space-separated."""
        ids = [f"input-{self.name}-hint"] if self.description else []
        if self.error:
            ids.append(f"input-{self.name}-error")
        return " ".join(ids)


@dataclass(frozen=True, slots=True)
class _Problem:
    """One entry of a form's error summary. `field` is the input it links to, if any."""

    field: str | None
    text: str


class _Pages:
    """Renders pages with the shared context, and keeps a valid CSRF cookie on each one."""

    def __init__(self, ctx: WebContext, templates: Jinja2Templates) -> None:
        self.ctx = ctx
        self.templates = templates

    def route(self, path: str, endpoint: _Endpoint, method: str = "GET") -> Route:
        """Return a route whose endpoint shows an error page for each Treadle error."""

        async def guarded(request: Request) -> Response:
            try:
                return await endpoint(request)
            except TreadleError as exc:
                mapped = _error_status(exc)
                if mapped is None:
                    raise
                issues = self.issues_of(exc) if isinstance(exc, WorkflowInvalidError) else ""
                return self.error(request, mapped[0], _sentence(str(exc)), issues=issues)

        return Route(path, guarded, methods=[method], name=f"dashboard.{endpoint.__name__}")

    def render(
        self,
        request: Request,
        name: str,
        context: Mapping[str, Any],
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Render a template, reusing the CSRF cookie when it's valid for this session."""
        token, fresh = self.csrf_token(request)
        page = {
            "title": "Treadle",
            "section": "",
            "version": self.ctx.version,
            "draining": self.ctx.draining(),
            "signed_in": self.signed_in(request),
            "csrf_token": token,
            "timezone": self.ctx.settings.timezone,
            **context,
        }
        response = self.templates.TemplateResponse(
            request, name, page, status_code=status, headers=headers
        )
        if fresh:
            self.set_cookie(response, CSRF_COOKIE, token)
        return response

    def csrf_token(self, request: Request) -> tuple[str, bool]:
        """Return the CSRF token for the request's pages, and whether it's a new one."""
        binding = session_binding(request.cookies.get(SESSION_COOKIE))
        cookie = request.cookies.get(CSRF_COOKIE)
        if cookie is not None and check_csrf(self.ctx.secret_key, cookie, cookie, binding):
            return cookie, False
        return issue_csrf(self.ctx.secret_key, binding), True

    def error(self, request: Request, status: int, message: str, *, issues: str = "") -> Response:
        """Render the error page; a 503 also tells clients when to retry."""
        heading = _HEADINGS.get(status, "Something went wrong")
        context = {"title": heading, "heading": heading, "message": message, "issues": issues}
        headers = _RETRY_AFTER if status == 503 else None
        return self.render(request, "error.html", context, status=status, headers=headers)

    def expired(self, request: Request) -> Response:
        """The page for a form whose CSRF check failed."""
        return self.error(request, 403, _FORM_EXPIRED)

    async def form(self, request: Request) -> _Form | None:
        """Return the submitted form when its CSRF token is valid, else None."""
        form = await read_form(request, self.ctx.settings.limits.api_body_bytes)
        binding = session_binding(request.cookies.get(SESSION_COOKIE))
        cookie = request.cookies.get(CSRF_COOKIE)
        valid = check_csrf(self.ctx.secret_key, cookie, _last(form, CSRF_FIELD), binding)
        return form if valid else None

    def signed_in(self, request: Request) -> bool:
        """True when authentication is on and the request has a valid session."""
        token = self.ctx.token
        cookie = request.cookies.get(SESSION_COOKIE)
        ttl = self.ctx.settings.server.session_ttl
        now = self.ctx.clock.now().timestamp()
        return token is not None and check_session(self.ctx.secret_key, token, cookie, now, ttl)

    def set_cookie(
        self, response: Response, name: str, value: str, *, max_age: int | None = None
    ) -> None:
        """Set an HttpOnly, SameSite=Strict cookie for the whole site."""
        # The CSRF cookie gets `Secure` with `secure_cookies` too, like the session cookie,
        # so neither travels over plain HTTP once TLS is configured.
        secure = self.ctx.settings.server.secure_cookies
        response.set_cookie(
            name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite="strict"
        )

    def issues_of(self, exc: WorkflowInvalidError) -> str:
        """Render the issues of an invalid workflow in the CLI format."""
        entry = self.ctx.workflows.get(exc.workflow_id)
        if entry is not None:
            return self.rendered_issues(entry)
        return "\n".join(issue.render(exc.workflow_id) for issue in exc.issues)

    def rendered_issues(self, entry: WorkflowEntry) -> str:
        """Render every issue of an entry in the CLI format, with source excerpts."""
        path = _display_path(entry.path, self.ctx.settings.project_dir)
        return "\n".join(issue.render(path, entry.source) for issue in entry.issues)


class _Dashboard:
    """The dashboard endpoints."""

    def __init__(self, ctx: WebContext, pages: _Pages) -> None:
        self.ctx = ctx
        self.pages = pages
        root = resources.files("treadle") / "web" / "static"
        self.static_files = {name: (root / name).read_bytes() for name in _STATIC_TYPES}

    async def static(self, request: Request) -> Response:
        """`GET /static/{name}`: the stylesheet and the script, from memory."""
        name = request.path_params["name"]
        content = self.static_files.get(name)
        if content is None:
            return self.pages.error(request, 404, "That file doesn't exist.")
        return Response(content, media_type=_STATIC_TYPES[name])

    async def login_page(self, request: Request) -> Response:
        """`GET /login`: the sign-in form, or 404 when authentication is off."""
        if self.ctx.token is None:
            return self._no_sign_in(request)
        next_url = safe_next(request.query_params.get("next"))
        return self._login_form(request, next_url)

    async def login(self, request: Request) -> Response:
        """`POST /login`: check the token, start a session, and go to `next`."""
        token = self.ctx.token
        if token is None:
            return self._no_sign_in(request)
        form = await self.pages.form(request)
        if form is None:
            return self.pages.expired(request)
        next_url = safe_next(_last(form, "next"))
        if not constant_time_equals(_last(form, "token") or "", token):
            return self._login_form(request, next_url, error="That token isn't valid.", status=401)
        now = self.ctx.clock.now().timestamp()
        session = issue_session(self.ctx.secret_key, token, now)
        response = RedirectResponse(next_url, 303)
        ttl = int(self.ctx.settings.server.session_ttl)
        self.pages.set_cookie(response, SESSION_COOKIE, session, max_age=ttl)
        csrf = issue_csrf(self.ctx.secret_key, session_binding(session))
        self.pages.set_cookie(response, CSRF_COOKIE, csrf)
        return response

    async def logout(self, request: Request) -> Response:
        """`POST /logout`: end the session and go to the sign-in page."""
        # Without a token there is no session to end and no sign-in page, so this answers
        # 404 like `/login`.
        if self.ctx.token is None:
            return self._no_sign_in(request)
        if await self.pages.form(request) is None:
            return self.pages.expired(request)
        response = RedirectResponse("/login", 303)
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            self.pages.set_cookie(response, name, "", max_age=0)
        return response

    async def workflows(self, request: Request) -> Response:
        """`GET /`: every workflow with its triggers, last run, and state."""
        last_runs = await self.ctx.reader.last_runs()
        states = _states_by_workflow(self.ctx.triggers.trigger_states())
        rows = []
        for entry in self.ctx.workflows.entries():
            state = _state(entry)
            row = {
                "entry": entry,
                "name": _name(entry),
                "state": state,
                "triggers": states.get(entry.workflow_id, []),
                "last_run": last_runs.get(entry.workflow_id),
                "issues": "" if state == "ok" else self.pages.rendered_issues(entry),
            }
            rows.append(row)
        context = {
            "title": "Workflows",
            "section": "workflows",
            "rows": rows,
            "workflows_dir": _display_path(
                self.ctx.settings.workflows_dir, self.ctx.settings.project_dir
            ),
        }
        return self.pages.render(request, "workflows.html", context)

    async def workflow(self, request: Request) -> Response:
        """`GET /workflows/{id}`: one workflow, its recent runs, and the run-now form."""
        entry = self._entry(request.path_params["workflow_id"])
        if entry is None:
            return self._unknown_workflow(request)
        return await self._workflow_page(request, entry)

    async def run_workflow(self, request: Request) -> Response:
        """`POST /workflows/{id}/run`: start a run from the form, then show it.

        An InputError shows the form again with 422 and a message for each field.
        """
        form = await self.pages.form(request)
        if form is None:
            return self.pages.expired(request)
        entry = self._entry(request.path_params["workflow_id"])
        if entry is None:
            return self._unknown_workflow(request)
        values = {
            key.removeprefix(_INPUT_PREFIX): items[-1]  # the last value wins, as with checkboxes
            for key, items in form.items()
            if key.startswith(_INPUT_PREFIX)
        }
        specs = {spec.name: spec for spec in entry.current.inputs} if entry.current else {}
        try:
            submission = await self.ctx.runs.submit(
                RunRequest(
                    workflow_id=entry.workflow_id,
                    kind=TriggerKind.MANUAL,
                    trigger={"kind": TriggerKind.MANUAL.value, "origin": Origin.DASHBOARD.value},
                    inputs=_supplied(values, specs),
                    inputs_are_strings=True,
                )
            )
        except InputError as exc:
            return await self._workflow_page(
                request, entry, values=values, errors=exc.errors, status=422
            )
        return self._show_run(submission, fallback=f"/workflows/{entry.workflow_id}")

    async def runs(self, request: Request) -> Response:
        """`GET /runs`: run history with the API's filters and "Older runs" paging."""
        params = request.query_params
        try:
            flt = _run_filter(params)
        except ValueError as exc:
            return self.pages.error(request, 400, _sentence(str(exc)))
        page = await self.ctx.reader.list_runs(flt)
        workflow_ids = {entry.workflow_id for entry in self.ctx.workflows.entries()}
        if flt.workflow_id:
            workflow_ids.add(flt.workflow_id)
        context = {
            "title": "Runs",
            "section": "runs",
            "runs": page.runs,
            "filters": flt,
            "workflow_ids": sorted(workflow_ids),
            "statuses": [status.value for status in RunStatus],
            "kinds": [kind.value for kind in TriggerKind],
            "older_url": _older_url(params, page.next_before_id),
        }
        return self.pages.render(request, "runs.html", context)

    async def run(self, request: Request) -> Response:
        """`GET /runs/{id}`: one run, its steps with log excerpts, and its actions."""
        detail = await self.ctx.reader.get_run(_run_id(request))
        if detail is None:
            return self._unknown_run(request)
        run = detail.run
        zone = ZoneInfo(self.ctx.settings.timezone)
        steps = []
        for step in detail.steps:
            tail = await self.ctx.reader.logs(
                run.id, step_id=step.step_id, limit=_LOG_TAIL, tail=True
            )
            steps.append(
                {"record": step, "log": "\n".join(_page_line(line, zone) for line in tail)}
            )
        last = await self.ctx.reader.logs(run.id, limit=1, tail=True)
        context = {
            "title": f"Run #{run.id}",
            "section": "runs",
            "run": run,
            "steps": steps,
            "active": not run.status.terminal,
            "last_seq": last[-1].seq if last else 0,
            "trigger_text": json.dumps(run.trigger, indent=2, ensure_ascii=False),
            "inputs": [(name, _json_text(value)) for name, value in run.inputs.items()],
        }
        return self.pages.render(request, "run.html", context)

    async def log_text(self, request: Request) -> Response:
        """`GET /runs/{id}/log.txt`: the whole log as plain text, streamed in pages."""
        run_id = _run_id(request)
        try:
            attempt = _int_param(request.query_params, "attempt", 1)
        except ValueError as exc:
            return self.pages.error(request, 400, _sentence(str(exc)))
        if await self.ctx.reader.get_run(run_id) is None:
            return self._unknown_run(request)
        step = request.query_params.get("step") or None
        lines = self._log_lines(run_id, step, attempt)
        return StreamingResponse(lines, media_type="text/plain; charset=utf-8")

    async def cancel(self, request: Request) -> Response:
        """`POST /runs/{id}/cancel`: request a cancel (or a force), then show the run."""
        form = await self.pages.form(request)
        if form is None:
            return self.pages.expired(request)
        run_id = _run_id(request)
        force = (_last(form, "force") or "").lower() in ("true", "on")
        outcome = await self.ctx.runs.cancel(run_id, CancelReason.USER, force=force)
        if not outcome.accepted:
            return self.pages.error(request, 409, _sentence(_refusal(outcome)))
        return RedirectResponse(f"/runs/{run_id}", 303)

    async def rerun(self, request: Request) -> Response:
        """`POST /runs/{id}/rerun`: submit a finished run again, then show the new run."""
        if await self.pages.form(request) is None:
            return self.pages.expired(request)
        run_id = _run_id(request)
        submission = await self.ctx.runs.rerun(run_id)
        return self._show_run(submission, fallback=f"/runs/{run_id}")

    async def _workflow_page(
        self,
        request: Request,
        entry: WorkflowEntry,
        *,
        values: Mapping[str, str] | None = None,
        errors: Mapping[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        recent = await self.ctx.reader.list_runs(
            RunFilter(workflow_id=entry.workflow_id, limit=_RECENT_RUNS)
        )
        specs = entry.current.inputs if entry.current else ()
        fields = _fields(specs, values or {}, errors or {})
        context = {
            "title": _name(entry),
            "section": "workflows",
            "entry": entry,
            "workflow": entry.current,
            "name": _name(entry),
            "state": _state(entry),
            "path": _display_path(entry.path, self.ctx.settings.project_dir),
            "triggers": self.ctx.triggers.trigger_states(entry.workflow_id),
            "issues": self.pages.rendered_issues(entry),
            "runs": recent.runs,
            "fields": fields,
            "problems": _problems(errors or {}, {field.name for field in fields}),
        }
        return self.pages.render(request, "workflow.html", context, status=status)

    async def _log_lines(
        self, run_id: int, step: str | None, attempt: int | None
    ) -> AsyncIterator[str]:
        after = 0
        while True:
            lines = await self.ctx.reader.logs(
                run_id, step_id=step, attempt=attempt, after_seq=after, limit=_LOG_PAGE
            )
            if lines:
                yield "".join(f"{_text_line(line)}\n" for line in lines)
            if len(lines) < _LOG_PAGE:
                return
            after = lines[-1].seq

    def _entry(self, workflow_id: str) -> WorkflowEntry | None:
        return self.ctx.workflows.get(workflow_id)

    def _show_run(self, submission: Submission, *, fallback: str) -> Response:
        target = f"/runs/{submission.run.id}" if submission.run else fallback
        return RedirectResponse(target, 303)

    def _login_form(
        self, request: Request, next_url: str, *, error: str | None = None, status: int = 200
    ) -> Response:
        context = {"title": "Sign in", "next": next_url, "error": error}
        return self.pages.render(request, "login.html", context, status=status)

    def _no_sign_in(self, request: Request) -> Response:
        return self.pages.error(request, 404, "Sign-in is off: this server has no access token.")

    def _unknown_workflow(self, request: Request) -> Response:
        workflow_id = request.path_params["workflow_id"]
        return self.pages.error(request, 404, f"Workflow '{workflow_id}' doesn't exist.")

    def _unknown_run(self, request: Request) -> Response:
        return self.pages.error(request, 404, f"Run {request.path_params['run_id']} doesn't exist.")


def _fields(
    specs: tuple[InputSpec, ...], values: Mapping[str, str], errors: Mapping[str, str]
) -> list[_Field]:
    """Build the run-now controls, prefilled with submitted values or the defaults."""
    return [
        _Field(
            name=spec.name,
            type=spec.type.value,
            description=spec.description,
            required=spec.required,
            value=values.get(spec.name, to_display(spec.default)),
            options=spec.options,
            error=errors.get(spec.name),
        )
        for spec in specs
    ]


def _problems(errors: Mapping[str, str], fields: set[str]) -> list[_Problem]:
    """List the error summary: field errors link to their control, others stand alone."""
    return [
        _Problem(name, f"{name}: {message}") if name in fields else _Problem(None, message)
        for name, message in errors.items()
    ]


def _supplied(values: Mapping[str, str], specs: Mapping[str, InputSpec]) -> dict[str, JSONValue]:
    """Return the inputs a form supplies: an empty field of an optional input isn't one."""
    # An empty field of a required input is still supplied, as "", so the run manager
    # judges it the way it judges the CLI's `-i name=`.
    return {
        name: value
        for name, value in values.items()
        if value or (name in specs and specs[name].required)
    }


def _older_url(params: QueryParams, before: int | None) -> str | None:
    """The runs page URL with the same filters, one page further back."""
    if before is None:
        return None
    pairs = [(key, value) for key, value in params.multi_items() if key != "before"]
    return "/runs?" + urlencode([*pairs, ("before", str(before))])


def _name(entry: WorkflowEntry) -> str:
    return entry.current.name if entry.current else entry.workflow_id


def _state(entry: WorkflowEntry) -> str:
    if entry.stale:
        return "stale"
    return "ok" if entry.valid else "invalid"


def _last(form: _Form, name: str) -> str | None:
    """The last submitted value of a field, which wins over earlier ones."""
    values = form.get(name)
    return values[-1] if values else None


def _sentence(text: str) -> str:
    """Turn a lowercase error message into a sentence for a page."""
    text = text.rstrip(".")
    return f"{text[:1].upper()}{text[1:]}."


def _page_line(line: LogLine, zone: ZoneInfo) -> str:
    stamp = line.ts.astimezone(zone).strftime("%H:%M:%S")
    return f"{stamp} #{line.attempt} {line.stream} | {line.text}"


def _text_line(line: LogLine) -> str:
    # `log.txt` lines follow `treadle logs` ("step#attempt stream | text") with an ISO UTC
    # timestamp, because a saved file has no time zone context; "-" marks a line that
    # belongs to the run rather than a step.
    where = f"{line.step_id}#{line.attempt}" if line.step_id else "-"
    return f"{iso_utc(line.ts)} {where} {line.stream} | {line.text}"


def _json_text(value: JSONValue) -> str:
    return json.dumps(value, ensure_ascii=False)


def _local_time(value: datetime, zone: str) -> str:
    return value.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _duration(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else format_duration(milliseconds / 1000)


def _status_icon(status: str) -> str:
    return _STATUS_ICONS.get(str(status), "•")
