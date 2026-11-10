# Triggers

A workflow runs when you start it by hand, or when one of its triggers fires. Treadle has
three kinds of triggers: schedules, file watches, and webhooks.

```yaml
triggers:
  schedule: "30 2 * * *"           # one schedule, written as a shorthand
  watch:
    paths: [~/Downloads]
    include: ["*.pdf"]
  webhook:
    - name: blog-push
      secret_env: BLOG_HOOK_SECRET
```

Each key takes one item or a list of items, and a workflow can have at most 20 trigger items
in total. Two identical triggers in one workflow are E304.

## How triggers start runs

- **Triggers fire only while `treadle serve` runs.** `treadle run` in a terminal starts one
  manual run and starts no triggers.
- Each firing submits a run, which goes through the workflow's
  [concurrency policy](workflow-syntax.md#concurrency): it's queued, or it's created as
  `skipped`.
- The trigger data is available to the run as the `trigger` context in expressions, and as
  a JSON file at `$TREADLE_EVENT_PATH` in each step.
- A workflow with a trigger can't have required inputs (E301), because triggers can't
  supply them. Inputs with defaults work.

Treadle identifies each schedule and watch by a key that it derives from the trigger's
settings, and each webhook by its name. When you reorder triggers, they keep their state,
such as the last schedule slot that fired or a watch's view of its folder. When you change a
schedule's or a watch's settings, the trigger starts fresh.

## Manual runs

Every workflow accepts manual runs, so there is no `manual` trigger. You can start one from
the CLI (`treadle run`), from the dashboard's **Run now** form, or through the API
(`POST /api/v1/workflows/{workflow_id}/runs`). A manual run's trigger data is:

```json
{"kind": "manual", "origin": "cli"}
```

The origin is `cli`, `api`, or `dashboard`.

## Schedules

A schedule fires on a cron expression in a time zone.

| Key | Type | Default | Rules |
|---|---|---|---|
| `cron` | string | required | See [Cron syntax](#cron-syntax). An expression that never fires is E302. |
| `timezone` | IANA zone name | the `timezone` in `treadle.toml` | A zone spelled exactly as in the time zone database, capitalization included, such as `Europe/London` or `UTC`. Any other name is E303, and the hint suggests the right spelling, such as `did you mean 'UTC'?`. |
| `catch_up` | `none`, `latest`, or `all` | `latest` | See [Missed schedules](#missed-schedules). |

A string sets `cron`, so `schedule: "30 2 * * *"` is a schedule with the defaults.

To see when an expression fires, with notes about daylight saving time, run `treadle cron`:

```console
$ treadle cron "30 2 * * *" --tz America/New_York --after 2026-03-07 -n 3
LOCAL                 UTC                   NOTE
2026-03-07 02:30 EST  2026-03-07T07:30:00Z
2026-03-08 03:00 EDT  2026-03-08T07:00:00Z  02:30 doesn't exist (DST gap); runs at 03:00
2026-03-09 02:30 EDT  2026-03-09T06:30:00Z
```

### Cron syntax

An expression has five fields, separated by spaces: minute, hour, day of month, month, and
day of week.

| Field | Values | Names |
|---|---|---|
| Minute | 0–59 | |
| Hour | 0–23 | |
| Day of month | 1–31 | |
| Month | 1–12 | `JAN`–`DEC` |
| Day of week | 0–7, where 0 and 7 are Sunday | `SUN`–`SAT` |

Names are case-insensitive. Each field is `*` or a comma-separated list of items, and each
item is a value or a range, optionally with a step:

| Item | Meaning |
|---|---|
| `*` | Every value |
| `5` | One value |
| `1-5`, `MON-FRI` | A range. It must be ascending, so `FRI-MON` is an error. |
| `*/15` | Every 15th value, starting with the first |
| `10/15` | Every 15th value from 10 to the end of the range: `10/15` means `10-59/15` for minutes |
| `1-5,10` | A list |

These macros are also accepted: `@yearly`, `@annually`, `@monthly`, `@weekly`, `@daily`,
`@midnight`, and `@hourly`.

When both day fields are restricted, that is, neither starts with `*`, a day matches when
**either** field matches, as in Vixie cron. `0 9 1 * MON` runs at 09:00 on the first of each
month and on every Monday.

Treadle rejects these features with a specific message: a sixth field for seconds, `?`, `L`,
`W`, `#`, and `@reboot`. Instead of `@reboot`, use `catch_up`. An invalid expression shows
where the problem is:

```console
$ treadle cron "0 25 * * *"
error: hour 25 isn't between 0 and 23
  0 25 * * *
    ^
```

### Time zones and daylight saving time

Treadle matches a cron expression against the wall clock in the schedule's time zone. On the
days when clocks change, the result depends on the kind of schedule:

- A schedule is **interval-like** when its hour field covers all 24 hours, as in `*`, `*/1`,
  or `0-23`. `@hourly` is interval-like.
- Every other schedule is **fixed-time**, including every other macro.

| When the time | Fixed-time schedule | Interval-like schedule |
|---|---|---|
| exists once | Runs at that time. | Runs at that time. |
| doesn't exist, because clocks move forward | Runs once, at the moment the clocks change. | Doesn't run. Real time keeps its spacing. |
| happens twice, because clocks move back | Runs the first time only. | Runs both times. |

So a nightly backup at 02:30 neither runs twice nor gets skipped on the days when clocks
change, and an every-15-minutes job keeps running every 15 minutes of real time.

`treadle cron` shows these cases in its NOTE column:

```console
$ treadle cron "30 1 * * *" --tz Europe/London --after 2026-10-24T12:00 -n 2
LOCAL                 UTC                   NOTE
2026-10-25 01:30 BST  2026-10-25T00:30:00Z  01:30 happens twice (DST overlap); runs only the first time
2026-10-26 01:30 GMT  2026-10-26T01:30:00Z
```

### Missed schedules

A slot is **late** when it's more than 60 seconds in the past, for example because the
machine was asleep or `treadle serve` wasn't running. `catch_up` decides what happens to late
slots:

| `catch_up` | Effect |
|---|---|
| `none` | Late slots don't run. |
| `latest` | Only the newest missed slot runs, once. A laptop that slept through the night still gets one backup, not a burst. |
| `all` | Every missed slot runs, oldest first, up to the newest 100. |

- Treadle logs a warning when it drops missed slots, such as "skipped 3 missed slots".
- A run for a late slot has `trigger.catch_up` set to `true`, and `treadle show` marks it
  "(catch-up)".
- Catch-up runs go through the concurrency policy like any other run.
- A new or changed schedule starts counting when Treadle first sees it, so it never catches
  up on slots from before that moment.
- A schedule that comes back while `treadle serve` runs, after you removed it or edited it
  away, also starts counting again, so it doesn't catch up on the slots that it missed
  meanwhile. That happens when you revert an edit, or move a file away and back.

Treadle wakes at least every 30 seconds to read the wall clock again. After a laptop wakes
up, or after the clock jumps, a due slot fires within about 30 seconds.

Treadle creates **at most one run per schedule slot**. It records the slot in the same
database transaction that creates the run, so a crash or a restart can't fire a slot twice.
If the run was interrupted, Treadle doesn't run that slot again: the run shows
`interrupted`, and you can re-run it.

### Schedule trigger data

```json
{"kind": "schedule", "cron": "30 2 * * *", "timezone": "Europe/London",
 "scheduled_for": "2026-03-29T03:00:00+01:00",
 "scheduled_for_utc": "2026-03-29T02:00:00.000Z",
 "fired_at": "2026-03-29T02:00:00.512Z", "late_by_s": 0, "catch_up": false}
```

`scheduled_for` is the slot in the schedule's time zone, and `late_by_s` counts the whole
seconds between the slot and the moment it fired.

## File watches

A watch trigger polls folders and starts a run when files appear, change, or disappear.

| Key | Type | Default | Rules |
|---|---|---|---|
| `paths` | string or list of strings | required | 1–20 folders. `~` expands, and a relative path resolves against the project directory. A missing path is W301. A path that contains the state directory or the workflows directory is W302. |
| `include` | list of glob patterns | `["*"]` | See [Which files count](#which-files-count). A bad pattern is E306. |
| `exclude` | list of glob patterns | `[".*", "*~", "*.tmp", "*.swp", "*.part", "*.crdownload"]` | Setting it replaces the whole default list. |
| `recursive` | boolean | `false` | Whether to watch subfolders. |
| `events` | list of `created`, `modified`, and `deleted` | all three | At least one. |
| `debounce` | duration | `2s` | 0–10m. How long the folder must stay quiet before a batch of changes fires. A scan when it ends confirms the quiet. |
| `poll_interval` | duration | `2s` | 500ms–1h. How often Treadle scans the folders. |
| `initial_scan` | boolean | `false` | When `true`, the first scan reports every existing file as `created`. |

A string sets `paths`, so `watch: ~/Downloads` watches one folder with the defaults.

`recursive` is `false` by default, so that a workflow that sorts files into subfolders of the
folder it watches can't trigger itself.

### Which files count

- `*` matches any characters except `/`, `?` matches one character except `/`, and `[...]`
  matches one character from a set. `[!...]` matches one character outside the set. `**` as
  a whole path segment matches zero or more folders.
- A pattern without `/` matches the file name at any depth. A pattern with `/` matches the
  path relative to the watched folder.
- **Matching is case-sensitive**, so `*.pdf` doesn't match `REPORT.PDF`. List both
  spellings when you need them.
- A file counts when it matches an `include` pattern and no `exclude` pattern. Treadle
  doesn't enter a subfolder whose name or relative path matches an `exclude` pattern.
- Only regular files count. Treadle ignores symbolic links and never follows them.

### Changes and batches

On each poll, Treadle compares the folder with the previous scan. A new file is `created`, a
missing file is `deleted`, and a file whose modification time, size, or inode changed is
`modified`. A rename shows up as `deleted` plus `created`.

Changes collect into a batch. The batch fires when no new change has arrived for
`debounce`, or at the latest 60 seconds (or `debounce`, when it's longer) after the batch's
first change, so constant activity can't hold it back forever. When `debounce` ends before
the next poll, Treadle scans at that moment, and fires the batch only when that scan finds
no new change. A file that a program is still writing holds the batch back, instead of
starting a run on every poll. These extra scans happen only while a batch waits, at most
once per `debounce`.

Within a batch, the events for one path combine:

| First event | Later event | Result |
|---|---|---|
| created | modified | created |
| created | deleted | nothing |
| deleted | created | modified |
| modified | modified | modified |
| modified | deleted | deleted |

Then Treadle drops the events that aren't in `events`. If any changes remain, the batch
starts one run.

### Safety rules

- **The first scan is a silent baseline**, unless `initial_scan` is `true`. Files that
  already exist don't start a run.
- **A missing folder never looks like mass deletion.** When a watched folder disappears or
  can't be read, the trigger becomes degraded and reports no `deleted` events. When the
  folder comes back, Treadle takes a new silent baseline. The dashboard, the API, and the
  daemon's log show a degraded trigger. On macOS, the folder of an unmounted drive under
  `/Volumes` disappears, so its trigger becomes degraded.
- **A folder that becomes a different directory gets a new silent baseline.** Treadle logs a
  warning, reports no events, and the trigger stays healthy. This happens when an unmounted
  drive leaves its empty mount point behind, when the drive is mounted again, when a
  symbolic link that you watch points somewhere else, and when the folder is deleted and
  created again. An empty mount point looks like an empty folder, not like a degraded
  trigger.
- **A subfolder that can't be read keeps its files' last known state.** With
  `recursive: true`, a subfolder that Treadle can't list, because of a permission or an I/O
  error, reports no events until Treadle can read it again. Then Treadle reports the
  changes made meanwhile, except that a subfolder that the baseline holds no files for joins
  the baseline silently. A deleted subfolder still reports its files as `deleted`. Treadle
  logs one warning per trigger about unreadable subfolders.
- A folder with more than `[limits] watch_max_entries` (default 10,000) entries also makes
  the trigger degraded, and the next poll tries again.
- Treadle keeps the baseline in memory only. It doesn't report changes made while
  `treadle serve` wasn't running. Use `initial_scan: true`, and a workflow that's safe to run
  twice on the same file, to cover that case.
- Reloading the workflows keeps a watch's baseline when its settings didn't change.

### Watch trigger data

A batch reports at most 1,000 changes. `total` counts every change, and `truncated` is
`true` when the list is cut.

```json
{"kind": "watch",
 "paths": ["/Users/ada/Downloads/report.pdf"],
 "changes": [{"event": "created", "path": "/Users/ada/Downloads/report.pdf",
              "root": "/Users/ada/Downloads", "rel": "report.pdf"}],
 "total": 1, "truncated": false}
```

File names are untrusted data: anyone who can write to the folder chooses them. Read them
from the file at `$TREADLE_EVENT_PATH`, never from a template inside a script:

```yaml
# treadle-example
name: Count new reports
triggers:
  watch:
    paths: ~/Reports
    include: ["*.csv"]
    events: [created]
steps:
  - id: count
    run: |
      jq -r '.changes[].path' "$TREADLE_EVENT_PATH" | while IFS= read -r file; do
        printf '%s: %s lines\n' "$file" "$(wc -l < "$file")"
      done
```

For a complete example in Python, see
[`examples/sort-downloads.yml`](../examples/sort-downloads.yml).

## Webhooks

A webhook trigger starts a run when a signed HTTP request arrives at
`POST /hooks/<name>` on the daemon.

| Key | Type | Default | Rules |
|---|---|---|---|
| `name` | string | required | The workflow ID pattern. It's the last part of the URL. Unique across all workflows: a name that another workflow already uses is E305. A workflow with E305 doesn't load, so it claims none of its names, and other workflows can still use its other names. |
| `secret_env` | environment variable name | required | The variable that holds the secret. Treadle looks in the environment first, then in `.env`. |
| `signature` | `hmac-sha256` or `token` | `hmac-sha256` | How the sender proves it knows the secret. |
| `signature_header` | header name | `X-Hub-Signature-256` for `hmac-sha256`, `X-Gitlab-Token` for `token` | Case-insensitive. |
| `timestamp_header` | header name or `null` | `null` | `hmac-sha256` only. See [Timestamped signatures](#timestamped-signatures). |
| `tolerance` | duration | `5m` | 10s–1h. Applies with `timestamp_header`. |
| `dedupe_window` | duration | `24h` | 0–7d. `0` turns off replay protection. |
| `max_body` | size | `[limits] webhook_body_bytes` (1 MiB) | From 1 KiB up to `[limits] webhook_body_bytes`. |
| `rate_limit` | integer | `60` | 0–6,000 deliveries per minute. `0` turns off the limit. |

**Every webhook needs a secret.** While the variable isn't set, or holds fewer than 16
characters, the hook is disabled and answers 503. `treadle validate` warns about this with
W311 and W312. Treadle reads the secret for each request, so after you change `.env`, the
new secret applies without a restart.

To make a secret, run:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Set up a sender

| Sender | `signature` | `signature_header` | In the sender's webhook settings |
|---|---|---|---|
| GitHub | `hmac-sha256` (default) | `X-Hub-Signature-256` (default) | Set the content type to `application/json`, and set the secret. |
| Gitea | `hmac-sha256` | `X-Gitea-Signature` | Set the content type to `application/json`, and set the secret. |
| Forgejo | `hmac-sha256` | `X-Forgejo-Signature` | Set the content type to `application/json`, and set the secret. |
| GitLab | `token` | `X-Gitlab-Token` (default for `token`) | Set the secret token. |

In each case, the URL is `https://<your host>/hooks/<name>`. For `hmac-sha256`, the header
may hold `sha256=<hex>`, as GitHub sends it, or the bare hex digest, as Gitea and Forgejo
send it. For how to reach the daemon from the internet, see
[TLS and remote access](operations.md#tls-and-remote-access).

### How Treadle checks a delivery

Treadle checks each delivery in this order and answers with the first failure:

| Check | Failure response |
|---|---|
| The hook exists. | 404 `not_found` |
| The daemon isn't shutting down. | 503 `shutting_down`, with `Retry-After: 30` |
| The secret is set and has at least 16 characters. | 503 `hook_disabled` |
| The body is at most `max_body`, checked from `Content-Length` and again while reading. | 413 `payload_too_large` |
| The signature, and the timestamp if any, verify. | 401 `unauthorized`, with the same body for every cause |
| The hook's rate limit allows the delivery. | 429 `rate_limited`, with `Retry-After` |
| A JSON body parses, with nesting at most 64 levels deep. | 400 `bad_request` |

Treadle compares signatures in constant time. It applies the rate limit only after the
signature verifies, so unsigned requests can't use up a real sender's allowance.

One 413 differs from the others. When a request declares a `Content-Length` larger than
`[limits] webhook_body_bytes`, Starlette, the web framework, refuses it before any of these
checks run. That answer is plain text, `Content Too Large`, without the JSON error shape
and without the security headers. A body that grows past that limit while it streams, and a
body larger than a hook's own `max_body`, get the JSON `payload_too_large` error.

When every check passes, Treadle submits the run and answers:

| Response | Meaning |
|---|---|
| `202 {"status": "queued", "run_id": 7}` | The run is queued. |
| `202 {"status": "skipped", "run_id": 9, "reason": "already_active"}` | The concurrency policy skipped the run. |
| `200 {"status": "duplicate", "run_id": 7}` | Treadle saw this delivery before. See [Replay protection](#replay-protection). |
| `503 shutting_down` or `503 workflow_invalid` | No run was created. |

Errors use the shape `{"error": {"code": "...", "message": "...", "details": null}}`.

### Replay protection

Treadle remembers a digest of the hook name and the signed message of every delivery. A
delivery with the same digest within `dedupe_window` (24 hours by default) gets
`200 duplicate` and starts no run. Treadle records the digest in the same transaction that
creates the run, so two copies that arrive at once still start one run.

- A sender's "redeliver" button sends the same body, so within the window, Treadle answers
  `duplicate`. To run the workflow again, use `treadle run --rerun RUN` instead.
- Treadle doesn't use delivery ID headers, such as `X-GitHub-Delivery`, for this check,
  because the signature doesn't cover them.
- After the window, the same signed body can start a run again. Write webhook workflows so
  that running them twice does no harm, or use a timestamped signature.

### Rate limits

Each hook allows `rate_limit` deliveries per minute (60 by default), with bursts of up to a
sixth of that (10 by default). A delivery over the limit gets `429 rate_limited` with a
`Retry-After` header.

### Timestamped signatures

With `timestamp_header` set, the sender signs `<timestamp>.<body>`, where the timestamp is
whole Unix seconds that the sender puts in that header. Treadle rejects a timestamp more than
`tolerance` away from its own clock, and keeps duplicates for at least twice the tolerance.

### Send a test delivery

This command signs a body the way GitHub does and sends it to a local daemon. Replace the
secret with the value of the hook's `secret_env` variable:

```sh
SECRET='<the webhook secret>'
BODY='{"ref":"refs/heads/main","after":"6dcb09b5b57875f334f61aebed695e2e4193db5e"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -i -X POST http://127.0.0.1:8765/hooks/blog-push \
  -H 'Content-Type: application/json' \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  --data "$BODY"
```

For a Gitea or Forgejo hook, send the bare digest in that sender's header instead, such as
`-H "X-Forgejo-Signature: $SIG"`. Sending the same command twice shows replay protection:
the second answer is `duplicate`.

### Webhook trigger data

```json
{"kind": "webhook", "hook": "blog-push", "method": "POST",
 "headers": {"content-type": "application/json", "x-forgejo-event": "push"},
 "query": {}, "content_type": "application/json",
 "body": {"ref": "refs/heads/main", "after": "6dcb09b5b57875f334f61aebed695e2e4193db5e"},
 "delivery": "f2b1c0de-0000-4000-8000-000000000001",
 "received_at": "2026-11-09T10:15:02.120Z"}
```

- `headers` has lowercase names. It leaves out `authorization`, `cookie`,
  `proxy-authorization`, and the signature or token header.
- For `application/json` and other `+json` content types, `body` is the parsed JSON. For any
  other content type, `body` is the text.
- `delivery` is the first of `X-GitHub-Delivery`, `X-Gitea-Delivery`, `X-Forgejo-Delivery`,
  and `X-Gitlab-Event-UUID` that the request has, or `null`.

Everything in a webhook request is untrusted. Pass values to scripts through `env`, as in
[`examples/deploy-on-push.yml`](../examples/deploy-on-push.yml). For the reasons, see
[Safe patterns for untrusted data](security-model.md#safe-patterns-for-untrusted-data).

## Reloading workflows

While `treadle serve` runs, it checks the workflows directory every second and applies
changes. To make it check at once, send it `SIGHUP`.

- **A valid change** applies right away. Triggers whose settings didn't change keep their
  state.
- **An invalid change** keeps the last good version running. The dashboard and the API mark
  the workflow as stale and show its issues, and the daemon logs them.
- **A deleted file** removes the workflow and cancels its queued runs with the reason
  `workflow_removed`. A run that's already running finishes.
- **A webhook name that another workflow uses (E305)** holds a file back only until the
  other workflow drops the name. Then the file loads on its own. Moving a hook from one file
  to another works in one edit or in two, whatever the file names. Two workflows that swap
  their hook names in one edit both stay invalid until you edit one of them again.
- **When applying a change fails unexpectedly**, for example because of a bug in Treadle or
  in a trigger, the daemon logs the file name and the error, and tries once more. If that
  try fails too, the daemon waits until the file changes again. Meanwhile, the workflow
  keeps its previous version.

A run uses the version of the workflow that was current when the run was created, even if
you edit the file while the run waits in the queue. A re-run uses the current version.

The daemon also reloads `.env` when it changes, for new runs and for webhook secrets. The
API token is the exception: a change to it takes effect only after a restart, and the
daemon logs a warning. A change to `treadle.toml` takes effect only after a restart, and the
daemon logs a reminder.
