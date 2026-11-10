"""Webhook triggers: verification, replay protection, rate limits, and payloads.

A delivery passes its checks in order: the hook exists, Treadle isn't draining, and the
secret is set (the precheck); the body fits; then the signature verifies, the rate limit
allows it, and a JSON body parses (`handle`). Each failed check has its own response.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, NoReturn

from treadle.clock import Clock, iso_utc
from treadle.errors import NotFoundError, ShuttingDownError, WorkflowInvalidError
from treadle.models import (
    Delivery,
    HookRequest,
    HookResponse,
    RunRecord,
    RunRequest,
    SignatureMode,
    Submission,
    SubmitOutcome,
    TriggerKind,
    TriggerState,
    WebhookTrigger,
    Workflow,
)
from treadle.protocols import RunSubmitter, SecretLookup
from treadle.serialize import error_json
from treadle.types import JSONObject, JSONValue

REDACTED_HEADERS: Final[frozenset[str]] = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)
DELIVERY_HEADERS: Final[tuple[str, ...]] = (
    "x-github-delivery",
    "x-gitea-delivery",
    "x-forgejo-delivery",
    "x-gitlab-event-uuid",
)
MIN_SECRET_LENGTH: Final = 16

_SIGNATURE: Final = re.compile(r"(?:sha256=)?([0-9a-f]{64})", re.ASCII | re.IGNORECASE)
_TIMESTAMP: Final = re.compile(r"[0-9]{1,15}", re.ASCII)
_MAX_NESTING: Final = 64
_RETRY_AFTER: Final = {"Retry-After": "30"}

_log = logging.getLogger(__name__)


def verify_hmac(secret: bytes, message: bytes, header_value: str | None) -> bool:
    """Check an HMAC-SHA256 signature header, `sha256=<hex>` or bare hex, in constant time.

    The stripped value must be exactly 64 hex characters after an optional, case-insensitive
    `sha256=`; the digests are compared as bytes.
    """
    if header_value is None:
        return False
    found = _SIGNATURE.fullmatch(header_value.strip())
    if found is None:
        return False
    expected = hmac.new(secret, message, hashlib.sha256).digest()
    return hmac.compare_digest(bytes.fromhex(found[1]), expected)


def verify_token(secret: bytes, header_value: str | None) -> bool:
    """Compare a token header with the secret through SHA-256 digests, in constant time."""
    if header_value is None:
        return False
    sent = hashlib.sha256(_header_bytes(header_value)).digest()
    return hmac.compare_digest(sent, hashlib.sha256(secret).digest())


def signed_message(
    trigger: WebhookTrigger, headers: Mapping[str, str], body: bytes, *, now: float
) -> bytes | None:
    """Return the signed message: the body, or `<timestamp>.<body>` with a timestamp header.

    The timestamp must be whole Unix seconds within `tolerance` of `now`. Returns None when
    the timestamp is missing, malformed, or outside the tolerance.
    """
    if trigger.timestamp_header is None:
        return body
    raw = headers.get(trigger.timestamp_header)
    stamp = None if raw is None else _TIMESTAMP.fullmatch(raw.strip())
    if stamp is None or abs(now - int(stamp[0])) > trigger.tolerance:
        return None
    return stamp[0].encode("ascii") + b"." + body


def delivery_digest(trigger: WebhookTrigger, message: bytes) -> str:
    """Return the replay key, the SHA-256 hex of `name + "\\0" + message`."""
    return hashlib.sha256(trigger.name.encode("utf-8") + b"\0" + message).hexdigest()


def webhook_payload(
    trigger: WebhookTrigger, request: HookRequest, *, received_at: datetime
) -> JSONObject:
    """Return the `trigger` payload of a webhook run.

    Headers lose the credentials and the signature or token header. A JSON content type
    (`application/json` or `*/*+json`) gives the parsed body; anything else gives the body
    as text. Raises ValueError for an invalid JSON body or nesting deeper than 64 levels.
    """
    received = {name.lower(): value for name, value in request.headers.items()}
    hidden = REDACTED_HEADERS | {trigger.signature_header}
    headers: JSONObject = {name: value for name, value in received.items() if name not in hidden}
    delivery = next((received[name] for name in DELIVERY_HEADERS if name in received), None)
    return {
        "kind": TriggerKind.WEBHOOK.value,
        "hook": trigger.name,
        "method": request.method,
        "headers": headers,
        "query": dict(request.query),
        "content_type": request.content_type,
        "body": _body(request),
        "delivery": delivery,
        "received_at": iso_utc(received_at),
    }


class TokenBucket:
    """A per-hook rate limit of `per_minute` requests, with a burst of `max(1, per_minute // 6)`.

    A limit of 0 disables it. Times are monotonic seconds.
    """

    def __init__(self, per_minute: int) -> None:
        self._rate = per_minute / 60.0
        self._capacity = float(max(1, per_minute // 6))
        self._tokens = self._capacity
        self._updated: float | None = None

    def take(self, now: float) -> float:
        """Take a token. Return 0.0 when allowed, else the seconds until one is free."""
        if not self._rate:
            return 0.0
        if self._updated is not None:
            refill = max(0.0, now - self._updated) * self._rate
            self._tokens = min(self._capacity, self._tokens + refill)
        self._updated = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        return (1.0 - self._tokens) / self._rate


@dataclass(slots=True)
class _Hook:
    """A registered hook and the token bucket it keeps across reloads."""

    workflow_id: str
    trigger: WebhookTrigger
    index: int
    bucket: TokenBucket


class WebhookRouter:
    """Routes deliveries to webhook triggers by name; implements WebhookHandler.

    Secrets are read at request time, so a rotated `.env` applies without a restart.
    """

    def __init__(
        self, *, submitter: RunSubmitter, secrets: Callable[[], SecretLookup], clock: Clock
    ) -> None:
        self._submitter = submitter
        self._secrets = secrets
        self._clock = clock
        self._hooks: dict[str, _Hook] = {}
        self._draining = False

    def update(self, workflow_id: str, workflow: Workflow | None) -> None:
        """Swap a workflow's hooks synchronously; a hook keeps its token bucket.

        The new settings apply at once. A changed `rate_limit` starts a new bucket.
        """
        triggers = {} if workflow is None else _hooks_of(workflow)
        for name in [name for name, h in self._hooks.items() if h.workflow_id == workflow_id]:
            if name not in triggers:
                del self._hooks[name]
        for name, (index, trigger) in triggers.items():
            old = self._hooks.get(name)
            # A bucket keeps the rate it was built with, so a hook whose `rate_limit`
            # changed gets a new bucket; every other edit keeps the old one.
            if old is not None and (old.trigger.key, old.trigger.rate_limit) == (
                trigger.key,
                trigger.rate_limit,
            ):
                bucket = old.bucket
            else:
                bucket = TokenBucket(trigger.rate_limit)
            self._hooks[name] = _Hook(workflow_id, trigger, index, bucket)

    def states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return the webhook trigger states, including disabled hooks."""
        chosen = [h for h in self._hooks.values() if workflow_id in (None, h.workflow_id)]
        chosen.sort(key=lambda h: (h.workflow_id, h.index))
        states = []
        for hook in chosen:
            problem = _secret_problem(self._secret(hook.trigger))
            error = None if problem is None else f"disabled: {problem}"
            states.append(_webhook_state(hook.workflow_id, hook.trigger, error))
        return states

    def set_draining(self) -> None:
        """Reject new deliveries with 503 from now on."""
        self._draining = True

    def precheck(self, name: str) -> HookResponse | int:
        """Return a 404 or 503 response before the body is read, or the hook's body limit."""
        checked = self._check(name)
        return checked if isinstance(checked, HookResponse) else checked[0].trigger.max_body

    async def handle(self, name: str, request: HookRequest) -> HookResponse:
        """Verify, rate-limit, parse, deduplicate, and submit one delivery.

        Every authentication failure gets the same 401 body, and the rate limit applies only
        to requests that verified.
        """
        checked = self._check(name)
        if isinstance(checked, HookResponse):
            return checked
        hook, secret = checked
        trigger = hook.trigger
        # Settings can change while the route reads the body, so `handle` runs the precheck
        # again and enforces the hook's own `max_body` itself.
        if len(request.body) > trigger.max_body:
            return _error(
                413, "payload_too_large", f"the body is larger than {trigger.max_body} bytes"
            )
        message = self._verify(trigger, secret.encode("utf-8"), request)
        if message is None:
            return _error(401, "unauthorized", "the request signature didn't verify")
        wait = hook.bucket.take(asyncio.get_running_loop().time())
        if wait:
            retry = {"Retry-After": str(max(1, math.ceil(wait)))}
            return _error(429, "rate_limited", "too many deliveries for this hook", retry)
        try:
            payload = webhook_payload(trigger, request, received_at=self._clock.now())
        except ValueError as exc:
            return _error(400, "bad_request", f"the body isn't valid JSON: {exc}")
        run_request = RunRequest(
            workflow_id=hook.workflow_id,
            kind=TriggerKind.WEBHOOK,
            trigger=payload,
            trigger_key=trigger.key,
            delivery=Delivery(name, delivery_digest(trigger, message), trigger.dedupe_window),
        )
        try:
            submission = await self._submitter.submit(run_request)
        except ShuttingDownError as exc:
            return _error(503, "shutting_down", str(exc), _RETRY_AFTER)
        except WorkflowInvalidError as exc:
            return _error(503, "workflow_invalid", str(exc))
        except NotFoundError as exc:  # the workflow went away after the precheck
            return _error(404, "not_found", str(exc))
        return _accepted(name, submission)

    def _check(self, name: str) -> HookResponse | tuple[_Hook, str]:
        """Run the checks that need no body: the hook exists, isn't draining, and has a secret."""
        hook = self._hooks.get(name)
        if hook is None:
            return _error(404, "not_found", f"no webhook is named '{name}'")
        if self._draining:
            return _error(503, "shutting_down", "Treadle is shutting down", _RETRY_AFTER)
        secret = self._secret(hook.trigger)
        if secret is None or _secret_problem(secret) is not None:
            message = f"webhook '{name}' is disabled until its secret is configured"
            return _error(503, "hook_disabled", message)
        return hook, secret

    def _secret(self, trigger: WebhookTrigger) -> str | None:
        return self._secrets().lookup_env(trigger.secret_env)

    def _verify(self, trigger: WebhookTrigger, secret: bytes, request: HookRequest) -> bytes | None:
        """Return the signed message when the request authenticates, else None."""
        header = request.headers.get(trigger.signature_header)
        if trigger.signature is SignatureMode.TOKEN:
            return request.body if verify_token(secret, header) else None
        now = self._clock.now().timestamp()
        message = signed_message(trigger, request.headers, request.body, now=now)
        if message is None or not verify_hmac(secret, message, header):
            return None
        return message


