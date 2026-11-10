# Architecture

This page explains how Treadle is built: the processes, the source layers, the life of a
run, and how Treadle stores state. It's for contributors, and for users who want to know
what Treadle guarantees and why.

## Design goals

- **Exact semantics.** Every run ends in exactly one final state. Each schedule slot creates
  at most one run, and each run starts at most once. Treadle commits each state change to
  SQLite before it acts on it.
- **Safe defaults.** The server listens on loopback only, every webhook needs a secret,
  Treadle masks secrets wherever it stores or prints text, and every resource has a limit.
- **Debuggable.** Every problem in a workflow file has a stable code and a location, and you
  can debug a failed run from the CLI without starting the daemon.
- **Small.** One process, one SQLite file, and five direct runtime dependencies: PyYAML,
  Starlette, uvicorn, Jinja2, and HTTPX. With their own dependencies, an install adds about
  a dozen third-party packages.

## Processes

`treadle serve` is one process with one asyncio event loop. SQLite calls and folder scans
run in worker threads, and each `shell` step runs as a child process in its own process
group.

```text
treadle serve
┌─────────────────────────────────────────────────────────────────────────────────┐
│ scheduler, watches, webhooks ──┐                                                │
│ dashboard and API ─────────────┼──> run manager ──> executor ──> step processes │
│ control poller ────────────────┘          │             │                       │
│                                           v             v                       │
│ registry, retention ───────> SQLite: runs, steps, logs, schedule state          │
└─────────────────────────────────────────────────────────────────────────────────┘
                                            ^
                                            │ reads the history; sends run and cancel requests
                              treadle runs, show, logs, run, cancel
```

- The **registry** loads the workflow files, and reloads them when they change.
- The **scheduler**, the **watch service**, and the **webhook router** turn trigger events
  into run requests.
- The **run manager** decides, for each request, whether the run is queued or skipped, and
  starts queued runs when they may start.
- The **executor** runs one run's steps: conditions, retries, timeouts, and cancellation.
- The **log sink** batches log lines into SQLite.
- The **control poller** answers `treadle run` and `treadle cancel` requests from other
  terminals.
- The **web application** serves the dashboard, the JSON API, and the webhook endpoint.
- **Retention** prunes old runs.

Each long-running part logs its own errors and keeps going, so one broken trigger never stops
the daemon. If a part stops unexpectedly, the daemon shuts down as it does after a signal and
exits 1.

### One executor per state directory

Exactly one process runs workflows for a state directory: the one that holds the lock on
`.treadle/treadle.lock`. That's `treadle serve`, or a `treadle run` in a terminal when no
daemon is running. The lock is an `flock` lock, so the operating system releases it when the
process dies, and a stale lock never blocks a restart. The holder writes its PID, kind, and
port to `.treadle/holder.json`.

The other commands work alongside the lock holder:

- **Read commands** (`list`, `runs`, `show`, `logs`) read SQLite directly, so they work
  whether or not a daemon is running.
- **`treadle run` and `treadle cancel`** write a request to the `control_requests` table and
  wait for the lock holder to answer it. The CLI needs no HTTP connection and no token:
  anyone who can write the state directory is already trusted.

The lock holder takes control requests one at a time, in order, and stores each answer
before it takes the next request. When it can't store an answer, it keeps the answer and
stores it first on its next pass. During shutdown, a request that it already took still
gets its answer.

When the CLI stops waiting, it withdraws its request in one conditional update, so a lock
holder that hasn't taken the request yet never acts on it. When the lock holder took the
request first, its answer is on the way: the CLI waits up to 10 more seconds for it, and
otherwise reports that the request may still take effect.

`flock` isn't reliable on network file systems, so keep the state directory on a local disk.

## Source layout

The source lives in `src/treadle/`, in ten layers. A module may import from any lower layer,
and from modules listed before it in its own layer. `tests/test_architecture.py` checks every
import, including imports inside functions and under `if TYPE_CHECKING:`.

