# Workflow syntax

A workflow is a YAML file in your project's workflows directory. It names its inputs, the
triggers that start it, and the steps that run, in order or in parallel. This page describes
every key that a workflow file accepts.

Related pages:

- [Expressions](expressions.md) covers `${{ ... }}` templates, `if` conditions, and functions.
- [Triggers](triggers.md) covers schedules, file watches, and webhooks in depth.
- [CLI reference](cli.md) covers `treadle validate` and the other commands.

## A first workflow

`treadle init` creates this workflow as `workflows/hello.yml`:

```yaml
# treadle-example
name: Hello
description: A first workflow. Run it with `treadle run hello -i name=Ada`.
inputs:
  name:
    type: string
    default: world
steps:
  - id: greet
    env:
      NAME: ${{ inputs.name }}
    run: |
      echo "Hello, $NAME!"
      echo "greeting=Hello, $NAME!" >> "$TREADLE_OUTPUT"
  - id: report
    uses: log
    with:
      message: "${{ steps.greet.outputs.greeting }} (run ${{ run.id }})"
```

- The workflow ID is `hello`, the file name without its extension.
- `greet` is a `shell` step, because it sets `run`. It receives the input through an
  environment variable and writes an output to the file at `$TREADLE_OUTPUT`.
- `report` runs after `greet`, because a step without `needs` waits for the step before it.
  It reads the output through `steps.greet.outputs.greeting`.

To check a workflow without running it, run `treadle validate`.

## Files and workflow IDs

- Each `*.yml` or `*.yaml` file directly inside the workflows directory holds one workflow.
  Treadle doesn't look in subdirectories.
- Treadle ignores files whose names start with `.` or `_`, or end with `~`.
- **The workflow ID is the file stem.** It must match `^[a-z0-9][a-z0-9_-]{0,63}$` (E057).
  The CLI, the API, the dashboard, and the run history use the ID. The `name` key is for
  display only.
- If both `x.yml` and `x.yaml` exist, the first one in sorted order (`x.yaml`) defines the
  workflow, and the other one gets E058.
- A file must be valid UTF-8 (E056), at most 1 MiB (E055), and a YAML mapping at the top
  level (E059).

Treadle loads only regular files, so every command skips a workflow file that is a symbolic
link. To use such a workflow, copy the file into the workflows directory instead.

- `treadle validate`, without a `PATH` or with a directory, warns about each link with W060,
  `the file is a symbolic link, so Treadle skips it`. The warning counts toward `--strict`,
  but the link counts neither as valid nor as invalid. With `--json`, the link appears under
  `files` with `"workflow": null` and `"valid": false`.
- `treadle list` prints a warning for each link to standard error, such as
  `warning: workflows/linked.yml: the file is a symbolic link, so Treadle skips it`.
- By its ID, `treadle run` and `treadle graph` treat a link as missing, and exit 4 with
  ``error: workflow 'linked' doesn't exist: workflows/linked.yml is a symbolic link, which Treadle skips; copy the file into workflows instead``.
- `treadle serve` skips a link without a warning.
- When you give `treadle validate` or `treadle graph` the path of a link, it checks the file
  that the link points to.

## YAML dialect

Treadle reads YAML with the YAML 1.2 core schema, which avoids the traps of YAML 1.1:

- Only `true`, `True`, `TRUE`, `false`, `False`, and `FALSE` are booleans. `yes`, `no`, `on`,
  and `off` stay strings, so `continue_on_error: yes` is an error (E103).
- `null`, `Null`, `NULL`, `~`, and an empty value are null.
- Integers are decimal (`0755` is 755), octal with `0o`, or hexadecimal with `0x`. An
  integer with more than 4,300 decimal digits, Python's limit, counts as infinite, and so
  does a `0o` or `0x` literal whose value is that large. Every key rejects such a number:
  integer keys, such as `max_parallel`, with E103, and the others with E104, such as
  `must be a finite number`.
- Dates and values such as `1:30` stay strings.

In a double-quoted string, every escape must produce a character that a string can hold.
These escapes are E050:

- An escape that writes NUL: `\0`, `\x00`, `\u0000`, or `\U00000000`.
- A lone UTF-16 surrogate, such as `\ud800`.
- A code point above U+10FFFF, such as `\U00110000`.

Two `\u` escapes that form a surrogate pair become one character, as in JSON, so
`"\ud83d\ude80"` means the same as `"\U0001F680"`, and a workflow file
that Python's `json.dumps` wrote loads with the right characters.

Treadle rejects these YAML features, so that every error location is exact:

