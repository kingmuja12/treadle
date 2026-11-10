"""The synchronous repository: every SQL statement that Treadle runs.

Every state write is a conditional UPDATE on the allowed predecessor statuses and returns
whether it applied, so a stale or duplicate transition is harmless and the first terminal
state always wins. Rows come back as `sqlite3.Row` and map to records by column name.

Runtime data reaches every JSON and TEXT column through `storable_text`, so text that isn't
valid Unicode, such as a lone surrogate from an untrusted JSON document, is stored with
replacement characters instead of failing the write.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, TypeVar, cast

from treadle.clock import from_ms
from treadle.config import RetentionSettings
from treadle.errors import StoreError
from treadle.models import (
    AttemptRecord,
    AttemptStatus,
    ControlRequest,
    Delivery,
    LogLine,
    LogStream,
    RunDetail,
    RunFilter,
    RunPage,
    RunReason,
    RunRecord,
    RunStatus,
    ScheduleSlot,
    SkipReason,
    StepRecord,
    StepStatus,
    TriggerKind,
    Workflow,
)
from treadle.types import JSONObject, JSONValue

_E = TypeVar("_E", bound=StrEnum)
_Params: TypeAlias = Sequence[object] | Mapping[str, object]

_DAY_MS: Final = 86_400_000
_DELIVERY_RETENTION_MS: Final = 7 * _DAY_MS
_CONTROL_RETENTION_MS: Final = _DAY_MS
# The same 60 seconds as control.CONTROL_EXPIRY, which sits in a higher layer.
_CONTROL_EXPIRY_MS: Final = 60_000
_MAX_PAGE: Final = 200
_INTERRUPTED_LINE: Final = "Run interrupted: the Treadle process stopped while this run was active."
_EXPIRED_RESULT: Final[JSONObject] = {
    "error": {"code": "expired", "message": "no Treadle process handled the request in time"}
}
# Every UTF-16 surrogate code point. In a Python string each one stands alone, and SQLite
# can't encode it as UTF-8.
_SURROGATE: Final = re.compile("[\ud800-\udfff]")
# A failed savepoint must be rolled back to and then released, or it stays open.
_UNDO_SAVEPOINT: Final = ("ROLLBACK TO nested", "RELEASE nested")
_LOG_QUERY: Final = """SELECT * FROM log_lines
    WHERE run_id = :run_id AND seq > :after
      AND (:step_id IS NULL OR step_id = :step_id)
      AND (:attempt IS NULL OR attempt = :attempt)
    ORDER BY seq"""

# Closes what no executor will finish: running attempts and running or retrying steps become
# interrupted, and pending steps skipped (run_interrupted). `:run` names one run, or is NULL
# for every run, as in recovery, where no executor runs.
_CLOSE_STEPS: Final = (
    """UPDATE step_attempts SET status = 'interrupted', finished_at = :now
       WHERE status = 'running' AND (:run IS NULL OR run_id = :run)""",
    """UPDATE steps SET status = 'interrupted', finished_at = :now
       WHERE status IN ('running', 'retrying') AND (:run IS NULL OR run_id = :run)""",
    """UPDATE steps SET status = 'skipped', skip_reason = 'run_interrupted', finished_at = :now
       WHERE status = 'pending' AND (:run IS NULL OR run_id = :run)""",
)
# Crash recovery, after `_CLOSE_STEPS` for every run: running runs get a log line, and then
# become interrupted.
_RECOVERY: Final = (
    """INSERT INTO log_lines (run_id, seq, ts, stream, text)
       SELECT id, (SELECT COALESCE(MAX(seq), 0) + 1 FROM log_lines WHERE run_id = runs.id),
              :now, 'system', :text
       FROM runs WHERE status = 'running'""",
    """UPDATE runs SET status = 'interrupted', reason = 'executor_stopped', finished_at = :now
       WHERE status = 'running'""",
)


@dataclass(frozen=True, slots=True)
class NewRun:
    """A run to insert with `create_run`, with the admission policy already decided.

    `trigger` and `inputs` are masked. `cancel_queued` and `replaced_active` carry the
    `replace` policy's work, which happens in the same transaction.
    """

    workflow: Workflow
    status: RunStatus
    reason: RunReason | None
    trigger_kind: TriggerKind
    trigger_key: str | None
    trigger: JSONObject
    inputs: JSONObject
    attempt: int
    rerun_of: int | None
    slot: ScheduleSlot | None
    delivery: Delivery | None
    cancel_queued: tuple[int, ...]
    replaced_active: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class CreateResult:
    """The result of `create_run`: the new run, or the earlier run of a duplicate delivery."""

    outcome: Literal["created", "duplicate", "slot_taken"]
    run: RunRecord | None


@dataclass(frozen=True, slots=True)
class StepSeed:
    """A step row that `start_run` inserts as `pending`."""

    step_id: str
    name: str
    position: int
    uses: str


@dataclass(frozen=True, slots=True)
class StepUpdate:
    """A conditional step transition. Optional fields left as None keep their stored values.

    `at_ms` becomes `started_at` on the first move to `running`, and `finished_at` on a move
    to a terminal status. `outputs` and `error` are masked.
    """

    status: StepStatus
    allowed_from: frozenset[StepStatus]
    at_ms: int
    attempts: int | None = None
    tolerated: bool | None = None
    skip_reason: SkipReason | None = None
    outputs: JSONObject | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NewLogLine:
    """A log line to append. `text` is masked."""

    seq: int
    ts_ms: int
    step_id: str | None
    attempt: int | None
    stream: LogStream
    text: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What startup recovery changed.

    `orphan_pgids` holds the recorded process groups of interrupted attempts that still
    answer `os.killpg(pgid, 0)`. `queued_runs` counts the runs that stay queued.
    """

    interrupted_runs: tuple[int, ...]
    orphan_pgids: tuple[int, ...]
    queued_runs: int
    expired_controls: int


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    """A stored workflow version. `path` is relative to the project directory."""

    digest: str
    workflow_id: str
    path: str
    source: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PruneStats:
    """How many rows retention deleted, per table."""

    runs: int = 0
    versions: int = 0
    deliveries: int = 0
    controls: int = 0