def _hooks_of(workflow: Workflow) -> dict[str, tuple[int, WebhookTrigger]]:
    """Return a workflow's hooks by name, with their positions among its triggers."""
    return {
        trigger.name: (index, trigger)
        for index, trigger in enumerate(workflow.triggers)
        if isinstance(trigger, WebhookTrigger)
    }


def _webhook_state(workflow_id: str, trigger: WebhookTrigger, error: str | None) -> TriggerState:
    """Return a hook's state: a summary such as "webhook /hooks/blog-push", and its health."""
    summary = f"webhook /hooks/{trigger.name}"
    return TriggerState(workflow_id, trigger.key, TriggerKind.WEBHOOK, summary, None, error)


def _secret_problem(secret: str | None) -> str | None:
    """Explain why a secret disables its hook, or return None for a usable secret."""
    if secret is None:
        return "secret is not set"
    if len(secret) < MIN_SECRET_LENGTH:
        return f"secret is shorter than {MIN_SECRET_LENGTH} characters"
    return None


def _header_bytes(value: str) -> bytes:
    """Return the bytes a sender sent; header values arrive decoded as Latin-1."""
    # A value outside Latin-1 can't come from the HTTP layer; UTF-8 keeps the comparison
    # total instead of raising.
    try:
        return value.encode("latin-1")
    except UnicodeEncodeError:
        return value.encode("utf-8")


