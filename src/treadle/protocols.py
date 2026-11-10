"""Structural interfaces between packages, so each package depends on a contract, not a class."""

from __future__ import annotations

from typing import Protocol

from treadle.models import (
    CancelOutcome,
    CancelReason,
    HookRequest,
    HookResponse,
    LogLine,
    ManagerStats,
    RegistryChange,
    RunDetail,
    RunFilter,
    RunPage,
    RunRecord,
    RunRequest,
    Submission,
    TriggerState,
    Workflow,
    WorkflowEntry,
)


class SecretLookup(Protocol):
    """Read access to secrets and to variables that may live in `.env`."""

    def get(self, name: str) -> str | None:
        """Return `secrets.NAME`: `TREADLE_SECRET_NAME` from the environment, then `.env`."""

    def lookup_env(self, name: str) -> str | None:
        """Return a variable from the process environment, then from `.env`."""


class WorkflowSource(Protocol):
    """The loaded workflows."""

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        """Return the entry for one workflow ID, or None."""

    def entries(self) -> list[WorkflowEntry]:
        """Return every entry, sorted by workflow ID."""

    async def version(self, digest: str) -> Workflow | None:
        """Return a stored workflow version, parsed again, or None when it no longer loads."""


class RunSubmitter(Protocol):
    """Accepts run requests."""

    async def submit(self, request: RunRequest) -> Submission:
        """Admit or reject a run request under the workflow's concurrency policy."""


class RunControl(RunSubmitter, Protocol):
    """Starts, cancels, and re-runs runs."""

    async def cancel(
        self, run_id: int, reason: CancelReason = CancelReason.USER, *, force: bool = False
    ) -> CancelOutcome:
        """Cancel a queued or running run. A second cancel counts as a force."""

    async def rerun(self, run_id: int) -> Submission:
        """Submit a finished run again with its stored trigger payload and inputs."""

    def stats(self) -> ManagerStats:
        """Return the active and queued run counts."""


class RunReader(Protocol):
    """Read access to run history."""

    async def get_run(self, run_id: int) -> RunDetail | None:
        """Return a run with its steps and attempts, or None."""

    async def list_runs(self, flt: RunFilter) -> RunPage:
        """Return one page of runs, newest first."""

    async def logs(
        self,
        run_id: int,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        after_seq: int = 0,
        limit: int = 1000,
        tail: bool = False,
    ) -> list[LogLine]:
        """Return log lines with `seq > after_seq` in ascending order.

        With `tail`, return the last `limit` matching lines, still in ascending order.
        """

    async def last_runs(self) -> dict[str, RunRecord]:
        """Return the newest run of each workflow, by workflow ID."""

    async def latest_run_id(self, workflow_id: str | None = None) -> int | None:
        """Return the newest run ID, optionally for one workflow, or None."""


class WebhookHandler(Protocol):
    """Handles `POST /hooks/<name>`."""

    def precheck(self, name: str) -> HookResponse | int:
        """Return an error response before the body is read, or the body limit in bytes."""

    async def handle(self, name: str, request: HookRequest) -> HookResponse:
        """Verify, rate-limit, parse, and submit one webhook delivery."""


class TriggerInfo(Protocol):
    """Trigger summaries and health, for the dashboard and API."""

    def trigger_states(self, workflow_id: str | None = None) -> list[TriggerState]:
        """Return the trigger states, optionally for one workflow."""


class TriggerSink(Protocol):
    """Receives workflow version changes."""

    def apply(self, change: RegistryChange) -> None:
        """Apply a change synchronously, without awaiting, so the swap is atomic."""


class ProcessTracker(Protocol):
    """Tracks live process groups, so a forced shutdown can kill them."""

    def add(self, pgid: int) -> None:
        """Start tracking a process group."""

    def discard(self, pgid: int) -> None:
        """Stop tracking a process group. Unknown IDs are ignored."""