| Layer | Modules, in order |
|---|---|
| 0 | `types`, `errors`, `units`, `clock`, `expr.ast`, and every package `__init__` |
| 1 | `models`, `schema` |
| 2 | `config`, `protocols`, `steps.base` |
| 3 | `expr.lexer`, `expr.parser`, `expr.analysis`, `expr.template`, `expr.functions`, `expr.evaluator`, `cron`, `yamlio`, `globs`, `fswatch`, `secrets`, `lock` |
| 4 | `store.db`, `store.migrations`, `store.repo`, `store.async_store`, `steps.procs`, `steps.shell`, `steps.http`, `steps.file`, `steps.log`, `steps.sleep`, `steps.builtin` |
| 5 | `workflow.inputs`, `workflow.graph`, `workflow.lint`, `workflow.loader`, `serialize`, `retention` |
| 6 | `engine.masking`, `engine.context`, `engine.logsink`, `engine.executor`, `engine.manager` |
| 7 | `registry`, `triggers.scheduler`, `triggers.watch`, `triggers.webhook`, `triggers.manager`, `control` |
| 8 | `web.security`, `web.api`, `web.dashboard`, `web.hooks`, `web.app`, `runner`, `daemon` |
| 9 | `cli.output`, `cli.scaffold`, `cli.follow`, `cli.commands`, `cli.parser`, `cli.main`, `__main__` |

Only a few modules may import third-party packages:

| Package | Modules |
|---|---|
| `yaml` | `yamlio` |
| `httpx` | `steps.base`, `steps.http`, `engine.executor`, `runner`, `daemon` |
| `starlette`, `jinja2` | `web.*` |
| `uvicorn` | `daemon` |

Every other module uses only the standard library and Treadle itself.

## The life of a run

### Run states

| From | To | When |
|---|---|---|
| (new) | `queued` | The concurrency policy admits the run. |
| (new) | `skipped` | The policy rejects the run (`already_active` or `queue_full`). |
| `queued` | `running` | The run manager starts it. |
| `queued` | `cancelled` | A user cancels it, a newer run replaces it, or its workflow file is deleted. A file deleted while Treadle was stopped cancels the run at the next start of `treadle serve`. |
| `queued` | `skipped` or `failed` | At startup, the daemon re-admits queued runs through the policy, and fails a run whose stored workflow version no longer loads. |
| `running` | `succeeded`, `failed`, `cancelled`, or `timed_out` | The run ends. |
| `running` | `interrupted` | The process that ran it stopped, and the next lock holder found it. |

Final states never change. Treadle writes every change as a conditional update, such as
`UPDATE runs SET status = ... WHERE id = ? AND status IN (...)`, so when two changes race,
the first one wins and the second one does nothing.

A finished run's status follows these rules, in order:

1. If the run timed out, it's `timed_out`.
2. Otherwise, if anything cancelled it, it's `cancelled`.
3. Otherwise, if a step failed or timed out without being tolerated, it's `failed`. The error
   names the first such step, as in `step 'archive' failed after 3 attempts: exit code 23`.
4. Otherwise it's `succeeded`, even when every step was skipped.

A run's reason explains a `skipped`, `cancelled`, `timed_out`, `interrupted`, or setup
failure:

| Reason | Meaning |
|---|---|
| `already_active` | The `skip` policy found a queued or running run. |
| `queue_full` | The `queue` policy found `max_queued` waiting runs. |
| `user` | Someone cancelled the run. |
| `replaced` | A newer run of a `replace` workflow replaced it. |
| `timeout` | The run reached its workflow `timeout`. |
| `shutdown` | Treadle shut down while the run ran: the daemon stopped, or the `treadle run` that ran it got `SIGTERM` or `SIGHUP`. |
| `workflow_removed` | Its workflow file was deleted while the run waited, or while Treadle was stopped. |
| `executor_stopped` | The Treadle process stopped while the run ran. |
| `setup_failed` | The run couldn't start its steps, for example because a workflow `env` value didn't render. |

### Step and attempt states

```text
pending ──condition false, upstream failure, or forced cancel──> skipped
pending ──render, validation, or condition error──> failed (no attempts)
pending ──> running ──attempt succeeded──> succeeded
            running ──attempt failed or timed out, attempts left──> retrying ──wait──> running
            running ──last attempt failed, or the error can't be retried──> failed
            running ──last attempt timed out──> timed_out
            running or retrying ──cancelled──> cancelled
running or retrying ──the process stopped──> interrupted
```

Each attempt ends `succeeded`, `failed`, `timed_out`, `cancelled`, or `interrupted`. A step's
log lines are always committed before its final status.

A run can also end without its executor: an internal error fails it, or a forced shutdown
records it as cancelled. Then its running steps and attempts end `interrupted`, and its
pending steps `skipped` with the reason `run_interrupted`, in the same write that finishes
the run.

### Admission