| Feature | Code |
|---|---|
| A key that appears twice in one mapping. The message names both locations. | E051 |
| Merge keys (`<<`) | E052 |
| Anchors (`&name`) and aliases (`*name`) | E053 |
| Tags, such as `!!str` | E054 |

## Value formats

### Durations

A duration is a non-negative number of seconds, or a string of one or more
`<number><unit>` parts with no spaces between them. The units are `d`, `h`, `m`, `s`, and
`ms`, each used at most once and in that order. Examples: `500ms`, `30s`, `5m`, `1h30m`,
`1.5h`, and `2d`. A numeric string such as `"30"` means seconds. Booleans aren't durations.

### Sizes

A size is a non-negative integer number of bytes, or a string with a unit: `B`, `KB`
(1,000 bytes), `MB`, `GB`, `KiB` (1,024 bytes), `MiB`, or `GiB`. Units are case-sensitive.
Examples: `64KiB`, `1MiB`, and `1.5KiB`.

### Shorthands

When a mapping has a primary key, you can write a scalar instead of the mapping, and the
scalar sets that key:

| Shorthand | Means |
|---|---|
| `retry: 3` | `retry: {attempts: 3}` |
| `concurrency: skip` | `concurrency: {policy: skip}` |
| `schedule: "0 2 * * *"` | `schedule: {cron: "0 2 * * *"}` |
| `watch: ~/Downloads` | `watch: {paths: [~/Downloads]}` |

## Names

| Name | Pattern |
|---|---|
| Workflow ID (the file stem), webhook `name` | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| Step `id`, input name, output name | `^[A-Za-z_][A-Za-z0-9_-]{0,63}$` |
| Environment variable, `.env` key, `secret_env` | `^[A-Za-z_][A-Za-z0-9_]*$` |

Environment variable names that start with `TREADLE_` are reserved for Treadle's own
variables. Using one in a workflow or step `env` is E405.

## Where expressions are allowed

You can use `${{ ... }}` templates in these places only:

- Workflow `env` values and step `env` values.
- `with` values, at any depth. Keys are never templates.
- Step `outputs` values.
- `if`, which takes a bare expression.

A `${{` anywhere else is E210, because Treadle needs every other value before a run starts.
For the language itself, see [Expressions](expressions.md).

## Top-level keys

