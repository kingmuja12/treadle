# Treadle

[![CI](https://github.com/kingmuja12/treadle/actions/workflows/ci.yml/badge.svg)](https://github.com/kingmuja12/treadle/actions/workflows/ci.yml)

Treadle is a self-hosted workflow automation engine for one machine: a laptop, a home
server, or a Raspberry Pi. You describe automations as YAML files, and Treadle runs them on a
schedule, when files change, when a webhook arrives, or when you ask. It keeps every run, step,
and log line in a searchable history, so you can find out why last night's backup failed from
the command line.

```yaml
name: Nightly backup
triggers:
  schedule: "30 2 * * *"
steps:
  - id: archive
    retry: {attempts: 3, delay: 30s}
    run: rsync -a --delete "$HOME/Documents/" /Volumes/Backup/documents/
  - id: alert
    if: failure()
    uses: http
    with:
      method: POST
      url: ${{ secrets.NTFY_URL }}
      body: "Backup failed in run ${{ run.id }}"
```

Treadle is at version 0.1. Work on it started in November 2026, and it hasn't had a release
yet. Expect rough edges, and changes that break compatibility before 1.0.

## Who Treadle is for

Treadle is for developers who automate their own machines, and who want:

- More than cron offers: retries with backoff, timeouts, steps that depend on each other, an
  alert when something fails, and a history to look back at.
- Workflow files that read like GitHub Actions, without a CI service, runners, or
  containers.
- One process and one SQLite file, instead of a server stack, a message queue, or a cloud
  account.

Treadle isn't a CI system or a distributed job scheduler. It runs every step locally, as the
user who started it, without a sandbox.

## Features

- **Four ways to start a run:** cron schedules with time zones and well-defined behavior on
  daylight saving time changes, file watches with glob filters and debouncing, signed
  webhooks from GitHub, Gitea, Forgejo, or GitLab, and manual runs from the CLI, the
  dashboard, or the API.
- **Five step types:** `shell` (bash, sh, Python, or a program without a shell), `http`,
  `file`, `log`, and `sleep`.
- **Control over failure:** retries with exponential backoff and jitter, timeouts per step
  and per run, `continue_on_error`, and cleanup steps that run after a failure, a cancel, or
  a timeout.
- **Expressions:** `${{ }}` templates with typed inputs, secrets, trigger data, step outputs,
  and status functions such as `failure()`.
- **Concurrency policies per workflow:** queue new runs, skip them while a run is active, or
  replace the active run.
- **Diagnostics:** `treadle validate` reports each problem with a stable code, its file,
  line, and column, and a hint, and warns when untrusted data could reach a shell script.
- **Run history:** `treadle runs`, `show`, and `logs`, with search, filters, and re-runs. A
  failed run's summary leads with the failure.
- **A dashboard and a JSON API** for workflows, runs, and live logs, with **Run now**,
  **Cancel**, and **Re-run**.
- **Safe defaults:** the server listens on loopback only, every webhook needs a secret,
  secrets are masked in logs and stored data, `file` steps stay inside configured folders,
  and every log, body, and queue has a size limit.
- **Reliability:** Treadle records each state change in SQLite before it acts on it, creates
  at most one run per schedule slot, marks the runs that a crash interrupted, shuts down
  gracefully, and prunes old runs on its own.

## Platform support

Treadle runs on **macOS and Linux** with Python 3.11 or later. **Windows isn't supported in
v0.1**, because Treadle relies on `fcntl` file locks, POSIX process groups, and POSIX
signals. Treadle needs SQLite 3.31 or later, which Python's `sqlite3` module usually
provides. The CI workflow tests Python 3.11 through 3.15 on Ubuntu and macOS.

## Install

Treadle isn't on PyPI yet. Install it from GitHub with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/kingmuja12/treadle
```

Or with [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/kingmuja12/treadle
```

Check the installation:

```sh
treadle version
```

To work on Treadle itself, set it up from source with uv:

```sh
git clone https://github.com/kingmuja12/treadle.git
cd treadle
uv sync
uv run treadle version
```

[CONTRIBUTING.md](CONTRIBUTING.md) explains how to run the tests and checks.

## Quickstart

1. Create a project with two example workflows:

   ```sh
   treadle init my-automations
   cd my-automations
   ```

2. Run the `hello` workflow:

   ```sh
   treadle run hello -i name=Ada
   ```

   The output shows each step as it ends:

   ```text
   Run #1  hello  (manual)
     ✓ greet    9ms
     ✓ report   1ms
   Run #1 succeeded in 13ms.
   ```

3. Look at the run:

   ```sh
   treadle show 1
   treadle logs 1
   ```

4. Start the daemon, which fires triggers and serves the dashboard:

   ```sh
   treadle serve
   ```

   To see the dashboard, open <http://127.0.0.1:8765>. While the daemon runs, `treadle run`
   in another terminal hands its runs to the daemon. To stop the daemon, press Ctrl-C.

To make the site check run every 15 minutes, uncomment its `triggers` lines in
`workflows/site-check.yml`. The daemon picks up the change within a few seconds.

## Examples

The [`examples/`](examples/) folder holds complete workflows that you can copy into your
project's `workflows/` folder:

| Example | What it does | What it shows |
|---|---|---|
| [`nightly-backup.yml`](examples/nightly-backup.yml) | Mirrors `~/Documents` to a backup drive every night, and sends a push notification when the backup fails. | Schedules, inputs, retries, `failure()`, and the `file` and `http` steps. |
| [`deploy-on-push.yml`](examples/deploy-on-push.yml) | Rebuilds and publishes a Hugo site when `main` changes on a Forgejo server. | Signed webhooks, the `replace` policy, and passing untrusted data safely. |
| [`sort-downloads.yml`](examples/sort-downloads.yml) | Moves new PDFs from `~/Downloads` into `~/Documents/PDFs`. | File watches, and reading the event file from a Python step. |
| [`site-uptime.yml`](examples/site-uptime.yml) | Checks a site every 5 minutes, and sends a push notification when it's down. | The `http` step, `catch_up: none`, and alerts through ntfy. |

Each file starts with comments that explain its setup. After you copy one, check it with
`treadle validate`.

## CLI overview

| Command | What it does |
|---|---|
| `treadle init [DIR]` | Creates a project with example workflows. |
| `treadle validate [PATH...]` | Checks workflow files. `--strict` also fails on warnings. |
| `treadle list` | Shows the workflows, their triggers, next fire times, and last runs. |
| `treadle run WORKFLOW -i KEY=VALUE` | Runs a workflow now and follows its progress. |
| `treadle run --rerun RUN` | Runs a finished run again. |
| `treadle runs` | Lists the run history, with filters and `--grep`. |
| `treadle show RUN` | Summarizes a run, leading with its failure. |
| `treadle logs RUN` | Prints a run's log lines. `-f` follows them. |
| `treadle cancel RUN` | Cancels a queued or running run. |
| `treadle serve` | Runs the daemon: triggers, the dashboard, and the API. |
| `treadle cron EXPR` | Shows when a cron expression fires, with daylight saving time notes. |
| `treadle graph WORKFLOW` | Prints a workflow's step graph as text, Mermaid, or JSON. |
| `treadle version` | Prints the versions of Treadle, Python, and SQLite. |

`RUN` is a run number such as `42`, or `last`, or `last:<workflow>`. Most commands accept
`--json`. For every option and exit code, see the [CLI reference](docs/cli.md).

## Security notes

- `treadle serve` listens on `127.0.0.1` by default. To listen on any other address, set
  `TREADLE_TOKEN` to a token of at least 32 random characters, in the environment or in
  `.env`. Without one, Treadle refuses to start.
- On a machine that other people use, set `TREADLE_TOKEN` even on loopback, because other
  local users can reach the port.
- Treadle serves plain HTTP. To reach it from another machine, use a reverse proxy with TLS,
  an SSH tunnel, or a VPN such as Tailscale.
- Steps run as you, with your permissions, and without a sandbox.
- Keep secrets in `.env`, with mode 0600. A step gets a secret only when it asks for one with
  `${{ secrets.NAME }}`, and Treadle masks secrets in logs and stored data. Masking can't
  catch a transformed secret, such as a base64-encoded one.
- Pass untrusted data, such as webhook fields and file names, to scripts through `env`,
  never as `${{ }}` inside a `run` script. `treadle validate` warns when a workflow does
  that.

For the threat model and the safe patterns, see the [security model](docs/security-model.md).
To report a vulnerability, follow [SECURITY.md](SECURITY.md).

## Documentation

- [Workflow syntax](docs/workflow-syntax.md): every key of a workflow file, the step types,
  and the diagnostic codes.
- [Expressions](docs/expressions.md): templates, contexts, operators, functions, and secrets.
- [Triggers](docs/triggers.md): schedules, file watches, and webhooks.
- [CLI reference](docs/cli.md): every command, the exit codes, and `treadle.toml`.
- [Architecture](docs/architecture.md): how Treadle runs, stores state, and recovers from
  crashes, and the HTTP API.
- [Security model](docs/security-model.md): threats, mitigations, and safe patterns.
- [Operations](docs/operations.md): systemd and launchd services, logs, backups, upgrades,
  and TLS.
- [Coming from GitHub Actions](docs/coming-from-github-actions.md): what's the same, and
  every difference.

## Project status

Treadle 0.1 is a first version. These limits are known:

- Windows isn't supported.
- File watches poll the file system; they don't use native file events.
- Treadle doesn't sandbox steps or limit their CPU and memory. Under systemd, use
  `MemoryMax=` and `CPUQuota=`.
- There's no TLS, and one shared token instead of user accounts.
- Treadle ignores a workflow file that is a symbolic link when it runs workflows, although
  `treadle validate` checks it. Copy the file instead.
- Third-party step types can't be installed yet.

## Roadmap

These items may come after 0.1. They aren't commitments, and their order may change.

- Step plugins, with an allowlist in `treadle.toml`.
- Watch baselines that survive restarts, so changes made while Treadle was stopped start
  runs.
- Native file events through FSEvents and inotify, with polling as the fallback.
- Concurrency groups that span workflows, and a `parallel` policy with a limit.
- Live log streaming in the dashboard and in `treadle logs -f`.
- Notification step types for ntfy and email.
- Re-running a run from the step that failed.
- `for_each` steps over a list.
- Reusable workflows that other workflows call as a step.
- Full-text log search.
- A Prometheus `/metrics` endpoint, and JSON-formatted daemon logs.
- `treadle service install`, which writes systemd and launchd units.
- `treadle doctor`, which checks permissions, the time zone, the SQLite version, the port,
  and the token.
- A JSON Schema for editor completion of workflow files.
- Secrets from the macOS Keychain and the Secret Service API.
- Windows support.

## Contributing

Bug reports and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.
It covers the setup, the tests and checks, the code style, and the commit conventions.

## License

Treadle is available under the MIT License. See [LICENSE](LICENSE).
