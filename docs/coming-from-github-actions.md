# Coming from GitHub Actions

Treadle's workflow files look like GitHub Actions workflows on purpose: `steps`, `run`,
`with`, `env`, `if`, `${{ }}` expressions, `$TREADLE_OUTPUT`, and `::add-mask::` work much as
you expect. This page lists every place where Treadle behaves differently, and why.

## A translated workflow

This GitHub Actions workflow fetches a number every morning and sends an alert when it's
high:

```yaml
name: Nightly report
on:
  schedule:
    - cron: "0 6 * * *"
  workflow_dispatch:
jobs:
  report:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Fetch stats
        id: stats
        run: |
          curl -fsS https://api.example.net/stats > stats.json
          echo "count=$(jq .count stats.json)" >> "$GITHUB_OUTPUT"
      - name: Notify
        if: ${{ steps.stats.outputs.count > 100 }}
        run: curl -fsS -d "count is high" "${{ secrets.NTFY_URL }}"
```

In Treadle, it becomes:

```yaml
# treadle-example
name: Nightly report
triggers:
  schedule:
    cron: "0 6 * * *"
    timezone: UTC
timeout: 15m
defaults:
  timeout: 15m
steps:
  - id: stats
    name: Fetch stats
    run: |
      curl -fsS https://api.example.net/stats > "$TREADLE_RUN_TMP/stats.json"
      echo "count=$(jq .count "$TREADLE_RUN_TMP/stats.json")" >> "$TREADLE_OUTPUT"
  - id: notify
    name: Notify
    if: steps.stats.outputs.count > 100
    env:
      NTFY_URL: ${{ secrets.NTFY_URL }}
    run: curl -fsS -d "count is high" "$NTFY_URL"
```

- `on` becomes `triggers`. Manual runs need no trigger.
- The job disappears: a Treadle file is one list of steps that runs on this machine.
- The steps run in the project directory, which persists between runs, so scratch files go
  to `$TREADLE_RUN_TMP`, which Treadle deletes when the run ends.