| Key | Type | Default | Rules |
|---|---|---|---|
| `name` | string | the file stem | 1–100 characters. Display only. |
| `description` | string | `""` | At most 2,000 characters. |
| `inputs` | mapping of name to input | `{}` | At most 50 inputs. See [Inputs](#inputs). |
| `env` | mapping of name to value | `{}` | Values are strings, numbers, booleans, or templates. `null` is E103. |
| `triggers` | mapping | `{}` | Keys `schedule`, `watch`, and `webhook`, each with one item or a list. At most 20 items in total. See [Triggers](triggers.md). |
| `concurrency` | string or mapping | `queue` | See [Concurrency](#concurrency). |
| `defaults` | mapping | see [Defaults and retries](#defaults-and-retries) | |
| `timeout` | duration | `2h` | 1s–7d. The limit for the whole run. |
| `cleanup_timeout` | duration | `5m` | 1s–1h. How long cleanup steps may run after a cancel or a run timeout. |
| `max_parallel` | integer | `4` | 1–32. The most steps of one run that run at the same time. |
| `steps` | list of steps | required | 1–100 steps. See [Steps](#steps). |

Every workflow accepts manual runs from the CLI, the API, and the dashboard, so there is no
`manual` trigger.

Treadle rejects keys from GitHub Actions with a targeted hint (E101):

| Key | Hint |
|---|---|
| `on` | Treadle uses `triggers`. `on` is a YAML 1.1 boolean trap. |
| `jobs` | Treadle has one step graph per file. Move the steps under `steps`. |
| `runs-on` | Treadle runs on this machine. Remove the key. |
| A kebab-case spelling of a known key, such as `continue-on-error` | use `continue_on_error` |

Any other unknown key is E101, with a suggestion when a known key is close. For more
differences, see [Coming from GitHub Actions](coming-from-github-actions.md).

## Inputs

`inputs` declares the values that a run accepts. You supply them with `-i KEY=VALUE` on the
command line, in the dashboard's **Run now** form, or as JSON through the API.

| Key | Type | Default | Rules |
|---|---|---|---|
| `type` | `string`, `number`, `integer`, `boolean`, or `choice` | `string` | |
| `description` | string | `""` | The dashboard shows it next to the field. |
| `required` | boolean | `false` | `required: true` together with a `default` is E104. |
| `default` | a value of the input's type | `null` | For `choice`, one of `options`. |
| `options` | list of strings | none | Required for `choice` (E102) and not allowed for other types (E104). 1–100 unique strings. |

An optional input that has no default and isn't supplied has the value `null`. A run request
that names an input the workflow doesn't declare fails. When a declared name is close, the
error suggests it.

A workflow with any `schedule`, `watch`, or `webhook` trigger can't declare a required input
(E301), because those triggers can't supply inputs. Give the input a default instead.

Treadle converts input values by their source:

- **Text** from `-i KEY=VALUE` and the dashboard form:
  - `string` keeps the text. `-i name=` supplies an empty string.
  - `number` accepts JSON number syntax, but not `NaN` or `Infinity`.
  - `integer` accepts an optional sign and digits.
  - `boolean` accepts `true` or `false`, in any letter case.
  - `choice` must match one option exactly.
  - In the dashboard, an empty field for an optional input means "not supplied".
- **JSON** from the API:
  - `string` and `choice` need JSON strings, and `boolean` needs a JSON boolean.
  - `integer` accepts integers, and floats with no fractional part, such as `3.0`.
  - `number` accepts integers and floats.
  - `number` and `integer` reject `true` and `false`.

If you repeat `-i` with the same key, the last value wins.

## Concurrency

A workflow has at most one active run at a time. `concurrency` decides what happens to a
new run while another run of the same workflow is queued or running.

| Key | Type | Default | Rules |
|---|---|---|---|
| `policy` | `queue`, `skip`, or `replace` | `queue` | |
| `max_queued` | integer | `10` | 1–1,000. Applies to `queue`. |

- **`queue`**: the new run waits for the runs before it. When `max_queued` runs are already
  waiting, the new run is created as `skipped` with the reason `queue_full`.
- **`skip`**: when a run is already queued or running, the new run is created as `skipped`
  with the reason `already_active`.
- **`replace`**: Treadle cancels every queued run of the workflow with the reason
  `replaced`, and asks the running run, if any, to cancel. The new run starts only after the
  old run has ended, so two runs of one workflow never overlap. More runs that arrive while
  the old run is cancelling don't force it, so its cleanup steps still run.

Across workflows, `[runs] max_concurrent` in `treadle.toml` (default 4) limits how many runs
are active at once. A workflow that waits for its own active run doesn't delay the runs of
other workflows.

A foreground `treadle run`, which runs the workflow in the terminal when no daemon is
running, starts its run at once. No other run can be active while it holds the state
directory.

## Defaults and retries

`defaults` sets values for every step that doesn't set its own.

| Key | Type | Default | Rules |
|---|---|---|---|
| `defaults.timeout` | duration | `30m` | 1s–24h. Applies to each step attempt. |
| `defaults.retry` | integer or retry mapping | `1` | An integer sets `attempts`. |
| `defaults.shell` | `bash`, `sh`, `python`, or a list of strings | `bash` | See [`shell`](#shell). |
| `defaults.cwd` | string | `.` | The working directory of `shell` steps. `~` expands, and a relative path resolves against the project directory. A `~user` whose home directory isn't known fails each step that uses it, without a retry, and the error names the path. |

A retry mapping takes these keys:

| Key | Type | Default | Rules |
|---|---|---|---|
| `attempts` | integer | `1` | 1–20. Counts the first try. |
| `delay` | duration | `1s` | 0–1h. The wait after the first failed attempt. |
| `backoff` | number | `2.0` | 1.0–10.0. Each wait is this many times the one before. |
| `max_delay` | duration | `5m` | At least `delay`, at most 6h. |
| `jitter` | number | `0.0` | 0.0–1.0. The fraction of each wait that Treadle shortens at random. |

A step-level `retry` overrides `defaults.retry` key by key. Keys that the step doesn't set
come from `defaults.retry`.

After failed attempt `n`, Treadle waits:

```text
base = min(max_delay, delay * backoff ** (n - 1))
wait = base * (1 - jitter * random())
```

Jitter only shortens a wait, so no wait exceeds `max_delay`. With `delay: 30s`,
`backoff: 2`, `max_delay: 10m`, and no jitter, the waits are 30s, 1m, 2m, 4m, 8m, 10m, and
10m. Cancelling the run interrupts a wait.

Treadle retries an attempt that failed or timed out. It doesn't retry an error in the
step's configuration, such as a template that fails to render, a malformed output file, or
a `file` step path outside the file roots.

## Steps

| Key | Type | Default | Rules |
|---|---|---|---|
| `id` | string | required | The step ID pattern. Unique in the workflow (E403). |
| `name` | string | the `id` | Display only. |
| `uses` | string | `shell` when `run` is set; otherwise required | A step type: `shell`, `http`, `file`, `log`, or `sleep` (E401, with a suggestion). |
| `run` | string | none | Shorthand for `uses: shell` and `with: {run: ...}`. Only valid when `uses` is absent or `shell` (E404). `with` can't also hold `run` or `argv` (E105). |
| `with` | mapping | `{}` | The step type's parameters. See [Step types](#step-types). |
| `needs` | step ID or list of step IDs | the previous step; none for the first step | Each ID must name an earlier step (E402). `needs: []` makes a step that waits for no other step. |
| `if` | expression or boolean | `success()` | A bare expression, one `${{ }}` that Treadle unwraps, or `true` or `false`. |
| `env` | mapping of name to value | `{}` | Like workflow `env`. |
| `retry` | integer or retry mapping | `defaults.retry` | See [Defaults and retries](#defaults-and-retries). |
| `timeout` | duration | `defaults.timeout` | 1s–24h. A timeout longer than the workflow `timeout` is W105. |
| `continue_on_error` | boolean | `false` | See [Tolerated failures](#tolerated-failures). |
| `outputs` | mapping of name to template | `{}` | See [Step outputs](#step-outputs). |

### Order and dependencies

**Steps run in order by default.** A step without `needs` waits for the step before it, as
steps in a GitHub Actions job do. A ported "back up, then delete" list never runs in
parallel by surprise.

To run steps at the same time, give them explicit `needs`. Because `needs` may name only
earlier steps, the order of the list is always a valid order, and cycles can't exist. If a
step names a later step, the error hint tells you which step to move.

In this workflow, `lint` and `test` both wait only for `checkout`, so they run at the same
time. `report` waits for both:

```yaml
# treadle-example
name: Parallel checks
steps:
  - id: checkout
    run: echo "checking out"
  - id: lint
    needs: [checkout]
    run: echo "linting"
  - id: test
    needs: [checkout]
    run: echo "testing"
  - id: report
    needs: [lint, test]
    uses: log
    with:
      message: "lint: ${{ steps.lint.status }}, test: ${{ steps.test.status }}"
```

`treadle graph WORKFLOW` prints the step graph as text, Mermaid, or JSON.

### Conditions

A step runs when its `if` is true. Without `if`, a step runs only when the run isn't being
cancelled and none of the steps it depends on, directly or through other steps, failed,
timed out, or was cancelled. Status functions such as `failure()` and `always()` change
that rule. For the details, see [Status functions](expressions.md#status-functions).

A skipped step doesn't stop the steps that depend on it. To run a step only when an earlier
step ran and succeeded, check its status:

```yaml
if: steps.build.status == 'succeeded'
```

Each skipped step records one reason:

| Reason | Meaning |
|---|---|
| `condition` | The step's `if` was false. |
| `upstream_failed` | The step's condition has no status function, and a step it depends on failed or timed out. |
| `run_cancelled` | The run was cancelling, or Treadle forced the cancellation. |
| `setup_failed` | The run failed before any step started, for example because a workflow `env` value didn't render. |
| `run_interrupted` | The step hadn't started when Treadle stopped running the run: the Treadle process stopped, an internal error failed the run, or a forced shutdown recorded the run as cancelled. |

### Timeouts and cancellation

- A step's `timeout` applies to each attempt. Waits between attempts don't count.
- The workflow `timeout` covers the whole run. When it fires, the run starts cancelling with
  the reason `timeout`.
- When a run starts cancelling, for any reason, Treadle stops the running steps whose
  condition is now false. A running step with `if: always()` or `if: cancelled()` keeps
  running, and pending steps with those conditions still start. These cleanup steps have
  `cleanup_timeout` to finish, and then Treadle forces the cancellation.
- Each step's process runs in its own process group. To stop a step, Treadle sends
  `SIGTERM` to the group, and then `SIGKILL` after `[runs] kill_grace` (default 5s). When
  Treadle forces the cancellation, it sends `SIGKILL` to the group at once, whether or not
  the step is already stopping.
- A `file` step runs in a thread, which a timeout or a cancel can't interrupt. Its attempt
  waits for the file operation to end, and logs whether it finished or failed. A timed-out
  `file` attempt ends `timed_out` without a retry. When Treadle forces the cancellation, the
  attempt stops waiting, and the operation may still finish after the step ends. If the
  operation ends before the run does, the step's log still says whether it finished or
  failed. The Treadle process that runs the step waits for the operation before it exits,
  unless a third signal stops `treadle serve`.
- A process that a step starts in the background, such as `server >/dev/null 2>&1 &`, keeps
  running for the later steps of the run, and the step's log says so. When the run ends,
  whether it succeeded, failed, or was cancelled, Treadle stops such processes: `SIGTERM`,
  and then `SIGKILL` after `[runs] kill_grace`. When Treadle forces the cancellation, for
  example in a forced shutdown, it kills them at once.
- Redirect the output of a background process, as that example does. A background process
  that keeps the step's output open keeps the step running.
- A process that leaves its process group with `setsid` escapes Treadle. Under systemd,
  `KillMode=mixed` stops it with the service.

This workflow pauses a sync tool with a marker file, and removes the marker whether the
maintenance step succeeds, fails, or is cancelled:

```yaml
# treadle-example
name: Maintenance window
timeout: 30m
cleanup_timeout: 1m
steps:
  - id: pause-sync
    run: touch "$HOME/.sync-paused"
  - id: maintain
    run: echo "running maintenance"
  - id: resume-sync
    if: always()
    run: rm -f "$HOME/.sync-paused"
```

Treadle deletes the run's scratch directory, `$TREADLE_RUN_TMP`, on its own when the run
ends, so files there need no cleanup step.

### Tolerated failures

With `continue_on_error: true`, a step whose final status is `failed` or `timed_out` is
**tolerated**. A tolerated step counts as passing for `success()`, for the steps that depend
on it, and for the run's result. `steps.<id>.status` still shows `failed` or `timed_out`.

## Step types

Each step type checks its `with` parameters. An unknown key is E101, with a suggestion.
Treadle checks templated values again after it renders them. When a rendered value is
invalid, the step fails without starting an attempt.

### `shell`

| `with` key | Type | Default | Notes |
|---|---|---|---|
| `run` | string | none | A script. Use exactly one of `run` and `argv` (E105, E106). |
| `argv` | list of strings | none | Runs a program directly, without a shell. |
| `shell` | `bash`, `sh`, `python`, or a list of strings | `defaults.shell` | Only with `run` (E407). |
| `cwd` | string | `defaults.cwd` | `~` expands, and a relative path resolves against the project directory. A `~user` whose home directory isn't known fails the step without a retry, and the error names the path. |
| `capture_stdout` | boolean | `false` | When `true`, the `stdout` output holds standard output, with one trailing newline removed. When standard output is longer than `[limits] stdout_capture_bytes` characters (default 64 KiB), the output holds only the complete lines among the last that many characters, so a single line longer than the limit captures nothing. |
| `success_codes` | list of integers | `[0]` | Exit codes from 0 to 255 that count as success. |

Treadle writes a `run` script to a file with mode 0600, and runs it as follows:

| `shell` | Command |
|---|---|
| `bash` | `bash --noprofile --norc -e -o pipefail <script>` |
| `sh` | `sh -e <script>` |
| `python` | `python3 -u <script>` |
| a list, such as `[zsh, -e]` | The list, with each element equal to `{0}` replaced by the script path. Without a `{0}` element, Treadle appends the path. |

Outputs:

- `exit_code`: the exit code. A process that a signal killed reports `128 + signal`.
- Each entry in the file at `$TREADLE_OUTPUT`. See [Step outputs](#step-outputs).
- `stdout`, when `capture_stdout` is `true`.

An exit code outside `success_codes` fails the attempt with the message `exit code N`, and
Treadle may retry it.

The operating system can't take a NUL character, or text that isn't valid Unicode, in a
command line, an environment variable, or a path. When a rendered `argv` item, `env` value,
or `cwd` holds one, for example from trigger data, the step fails without a retry. The error
names the parameter, never the value.

### `http`

The `http` step sends one request per attempt with a shared client that sends
`User-Agent: treadle/0.1.0` and follows at most 5 redirects when `follow_redirects` is on.

| `with` key | Type | Default | Notes |
|---|---|---|---|
| `method` | `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, or `OPTIONS` | `GET` | Any letter case. |
| `url` | string | required | An `http` or `https` URL. |
| `headers` | mapping of strings | `{}` | |
| `query` | mapping of values or lists of values | `{}` | A list repeats the parameter, and `null` sends an empty value. |
| `json` | any JSON value | none | Sent as JSON, with `Content-Type: application/json` unless you set one. Can't be used with `body`. |
| `body` | string | none | Sent as UTF-8. |
| `timeout` | duration | `30s` | The limit for the whole exchange: the request, its redirects, and the response body. When you set it, it can't be longer than the step timeout (E407). |
| `expect` | list of status codes or patterns such as `"2xx"` | `["2xx"]` | Any other status fails the attempt. |
| `follow_redirects` | boolean | `false` | Off by default, because a redirect to another host would still carry custom headers, such as `X-Api-Key`. |
| `max_response_bytes` | size | `1MiB` | At most 10 MiB. The limit counts the decoded body. A longer response fails the step without a retry. |

Outputs, which every `http` step has:

| Output | Value |
|---|---|
| `status` | The status code. |
| `headers` | The response headers, with lowercase names. Treadle joins repeated headers with `, `. |
| `body` | The body as text, decoded with the response's charset. |
| `json` | The parsed body, when the content type is `application/json` or ends in `+json` and the body parses. Otherwise `null`. |
| `url` | The final URL. |
| `elapsed_ms` | How long the request took, in milliseconds. |

How the step handles cookies, redirects, and compressed bodies:

- **Treadle keeps no cookies.** A cookie that a response sets never reaches a later request,
  or another step. To send a cookie, set a `Cookie` header in `headers`. A redirect drops
  that header.
- **With `follow_redirects: true`, the step follows redirects itself.** It never reads the
  body of a redirect response, and a cookie that a redirect sets reaches only the later
  redirects of the same request.
- **The step decodes gzip and deflate bodies itself**, and stops decoding one byte past
  `max_response_bytes`, so a small compressed response can't use much memory. It sends
  `Accept-Encoding: gzip, deflate` unless `headers` sets that header. A body in any other
  content encoding arrives as the server sent it. A body that is compressed more than once,
  such as one with `Content-Encoding: gzip, gzip`, fails the step without a retry.
- **When the response's charset names a codec that can't decode text**, such as `base64` or
  `idna`, Treadle decodes the body as UTF-8 instead, and logs a line that says so.

A status outside `expect` fails the attempt. The error names the status, and includes the
start of the body: up to 2,048 characters, without a line that this cut would split. When
the body's first line is longer than that, the error includes none of the body. For a
redirect that the step didn't follow, the error also names the target and suggests
`follow_redirects: true`. When the step's retry policy allows more attempts, Treadle retries
timeouts, connection errors, and unexpected statuses. Each request writes one log line, such
as `GET https://example.com/ -> 200 (123 ms)`.

### `file`

The `file` step changes files inside the file roots, `[security] file_roots` in
`treadle.toml`. The default roots are the project directory and your home directory.

| `with` key | Applies to | Default | Notes |
|---|---|---|---|
| `action` | all | required | `write`, `append`, `copy`, `move`, `delete`, or `mkdir`. |
| `path` | all | required | The target. For `copy` and `move`, the destination. |
| `source` | `copy`, `move` | required for them | |
| `content` | `write`, `append` | `""` | |
| `overwrite` | `write`, `copy`, `move` | `false` | When `false`, an existing destination fails the step. |
| `parents` | all except `delete` | `true` | Creates missing parent directories. |
| `recursive` | `copy`, `delete` | `false` | Required to copy or delete a directory. |
| `missing_ok` | `delete` | `false` | When `true`, a missing target isn't an error. |
| `mode` | `write`, `mkdir` | none | An octal string, such as `"0644"`. |

A key that doesn't apply to the action is E407.

Path rules:

- `~` expands, a relative path resolves against the project directory, and symbolic links in
  the path resolve. For `delete`, Treadle doesn't follow a final symbolic link, so it deletes
  the link itself. A `~user` whose home directory isn't known fails the step without a
  retry, and the error names the path.
- The resolved path must be inside a file root. Otherwise the step fails without a retry,
  and the error names the `[security] file_roots` setting.
- The step never changes anything in the state directory. It never deletes or moves a file
  root, the project directory, the workflows directory, or a directory that contains one of
  them.
- Treadle compares paths with the file roots, the state directory, and those protected
  directories by file identity, not only by their text. On a file system that ignores letter
  case or Unicode normalization, such as APFS on macOS, another spelling of a protected path
  is still refused, and another spelling of a file root is accepted.
- A `copy` with `recursive` and `overwrite` merges into an existing directory, and follows
  the symbolic links that the destination holds. Before it copies anything, the step checks
  each such link where the source has a file or a directory: a link that leads outside the
  file roots or into the state directory fails the step. A link that stays inside the file
  roots is followed.
- `write` is atomic: Treadle writes a temporary file in the same directory, syncs it to
  disk, and renames it over the target.
- A rendered `path` or `source` that holds a NUL character or text that isn't valid
  Unicode fails the step without a retry, and so does `content` that isn't valid Unicode.
  The error names the parameter, never the value.

A step timeout or a cancel doesn't interrupt a file operation. See
[Timeouts and cancellation](#timeouts-and-cancellation).

Outputs: `path` (the resolved target), `bytes` (the bytes written by `write` and `append`,
otherwise 0), and `changed` (`false` when `delete` found nothing to delete or `mkdir` found
an existing directory).

### `log`

`log` writes one log line, `[level] message`. It has no outputs.

| `with` key | Type | Default |
|---|---|---|
| `message` | string | required |
| `level` | `info`, `warning`, or `error` | `info` |

### `sleep`

`sleep` waits, and cancelling the run interrupts it. It has no outputs.

| `with` key | Type | Default |
|---|---|---|
| `duration` | duration, 0–24h | required |

## Step environment

A step's process gets these environment variables, with later layers winning:

1. The environment that Treadle started with, without any `TREADLE_*` variable, the API
   token variable (`[server] token_env`), or any webhook `secret_env` variable.
2. The rendered workflow `env`.
3. The rendered step `env`.
4. Treadle's own variables, which you can't override:

| Variable | Value |
|---|---|
| `TREADLE_RUN_ID` | The run ID. |
| `TREADLE_RUN_ATTEMPT` | The run's attempt number: 1, or one more than the run it re-runs. |
| `TREADLE_WORKFLOW` | The workflow ID. |
| `TREADLE_STEP_ID` | The step ID. |
| `TREADLE_STEP_ATTEMPT` | The step's attempt number, starting at 1. |
| `TREADLE_TRIGGER` | `manual`, `schedule`, `watch`, or `webhook`. |
| `TREADLE_OUTPUT` | The file for this attempt's outputs. |
| `TREADLE_RUN_TMP` | A scratch directory that the run's steps share. Treadle deletes it when the run ends. |
| `TREADLE_EVENT_PATH` | A JSON file with the trigger data (mode 0600). |
| `TREADLE_PROJECT_DIR` | The project directory. |

Treadle never exports values from `.env` on its own. A step receives a secret only when it
asks for one with `${{ secrets.NAME }}`, usually in its `env`.

## Step outputs

A `shell` step sets outputs by writing to the file at `$TREADLE_OUTPUT`. The file takes two
forms of entry, and Treadle ignores blank lines:

```bash
echo "version=1.4.2" >> "$TREADLE_OUTPUT"
{
  echo "notes<<EOF"
  echo "line one"
  echo "line two"
  echo "EOF"
} >> "$TREADLE_OUTPUT"
```

- `KEY=value` sets a one-line value.
- `KEY<<DELIMITER` starts a value that runs until a line that equals `DELIMITER`.
- Keys use the output name pattern. `exit_code` and `stdout` are reserved. The file can hold
  at most `[limits] output_file_bytes` (default 1 MiB).
- A malformed file fails the step without a retry, unless the script also exited with a
  failing code. In that case Treadle ignores the file and logs why.

Values from the output file are strings. Later steps read outputs as
`steps.<id>.outputs.<name>`, and only from steps they depend on.

The step key `outputs` declares more outputs with templates. Treadle evaluates them after
the final attempt succeeds, with `result` holding the step's own outputs, and merges them
over those outputs. A declared output can have any JSON type. Its name can't reuse one of
the step type's own output names (E406). One step's outputs can hold at most
`[limits] outputs_bytes` (default 1 MiB) in total.

```yaml
# treadle-example
name: Release notes
steps:
  - id: version
    with:
      capture_stdout: true
    run: |
      echo '{"version": "1.4.2", "channel": "stable"}'
    outputs:
      number: ${{ fromJSON(result.stdout).version }}
  - id: announce
    uses: log
    with:
      message: "Releasing ${{ steps.version.outputs.number }}"
```

### Masking values that a step creates

When a `shell` step prints a line `::add-mask::VALUE` to standard output, Treadle masks
`VALUE` as `***` for the rest of the run, and logs the line itself as `::add-mask::***`.
Use it for tokens that a step creates at run time, and print the `::add-mask::` line before
the value. For how masking works, see [Masking](expressions.md#masking).

- Treadle reads a step's output in chunks, and registers every `::add-mask::` value in a
  chunk before it writes any of the chunk's lines. So Treadle masks a value in its whole
  chunk, even in a line that came before its `::add-mask::` line.
- A run masks at most 1,000 values from `::add-mask::` lines. A step that adds more fails
  without a retry: Treadle drops the rest of its output and its outputs, and logs a notice.

## Logs

Treadle stores each line that a step prints, with its stream (`stdout`, `stderr`, or
`system` for Treadle's own lines). These limits in `treadle.toml` bound the history:

| Limit | Default | Effect |
|---|---|---|
| `[limits] step_log_bytes` | 1 MiB | The most log text one attempt stores. Treadle drops the rest and says so in the log. |
| `[limits] run_log_bytes` | 16 MiB | The most log text one run stores. |
| `[limits] line_bytes` | 16 KiB | A longer line is cut and ends with ` [line truncated]`. |

Treadle's own lines don't count toward the limits. A step keeps running after its output
reaches a limit.

## Validation and diagnostics

`treadle validate` loads each workflow and reports every problem with a stable code, a
location, and a hint:

```text
workflows/broken.yml:4:9: error[E204]: unknown function 'startswith'
   |
 4 |     if: startswith(trigger.body.ref, 'refs/tags/')
   |         ^
   = help: did you mean 'startsWith'? Function names are case-sensitive.
```

Treadle reports at most 50 issues per file. Errors make a workflow invalid, so it can't run.
Warnings don't, but `treadle validate --strict` fails on them too, which suits a CI check.

The location of a problem inside a string is exact for plain scalars, single-quoted scalars
(up to the first `''` escape), and literal block scalars (`|`). For double-quoted and folded
(`>`) scalars, the location is the start of the value.

A problem with a key itself points at the key, not at its value: an unknown key (E101),
keys that can't be used together (E105), and a key name that doesn't match its pattern,
such as an `env`, input, or output name (E104, `invalid key ...`).

Treadle parses every template and `if` when it loads the file, and checks that:

- Every expression parses (E201).
- Every context name is known (E202).
- `steps.<id>` names a step (E208) that this step depends on, directly or through other
  steps (E203).
- A step field is `outputs` or `status` (E209).
- `inputs.<name>` is declared (E207).
- Function names (E204) and argument counts (E206) are valid.
- Status functions appear only in `if` (E205).
- `result` appears only in step `outputs` (E211), and `steps` doesn't appear in workflow
  `env` (E212).
- For a step type with fixed outputs, `steps.<id>.outputs.<name>` names one of its outputs
  or a declared output (W106).

### Diagnostic codes

| Code | Meaning |
|---|---|
| E050 | YAML syntax error |
| E051 | Duplicate key |
| E052 | Merge keys (`<<`) aren't supported |
| E053 | Anchors and aliases aren't supported |
| E054 | Tags aren't supported |
| E055 | The file is larger than 1 MiB |
| E056 | The file isn't valid UTF-8 |
| E057 | The file name isn't a valid workflow ID |
| E058 | Duplicate workflow ID (`x.yml` and `x.yaml`) |
| E059 | The top level isn't a mapping |
| E101 | Unknown key, with a suggestion or a targeted hint |
| E102 | Missing required key |
| E103 | Wrong type |
| E104 | Invalid value: a pattern, choice, range, or combination |
| E105 | Keys that can't be used together |
| E106 | One of several keys is required |
| E201 | Expression syntax error |
| E202 | Unknown context |
| E203 | Reference to a step that this step doesn't depend on |
| E204 | Unknown function |
| E205 | Status function outside `if` |
| E206 | Wrong number of arguments |
| E207 | Undeclared input |
| E208 | Unknown step ID in an expression |
| E209 | Unknown step field (use `outputs` or `status`) |
| E210 | Expression in a field that doesn't accept expressions |
| E211 | `result` outside step `outputs` |
| E212 | `steps` in workflow `env` |
| E301 | Required input in a workflow with schedule, watch, or webhook triggers |
| E302 | Invalid cron expression, including one that never fires |
| E303 | Unknown time zone |
| E304 | Duplicate trigger |
| E305 | Webhook name that another workflow already uses |
| E306 | Invalid glob pattern |
| E401 | Unknown step type |
| E402 | `needs` names an unknown or later step |
| E403 | Duplicate step ID |
| E404 | `run` with a step type other than `shell` |
| E405 | Reserved environment variable name (`TREADLE_` prefix) |
| E406 | A declared output reuses one of the step type's output names |
| E407 | Invalid step parameters, such as a key that doesn't apply, or an `http` timeout longer than the step timeout |
| W060 | A workflow file that is a symbolic link, which Treadle skips |
| W101 | Untrusted trigger data or HTTP response data in a `shell` `run` script |
| W102 | An input or step output in a `shell` `run` script |
| W103 | Untrusted data in a path or URL: `cwd`, `file` paths, or the `http` `url` |
| W104 | A secret in `shell` `argv`, where process listings can show it |
| W105 | A step timeout that is longer than the workflow timeout |
| W106 | An unknown output name for a step type with fixed outputs |
| W201 | A secret that isn't defined |
| W202 | A secret shorter than 4 characters, which masking ignores |
| W301 | A watch path that doesn't exist |
| W302 | A watch path that contains the state directory or the workflows directory |
| W311 | A webhook secret variable that isn't set, which disables the hook |
| W312 | A webhook secret shorter than 16 characters, which disables the hook |

W201, W202, W301, W302, W311, and W312 depend on the machine: its files and its `.env`.
`treadle validate` runs these checks only inside a project, that is, when it finds a
`treadle.toml`. For W101 through W104 and the safe patterns that avoid them, see the
[security model](security-model.md#safe-patterns-for-untrusted-data).