class _SlotTaken(Exception):
    """Rolls `create_run` back when the schedule slot's compare-and-set fails."""


class Repository:
    """Run state, logs, schedules, control requests, and retention over one connection.

    The connection must come from `store.db.connect`, which turns off implicit transactions.
    Each method that runs several statements wraps them in `transaction()`, so a call is
    atomic on its own and also composes inside an enclosing transaction.
    """

    def __init__(self, conn: sqlite3.Connection, *, project_dir: Path) -> None:
        """Wrap `conn`, and set its `row_factory` to `sqlite3.Row`.

        Workflow paths are stored relative to `project_dir`.
        """
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._project_dir = project_dir

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block in one write transaction: `BEGIN IMMEDIATE`, then COMMIT or ROLLBACK.

        A nested call joins the enclosing transaction through a savepoint, so a nested block
        that raises undoes only its own writes, and the enclosing block decides the rest.
        """
        # A nested call joins the outer transaction through a savepoint, which lets
        # `create_run` roll back its own writes for `slot_taken` inside `AsyncStore.write`,
        # which always wraps the call in a transaction.
        if self._conn.in_transaction:
            with self._bracket("SAVEPOINT nested", "RELEASE nested", _UNDO_SAVEPOINT):
                yield
        else:
            with self._bracket("BEGIN IMMEDIATE", "COMMIT", ("ROLLBACK",)):
                yield

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Run the block in one read transaction (`BEGIN DEFERRED`), for a consistent view.

        Inside an open transaction, the block joins it.
        """
        if self._conn.in_transaction:
            yield
        else:
            with self._bracket("BEGIN DEFERRED", "COMMIT", ("ROLLBACK",)):
                yield

    # Run lifecycle

    def create_run(self, new: NewRun) -> CreateResult:
        """Insert a run, in one transaction with its delivery, slot, and replace work.

        The delivery is recorded first, then the slot advances, the workflow version and the
        run are stored, and the replaced runs are cancelled. A delivery seen within its
        window returns `duplicate` with the earlier run, which is None when retention pruned
        it. A failed slot compare-and-set rolls everything back and returns `slot_taken`.
        """
        try:
            with self.transaction():
                return self._create_run(new)
        except _SlotTaken:
            return CreateResult("slot_taken", None)

    def start_run(self, run_id: int, steps: Sequence[StepSeed], *, at_ms: int, pid: int) -> bool:
        """Move a run from `queued` to `running`, and insert its steps as `pending`."""
        with self.transaction():
            started = self._changed(
                """UPDATE runs SET status = 'running', started_at = ?, executor_pid = ?
                   WHERE id = ? AND status = 'queued'""",
                (at_ms, pid, run_id),
            )
            if started:
                self._conn.executemany(
                    """INSERT INTO steps (run_id, step_id, name, position, uses, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    [(run_id, step.step_id, step.name, step.position, step.uses) for step in steps],
                )
        return started

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        *,
        at_ms: int,
        reason: RunReason | None,
        error: str | None,
    ) -> bool:
        """Move a `running` run to a terminal status with its reason and masked error."""
        return self._changed(
            """UPDATE runs SET status = ?, reason = ?, error = ?, finished_at = ?
               WHERE id = ? AND status = 'running'""",
            (status, reason, _storable(error), at_ms, run_id),
        )

    def finish_abandoned_run(
        self,
        run_id: int,
        status: RunStatus,
        *,
        at_ms: int,
        reason: RunReason | None,
        error: str | None,
    ) -> bool:
        """Finish a run whose executor stopped before it finished, such as after an error.

        In one transaction, the run moves as `finish_run` moves it, and what its executor
        left unfinished closes as crash recovery closes it: running attempts and running or
        retrying steps become `interrupted`, and pending steps `skipped` (`run_interrupted`).
        The steps close even when another writer finished the run. Returns whether the run
        moved.
        """
        with self.transaction():
            finished = self.finish_run(run_id, status, at_ms=at_ms, reason=reason, error=error)
            for statement in _CLOSE_STEPS:
                self._conn.execute(statement, {"now": at_ms, "run": run_id})
        return finished

    def fail_queued(self, run_id: int, *, at_ms: int, error: str) -> bool:
        """Move a `queued` run to `failed` with reason `setup_failed`."""
        return self._changed(
            """UPDATE runs SET status = 'failed', reason = 'setup_failed', error = ?,
                   finished_at = ?
               WHERE id = ? AND status = 'queued'""",
            (storable_text(error), at_ms, run_id),
        )

    def skip_queued(self, run_ids: Sequence[int], *, reason: RunReason, at_ms: int) -> list[int]:
        """Move `queued` runs to `skipped`, and return the IDs that moved."""
        sql = """UPDATE runs SET status = 'skipped', reason = ?, finished_at = ?
                 WHERE id = ? AND status = 'queued'"""
        with self.transaction():
            return [run_id for run_id in run_ids if self._changed(sql, (reason, at_ms, run_id))]

    def cancel_queued(
        self,
        run_ids: Sequence[int],
        *,
        reason: RunReason,
        at_ms: int,
        replaced_by: int | None = None,
    ) -> list[int]:
        """Move `queued` runs to `cancelled`, and return the IDs that moved.

        `replaced_by`, when given, links each cancelled run to the run that replaced it.
        """
        sql = """UPDATE runs SET status = 'cancelled', reason = ?, finished_at = ?,
                     replaced_by = COALESCE(?, replaced_by)
                 WHERE id = ? AND status = 'queued'"""
        with self.transaction():
            return [
                run_id
                for run_id in run_ids
                if self._changed(sql, (reason, at_ms, replaced_by, run_id))
            ]

    def mark_cancel_requested(self, run_id: int, *, at_ms: int) -> None:
        """Record when a cancel was first requested, while the run is still active."""
        self._conn.execute(
            """UPDATE runs SET cancel_requested_at = ?
               WHERE id = ? AND cancel_requested_at IS NULL AND status IN ('queued', 'running')""",
            (at_ms, run_id),
        )

    def update_step(self, run_id: int, step_id: str, update: StepUpdate) -> bool:
        """Apply a step transition when the step's status is in `update.allowed_from`."""
        return self._changed(
            """UPDATE steps SET status = :status,
                   attempts = COALESCE(:attempts, attempts),
                   tolerated = COALESCE(:tolerated, tolerated),
                   skip_reason = COALESCE(:skip_reason, skip_reason),
                   outputs_json = COALESCE(:outputs, outputs_json),
                   error = COALESCE(:error, error),
                   started_at = CASE WHEN :status = 'running' THEN COALESCE(started_at, :at)
                                     ELSE started_at END,
                   finished_at = CASE WHEN :terminal THEN :at ELSE finished_at END
               WHERE run_id = :run_id AND step_id = :step_id
                 AND instr(:allowed, ',' || status || ',') > 0""",
            {
                "status": update.status,
                "attempts": update.attempts,
                "tolerated": update.tolerated,
                "skip_reason": update.skip_reason,
                "outputs": None if update.outputs is None else _dumps(update.outputs),
                "error": _storable(update.error),
                "at": update.at_ms,
                "terminal": update.status.terminal,
                "run_id": run_id,
                "step_id": step_id,
                "allowed": _status_set(update.allowed_from),
            },
        )

    def start_attempt(
        self, run_id: int, step_id: str, attempt: int, *, at_ms: int, delay_before_ms: int
    ) -> None:
        """Insert a `running` attempt. `delay_before_ms` is the backoff wait before it."""
        self._conn.execute(
            """INSERT INTO step_attempts (run_id, step_id, attempt, status, started_at,
                   delay_before_ms)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (run_id, step_id, attempt, at_ms, delay_before_ms),
        )

    def set_attempt_pgid(self, run_id: int, step_id: str, attempt: int, pgid: int) -> None:
        """Record an attempt's process group, so recovery can warn about orphans."""
        self._conn.execute(
            "UPDATE step_attempts SET pgid = ? WHERE run_id = ? AND step_id = ? AND attempt = ?",
            (pgid, run_id, step_id, attempt),
        )

    def finish_attempt(
        self,
        run_id: int,
        step_id: str,
        attempt: int,
        *,
        status: AttemptStatus,
        at_ms: int,
        exit_code: int | None,
        error: str | None,
        log_bytes: int,
        log_truncated: bool,
    ) -> bool:
        """Move a `running` attempt to its final status. `error` is masked."""
        return self._changed(
            """UPDATE step_attempts SET status = ?, finished_at = ?, exit_code = ?, error = ?,
                   log_bytes = ?, log_truncated = ?
               WHERE run_id = ? AND step_id = ? AND attempt = ? AND status = 'running'""",
            (
                status,
                at_ms,
                exit_code,
                _storable(error),
                log_bytes,
                log_truncated,
                run_id,
                step_id,
                attempt,
            ),
        )

    def append_logs(self, run_id: int, lines: Sequence[NewLogLine]) -> None:
        """Insert log lines with one `executemany`."""
        with self.transaction():
            self._conn.executemany(
                """INSERT INTO log_lines (run_id, seq, ts, step_id, attempt, stream, text)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        line.seq,
                        line.ts_ms,
                        line.step_id,
                        line.attempt,
                        line.stream,
                        storable_text(line.text),
                    )
                    for line in lines
                ],
            )

    def recover(self, *, at_ms: int) -> RecoveryReport:
        """Mark what a stopped process left running as interrupted, in one transaction.

        Running attempts and steps become `interrupted`, pending steps `skipped`
        (`run_interrupted`), and the runs `interrupted` (`executor_stopped`) after a system
        log line. A finished run's leftover attempts and steps, which an executor that failed
        may leave, close the same way. Unconsumed control requests older than 60 seconds
        expire. Recovery never signals a recorded process group; it only reports the ones
        that still exist.
        """
        with self.transaction():
            runs = self._rows("SELECT id FROM runs WHERE status = 'running' ORDER BY id")
            pgids = self._rows(
                """SELECT DISTINCT a.pgid FROM step_attempts a JOIN runs r ON r.id = a.run_id
                   WHERE r.status = 'running' AND a.status = 'running' AND a.pgid IS NOT NULL
                   ORDER BY a.pgid"""
            )
            for statement in _CLOSE_STEPS:
                self._conn.execute(statement, {"now": at_ms, "run": None})
            for statement in _RECOVERY:
                self._conn.execute(statement, {"now": at_ms, "text": _INTERRUPTED_LINE})
            expired = self._conn.execute(
                """UPDATE control_requests SET consumed_at = ?, result = ?
                   WHERE consumed_at IS NULL AND created_at < ?""",
                (at_ms, _dumps(_EXPIRED_RESULT), at_ms - _CONTROL_EXPIRY_MS),
            ).rowcount
            queued = self._scalar("SELECT COUNT(*) FROM runs WHERE status = 'queued'")
        return RecoveryReport(
            interrupted_runs=tuple(row["id"] for row in runs),
            orphan_pgids=tuple(row["pgid"] for row in pgids if _group_exists(row["pgid"])),
            queued_runs=queued,
            expired_controls=expired,
        )

    # Schedule state

    def schedule_slot(self, key: str) -> int | None:
        """Return the stored `last_slot` of a schedule trigger, or None."""
        row = self._row("SELECT last_slot FROM schedule_state WHERE trigger_key = ?", (key,))
        return None if row is None else cast(int, row["last_slot"])

    def init_schedule(self, key: str, workflow_id: str, slot_ms: int, *, at_ms: int) -> int:
        """Insert the schedule state unless it exists, then return the stored `last_slot`."""
        with self.transaction():
            self._conn.execute(
                """INSERT OR IGNORE INTO schedule_state (trigger_key, workflow_id, last_slot,
                       updated_at)
                   VALUES (?, ?, ?, ?)""",
                (key, workflow_id, slot_ms, at_ms),
            )
            return cast(int, self.schedule_slot(key))

    def advance_schedule(self, key: str, previous_ms: int, new_ms: int, *, at_ms: int) -> bool:
        """Compare-and-set `last_slot` from `previous_ms` to `new_ms`."""
        return self._changed(
            """UPDATE schedule_state SET last_slot = ?, updated_at = ?
               WHERE trigger_key = ? AND last_slot = ?""",
            (new_ms, at_ms, key, previous_ms),
        )

    def prune_schedules(
        self,
        *,
        keep: Collection[str],
        valid_workflows: Collection[str],
        known_workflows: Collection[str],
    ) -> int:
        """Delete the schedule state of removed workflows, and stale keys of valid ones.

        A row goes when its workflow has no file (not in `known_workflows`), or when its
        workflow is valid and its key isn't in `keep`, the current schedule keys. An invalid
        workflow keeps its state, so fixing the file resumes the schedule.
        """
        with self.transaction():
            stale = [
                row["trigger_key"]
                for row in self._rows("SELECT trigger_key, workflow_id FROM schedule_state")
                if row["workflow_id"] not in known_workflows
                or (row["workflow_id"] in valid_workflows and row["trigger_key"] not in keep)
            ]
            self._conn.executemany(
                "DELETE FROM schedule_state WHERE trigger_key = ?", [(key,) for key in stale]
            )
        return len(stale)

    # Control requests

    def enqueue_control(
        self, kind: Literal["run", "cancel"], payload: JSONObject, *, at_ms: int
    ) -> int:
        """Insert a control request and return its ID."""
        cursor = self._conn.execute(
            "INSERT INTO control_requests (kind, payload, created_at) VALUES (?, ?, ?)",
            (kind, _dumps(payload), at_ms),
        )
        return cast(int, cursor.lastrowid)

    def claim_controls(self, *, at_ms: int, limit: int = 50) -> list[ControlRequest]:
        """Claim up to `limit` unconsumed control requests in ID order, setting `consumed_at`."""
        with self.transaction():
            rows = self._rows(
                "SELECT * FROM control_requests WHERE consumed_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            )
            self._conn.executemany(
                "UPDATE control_requests SET consumed_at = ? WHERE id = ?",
                [(at_ms, row["id"]) for row in rows],
            )
        return [
            ControlRequest(
                id=row["id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=from_ms(row["created_at"]),
            )
            for row in rows
        ]

    def complete_control(self, request_id: int, result: JSONObject, *, at_ms: int) -> None:
        """Store a control request's result, marking it consumed if nothing claimed it."""
        self._conn.execute(
            """UPDATE control_requests SET result = ?, consumed_at = COALESCE(consumed_at, ?)
               WHERE id = ?""",
            (_dumps(result), at_ms, request_id),
        )

    def withdraw_control(self, request_id: int, result: JSONObject, *, at_ms: int) -> bool:
        """Consume a control request that nothing has claimed yet, storing `result`.

        Returns whether it applied. A lock holder that claimed the request first keeps it,
        and the answer that it stores later stands. A withdrawn request is never claimed.
        """
        return self._changed(
            """UPDATE control_requests SET consumed_at = ?, result = ?
               WHERE id = ? AND consumed_at IS NULL""",
            (at_ms, _dumps(result), request_id),
        )

    def control_result(self, request_id: int) -> JSONObject | None:
        """Return a control request's result, or None while it has none."""
        row = self._row("SELECT result FROM control_requests WHERE id = ?", (request_id,))
        if row is None or row["result"] is None:
            return None
        return cast(JSONObject, json.loads(row["result"]))

    # Reads

    def get_run(self, run_id: int) -> RunRecord | None:
        """Return one run, or None."""
        row = self._row("SELECT * FROM runs WHERE id = ?", (run_id,))
        return None if row is None else _run(row)

    def get_run_detail(self, run_id: int) -> RunDetail | None:
        """Return one run with its steps in position order and their attempts, or None."""
        with self.snapshot():
            run = self.get_run(run_id)
            if run is None:
                return None
            attempts: defaultdict[str, list[AttemptRecord]] = defaultdict(list)
            for row in self._rows(
                "SELECT * FROM step_attempts WHERE run_id = ? ORDER BY step_id, attempt", (run_id,)
            ):
                attempts[row["step_id"]].append(_attempt(row))
            steps = self._rows("SELECT * FROM steps WHERE run_id = ? ORDER BY position", (run_id,))
        return RunDetail(run, tuple(_step(row, attempts[row["step_id"]]) for row in steps))

    def list_runs(self, flt: RunFilter) -> RunPage:
        """Return one page of matching runs, newest first, with `flt.limit` clamped to 1-200.

        `flt.text` matches the run error or any log line of the run, ignoring the case of
        ASCII letters, as SQL `LIKE` does.
        """
        # SQLite's lower() folds ASCII letters only, the same rule as LIKE, which `--grep`
        # promises; instr() then needs no wildcard escaping.
        limit = min(max(flt.limit, 1), _MAX_PAGE)
        rows = self._rows(
            """SELECT * FROM runs r
               WHERE (:workflow IS NULL OR r.workflow_id = :workflow)
                 AND (:statuses = '' OR instr(:statuses, ',' || r.status || ',') > 0)
                 AND (:kind IS NULL OR r.trigger_kind = :kind)
                 AND (:before IS NULL OR r.id < :before)
                 AND (:text IS NULL
                      OR instr(lower(r.error), lower(:text)) > 0
                      OR EXISTS (SELECT 1 FROM log_lines l
                                 WHERE l.run_id = r.id AND instr(lower(l.text), lower(:text)) > 0))
               ORDER BY r.id DESC LIMIT :limit""",
            {
                "workflow": flt.workflow_id,
                "statuses": _status_set(flt.statuses),
                "kind": flt.trigger_kind,
                "before": flt.before_id,
                "text": flt.text or None,
                "limit": limit + 1,  # one more than a page tells whether another page exists
            },
        )
        page = tuple(_run(row) for row in rows[:limit])
        return RunPage(page, page[-1].id if len(rows) > limit else None)

    def last_runs(self) -> dict[str, RunRecord]:
        """Return the newest run of each workflow, by workflow ID."""
        rows = self._rows(
            "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY workflow_id)"
        )
        return {row["workflow_id"]: _run(row) for row in rows}

    def latest_run_id(self, workflow_id: str | None = None) -> int | None:
        """Return the newest run ID, of one workflow when `workflow_id` is set, or None."""
        # A named parameter, not ?1: early Python 3.12 releases warn that a numbered
        # placeholder is a named one when the parameters come as a sequence.
        return cast(
            "int | None",
            self._scalar(
                "SELECT MAX(id) FROM runs WHERE :workflow_id IS NULL OR workflow_id = :workflow_id",
                {"workflow_id": workflow_id},
            ),
        )

    def queued_runs(self) -> list[RunRecord]:
        """Return the queued runs in ID order."""
        return [
            _run(row)
            for row in self._rows("SELECT * FROM runs WHERE status = 'queued' ORDER BY id")
        ]

    def logs(
        self,
        run_id: int,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        after_seq: int = 0,
        limit: int = 1000,
        tail: bool = False,
    ) -> list[LogLine]:
        """Return up to `limit` log lines with `seq > after_seq`, in ascending `seq` order.

        `step_id` and `attempt` filter when they're set. With `tail`, the lines are the last
        `limit` matching lines instead of the first.
        """
        rows = self._rows(
            _LOG_QUERY + (" DESC LIMIT :limit" if tail else " LIMIT :limit"),
            {
                "run_id": run_id,
                "after": after_seq,
                "step_id": step_id,
                "attempt": attempt,
                "limit": max(limit, 0),
            },
        )
        lines = [_log_line(row) for row in rows]
        return lines[::-1] if tail else lines

    def last_log_seq(self, run_id: int) -> int:
        """Return the highest log `seq` of a run, or 0 when it has none."""
        return cast(
            int,
            self._scalar("SELECT COALESCE(MAX(seq), 0) FROM log_lines WHERE run_id = ?", (run_id,)),
        )

    def workflow_version(self, digest: str) -> WorkflowVersion | None:
        """Return a stored workflow version, or None."""
        row = self._row("SELECT * FROM workflow_versions WHERE digest = ?", (digest,))
        if row is None:
            return None
        return WorkflowVersion(
            digest=row["digest"],
            workflow_id=row["workflow_id"],
            path=row["path"],
            source=row["source"],
            created_at=from_ms(row["created_at"]),
        )

    # Retention

    def prune_batch(self, policy: RetentionSettings, *, now_ms: int, batch: int = 100) -> int:
        """Delete up to `batch` terminal runs, log lines first, and return how many went.

        A run goes when it finished more than `max_age` ago, or when it falls outside the
        newest `max_runs_per_workflow` runs of its workflow. Queued and running runs always
        stay. The cascade deletes the steps and attempts.
        """
        with self.transaction():
            doomed = [
                (row["id"],)
                for row in self._rows(
                    """SELECT id FROM (
                         SELECT id, status, finished_at,
                                ROW_NUMBER() OVER (PARTITION BY workflow_id ORDER BY id DESC) AS rn
                         FROM runs)
                       WHERE status NOT IN ('queued', 'running')
                         AND (finished_at < :cutoff OR rn > :keep)
                       ORDER BY id LIMIT :batch""",
                    {
                        "cutoff": now_ms - round(policy.max_age * 1000),
                        "keep": policy.max_runs_per_workflow,
                        "batch": batch,
                    },
                )
            ]
            self._conn.executemany("DELETE FROM log_lines WHERE run_id = ?", doomed)
            self._conn.executemany("DELETE FROM runs WHERE id = ?", doomed)
        return len(doomed)

    def prune_misc(self, *, now_ms: int, keep_digests: Collection[str]) -> PruneStats:
        """Delete old deliveries and control requests, and unused workflow versions.

        Webhook deliveries go after 7 days, and consumed control requests after 1 day. A
        workflow version goes when no run references it and it isn't in `keep_digests`.
        """
        with self.transaction():
            deliveries = self._conn.execute(
                "DELETE FROM webhook_deliveries WHERE received_at < ?",
                (now_ms - _DELIVERY_RETENTION_MS,),
            ).rowcount
            controls = self._conn.execute(
                "DELETE FROM control_requests WHERE consumed_at IS NOT NULL AND created_at < ?",
                (now_ms - _CONTROL_RETENTION_MS,),
            ).rowcount
            unused = [
                (row["digest"],)
                for row in self._rows(
                    """SELECT digest FROM workflow_versions v
                       WHERE NOT EXISTS (SELECT 1 FROM runs WHERE runs.digest = v.digest)"""
                )
                if row["digest"] not in keep_digests
            ]
            self._conn.executemany("DELETE FROM workflow_versions WHERE digest = ?", unused)
        return PruneStats(versions=len(unused), deliveries=deliveries, controls=controls)

    def incremental_vacuum(self, pages: int = 2000) -> None:
        """Return up to `pages` free pages to the file system.

        Call it outside a transaction: inside one, it raises StoreError and changes nothing.
        """
        if self._conn.in_transaction:
            raise StoreError("an incremental vacuum can't run inside a transaction")
        # The pragma frees one page each time SQLite steps it. Python 3.11's sqlite3 steps it
        # only once, even when every row is fetched, because its rows have no columns, while
        # executescript() runs it to the end on every version. On 3.11, executescript() would
        # also commit an open transaction first, which the check above rules out.
        self._conn.executescript(f"PRAGMA incremental_vacuum({int(pages)});")

    # Helpers

    @contextmanager
    def _bracket(self, begin: str, end: str, undo: Sequence[str]) -> Iterator[None]:
        """Run the block between `begin` and `end`, or run `undo` when the block raises."""
        self._conn.execute(begin)
        try:
            yield
            self._conn.execute(end)
        except BaseException:
            # SQLite rolls some errors back by itself; then there is nothing left to undo.
            if self._conn.in_transaction:
                for statement in undo:
                    self._conn.execute(statement)
            raise

    def _create_run(self, new: NewRun) -> CreateResult:
        """Do `create_run`'s writes in order. Raises _SlotTaken to roll them back."""
        now = new.created_at_ms
        delivery = new.delivery if new.delivery is not None and new.delivery.window > 0 else None
        if delivery is not None and not self._record_delivery(delivery, now):
            return CreateResult("duplicate", self._delivery_run(delivery))
        slot = new.slot
        if slot is not None and not self.advance_schedule(
            slot.key, slot.previous_ms, slot.slot_ms, at_ms=now
        ):
            raise _SlotTaken
        self._store_version(new.workflow, now)
        run_id = self._insert_run(new)
        if delivery is not None:
            self._conn.execute(
                "UPDATE webhook_deliveries SET run_id = ? WHERE hook = ? AND digest = ?",
                (run_id, delivery.hook, delivery.digest),
            )
        self.cancel_queued(
            new.cancel_queued, reason=RunReason.REPLACED, at_ms=now, replaced_by=run_id
        )
        if new.replaced_active is not None:
            self._conn.execute(
                "UPDATE runs SET replaced_by = ? WHERE id = ?", (run_id, new.replaced_active)
            )
        return CreateResult("created", self.get_run(run_id))

    def _record_delivery(self, delivery: Delivery, now_ms: int) -> bool:
        """Record a delivery digest. False means it was already seen within its window."""
        return self._changed(
            """INSERT INTO webhook_deliveries (hook, digest, received_at) VALUES (?, ?, ?)
               ON CONFLICT (hook, digest) DO UPDATE
                 SET received_at = excluded.received_at, run_id = NULL
                 WHERE webhook_deliveries.received_at <= ?""",
            (delivery.hook, delivery.digest, now_ms, now_ms - round(delivery.window * 1000)),
        )

    def _delivery_run(self, delivery: Delivery) -> RunRecord | None:
        """Return the run that a recorded delivery created, unless it was pruned."""
        row = self._row(
            """SELECT r.* FROM webhook_deliveries d JOIN runs r ON r.id = d.run_id
               WHERE d.hook = ? AND d.digest = ?""",
            (delivery.hook, delivery.digest),
        )
        return None if row is None else _run(row)

    def _store_version(self, workflow: Workflow, now_ms: int) -> None:
        """Store the workflow version that the new run is bound to, once per digest."""
        self._conn.execute(
            """INSERT OR IGNORE INTO workflow_versions (digest, workflow_id, path, source,
                   created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (workflow.digest, workflow.id, self._relative(workflow.path), workflow.source, now_ms),
        )

    def _insert_run(self, new: NewRun) -> int:
        """Insert the run row and return its ID. A skipped run is finished on arrival."""
        # The unique (trigger_key, scheduled_for) index backs up the slot's
        # compare-and-set. If it refuses a row (possible only after last_slot moved back),
        # the sqlite3.IntegrityError propagates and rolls back the whole `create_run`, and
        # the scheduler sees an ordinary submit error, which it logs and moves past.
        cursor = self._conn.execute(
            """INSERT INTO runs (workflow_id, workflow_name, digest, status, reason,
                   trigger_kind, trigger_key, trigger_json, inputs_json, scheduled_for, attempt,
                   rerun_of, created_at, finished_at)
               VALUES (:workflow_id, :workflow_name, :digest, :status, :reason, :trigger_kind,
                   :trigger_key, :trigger, :inputs, :scheduled_for, :attempt, :rerun_of, :now,
                   :finished_at)""",
            {
                "workflow_id": new.workflow.id,
                "workflow_name": new.workflow.name,
                "digest": new.workflow.digest,
                "status": new.status,
                "reason": new.reason,
                "trigger_kind": new.trigger_kind,
                "trigger_key": new.trigger_key,
                "trigger": _dumps(new.trigger),
                "inputs": _dumps(new.inputs),
                "scheduled_for": None if new.slot is None else new.slot.slot_ms,
                "attempt": new.attempt,
                "rerun_of": new.rerun_of,
                "now": new.created_at_ms,
                "finished_at": new.created_at_ms if new.status is RunStatus.SKIPPED else None,
            },
        )
        return cast(int, cursor.lastrowid)

    def _relative(self, path: Path) -> str:
        """Return `path` relative to the project directory, for `workflow_versions.path`."""
        # A workflow outside the project directory (a `workflows_dir` set
        # elsewhere) keeps its absolute path, which `project_dir / path` still resolves.
        if path.is_relative_to(self._project_dir):
            return str(path.relative_to(self._project_dir))
        return str(path)

    def _rows(self, sql: str, params: _Params = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def _row(self, sql: str, params: _Params = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
        return row

    def _scalar(self, sql: str, params: _Params = ()) -> Any:
        """Return the first column of a query that always yields one row, such as an aggregate."""
        return self._conn.execute(sql, params).fetchone()[0]

    def _changed(self, sql: str, params: _Params) -> bool:
        """Execute one write and return whether it changed a row."""
        return self._conn.execute(sql, params).rowcount > 0


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        workflow_id=row["workflow_id"],
        workflow_name=row["workflow_name"],
        digest=row["digest"],
        status=RunStatus(row["status"]),
        reason=_optional(RunReason, row["reason"]),
        error=row["error"],
        trigger_kind=TriggerKind(row["trigger_kind"]),
        trigger_key=row["trigger_key"],
        trigger=json.loads(row["trigger_json"]),
        inputs=json.loads(row["inputs_json"]),
        scheduled_for=_time(row["scheduled_for"]),
        attempt=row["attempt"],
        rerun_of=row["rerun_of"],
        replaced_by=row["replaced_by"],
        created_at=from_ms(row["created_at"]),
        started_at=_time(row["started_at"]),
        finished_at=_time(row["finished_at"]),
        cancel_requested_at=_time(row["cancel_requested_at"]),
    )


def _step(row: sqlite3.Row, attempts: Sequence[AttemptRecord]) -> StepRecord:
    return StepRecord(
        step_id=row["step_id"],
        name=row["name"],
        position=row["position"],
        uses=row["uses"],
        status=StepStatus(row["status"]),
        tolerated=bool(row["tolerated"]),
        skip_reason=_optional(SkipReason, row["skip_reason"]),
        started_at=_time(row["started_at"]),
        finished_at=_time(row["finished_at"]),
        outputs=json.loads(row["outputs_json"]),
        error=row["error"],
        attempts=tuple(attempts),
    )


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        attempt=row["attempt"],
        status=AttemptStatus(row["status"]),
        started_at=from_ms(row["started_at"]),
        finished_at=_time(row["finished_at"]),
        exit_code=row["exit_code"],
        error=row["error"],
        delay_before_ms=row["delay_before_ms"],
        log_bytes=row["log_bytes"],
        log_truncated=bool(row["log_truncated"]),
    )


def _log_line(row: sqlite3.Row) -> LogLine:
    return LogLine(
        seq=row["seq"],
        ts=from_ms(row["ts"]),
        step_id=row["step_id"],
        attempt=row["attempt"],
        stream=LogStream(row["stream"]),
        text=row["text"],
    )


def _optional(kind: type[_E], value: str | None) -> _E | None:
    return None if value is None else kind(value)


def _time(ms: int | None) -> datetime | None:
    return None if ms is None else from_ms(ms)


def storable_text(text: str) -> str:
    """Return `text` with each lone surrogate replaced by U+FFFD, so SQLite can store it.

    A Python string can hold surrogates that no UTF-8 text can, for example from the JSON
    escape `\\ud800` in an untrusted document. Valid text comes back unchanged.
    """
    return _SURROGATE.sub("\ufffd", text)


def _storable(text: str | None) -> str | None:
    return None if text is None else storable_text(text)


def _dumps(value: JSONValue) -> str:
    """Return compact JSON for a JSON column, with `storable_text` applied to it."""
    return storable_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _status_set(statuses: Collection[str]) -> str:
    """Encode statuses as ",a,b," for `instr` membership tests; "" stands for no filter.

    One static statement then serves any number of statuses.
    """
    return f",{','.join(sorted(statuses))}," if statuses else ""


def _group_exists(pgid: int) -> bool:
    """True when the process group still exists. Signal 0 checks without signalling."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, but another user owns it
    return True