def _body(request: HookRequest) -> JSONValue:
    """Parse a JSON body strictly, or decode any other body as text with replacements."""
    media = request.content_type or ""
    if media != "application/json" and not (media.endswith("+json") and "/" in media):
        return request.body.decode("utf-8", "replace")
    try:
        value: JSONValue = json.loads(
            request.body.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite
        )
    except RecursionError:
        raise ValueError(f"nesting is deeper than {_MAX_NESTING} levels") from None
    if _depth(value) > _MAX_NESTING:
        raise ValueError(f"nesting is deeper than {_MAX_NESTING} levels")
    return value


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} isn't valid JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"the number {text} is out of range")
    return value


def _depth(value: JSONValue) -> int:
    """Return how deeply arrays and objects nest in `value`, without recursion."""
    deepest = 0
    pending: list[tuple[JSONValue, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict | list):
            depth += 1
            deepest = max(deepest, depth)
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth) for child in children)
    return deepest


def _accepted(name: str, submission: Submission) -> HookResponse:
    """Answer a submitted delivery: 200 for a duplicate, else 202 queued or skipped."""
    run = submission.run
    run_id = None if run is None else run.id
    _log.info("webhook %s: run %s %s", name, run_id, submission.outcome.value)
    if submission.outcome is SubmitOutcome.DUPLICATE:
        return HookResponse(200, {"status": "duplicate", "run_id": run_id})
    body: JSONObject = {"status": submission.outcome.value, "run_id": run_id}
    if submission.outcome is SubmitOutcome.SKIPPED:
        body["reason"] = _reason(run)
    return HookResponse(202, body)


def _reason(run: RunRecord | None) -> str | None:
    return None if run is None or run.reason is None else run.reason.value


def _error(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> HookResponse:
    return HookResponse(status, error_json(code, message), dict(headers or {}))