- `$GITHUB_OUTPUT` becomes `$TREADLE_OUTPUT`.
- The secret reaches the script through `env`. See
  [Safe patterns for untrusted data](security-model.md#safe-patterns-for-untrusted-data).

When you run `treadle validate` on a file that still has GitHub Actions keys, the hints say
what to change:

```text
ported.yml:2:1: error[E101]: unknown key 'on' in workflow
   |
 2 | on:
   | ^
   = help: Treadle uses `triggers`. `on` is a YAML 1.1 boolean trap.
```

## Structure

| GitHub Actions | Treadle | Why |
|---|---|---|
| `.github/workflows/*.yml` in a repository | `*.yml` files in the project's workflows directory | Treadle runs on your machine, not for a repository. |
| The workflow `name` identifies it | The file name without its extension is the workflow ID, and `name` is for display | The file name is unique and stable, so it works in commands and URLs. |
| `jobs`, each with `runs-on` | One step graph per file, on this machine | There are no runners. To run steps in parallel, use `needs`. |
| Steps in a job run in order | Steps run in order too, unless `needs` says otherwise | A step without `needs` waits for the step before it. |
| `needs` between jobs | `needs` between steps, naming earlier steps only | Listing order is always valid, and cycles can't exist. |
| A step `id` is optional | Every step needs an `id` | The run history, the CLI, and expressions refer to steps by ID. |
| `uses: owner/action@v5` | `uses:` names a built-in step type: `shell`, `http`, `file`, `log`, or `sleep` | Treadle doesn't download or run third-party code. |
| `shell:` and `working-directory:` on a step | `with: {shell: bash, cwd: ~/src}` | They're parameters of the `shell` step type. |
| `defaults.run.shell` | `defaults.shell`, and `defaults.cwd` | |
| `timeout-minutes` | `timeout` on a step (per attempt, default `30m`) and on the workflow (default `2h`), as a duration | |
| No built-in retries | `retry`, with backoff and jitter | Home automation meets flaky networks. |
| `continue-on-error` | `continue_on_error` | Every Treadle key uses snake_case, and the kebab-case spelling gets a hint. |
| `strategy.matrix`, reusable workflows, composite actions | Not supported | Write separate workflows. |
| Artifacts, caches, service containers, environments | Not supported | Steps share files through the project directory or `$TREADLE_RUN_TMP`. |
| `concurrency: {group: ..., cancel-in-progress: true}` | `concurrency: queue`, `skip`, or `replace`, per workflow | `replace` works like `cancel-in-progress`, but the new run starts only after the old one ends. There are no groups. |

## Triggers

| GitHub Actions | Treadle |
|---|---|
| `on: workflow_dispatch` | Every workflow accepts manual runs from `treadle run`, the dashboard, and the API. |
| `on: schedule` | `schedule`, in any time zone, with defined rules for daylight saving time and a `catch_up` policy for slots that the machine missed while it was asleep or off. See [Schedules](triggers.md#schedules). |
| `on: push`, `pull_request`, and other repository events | A `webhook` trigger that your Git server calls, signed with a shared secret. See [Webhooks](triggers.md#webhooks). |
| None | `watch`, which runs a workflow when files change. |
| `inputs` of type `string`, `number`, `boolean`, `choice`, or `environment` | `string`, `number`, `integer`, `boolean`, or `choice`. A workflow with any trigger can't have required inputs, because triggers can't supply them. |

## Contexts and variables

| GitHub Actions | Treadle |
|---|---|
| `github.event` | `trigger`, such as `trigger.body` for a webhook's JSON body. |
| `github.run_id`, `github.run_attempt`, `github.workflow` | `run.id`, `run.attempt`, and `run.workflow`. |
| `needs.<job>.outputs.<name>` | `steps.<id>.outputs.<name>` |
| `steps.<id>.outcome` and `steps.<id>.conclusion` | `steps.<id>.status`: `succeeded`, `failed`, `timed_out`, `cancelled`, or `skipped`. |
| `vars.<name>` | `env`, in the workflow or the step. |
| `env` holds only the variables the workflow defines | `env` also holds the environment that Treadle started with, without `TREADLE_*` and secret variables, so `env.HOME` works. |
| `secrets.<name>` from the repository settings | `secrets.<name>` from `.env`, or from a `TREADLE_SECRET_<name>` variable. See [Secrets](expressions.md#secrets). |
| `$GITHUB_OUTPUT` | `$TREADLE_OUTPUT`, with the same `KEY=value` and `KEY<<DELIMITER` formats. |
| `$GITHUB_ENV` and `$GITHUB_PATH` | Not supported. Pass values to later steps as outputs, so the data flow shows in the file. |
| `$GITHUB_EVENT_PATH` | `$TREADLE_EVENT_PATH` |
| `$GITHUB_RUN_ID`, `$GITHUB_RUN_ATTEMPT`, `$GITHUB_WORKFLOW` | `$TREADLE_RUN_ID`, `$TREADLE_RUN_ATTEMPT`, `$TREADLE_WORKFLOW`, and more. See [Step environment](workflow-syntax.md#step-environment). |
| `::add-mask::VALUE` | The same. |
| Other workflow commands, such as `::error::` and `::group::` | Not supported. Treadle logs them as ordinary lines. |

## Expressions

Treadle's expression language follows GitHub's syntax with stricter rules. For the full
reference, see [Expressions](expressions.md).

- **String comparison is case-sensitive.** `'Main' == 'main'` is `false`, while GitHub
  ignores case. Use `lower()` when you need a comparison that ignores case.
- **Equality doesn't convert types,** except between a number and a numeric string:
  `steps.count.outputs.n == 3` works, but `'true' == true`, `true == 1`, and `null == 0` are
  all `false`. GitHub converts both sides to numbers.
- **Comparing mismatched types is an error.** `null < 3` fails the step, where GitHub would
  convert `null` to `0`.
- **Strings use single quotes only.** A double quote is an error, with the hint "use single
  quotes".
- **An `if` must be one expression.** A bare expression or a single `${{ }}` works; text
  mixed with `${{ }}` is an error.
- **Expressions work only in `env`, `with`, step `outputs`, and `if`.** GitHub allows them
  in more places, such as `timeout-minutes`. Treadle needs every other value before a run
  starts.
- **Functions:** Treadle has `contains`, `startsWith`, `endsWith`, `format`, `join`,
  `toJSON`, and `fromJSON`, and adds `lower`, `upper`, `trim`, `length`, and `default`. It
  has no `hashFiles`, and no object filters such as `labels.*.name`.
- **Function names are case-sensitive.** `startswith` is an error that suggests
  `startsWith`.

## Conditions and status functions

- An `if` without a status function means `success() && (...)`, as in GitHub Actions.
- **Status functions look only at the steps that this step depends on**, directly or through
  other steps. With the default order, that's every earlier step, as in a GitHub job. With
  explicit `needs`, a step in a parallel branch isn't affected by a failure in another
  branch.
- **A skipped step doesn't stop the steps that depend on it.** `success()` counts skipped
  steps as passing. GitHub skips a job whose needed job was skipped. To run a step only when
  an earlier step ran and succeeded, check `steps.<id>.status == 'succeeded'`.
- **`cancelled()` is true for every kind of cancellation:** a user's cancel, a replacing
  run, the run's timeout, and a daemon shutdown. Cleanup steps with `if: always()` or
  `if: cancelled()` run within `cleanup_timeout`.

## YAML

- Treadle reads YAML 1.2. `yes`, `no`, `on`, and `off` are strings, not booleans. Write
  `true` and `false`.
- Treadle rejects anchors, aliases, merge keys, and tags, so that every error points at the
  exact line. Repeat the value instead.
- A key that appears twice in one mapping is an error.

## Running

- **Steps run on your machine, as your user, without a sandbox or a clean virtual machine.**
  Files that one run leaves behind are still there for the next run.
- The default working directory is the project directory, not a checkout of a repository.
- The `bash` shell runs scripts with `bash --noprofile --norc -e -o pipefail`, as GitHub
  does. `sh` runs `sh -e`, and `python` runs `python3 -u`.
- A workflow's own `env` reaches its steps, but nothing from `.env` does, unless a step asks
  for it with `${{ secrets.NAME }}`.
- Logs go to Treadle's database, with secrets masked, and with limits on their size. Read
  them with `treadle logs`, or in the dashboard.
