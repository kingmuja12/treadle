# Changelog

This file records every notable change to Treadle. Its format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Treadle follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, a minor version may
change behavior in ways that aren't backward compatible.

## [Unreleased]

The first version, 0.1.0. Nothing is tagged yet.

### Added

- Workflow files in YAML, with the file name as the workflow ID, typed inputs, workflow and
  step `env`, `defaults`, per-workflow concurrency policies (`queue`, `skip`, and
  `replace`), and run and cleanup timeouts.
- A strict YAML 1.2 loader that rejects duplicate keys, anchors, aliases, merge keys, and
  tags.
- Step graphs that run in order by default, with `needs` for parallel steps, `if`
  conditions, `continue_on_error`, retries with exponential backoff and jitter, and step
  outputs through `$TREADLE_OUTPUT` and templated `outputs`.
- The `shell`, `http`, `file`, `log`, and `sleep` step types.
- An expression language for `${{ }}` templates and `if` conditions, with the `inputs`,
  `env`, `secrets`, `trigger`, `run`, `workflow`, `steps`, and `result` contexts, status
  functions, and 12 other functions.
- Schedule triggers with cron expressions, time zones, defined behavior on daylight saving
  time changes, and the `none`, `latest`, and `all` catch-up policies. Each schedule slot
  creates at most one run.
- Watch triggers that poll folders, with glob filters, debouncing, and safe handling of
  folders that disappear.
- Webhook triggers with HMAC-SHA256 or token verification, replay protection, rate limits,
  and optional timestamped signatures.
- Secrets from `.env` and `TREADLE_SECRET_*` variables, masked in logs, stored data, and the
  daemon's log, plus `::add-mask::` for values that steps create.
- Diagnostics with stable codes, file positions, and hints, including warnings about
  untrusted data in scripts, paths, and URLs.
- The `treadle` command: `init`, `validate`, `list`, `run`, `runs`, `show`, `logs`, `cancel`,
  `serve`, `cron`, `graph`, and `version`, most with `--json` output.
- The `treadle serve` daemon, with hot reload of workflows and of the secrets in `.env`,
  graceful shutdown, readiness notification for systemd's `Type=notify`, crash recovery
  that marks interrupted runs, and automatic pruning of old runs.
- A dashboard and a JSON API, protected by a token when you set one, with sign-in sessions,
  CSRF protection, a `Host` check against DNS rebinding, and security headers.
- Run history in SQLite, with `--grep` search, re-runs, and backups before schema upgrades.
- Documentation, and four example workflows: a nightly backup, a deploy on push, a
  downloads sorter, and a site uptime check.

[Unreleased]: https://github.com/kingmuja12/treadle/commits/main