The run manager is an actor: one task takes requests from a queue, and finishes each one,
database writes included, before it starts the next. Admission decisions never interleave, so
the order in the database matches the order of events.

The manager starts queued runs in run-ID order. A run starts when its workflow has no active
run and fewer than `[runs] max_concurrent` runs are active. A workflow that waits for its own
run doesn't hold back other workflows. The manager first commits `queued` to `running`, and
only then starts the run's task, so a run starts at most once.

### Execution

For each run, the executor:

1. Creates a scratch directory, writes the trigger data to `event.json`, and renders the
   workflow `env`.
2. Starts the run timer.
3. Repeatedly starts each pending step whose dependencies have ended, up to `max_parallel`
   steps at a time, after it evaluates the step's `if`.
4. On the first cancel request, marks the run as cancelling, starts the cleanup timer, and
   stops the running steps whose condition is now false.
5. Works out the run's status, closes the log, records the result, and deletes the scratch
   directory.

Each step renders its `env` and `with`, checks the rendered parameters, and then runs its
attempts. Between attempts it waits as its retry policy says. A cancel interrupts the wait.

## Storage

Treadle keeps everything in one SQLite database, `.treadle/treadle.db`:

| Table | Holds |
|---|---|
| `workflow_versions` | The source of each workflow version that a run used, by digest. |
| `runs` | One row per run: status, reason, error, trigger data, inputs, and times. |
| `steps`, `step_attempts` | Each step and attempt of a run, with statuses, outputs, errors, and exit codes. |
| `log_lines` | Every log line, by run and sequence number. |
| `schedule_state` | The last slot that each schedule processed. |
| `webhook_deliveries` | Digests of recent deliveries, for replay protection. |
| `control_requests` | Requests from `treadle run` and `treadle cancel`, and their answers. |
| `schema_version` | The applied schema migrations. |

- **Durability.** The database uses write-ahead logging (WAL) and `synchronous=FULL`, so a
  committed schedule slot survives a power loss before its run starts.
- **Logs in SQLite.** One transactional store keeps logs consistent with run state:
  retention deletes a run and its logs together, a crash can't leave a dangling log file, and
  `treadle runs --grep` is one query. The log sink batches inserts, every 250 milliseconds or
  500 lines, and the per-attempt and per-run log limits bound the database's size.
- **Masking before storage.** Treadle masks secrets before anything reaches the database.
- **Text that isn't valid Unicode.** Treadle stores U+FFFD in place of each lone surrogate,
  such as one that the JSON escape `\ud800` in a webhook body produces, instead of failing
  the write. Log lines show U+FFFD too.
- **Workflow versions.** When Treadle creates a run, it stores the workflow's source by its
  SHA-256 digest. A queued run executes its own version, even if you edit the file while it
  waits. A re-run uses the current version.
- **Threads.** The event loop never calls SQLite. One writer thread runs writes in order, and
  a small pool of reader threads serves reads. Other processes can read at any time.
- **Requirements.** Treadle needs SQLite 3.31 or later.

### Migrations

When the daemon or a foreground `treadle run` opens a database that has data and an older
schema, Treadle first copies it to `.treadle/backups/`, and keeps the newest 3 copies. Then it
applies the migrations in one transaction, so a failed migration leaves the old schema
intact. A database from a newer version of Treadle stops the command with exit code 3 and the
message `this database was created by a newer Treadle (schema 2 > 1); upgrade Treadle`.
Treadle never downgrades a database.

### Retention

The daemon prunes the history when it starts, and then every `[retention] interval` (1 hour
by default). A foreground `treadle run` prunes up to 500 runs after its run ends.

- Treadle deletes finished runs older than `[retention] max_age` (30 days by default), and
  the runs beyond the newest `[retention] max_runs_per_workflow` (500) of each workflow.
- It never deletes a queued or running run.
- It deletes webhook delivery digests after 7 days, answered control requests after 1 day,
  and workflow versions that no run uses and that aren't current.
- It deletes in batches of 100 runs, each in its own transaction, so it never holds the
  database for long. Then it returns free pages to the file system.

## Crash recovery

When a process takes the lock, before anything else, it recovers what a crashed lock holder
left behind, in one transaction:

1. The attempts, steps, and runs that were running become `interrupted`, and the run's reason
   is `executor_stopped`.
2. Pending steps of those runs become `skipped` with the reason `run_interrupted`.
3. Each interrupted run gets the log line "Run interrupted: the Treadle process stopped while
   this run was active."
