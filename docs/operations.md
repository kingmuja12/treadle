# Operations

This guide covers running `treadle serve` as a long-lived service or in a container, reading
its logs, backing up and upgrading it, and reaching it from other machines.

## Before you start

- Use macOS or Linux. Windows isn't supported in v0.1, because Treadle relies on `fcntl`
  file locks, POSIX process groups, and POSIX signals.
- Install Treadle as described in the [README](../README.md#install), and create a project
  with `treadle init`. This guide uses `~/automations` as the project directory.
- Check the workflows: `treadle -C ~/automations validate`.
- Keep the project's state directory, `.treadle/` by default, on a local disk. Treadle locks
  it with `flock`, which isn't reliable on network file systems.
- On a machine that other people use, set `TREADLE_TOKEN` in `.env`. See
  [Network exposure](security-model.md#network-exposure).

## Run Treadle with systemd

On Linux, run Treadle as a systemd user service. Create
`~/.config/systemd/user/treadle.service`:

```ini
[Unit]
Description=Treadle workflow automation

[Service]
# Treadle tells systemd when it listens.
Type=notify
WorkingDirectory=%h/automations
ExecStart=%h/.local/bin/treadle serve
ExecReload=/bin/kill -HUP $MAINPID
# Steps don't see your login shell's PATH; list the directories their tools live in.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=5
# Exit code 3 is a configuration problem; restarting doesn't fix it.
RestartPreventExitStatus=3
# SIGTERM goes to Treadle alone, so that cleanup steps can finish. When Treadle exits,
# systemd kills every process left in the service, including steps that a crash left.
KillMode=mixed
TimeoutStopSec=60
# Optional limits for Treadle and every step it starts.
MemoryMax=1G
CPUQuota=200%

[Install]
WantedBy=default.target
```

Then load and start it:

```sh
systemctl --user daemon-reload
systemctl --user enable --now treadle
loginctl enable-linger "$USER"
```

`loginctl enable-linger` keeps your user services running after you log out, and starts them
at boot.

- `%h` is your home directory. `uv tool install` and `pipx install` put `treadle` in
  `~/.local/bin`. If yours is elsewhere, `command -v treadle` shows where.
- With `Type=notify`, `treadle serve` sends `READY=1` to systemd once it listens, so systemd
  counts the service as started only then. A `systemctl --user reload treadle` right after
  a start or a restart waits until the daemon handles signals. Steps don't inherit the
  `NOTIFY_SOCKET` variable.
- `KillMode=mixed` sends `SIGTERM` to Treadle alone. Treadle cancels its runs, gives their
  cleanup steps time to finish, and stops the step processes itself. Once Treadle has
  exited, systemd kills every process that is left in the service. That includes the steps
  that a crashed instance left behind, which Treadle never signals, because their IDs may
  belong to other processes by now. With `KillMode=control-group`, systemd would also send
  `SIGTERM` to every step process at once, which ends running cleanup steps.
- A user service can't wait for the system's `network-online.target`, so this unit doesn't
  name it. When Treadle needs the network at startup, for example to listen on a VPN
  address, `Restart=on-failure` starts it again 5 seconds after a failed start. To order
  Treadle after the network, run it as a system service with `User=` set to your user.
- `TimeoutStopSec=60` leaves room for a graceful shutdown: up to `[runs] shutdown_grace`
  (15 seconds by default) for cleanup steps, plus the time it takes to stop the remaining
  processes.
- `MemoryMax=` and `CPUQuota=` cover the steps too, because they run inside the service.
  Treadle has no limits of its own for CPU and memory. In a user service, these limits work
  only when your system delegates the memory and CPU controllers to user services. If it
  doesn't, run Treadle as a system service with `User=` set to your user instead.

Manage the service:

| Task | Command |
|---|---|
| See the logs | `journalctl --user -u treadle -f` |
| Reload the workflows now | `systemctl --user reload treadle` |
| Restart, for example after an upgrade, or a change to `treadle.toml` or to the API token | `systemctl --user restart treadle` |
| Stop | `systemctl --user stop treadle` |

If the daemon listens on a VPN address, such as a Tailscale address, it exits 8 when it
starts before the address exists. `Restart=on-failure` starts it again 5 seconds later.

## Run Treadle with launchd

On macOS, run Treadle as a launchd agent. Create
`~/Library/LaunchAgents/local.treadle.serve.plist`, and replace `ada` with your user name:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.treadle.serve</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/ada/.local/bin/treadle</string>
    <string>serve</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/ada/automations</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/ada/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ExitTimeOut</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>/Users/ada/Library/Logs/treadle.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/ada/Library/Logs/treadle.log</string>
</dict>
</plist>
```

Then load it:

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.treadle.serve.plist
```

- `KeepAlive` with `SuccessfulExit` set to `false` restarts Treadle when it exits with an
  error, but not after a clean stop. launchd has no setting that skips the restart for one
  exit code, so after a configuration error (exit code 3), launchd starts Treadle again
  about every 10 seconds, and each attempt logs the error. Fix the problem that the log
  shows, and the next attempt starts Treadle. To stop the attempts meanwhile, unload the
  agent with `launchctl bootout`.
- `ExitTimeOut` gives Treadle 60 seconds to shut down after `SIGTERM`, before launchd sends
  `SIGKILL`.
- launchd starts Treadle with a minimal `PATH`. The plist's `PATH` entry adds the usual
  Homebrew and user directories, so steps find tools such as `rsync`, `jq`, and `hugo`.
- macOS protects folders such as Documents, Downloads, and external volumes. A step that
  works in Terminal may fail under launchd with `Operation not permitted`. To fix it, give
  the Python interpreter that runs Treadle access in **System Settings** > **Privacy &
  Security**, under **Files and Folders** or **Full Disk Access**.

Manage the agent:

| Task | Command |
|---|---|
| See the logs | `tail -f ~/Library/Logs/treadle.log` |
| Reload the workflows now | `launchctl kill HUP "gui/$(id -u)/local.treadle.serve"` |
| Restart, for example after an upgrade, or a change to `treadle.toml` or to the API token | `launchctl kickstart -k "gui/$(id -u)/local.treadle.serve"` |
| Stop and unload | `launchctl bootout "gui/$(id -u)/local.treadle.serve"` |

## Run Treadle in a container

Treadle has no official container image. Build one that installs Treadle as the
[README](../README.md#install) describes, and run `treadle serve` in it under an init
process. With Docker, the `--init` option adds one:

```sh
docker run --init --stop-timeout 60 \
  -v /srv/automations:/automations -w /automations \
  IMAGE treadle serve
```

Replace `IMAGE` with your image. This command runs the project in `/srv/automations` on the
host. With Docker Compose, set `init: true` and `stop_grace_period: 60s` on the service. To
run the image without `--init`, install tini in it, and make tini the entrypoint:

```dockerfile
ENTRYPOINT ["tini", "--"]
CMD ["treadle", "serve"]
```

### Why Treadle needs an init process

Without an init process, Treadle runs as PID 1, the container's first process. PID 1 adopts
every process whose parent exits, such as the background processes of a step whose shell
has ended. Treadle waits only for the processes that it starts. When an adopted process
ends, for example because Treadle killed it, nothing reaps it, and it stays a zombie. Linux
still counts a zombie as a member of its process group, so the step's group never looks
empty. Each time Treadle stops such a group, it waits out the whole `[runs] kill_grace`,
sends `SIGKILL`, and waits 2 more seconds. With the default grace of 5 seconds, a step that
times out after 1 second takes about 8 seconds to end. A forced cancel sends `SIGKILL` at
once, but still waits the 2 seconds. The zombies also keep their process IDs until Treadle
exits.

An init process such as tini, which `--init` runs, reaps those processes at once. It
forwards signals to Treadle, so `docker stop` still shuts Treadle down gracefully, and
`docker kill --signal HUP` still reloads the workflows.

### Container settings

- `docker stop` sends `SIGTERM`, and then `SIGKILL` when the container's stop timeout runs
  out. Docker's default timeout can be shorter than `[runs] shutdown_grace` (15 seconds by
  default), and `SIGKILL` ends the cleanup steps that are still running. `--stop-timeout 60`
  gives Treadle the time that `TimeoutStopSec=60` gives it under systemd. When Treadle
  exits, the container stops, and so does every process left in it, including the steps
  that a crashed Treadle left behind.
- Inside a container, `127.0.0.1` is the container's own loopback address, which a published
  port can't reach. To publish the port, listen on every address with `--host 0.0.0.0`, set
  `TREADLE_TOKEN`, which Treadle requires on any address other than loopback, and publish
  the port on the host's loopback address only, as in `-p 127.0.0.1:8765:8765`.
- Keep the project directory outside the container, as the example's `-v` option does, so
  the run history in `.treadle/treadle.db` outlives the container. Keep it on a local disk,
  as [Before you start](#before-you-start) explains.
- `--memory` and `--cpus` limit Treadle and every step it starts, as `MemoryMax=` and
  `CPUQuota=` do under systemd.
- Set `timezone` in `treadle.toml` instead of `local`. A container's local time zone is
  often UTC, so with `local`, schedules fire in UTC.

## Logs

The daemon writes its log to standard error, which journald and launchd capture. Each line
holds the time in UTC, the level, the component, and the message:

```text
2026-11-10T13:06:16.508Z INFO treadle.daemon listening on http://127.0.0.1:8765
2026-11-10T13:06:17.985Z INFO treadle.web POST /hooks/deploy 202 2ms
2026-11-10T13:06:17.997Z INFO treadle.engine run=1 succeeded
```

- Set the level with `[logging] level` in `treadle.toml`, or with `treadle serve
  --log-level`: `debug`, `info` (the default), `warning`, or `error`.
- The daemon masks secrets in every line. It logs each HTTP request's method, path, status,
  and duration, but never request bodies or headers.
- A step's own output goes to the run history, not to the daemon's log. Read it with
  `treadle logs RUN`.

## Signals

| Signal | Effect |
|---|---|
| `SIGHUP` | Reloads the workflows now. The daemon also reloads them by itself, within a few seconds of a change. During startup, `SIGHUP` does nothing, because startup loads every workflow file anyway. |
| First `SIGTERM` or `SIGINT` | Shuts down gracefully: refuses new runs, cancels the running runs with the reason `shutdown`, gives their cleanup steps up to `[runs] shutdown_grace`, and exits 0. A run that is already cancelling, for example after a user's cancel, keeps its reason, and its cleanup steps keep running within the same grace. Queued runs stay queued for the next start. During startup, the daemon shuts down as soon as it serves. |
| Second `SIGTERM` or `SIGINT` | Stops the runs at once. |
| Third `SIGTERM` or `SIGINT` | Exits 130 at once. The next start marks the leftover runs as interrupted. |

The daemon handles these signals from the moment its startup begins. A `SIGHUP` that
arrives in about the first 0.3 seconds of the process, before `treadle serve` begins, can
still end it. Under systemd, `Type=notify` keeps a reload from coming that early.

A change to `treadle.toml` needs a restart, and the daemon logs a reminder. Changes to
`.env` apply without a restart, except a change to the API token: `TREADLE_TOKEN`, or the
variable that `[server] token_env` names. Until a restart, the API keeps checking, and
masking, the token that the daemon started with. When `.env` changes the token, the daemon
logs a warning:

```text
2026-11-10T12:30:44.069Z WARNING treadle.daemon TREADLE_TOKEN changed in .env; restart Treadle to apply it. Until then, the API doesn't require a token.
```

- When the daemon started with a token, the warning ends with
  `Until then, the API accepts only the previous token.`
- When Treadle couldn't start with the new value, such as a token shorter than 32
  characters, or no token on an address other than loopback, the warning reads
  `TREADLE_TOKEN changed in .env, but Treadle can't start with the new value: <reason>. Until a restart, ...`.
- A change back to the token in use logs `TREADLE_TOKEN in .env is the token in use again`
  at the `INFO` level.

## Backups

The database, `.treadle/treadle.db`, holds the run history and the schedule state. Back up
your project files too: the workflows, `treadle.toml`, and `.env`.

To back up the database while Treadle runs, use SQLite's backup command. A plain file copy
can miss changes that are still in the write-ahead log.

```sh
cd ~/automations
umask 077
sqlite3 .treadle/treadle.db ".backup 'treadle-backup.db'"
```

`umask 077` keeps the copy readable only by you, as the original is. The `sqlite3` command
ships with macOS. On Debian and Ubuntu, install the `sqlite3` package.

To restore a backup:

1. Stop Treadle.
2. Replace `.treadle/treadle.db` with the backup, and delete `treadle.db-wal` and
   `treadle.db-shm` if they exist.
3. Start Treadle.

`.treadle/secret.key` only signs dashboard sessions. If you lose it, Treadle creates a new
one, and everyone signs in again.

## Upgrades

Treadle is in its first release cycle, so read the [changelog](../CHANGELOG.md) before you
upgrade. To upgrade:

1. Stop the service.
2. Upgrade Treadle: `uv tool upgrade treadle`, or `pipx reinstall treadle`.
3. Run `treadle version`, and `treadle validate` in the project.
4. Start the service.

When a new version changes the database schema, Treadle first copies the database to
`.treadle/backups/`, and keeps the newest 3 copies. Then it migrates the database. Treadle
never downgrades a database: an older version refuses to open a newer database and exits 3.
To go back to an older version, restore the backup from before the upgrade.

## Retention

The daemon deletes old runs when it starts, and then every hour. By default, it keeps runs for
30 days, and at most 500 runs per workflow. It never deletes queued or running runs. To
change the limits, set the `[retention]` keys in `treadle.toml`:

```toml
[retention]
max_age = "90d"
max_runs_per_workflow = 2000
interval = "6h"
```

For every key and its range, see [Configuration file](cli.md#configuration-file).

## TLS and remote access

Treadle serves plain HTTP. To use the dashboard, the API, or webhooks from another machine,
put an encrypted connection in front of it. Pick one of these approaches.

### Reverse proxy

Keep Treadle on `127.0.0.1`, and let a proxy terminate TLS. With [Caddy](https://caddyserver.com),
which gets a certificate on its own:

```text
treadle.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

With nginx, keep the `Host` header, which Treadle checks:

```nginx
server {
    listen 443 ssl;
    server_name treadle.example.com;
    ssl_certificate     /etc/letsencrypt/live/treadle.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/treadle.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        client_max_body_size 1m;
    }
}
```

Then configure Treadle for the proxy:

```toml
[server]
allowed_hosts = ["treadle.example.com"]
secure_cookies = true
```

- **Set `TREADLE_TOKEN` in `.env`.** Treadle requires a token only on addresses other than
  loopback, but the proxy makes the loopback daemon reachable from the network.
- `allowed_hosts` lets the dashboard and API accept the proxy's host name. Without it, they
  answer 400 `Invalid host header`.
- `secure_cookies` marks the session cookie `Secure`, so browsers send it only over HTTPS.
- Treadle ignores `X-Forwarded-*` headers, so a client can't use them to pretend to be
  someone else.
- Browsers that don't send the `Sec-Fetch-Site` header, such as Safari before version 16.4,
  can't submit the dashboard's forms through a TLS proxy. Use a current browser.

### Webhooks only

When only webhook senders need to reach Treadle, expose only `/hooks/`. With Caddy:

```text
hooks.example.com {
    handle /hooks/* {
        reverse_proxy 127.0.0.1:8765
    }
    handle {
        respond 404
    }
}
```

Webhook requests skip the `Host` check and need no token, because their signatures protect
them, so this setup needs no change to `treadle.toml`.

### SSH tunnel

To use the dashboard from your laptop without exposing anything, forward a local port to the
server:

```sh
ssh -N -L 8765:127.0.0.1:8765 ada@home-server
```

Then open `http://127.0.0.1:8765` on the laptop.

### Tailscale or another VPN

To reach Treadle over a VPN such as Tailscale, which encrypts the traffic, listen on the
machine's VPN address and set a token:

```toml
[server]
host = "100.101.102.103"   # this machine's address, from `tailscale ip -4`
```

On an address other than loopback, Treadle refuses to start without a token of at least 32
characters in `TREADLE_TOKEN`.

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `error: port 8765 on 127.0.0.1 is in use` | Another program uses the port. Set `[server] port`, or pass `--port`. |
| `error: another Treadle process holds the state directory lock (serve, pid 4242, port 8765)` | A daemon already runs for this project. You don't need a second one: `treadle run` hands runs to it. |
| ``error: another `treadle run` (pid 4242) holds .treadle; ...`` | A run in another terminal holds the project. Wait for it to end, or start `treadle serve`, which runs workflows concurrently. |
| `warning: process group 4242 of an interrupted run still exists; ...` | A crashed Treadle left a step running. List its processes with `pgrep -g 4242 -l`. If they're yours and you don't need them, stop them with `kill -- -4242`. |
| A webhook answers 503 `hook_disabled` | The hook's `secret_env` variable isn't set, or holds fewer than 16 characters. Set it in `.env`. |
| A webhook answers 401 `unauthorized` | The secret, the signature header, or the signed body differs from what Treadle expects. Check `signature_header`, and test with the command in [Send a test delivery](triggers.md#send-a-test-delivery). |
| The dashboard answers 400 `Invalid host header` | Add the host name you use to `[server] allowed_hosts`. |
| The API accepts the old token after you changed `TREADLE_TOKEN`, or answers 401 to the new one | The daemon applies a token change only after a restart, and logs a warning until then. Restart `treadle serve`. |
| The request log shows `POST /hooks/<name> 400` with no other error | The client disconnected before it sent the whole body. Treadle answers 400 to such a request, on `/hooks/`, the API, or a dashboard form, and logs no traceback. |
| A schedule doesn't fire | Schedules fire only while `treadle serve` runs. Check the NEXT FIRE column of `treadle list`, and the times that `treadle cron` shows. A slot missed while the daemon was down runs only as `catch_up` allows. |
| A watch doesn't fire | The first scan is silent, patterns are case-sensitive, and a missing folder makes the trigger degraded. The dashboard shows the trigger's health. |
| `error: this database was created by a newer Treadle ...` | Upgrade Treadle, or restore a backup from before the upgrade. |
