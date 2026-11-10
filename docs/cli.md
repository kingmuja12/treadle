# CLI reference

The `treadle` command manages a project, runs workflows, and reads the run history. Global
options come before the command:

```text
treadle [GLOBAL OPTIONS] COMMAND [OPTIONS] [ARGUMENTS]
```

`python -m treadle` works the same way. `treadle --help` lists the commands, and
`treadle COMMAND --help` describes one command's options.

| Command | What it does |
|---|---|
| [`init`](#treadle-init) | Creates a project with example workflows. |
| [`validate`](#treadle-validate) | Checks workflow files and reports their issues. |
| [`list`](#treadle-list) | Shows the workflows, their triggers, and their last runs. |
| [`run`](#treadle-run) | Runs a workflow now and follows its progress. |
| [`runs`](#treadle-runs) | Lists the run history, newest first. |
| [`show`](#treadle-show) | Summarizes a run, leading with its failure. |
| [`logs`](#treadle-logs) | Prints a run's log lines. |
| [`cancel`](#treadle-cancel) | Cancels a queued or running run. |
| [`serve`](#treadle-serve) | Runs the daemon: triggers, the dashboard, and the API. |
| [`cron`](#treadle-cron) | Shows when a cron expression fires, with daylight saving time notes. |
| [`graph`](#treadle-graph) | Prints a workflow's step graph. |
| [`version`](#treadle-version) | Prints the versions of Treadle and its runtime. |

## Global options

| Option | Effect |
|---|---|
| `--config PATH` | Uses this config file. The `TREADLE_CONFIG` environment variable does the same. |
| `-C DIR` | Runs as if Treadle started in `DIR`. A missing `DIR` exits 2 with `error: -C DIR: no such directory`. |
| `--workflows-dir DIR` | Overrides `workflows_dir`. |
| `--state-dir DIR` | Overrides `state_dir`. |
| `--no-color` | Turns color off. |
| `--debug` | Shows tracebacks for unexpected errors, and logs at the debug level. |
| `--version` | Prints what `treadle version` prints, and exits. |

Paths in options resolve against the current directory, after `-C`.

Treadle uses color only when standard output is a terminal. Setting `NO_COLOR`, or
`TERM=dumb`, also turns color off.

## Projects

A project is a directory with a `treadle.toml` file. Treadle finds the file by searching the
current directory and then each parent directory. The project directory is the directory
that holds the file.

These commands need a project, and exit 3 without one:

- `list`, `run`, `runs`, `show`, `logs`, `cancel`, and `serve`.
- `validate` without a `PATH`, and `graph` with a workflow ID instead of a path.

```console
$ treadle list
error: no treadle.toml found in /home/ada or its parents; run `treadle init`
```

This rule keeps Treadle from creating state directories in unexpected places.

## Run references

Commands that take `RUN` accept:

| Reference | Run |
|---|---|
| `42` or `#42` | Run 42 |
| `last` | The newest run of any workflow |
| `last:<workflow>` | The newest run of one workflow, such as `last:nightly-backup` |

A malformed reference exits 2:
`error: invalid run reference 'latest'; use 42, #42, last, or last:<workflow>`. A run that
doesn't exist exits 4, with `error: run 99 doesn't exist`,
`error: workflow 'ghost' has no runs`, or `error: there are no runs yet`.

## Output conventions

- Results, including a run's progress, go to standard output. Errors, warnings, and notes
  such as `Cancelling run #19 (press Ctrl-C again to force)` go to standard error.
- An error prints as `error: <message>`, and the command exits with the error's
  [exit code](#exit-codes). A warning prints as `warning: <message>`.
- An unexpected error prints `internal error: <type>: <message>` and exits 1. With
  `--debug`, the traceback follows.
- When the reader of the output goes away, as with `treadle logs 1 | head`, the command
  stops quietly and exits 1.
- Commands with `--json` print JSON instead of text. The shapes match the
  [API](architecture.md#http-api), and times are ISO 8601 strings in UTC.
- Tables show times in the project's time zone.

Statuses show a symbol before their name. When standard output can't encode the symbols,
Treadle prints a bracketed word instead:

| Status | Symbol | Without UTF-8 |
|---|---|---|
| `succeeded` | ✓ | `[ok]` |
| `failed` | ✗ | `[fail]` |
| `skipped` | ⊘ | `[skip]` |
| `timed_out` | ⧖ | `[time]` |
| `cancelled` | ⊗ | `[cancel]` |
| `interrupted` | ↯ | `[stop]` |
| `running` | ▸ | `[run]` |
| `retrying` | ↻ | `[retry]` |
| `queued`, `pending` | … | `[wait]` |

## `treadle init`

```text
treadle init [DIR] [--force]
```

Creates a project in `DIR`, or in the current directory:

```console
$ treadle init my-automations
Treadle project in my-automations:
  created  treadle.toml
  created  workflows/hello.yml
  created  workflows/site-check.yml
  created  .env.example
  created  .gitignore

Next steps:
  cd my-automations
  treadle run hello -i name=Ada
  treadle serve

On a machine that other people use, set TREADLE_TOKEN in .env before you serve.
See .env.example.
```

| File | Contents |
|---|---|
| `treadle.toml` | Every setting, commented out with its default, and `timezone` set to the time zone that Treadle detects from `TZ` or `/etc/localtime`. When it can't detect one, it warns and writes `UTC`. |
| `workflows/hello.yml` | A first workflow with an input, an output, and a `log` step. |
| `workflows/site-check.yml` | An HTTP check with retries and an alert. Its schedule is commented out, so a new daemon makes no network calls. |
| `.env.example` | Comments that describe the secrets the examples use. |
| `.gitignore` | Lines that keep `.env` and `.treadle/` out of Git. |

Each file reports one status:

- `created`: the file didn't exist.
- `skipped`: the file exists. With `--force`, `skipped` means the file already had the same
  content.
- `updated`: `--force` replaced a file whose content differed.

Treadle never replaces `.gitignore`. It appends the lines it needs, under the comment
`# Treadle: secrets and local state`, and only the ones that are missing.

Exit codes: 0; 2 when `DIR` is a file; 1 when Treadle can't write the files, with
`error: can't create the project in DIR: <reason>`.

## `treadle validate`

```text
treadle validate [PATH...] [--strict] [--json]
```

Checks workflow files and prints each issue, followed by a blank line, and then a summary.
Without `PATH`, it checks every workflow in the project's workflows directory. A `PATH` can
be a file or a directory, and needs no project.

```console
$ treadle validate
workflows/broken.yml:4:9: error[E204]: unknown function 'startswith'
   |
 4 |     if: startswith(trigger.body.ref, 'refs/tags/')
   |         ^
   = help: did you mean 'startsWith'? Function names are case-sensitive.

workflows/deploy.yml:8:30: warning[W101]: `trigger.body.ref` puts untrusted data into the `run` script
   |
 8 |     run: echo "Deploying ${{ trigger.body.ref }}"
   |                              ^
   = help: pass it through env instead: set `env: {VALUE: ${{ trigger.body.ref }}}`, then quote "$VALUE" in the script

2 valid, 1 invalid (1 error, 1 warning)
```

| Option | Effect |
|---|---|
| `--strict` | Fails on warnings too. Use it in CI. |
| `--json` | Prints `{"valid", "errors", "warnings", "files": [{"path", "workflow", "valid", "issues": [Issue]}]}`. With `--strict`, `valid` is `false` when there are warnings. |

Each Issue is `{"file", "severity", "code", "message", "path", "line", "column", "hint"}`,
where `path` is the location inside the workflow, such as `steps[0].run`.

Inside a project, `validate` also runs the checks that depend on the machine, such as
whether a secret or a watched folder exists. Outside a project, it skips them and says so:
`1 valid, 0 invalid; machine-specific checks skipped (no treadle.toml)`. When it finds no
workflow files, it prints `No workflow files in workflows.` and exits 0.

Exit codes: 0; 1 on errors, or on warnings with `--strict`; 3 without a project when you
give no `PATH`; 4 when a `PATH` doesn't exist (`error: nope.yml doesn't exist`).

For every diagnostic code, see [Diagnostic codes](workflow-syntax.md#diagnostic-codes).

## `treadle list`

```text
treadle list [--json]
```

Shows each workflow. It reads the files directly, so it works whether or not a daemon runs.

```console
$ treadle list
ID              NAME            TRIGGERS                         NEXT FIRE             LAST RUN        STATE
backup-test     Backup test     -                                -                     #3 ✗ failed     ok
hello           Hello           -                                -                     #2 ✓ succeeded  ok
nightly-backup  Nightly backup  cron 30 2 * * * (Europe/London)  2026-11-11 02:30 GMT  -               ok
site-check      Site check      -                                -                     -               ok
```

- STATE is `ok` or `invalid`. An invalid workflow shows `-` for NAME, and a footer such as
  ``1 workflow is invalid; `treadle validate` shows the issues.`` follows the table.
- The dashboard and the API also show runtime health, such as a degraded watch, and the
  `stale` state of a workflow whose last edit was invalid. `list` doesn't, because it
  doesn't ask the daemon.
- With no workflows, it prints `No workflows in workflows.`.
- `--json` prints `{"workflows": [Workflow]}`.

Exit codes: 0; 3.

## `treadle run`

```text
treadle run WORKFLOW [-i KEY=VALUE]... [-v] [--json] [--detach]
treadle run --rerun RUN [-v] [--json] [--detach]
```

Starts a run and follows it until it ends.

| Option | Effect |
|---|---|
| `-i`, `--input KEY=VALUE` | Sets an input. Repeat it for more inputs. `-i name=` sets an empty string. |
| `--rerun RUN` | Re-runs a finished run with its trigger data and inputs, using the workflow's current version. Use it instead of `WORKFLOW` and `-i`. |
| `-v`, `--verbose` | Streams the log lines too. |
| `--json` | Prints JSON Lines events instead of text. |
| `--detach` | Returns once the daemon has queued the run. Needs a running daemon. |

```console
$ treadle run hello -i name=Ada
Run #1  hello  (manual)
  ✓ greet    9ms
  ✓ report   1ms
Run #1 succeeded in 13ms.
```

Each step's line prints when the step ends. A failed run leads with the failure: each
retried attempt, the skipped steps, the last 20 log lines of the failed attempt, and the
next command to try.

```console
$ treadle run backup-test
Run #3  backup-test  (manual)
  ✓ check-drive   5ms
  ✗ archive       attempt 1/2: exit code 23; retrying in 1s
  ✗ archive       attempt 2/2: exit code 23 (1s)
  ⊘ record        skipped (upstream_failed)
  ✓ alert         1ms
--- archive, attempt 2: last 20 lines ---
sending incremental file list
rsync: connection refused (111)
attempt 2 failed: exit code 23
Run #3 failed in 1s. Details: treadle show 3
```

With `-v`, log lines stream as they arrive. Lines from the run itself, rather than a step,
show `run` in place of `step#attempt`, and the gutter is `|` without UTF-8:

```text
  │ greet#1 stdout: Hello, Ada!
  │ report#1 system: [info] Hello, Ada! (run 2)
```

With `--json`, each line is one event:

| Event | When |
|---|---|
| `{"event": "run_created", "run": Run}` | First. |
| `{"event": "step", "step": Step}` | When a step's status or attempt count changes. |
| `{"event": "log", "line": Log}` | For each log line, with `-v`. |
| `{"event": "run_finished", "run": Run, "exit_code": 1}` | Last. |

### Where the run happens

- **No daemon is running:** the run happens in this terminal. The terminal holds the state
  directory while the run lasts, so a second `treadle run` in another terminal exits 8:

  ```text
  error: another `treadle run` (pid 4242) holds .treadle; start `treadle serve` to run workflows concurrently
  ```

  `--detach` exits 2, because there's no daemon to hand the run to. Queued runs from an
  earlier `treadle serve` stay queued for the next one.
- **`treadle serve` is running:** the command asks the daemon to start the run, waits up to
  10 seconds for the answer, and then follows the run with the same output. If the daemon
  doesn't answer, the command withdraws the request and exits 8 with
  `error: the daemon (pid 4242) didn't respond`. A daemon that hasn't picked up the request
  by then never acts on it.

With `--detach`, the command prints `Run #10 queued` and exits 0, or
`Run #11 skipped (already_active)` and exits 7. With `--json`, it prints one `run_created`
event.

### Ctrl-C

- **During a run in this terminal**, the first Ctrl-C cancels the run and prints
  `Cancelling run #19 (press Ctrl-C again to force)`. Cleanup steps, such as those with
  `if: always()`, still run. A second Ctrl-C forces the cancellation. The command exits 130
  when the run has ended.
- **While following a daemon's run**, Ctrl-C stops following, and the command exits 130.
  The run keeps going:

  ```text
  Run #13 continues in the daemon. Follow it with `treadle logs 13 -f`.
  ```
- **While waiting for the daemon to answer**, Ctrl-C withdraws the request and prints
  `Stopped before the daemon started a run.`.

### Exit codes of `treadle run`

| Code | Meaning |
|---|---|
| 0 | The run succeeded, or `--detach` queued it. |
| 1 | The run failed. |
| 2 | A usage error or invalid inputs, such as ``error: input 'nam' isn't declared (did you mean 'name'?)`` or `error: input 'name' has no value (write name=VALUE)`. |
| 3 | The workflow is invalid. Its issues follow the error. |
| 4 | The workflow or the run to re-run doesn't exist. |
| 5 | The run was cancelled or interrupted. |
| 6 | The run timed out. |
| 7 | The concurrency policy skipped the run. |
| 8 | Busy: another `treadle run` holds the state directory, or the daemon didn't answer or is shutting down. |
| 130 | Interrupted with Ctrl-C. |

Before a run in this terminal starts, Treadle recovers what a crashed Treadle process left
behind, and may warn about it:

```text
warning: run=2 was interrupted: the Treadle process stopped while it ran
```

Treadle never runs an interrupted run again on its own, because its steps may not be safe to
repeat. `treadle show` suggests `treadle run --rerun 2`.

## `treadle runs`

```text
treadle runs [--workflow ID] [--status STATUS]... [--trigger KIND] [--grep TEXT]
             [--limit N] [--before ID] [--json]
```

Lists runs, newest first.

```console
$ treadle runs --limit 2
ID  WORKFLOW     STATUS       TRIGGER  STARTED                  DURATION
3   backup-test  ✗ failed     manual   2026-11-10 09:11:22 GMT  1s
2   hello        ✓ succeeded  manual   2026-11-10 09:11:21 GMT  9ms
Older runs: treadle runs --limit 2 --before 2
```

| Option | Effect |
|---|---|
| `--workflow ID` | Only this workflow's runs. |
| `--status STATUS` | Only runs with this status: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `timed_out`, `skipped`, or `interrupted`. Repeat it for more statuses. |
| `--trigger KIND` | Only runs that `manual`, `schedule`, `watch`, or `webhook` started. |
| `--grep TEXT` | Only runs whose error or any log line contains `TEXT`, ignoring case. |
| `--limit N` | At most `N` runs, from 1 to 200. The default is 20. |
| `--before ID` | Only runs older than run `ID`. |
| `--json` | Prints `{"runs": [Run], "next_before_id"}`. |

When more runs exist, the footer repeats your filters in the command for the next page.
DURATION shows `running` for a run that is still running. Without any run, the command
prints `No runs yet.`, and when filters match nothing, `No runs match.`.

Exit codes: 0; 2 for an invalid option value; 3.

## `treadle show`

```text
treadle show RUN [--json]
```

Summarizes one run, leading with its failure:

```console
$ treadle show 3
Run #3 · backup-test · failed · manual (cli)
Started 2026-11-10 09:11:22 GMT · took 1s · attempt 1
Error: step 'archive' failed after 2 attempts: exit code 23

  STEP         STATUS     ATTEMPTS  DURATION  NOTE
  check-drive  ✓ ok       1         5ms
  archive      ✗ failed   2         1s        exit code 23
  record       ⊘ skipped  0         -         upstream_failed
  alert        ✓ ok       1         1ms

--- archive, attempt 2: last 20 lines ---
sending incremental file list
rsync: connection refused (111)
attempt 2 failed: exit code 23

Hint: treadle logs 3 --step archive
```

- The heading names how the run started: `manual (cli)`, `schedule 2026-11-09 02:30 GMT`
  (with `(catch-up)` for a late slot), `webhook /hooks/deploy`, or `watch (2 changes)`. A
  re-run adds `re-run of #N`.
- A run that hasn't started shows `Created ...` instead of `Started ...`.
- The NOTE column holds the error, the reason a step was skipped, or `tolerated: <error>`
  for a failure that `continue_on_error` tolerated.
- The hint suggests `treadle logs N --step S` for a failed step, `treadle logs N -f` for a
  run that is still active, and `treadle run --rerun N` for an interrupted run.
- Without UTF-8, `·` becomes `-`.
- `--json` prints the run with its steps and attempts (RunDetail).

Exit codes: 0; 2 for a malformed reference; 3; 4 for a missing run.

## `treadle logs`

```text
treadle logs RUN [--step ID] [--attempt N] [-f] [--json]
```

Prints a run's log lines as `HH:MM:SS step#attempt stream | text`, in the project's time
zone. Lines from the run itself show `-` in place of `step#attempt`. The stream is `stdout`,
`stderr`, or `system` for Treadle's own lines.

```console
$ treadle logs 3 --step archive --attempt 1
09:11:22 archive#1 stdout | sending incremental file list
09:11:22 archive#1 stderr | rsync: connection refused (111)
09:11:22 archive#1 system | attempt 1 failed: exit code 23
09:11:22 archive#1 system | retrying in 1s (attempt 2 of 2)
```

| Option | Effect |
|---|---|
| `--step ID` | Only this step's lines. A step the run doesn't have exits 4: `error: run 3 has no step 'ghost'`. |
| `--attempt N` | Only this attempt's lines. |
| `-f`, `--follow` | Keeps printing new lines, checking every 0.5 seconds, until the run has ended and no lines remain. |
| `--json` | Prints one Log object per line: `{"seq", "ts", "step", "attempt", "stream", "text"}`. |

Exit codes: 0; 2 for a malformed reference; 3; 4.

## `treadle cancel`

```text
treadle cancel RUN [--force] [--wait SECONDS] [--json]
```

Cancels a queued or running run. A running run starts cancelling: Treadle stops its steps,
and cleanup steps get the workflow's `cleanup_timeout` to finish.

| Option | Effect |
|---|---|
| `--force` | Stops the run at once, without waiting for cleanup steps. |
| `--wait SECONDS` | How long to wait for the answer of the process that runs the run. The default is 5. With `0`, the command sends the request, prints `Cancel requested.`, and exits. |
| `--json` | Prints `{"run_id", "status", "accepted", "forced"}`. With `--wait 0`, `accepted` is `null`. |

The command prints one of:

- `Cancel requested for run #13.`, or `Cancel requested for run #13 (forced).`
- `Cancelled queued run #15.`
- `Run #12 already finished (skipped).` This isn't an error.
- `Run #13 is already finishing.`

When `treadle serve` or a `treadle run` in another terminal is running the run, the command
asks that process. If it doesn't answer in time, the command withdraws the request and exits
8 with `error: the daemon (pid 4242) didn't respond` or
``error: the running `treadle run` (pid 4242) didn't respond``. When no Treadle process is
running, the command cancels a queued run in the database directly. A run that a crashed
process left running then reports `already finished (interrupted)`.

Exit codes: 0, including for a run that already finished; 2 for a malformed reference; 3;
4; 8.

## `treadle serve`

```text
treadle serve [--host HOST] [--port PORT] [--log-level LEVEL]
```

Runs the daemon in the foreground: the triggers, the run queue, the dashboard, and the API.

| Option | Effect |
|---|---|
| `--host HOST` | The address to listen on. Overrides `[server] host`. |
| `--port PORT` | The port, from 0 to 65535. `0` picks a free port. Overrides `[server] port`. |
| `--log-level LEVEL` | `debug`, `info`, `warning`, or `error`. Overrides `[logging] level` and `--debug`. |

The daemon prints nothing to standard output. It logs to standard error, one line per event,
with the time in UTC:

```text
2026-11-10T09:11:47.586Z INFO treadle.daemon listening on http://127.0.0.1:8765
2026-11-10T09:11:49.269Z INFO treadle.engine run=1 workflow=deploy queued
2026-11-10T09:11:49.270Z INFO treadle.triggers.webhook webhook deploy: run 1 queued
2026-11-10T09:11:49.271Z INFO treadle.web POST /hooks/deploy 202 2ms
2026-11-10T09:11:49.271Z INFO treadle.engine run=1 workflow=deploy started
2026-11-10T09:11:49.280Z INFO treadle.engine run=1 step=show succeeded
2026-11-10T09:11:49.281Z INFO treadle.engine run=1 succeeded
2026-11-10T09:11:50.794Z INFO treadle.daemon shutting down; signal again to stop the runs at once
2026-11-10T09:11:50.955Z INFO treadle.daemon stopped
```

The daemon logs each request as method, path, status, and duration. It never logs request
bodies or headers, and it masks secrets in every line.

Signals:

| Signal | Effect |
|---|---|
| First `SIGTERM` or `SIGINT` (Ctrl-C) | Stops accepting runs, cancels the running runs with the reason `shutdown`, gives their cleanup steps up to `[runs] shutdown_grace` (default 15s), and exits 0. Queued runs stay queued for the next start. |
| Second `SIGTERM` or `SIGINT` | Stops the runs at once. |
| Third `SIGTERM` or `SIGINT` | Exits 130 at once. The next start marks the leftover runs as interrupted. |
| `SIGHUP` | Reloads the workflows now. |

Exit codes:

| Code | Cause |
|---|---|
| 0 | A signal stopped the daemon. |
| 1 | A part of the daemon stopped unexpectedly. The daemon logs `shutting down, because <part> stopped`, finishes the runs as after a signal, and exits. |
| 3 | A configuration problem, such as `set TREADLE_TOKEN (at least 32 characters) or bind 127.0.0.1` or `TREADLE_TOKEN must be at least 32 characters long`. |
| 8 | Another Treadle process holds the state directory, as in `another Treadle process holds the state directory lock (serve, pid 4242, port 8765)`, or the port can't be used, as in `port 8765 on 127.0.0.1 is in use`. |

For running the daemon as a service, see [Operations](operations.md).

## `treadle cron`

```text
treadle cron EXPR [--tz ZONE] [-n COUNT] [--after ISO] [--json]
```

Shows the next times that a cron expression fires, with notes for daylight saving time:

```console
$ treadle cron "*/15 2 * * *" --tz America/New_York --after 2026-03-08T00:00 -n 2
LOCAL                 UTC                   NOTE
2026-03-08 03:00 EDT  2026-03-08T07:00:00Z  02:00 to 02:45 don't exist (DST gap); runs once at 03:00
2026-03-09 02:00 EDT  2026-03-09T06:00:00Z
```

| Option | Effect |
|---|---|
| `--tz ZONE` | An IANA time zone. The default is the project's time zone, then the local time zone, then UTC. |
| `-n COUNT` | How many times to show, from 1 to 100. The default is 5. |
| `--after ISO` | Shows times strictly after this one. A time without an offset is wall time in `--tz`. The default is now. |
| `--json` | Prints `{"expression", "timezone", "fires": [{"local", "utc", "note"}]}`. |

`cron` needs no project. Errors exit 2:

- An invalid expression shows the problem with a caret, such as
  `error: hour 25 isn't between 0 and 23`.
- ``error: unknown time zone 'Mars/Olympus'; use an IANA name such as Europe/London``
- ``error: --after: invalid date or time 'yesterday'; use ISO 8601, such as 2026-03-07T02:30``

For the rules behind the notes, see [Time zones and daylight saving time](triggers.md#time-zones-and-daylight-saving-time).

## `treadle graph`

```text
treadle graph WORKFLOW [--format text|mermaid|json]
```

Prints the step graph of a workflow, given by its ID in the project or by a file path:

```console
$ treadle graph backup-test
[0] check-drive  shell  if: success()
[1] archive  shell  needs: check-drive (implicit)  if: success()
[2] record  log  needs: archive (implicit)  if: success()
[2] alert  log  needs: archive  if: failure()
```

The number in brackets is the step's depth in the graph. `(implicit)` marks a dependency
that comes from the order of the steps rather than from `needs`.

- `--format mermaid` prints a `flowchart TD` diagram, with dashed arrows for implicit
  dependencies.
- `--format json` prints `{"workflow", "nodes": [{"id", "name", "uses", "if", "depth"}],
  "edges": [{"from", "to", "implicit"}]}`.

Exit codes: 0; 3 for an invalid workflow, whose issues follow the error; 4 when the workflow
doesn't exist, as in ``error: workflow 'ghost' doesn't exist in workflows``.

## `treadle version`

```text
treadle version [--json]
```

```console
$ treadle version
treadle 0.1.0 (Python 3.12.13, darwin, SQLite 3.50.4, schema 1)
```

`--json` prints `{"treadle", "python", "platform", "sqlite", "schema_version"}`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success: the run succeeded, the files are valid, or the command finished. |
| 1 | The run failed, validation found problems, or an unexpected error occurred. |
| 2 | A usage error: bad arguments, bad inputs, or an invalid cron expression. |
| 3 | A configuration error: an invalid `treadle.toml`, no project, an invalid workflow, or a database from a newer Treadle. |
| 4 | The workflow, run, or file doesn't exist. |
| 5 | The run was cancelled or interrupted. |
| 6 | The run timed out. |
| 7 | The concurrency policy skipped the run. |
| 8 | Busy: the state directory is in use, the port is in use, or the daemon didn't respond. |
| 130 | Interrupted with Ctrl-C. |

## Configuration file

`treadle.toml` configures a project. Every key is optional. This file shows every key with
its default:

```toml
workflows_dir = "workflows"
state_dir     = ".treadle"
env_file      = ".env"          # "" turns it off
timezone      = "local"         # or an IANA name, such as "Europe/London"

[server]
host           = "127.0.0.1"
port           = 8765           # 0 picks a free port
token_env      = "TREADLE_TOKEN"
allowed_hosts  = []
secure_cookies = false
session_ttl    = "12h"

[runs]
max_concurrent = 4
kill_grace     = "5s"
shutdown_grace = "15s"

[retention]
max_age               = "30d"
max_runs_per_workflow = 500
interval              = "1h"

[limits]
step_log_bytes       = "1MiB"
run_log_bytes        = "16MiB"
line_bytes           = "16KiB"
output_file_bytes    = "1MiB"
outputs_bytes        = "1MiB"
stdout_capture_bytes = "64KiB"
webhook_body_bytes   = "1MiB"
api_body_bytes       = "64KiB"
watch_max_entries    = 10000

[security]
file_roots = [".", "~"]

[logging]
level = "info"                  # debug, info, warning, or error
```

| Key | Meaning | Allowed values |
|---|---|---|
| `workflows_dir` | Where the workflow files are. | A path |
| `state_dir` | Where Treadle keeps its database and lock. It must be on a local file system. | A path |
| `env_file` | The secrets file. | A path, or `""` for none |
| `timezone` | The default time zone for schedules and the time zone of the CLI's tables. `local` detects the zone from `TZ` or `/etc/localtime`, and falls back to UTC with a warning. | `local` or an IANA name |
| `server.host` | The address that `treadle serve` listens on. Any address other than a loopback address needs a token. | A host or address |
| `server.port` | The port. | 0–65535 |
| `server.token_env` | The variable that holds the API and dashboard token. Treadle looks in the environment first, then in `.env`. | A variable name |
| `server.allowed_hosts` | More host names that the dashboard and API accept in the `Host` header, such as the public name of a reverse proxy. A wildcard must have the form `*.example.com`. | A list of host names |
| `server.secure_cookies` | Marks cookies `Secure`. Turn it on behind an HTTPS proxy. | `true` or `false` |
| `server.session_ttl` | How long a dashboard sign-in lasts. | 5m–30d |
| `runs.max_concurrent` | The most runs that are active at once, across workflows. | 1–64 |
| `runs.kill_grace` | How long a stopped step's processes get between `SIGTERM` and `SIGKILL`. | 0s–60s |
| `runs.shutdown_grace` | How long cleanup steps may run when the daemon shuts down. | 1s–10m |
| `retention.max_age` | Finished runs older than this are deleted. | 1h–3650d |
| `retention.max_runs_per_workflow` | Each workflow keeps at most this many runs. | 1–100,000 |
| `retention.interval` | How often the daemon prunes. | 1m–7d |
| `limits.step_log_bytes` | The log text that one step attempt keeps. | 64 KiB–64 MiB |
| `limits.run_log_bytes` | The log text that one run keeps. | 1 MiB–1 GiB |
| `limits.line_bytes` | The longest log line. | 1 KiB–1 MiB |
| `limits.output_file_bytes` | The largest `$TREADLE_OUTPUT` file. | 1 KiB–16 MiB |
| `limits.outputs_bytes` | The largest set of outputs of one step. | 1 KiB–16 MiB |
| `limits.stdout_capture_bytes` | The most standard output that `capture_stdout` keeps. | 1 KiB–1 MiB |
| `limits.webhook_body_bytes` | The largest webhook body, and the most that a hook's `max_body` can allow. | 1 KiB–10 MiB |
| `limits.api_body_bytes` | The largest API or form request body. | 4 KiB–1 MiB |
| `limits.watch_max_entries` | The most entries that one watched folder may hold. | 1–500,000 |
| `security.file_roots` | The directories that `file` steps may change. | At least one path |
| `logging.level` | The daemon's log level. | `debug`, `info`, `warning`, or `error` |

Relative paths in the file resolve against the file's directory, and `~` expands. An unknown
key is an error with a suggestion, and every problem in the file is reported at once:

```text
error: invalid configuration in /home/ada/project/treadle.toml
treadle.toml: error[E101]: unknown key 'hots' in [server] (at server.hots)
   = help: did you mean 'host'?
treadle.toml: error[E104]: must be between 0 and 65535 (at server.port)
```

### Precedence

From highest to lowest:

1. Command-line options, such as `--port` and `--state-dir`. Their paths resolve against the
   current directory.
2. `--config` or `TREADLE_CONFIG`, which only choose the file.
3. The `treadle.toml` that Treadle finds by searching upward from the current directory.
4. The defaults.

## State directory

Treadle keeps its state in `.treadle/` by default. `treadle init` adds it to `.gitignore`.

| Path | Contents |
|---|---|
| `treadle.db` | The SQLite database: run history, logs, and schedule state. Mode 0600. While Treadle runs, `treadle.db-wal` and `treadle.db-shm` sit next to it. |
| `treadle.lock` | The lock that lets one process at a time run workflows. |
| `holder.json` | Which process holds the lock: its PID, kind, start time, version, host, and port. Present only while a process holds the lock. |
| `secret.key` | 32 random bytes that sign dashboard sessions and form tokens. Mode 0600. |
| `backups/` | Copies of the database from before each schema upgrade. Treadle keeps the newest 3. |
| `tmp/` | Scratch directories for running runs. Treadle deletes each one when its run ends. |

The directory has mode 0700. Deleting `secret.key` signs everyone out of the dashboard.
Deleting `treadle.db` erases the history. To back up the database, see
[Backups](operations.md#backups).