4. In runs that already finished, the attempts and steps that a failed executor left
   running or pending close the same way.
5. Control requests older than 60 seconds expire.

Then it deletes the old scratch directories. The daemon re-admits the queued runs through
their concurrency policies, in order:

- The daemon cancels a queued run whose workflow file was deleted while Treadle was
  stopped, with the reason `workflow_removed`. When no workflow loads at all, for example
  because the workflows directory can't be read, such runs stay queued, and the daemon
  logs a warning.
- A queued run whose stored workflow version no longer loads fails.

A foreground `treadle run` leaves the queued runs for the next `treadle serve`.

Treadle never runs an interrupted run again on its own, because its steps may not be safe to
repeat. You can re-run it with `treadle run --rerun RUN`.

Treadle also never signals a process group that a dead process recorded, because the
operating system may have given that ID to another process. When such a group still exists,
Treadle logs a warning. Under systemd, `KillMode=mixed` kills those processes when the
service stops. See [Operations](operations.md#run-treadle-with-systemd).

## Startup

`treadle serve` handles signals from the moment its startup begins. Then it:

1. Checks the API token, takes the lock, and opens the database, which migrates it when
   needed.
2. Recovers what a crashed lock holder left behind, as [Crash recovery](#crash-recovery)
   describes.
3. Loads every workflow file, and binds the listening sockets.
4. Re-admits the queued runs, and starts the triggers, the control poller, retention, and
   the HTTP server.
5. Logs `listening on http://127.0.0.1:8765`, and sends `READY=1` to the service manager
   when `NOTIFY_SOCKET` names one.

A `SIGHUP` during startup does nothing, because step 3 reads every file anyway. A first
`SIGTERM` or `SIGINT` during startup shuts the daemon down as soon as it serves. A `SIGHUP`
that arrives in about the first 0.3 seconds of the process, before Treadle's own code runs,
can still end it; under systemd, `Type=notify` keeps a reload from coming that early.

When one of the first three steps fails, the daemon exits with that error's code, such as 8
for a port in use. When step 4 or 5 fails, the daemon logs `starting the services failed`,
shuts down as after a signal, and exits 1.

## Shutdown

On the first `SIGTERM` or `SIGINT`, the daemon:

1. Refuses new runs. Webhooks and run requests get 503 with `Retry-After: 30`, while reads
   keep working, and the dashboard shows a banner.
2. Stops the scheduler, the watches, the registry, the control poller, and retention.
3. Cancels the running runs with the reason `shutdown`, and gives their cleanup steps up to
   `[runs] shutdown_grace` (15 seconds by default). A run that is already cancelling, for
   example after a user's cancel or a replacement, keeps its reason, and its cleanup steps
   keep running within the same grace. Then the daemon forces the cancellation of the runs
   that are left, and records each run that still hasn't stopped as cancelled.
4. Kills every step process group that a forced cancel left behind, and logs a line such as
   `killed 2 process groups that cancelled steps left running`.
5. Stops the HTTP server.
6. Closes the database, releases the lock, removes `holder.json`, and exits 0.

Queued runs stay queued and run after the next start. A second signal skips the rest of the
grace period. A third exits at once with code 130, and the next start recovers the leftovers
as interrupted runs.

## HTTP API

`treadle serve` serves a JSON API under `/api/v1/`. When a token is set, each request needs
`Authorization: Bearer <token>`, or the dashboard's session cookie. Requests that send a body
must use `Content-Type: application/json`, or they get 415. The CLI's `--json` output uses the
same shapes.

| Method | Path | Response |
|---|---|---|
| GET | `/api/v1/health` | 200 `{"status": "ok", "version": "0.1.0"}`, or 503 with `"status": "draining"` during shutdown. Needs no token. |
| GET | `/api/v1/status` | 200 `{"version", "schema_version", "uptime_s", "active_runs", "queued_runs", "draining", "workflows": {"valid", "invalid"}}` |
| GET | `/api/v1/workflows` | 200 `{"workflows": [Workflow]}` |
| GET | `/api/v1/workflows/{workflow_id}` | 200 WorkflowDetail; 404 |
| POST | `/api/v1/workflows/{workflow_id}/runs` | Body `{"inputs": {...}}`, where `inputs` is optional. 202 `{"outcome": "queued" or "skipped", "run": Run}`; 404; 409 `workflow_invalid`; 422 `invalid_inputs`; 503 `shutting_down` |
| GET | `/api/v1/runs` | Query `workflow`, `status` (repeatable), `trigger`, `q`, `before`, and `limit` (1–200, default 50). 200 `{"runs": [Run], "next_before_id"}` |
| GET | `/api/v1/runs/{run_id}` | 200 RunDetail; 404 |
| GET | `/api/v1/runs/{run_id}/logs` | Query `step`, `attempt`, `after` (default 0), and `limit` (1–5,000, default 1,000). 200 `{"lines": [Log], "next_after", "complete"}`; 404 |
| POST | `/api/v1/runs/{run_id}/cancel` | Body `{"force": false}`. 202 CancelOutcome; 404; 409 `already_finished` |
| POST | `/api/v1/runs/{run_id}/rerun` | Body `{}`. 202 `{"outcome", "run"}`; 404; 409 `conflict` or `workflow_invalid`; 422; 503 |

To follow a run's logs, pass the `next_after` value of each response as `after` in the next
request, until `complete` is `true`.

```console
$ curl -s -X POST http://127.0.0.1:8765/api/v1/workflows/hello/runs \
    -H 'Content-Type: application/json' -d '{"inputs": {"name": "API"}}'
{"outcome":"queued","run":{"id":16,"workflow":"hello","workflow_name":"Hello",...}}
```

### JSON shapes

**Run:**

```json
{"id": 42, "workflow": "nightly-backup", "workflow_name": "Nightly backup",
 "digest": "3f9a1c0be2d4...", "status": "failed", "reason": null,
 "error": "step 'archive' failed after 3 attempts: exit code 23",
 "trigger_kind": "schedule", "trigger": {"kind": "schedule", "cron": "30 2 * * *"},
 "inputs": {"dry_run": false}, "attempt": 1, "rerun_of": null, "replaced_by": null,
 "scheduled_for": "2026-11-09T02:30:00.000Z", "created_at": "2026-11-09T02:30:00.004Z",
 "started_at": "2026-11-09T02:30:00.010Z", "finished_at": "2026-11-09T02:31:12.551Z",
 "duration_ms": 72541}
```

- **RunDetail** is a Run plus `"steps": [Step]`.
- **Step:** `{"id", "name", "position", "uses", "status", "tolerated", "skip_reason",
  "started_at", "finished_at", "duration_ms", "outputs", "error", "attempts": [Attempt]}`.
- **Attempt:** `{"attempt", "status", "exit_code", "error", "delay_before_ms", "started_at",
  "finished_at", "duration_ms", "log_bytes", "log_truncated"}`.
- **Log:** `{"seq", "ts", "step", "attempt", "stream", "text"}`, where `step` and `attempt`
  are `null` for the run's own lines.
- **Workflow:** `{"id", "name", "description", "path", "valid", "stale", "digest", "issues":
  [Issue], "concurrency": {"policy", "max_queued"}, "triggers": [Trigger], "last_run": Run}`.
  **WorkflowDetail** adds `"inputs"` and `"steps"`.
- **Trigger:** `{"kind", "key", "summary", "next_fire", "error"}`. `error` describes a
  degraded watch or a disabled webhook.
- **CancelOutcome:** `{"run_id", "status", "accepted", "forced"}`.
- **Error:** `{"error": {"code", "message", "details"}}`. The codes are `bad_request`,
  `unauthorized`, `forbidden`, `not_found`, `conflict`, `already_finished`,
  `workflow_invalid`, `invalid_inputs`, `unsupported_media_type`, `payload_too_large`,
  `rate_limited`, `shutting_down`, `hook_disabled`, `busy`, `expired`, and `internal`.

One answer doesn't use the error shape. When a request declares a `Content-Length` larger
than the body limit, `[limits] api_body_bytes` for the API and the dashboard's forms or
`[limits] webhook_body_bytes` for webhooks, Starlette answers 413 with the plain text
`Content Too Large`, and without the security headers. A body that grows past the limit
while it streams gets the JSON `payload_too_large` error.

Times are ISO 8601 strings in UTC, and missing values are `null`.

### Dashboard

The dashboard at `/` lists the workflows with their triggers, next fire times, health, and
last runs. A workflow's page shows its steps and issues, its recent runs, and a **Run now**
form that follows the input types. The runs page filters the history, and a run's page shows
its steps, its trigger data, and a live log, with **Cancel run** and **Re-run** buttons.
Every page works without JavaScript. The script adds live updates to a running run's page,
and asks you to confirm before it cancels a run.
