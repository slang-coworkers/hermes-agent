---
sidebar_position: 4
---

# Running Many Gateways at Once

Operate multiple [profiles](./profiles.md) — each with its own bot tokens,
sessions, and memory — as managed services on a single machine. This page
covers the operational concerns: starting them all together, viewing logs
across profiles, preventing the host from sleeping, and recovering from common
launchd/systemd quirks.

If you only run one Hermes agent, you don't need this page — see
[Profiles](./profiles.md) for the basics. And if your instances live on
*different* machines that one desktop app should reach simultaneously, see
[Connecting Desktop to Many Hermes Instances](./multi-connection-desktop.md).

## When to use this

You want this setup when you have two or more Hermes agents that should all
be online at the same time. Common reasons:

- A personal assistant on one Telegram bot and a coding agent on another
- One agent per family member or one per Slack workspace
- Sandbox + production instances of the same configuration
- A research agent + a writing agent + a cron-driven bot — each with isolated
  memory and skills

Every profile already gets its own per-platform LaunchAgent
(`ai.hermes.gateway-<name>.plist`) or systemd user service
(`hermes-gateway-<name>.service`). This guide adds the patterns for managing
them collectively.

## Quick start

```bash
# Create profiles (once)
hermes profile create coder
hermes profile create personal-bot
hermes profile create research

# Configure each
coder setup
personal-bot setup
research setup

# Install each gateway as a managed service
coder gateway install
personal-bot gateway install
research gateway install

# Start them all
coder gateway start
personal-bot gateway start
research gateway start
```

That's it — three independent agents, each on its own process, restarting
automatically on crash and on user login.

## Alternative: one gateway for all profiles (multiplexing)

The model above runs **one process per profile**. That is the default and is
the right choice for most setups. But on a host with many profiles — or a
container deployment where one process per profile is operationally heavy — you
can instead run a **single multiplexing gateway**: the default profile's gateway
becomes the sole inbound process and serves messages for *every* profile on the
box.

This is **opt-in** and **off by default**. When it's off, nothing on this page
changes — every behavior below is inert.

### When to prefer multiplexing

- A container/VPS deployment where N supervisor units, N ports, and N PID files
  are a burden.
- Many low-traffic profiles that don't each justify a full process.
- You want a single thing to start, monitor, and restart.

Stick with one-process-per-profile when you want hard process-level isolation
between profiles (separate memory footprints, independent crash domains, the
ability to restart one profile without touching the others).

### How to opt in

Set the flag on the **default profile** (it owns the multiplexer) and restart
its gateway:

```bash
hermes config set gateway.multiplex_profiles true
hermes gateway restart
```

Equivalently, in the default profile's `~/.hermes/config.yaml`:

```yaml
gateway:
  multiplex_profiles: true
```

(The flag is also accepted as a top-level `multiplex_profiles: true` for
convenience.) On the next start the default gateway enumerates every profile,
brings up each profile's enabled platforms under that profile's own
credentials, and routes each inbound message to the profile it belongs to. Each
turn resolves the routed profile's config, skills, memory, SOUL, **and provider
keys** — credentials are never shared across profiles.

You do **not** run `hermes gateway start` for the secondary profiles — the
default gateway serves them. See the contract changes below.

### What changes when multiplexing is on

Enabling the flag changes how a few things behave. All of these revert the
moment the flag is off.

#### 1. Secondary profiles must not start their own gateway

With a multiplexer running, a named-profile `hermes gateway start` / `run` is a
**hard error**, pointing you back at the multiplexer:

```
The default gateway is running as a profile multiplexer and already serves
profile 'coder'. ...
```

The multiplexer is the single inbound process; a second profile gateway would
double-bind that profile's platforms. Pass `--force` only if you deliberately
want a separate process for that profile (not recommended while the multiplexer
is running). The cross-profile lifecycle wrapper script earlier on this page is
therefore **not** used in multiplex mode — you only manage the default gateway.

#### 2. HTTP-inbound platforms are reached via a `/p/<profile>/` URL prefix

Webhook (and other HTTP-inbound) traffic for a secondary profile arrives on the
default listener under a profile prefix, **not** a second port:

```
# default profile
POST http://host:8644/webhooks/<route>
# the "coder" profile, same listener
POST http://host:8644/p/coder/webhooks/<route>
```

An unknown or unconfigured profile in the prefix returns `404`. Because the one
shared listener already serves every profile this way, a **secondary profile
must not enable a port-binding platform itself** — doing so is a config error
that skips the entire secondary profile while the default and other healthy
profiles continue. The warning names the skipped profile and every conflicting
platform:

```
Skipping secondary profile 'coder' due to port-binding config error: Profile
'coder' enables port-binding platform(s) webhook, but gateway.multiplex_profiles
is on. ... Remove these platform entries from profile 'coder's config.yaml or
configure them only on the default profile.
```

Port-binding platforms covered by this rule: `webhook`, `api_server`,
`msgraph_webhook`, `feishu`, `wecom_callback`, `bluebubbles`, `sms`,
`whatsapp_cloud`, `line`. Configure any of these **only on the default profile**;
every profile is reachable through its `/p/<profile>/` prefix.

Authentication follows the profile named in the URL. Unprefixed endpoints keep
using the default listener's existing credentials.

- `/p/coder/...` API-server requests must use `API_SERVER_KEY` from
  `~/.hermes/profiles/coder/.env`; the default listener key is rejected.
- A webhook route that targets `coder` must declare `profile: coder` beside
  its existing route-specific `secret` in the default profile's
  `config.yaml`. That secret is then accepted only at
  `/p/coder/webhooks/<route>` and is rejected on every other profile prefix.
- Webhook routes without `profile` remain default-profile routes and are not
  reachable through a named profile prefix.

Keep port-binding platforms disabled in secondary profile configs. The shared
listener and its route definitions stay on the default profile; profile
binding controls which profile each authenticated webhook route may execute.
Named API requests fail closed when the target profile has no
`API_SERVER_KEY`.

Only this shared-listener conflict degrades to a skipped profile. Security
configuration errors remain fatal: for example, an `open` own-policy platform
without `GATEWAY_ALLOW_ALL_USERS` or its platform-specific allow-all opt-in
still aborts gateway startup rather than silently dropping the unsafe profile.

#### 3. Per-credential platforms still need their own token per profile

Polling/connection platforms (Telegram, Discord, Slack, Matrix, Signal, …) work
fine multiplexed, but each profile that enables one must supply its **own** bot
token — the same token cannot be polled by two profiles at once. If two profiles
configure the same `(platform, token)`, startup fails fast naming both profiles
(see [Token-conflict safety](#token-conflict-safety) — the rule is unchanged,
it's just enforced inside the one process now).

#### 4. Session keys are namespaced by profile

Each profile's sessions live under an `agent:<profile>:…` namespace so two
profiles on the same platform/chat never collide in the shared session store.
The **default** profile keeps the historical `agent:main:…` namespace
byte-for-byte, so existing default-profile sessions are unaffected — no
migration, no orphaned history.

#### 5. One PID/lock and one status surface

There is a single process-level PID and lock (the multiplexer, under the default
home). `hermes status` reports the multiplexer and the profiles it serves;
`hermes status -p <name>` slices to one profile. Each profile still writes its
own `runtime_status.json` under its own home, so existing per-profile readers
keep working.

#### What does **not** change

Per-profile `.env` credential isolation is preserved and, if anything,
stricter: a profile's keys are resolved from its own scope and are never unioned
into a shared environment (this also means subprocesses like MCP servers and
Kanban workers only ever see their own profile's secrets). Kanban,
profile-scoped skills/memory/SOUL, and model routing all behave per-profile
exactly as they do with separate gateways.

### Serving selected profiles

By default, `gateway.multiplex_profiles: true` serves every valid named profile
on the host. To keep unrelated profiles installed without starting their
adapters or cron jobs, set `gateway.multiplex_profile_allowlist`:

```yaml
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist:
    - worker
    - guest
```

The default profile is always served and does not need to be listed. An unset
allowlist preserves the historical serve-all behavior; an empty list serves
only the default profile. Names are normalized and deduplicated. Invalid list
entries or names that are not installed are skipped with a warning. A malformed
non-list value fails safely to default-only.

The resulting served set also controls `/p/<profile>/` API and webhook prefixes,
runtime status, profile-route eligibility, and which profiles the in-process
cron scheduler ticks. A named profile outside the allowlist may still run its
own standalone gateway.

### Routing shared-bot chats to profiles (`profile_routes`)

Multiplexing selects a profile per **credential** (each profile's own bot
token) or per **URL prefix** (`/p/<profile>/` for HTTP platforms). When several
communities share **one** bot token — for example one Discord bot serving many
guilds — you can additionally route specific guilds/channels/threads to
different profiles with `gateway.profile_routes`:

```yaml
gateway:
  multiplex_profiles: true
  profile_routes:
    # An entire Discord server → one profile
    - name: acme-server
      platform: discord
      guild_id: "1234567890"
      profile: acme

    # One channel in that server → a different profile
    - name: acme-support
      platform: discord
      guild_id: "1234567890"
      chat_id: "9876543210"
      profile: acme-support

    # A Telegram group (no guild concept — chat_id only)
    - name: tg-group
      platform: telegram
      chat_id: "-1001234567890"
      profile: tg-profile
```

Routes are matched most-specific-first (`thread_id` > `chat_id` > `guild_id`),
all declared fields must hold (AND), and a route keyed on a channel also
matches threads/forum posts whose parent is that channel. Messages that match
no route stay on the default/active profile. The routed profile gets the full
per-profile isolation described above (config, skills, memory, credentials,
session namespace). Routing works on every platform adapter, not just Discord.

`profile_routes` requires `gateway.multiplex_profiles: true`; with
multiplexing off the routes are ignored. If an explicit route matches but its
target profile is not installed or is outside `multiplex_profile_allowlist`,
the gateway rejects that ingress and logs the route and target. It does not run
the default profile. Traffic that matches no route keeps the historical
default-profile behavior.

Once a chat resolves to a profile, **how that profile engages** in the chat
(mention-only, mention-sticky, pattern wake-words, or always-on, plus an optional
channel/chat scope) is per-profile adapter config. When you compose a fleet with
`nv-coworker-compose`, declare it with an `engage` block and the renderer
translates it into the per-platform gate keys — see
[Fleet engage modes](./fleet-engage-modes.md).

## Start, stop, or restart all gateways at once

The CLI ships with single-profile lifecycle commands. To act across every
profile, wrap them in a shell loop. Put the snippet below in
`~/.local/bin/hermes-gateways` and `chmod +x` it:

```sh
#!/bin/sh
set -eu

# Add or remove profile names here as you create / delete profiles.
profiles="default coder personal-bot research"

usage() {
  echo "Usage: hermes-gateways {start|stop|restart|status|list}"
}

run_for_profile() {
  profile="$1"
  action="$2"
  if [ "$profile" = "default" ]; then
    hermes gateway "$action"
  else
    hermes -p "$profile" gateway "$action"
  fi
}

action="${1:-}"
case "$action" in
  start|stop|restart|status)
    for profile in $profiles; do
      echo "==> $action $profile"
      run_for_profile "$profile" "$action"
    done
    ;;
  list)
    hermes gateway list
    ;;
  *)
    usage
    exit 2
    ;;
esac
```

Then:

```bash
hermes-gateways start      # start every configured profile
hermes-gateways stop       # stop every configured profile
hermes-gateways restart    # restart all
hermes-gateways status     # status across all
hermes-gateways list       # delegates to `hermes gateway list`
```

:::tip
The `default` profile is targeted with `hermes gateway <action>` (no `-p`),
not `hermes -p default gateway <action>`. The wrapper above handles both forms.
:::

## Manage one profile

The shortcut commands every profile installs:

```bash
coder gateway run        # foreground (Ctrl-C to stop)
coder gateway start      # start the managed service
coder gateway stop       # stop the managed service
coder gateway restart    # restart
coder gateway status     # status
coder gateway install    # create the LaunchAgent / systemd unit
coder gateway uninstall  # remove the service file
```

These are equivalent to `hermes -p coder gateway <action>` — useful if a
profile alias is not on `PATH` or if you target profiles dynamically from a
script.

## Service files

Each profile installs its own service with a unique name, so installations
never clash:

| Platform | Path                                                              |
| -------- | ----------------------------------------------------------------- |
| macOS    | `~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist`        |
| Linux    | `~/.config/systemd/user/hermes-gateway-<profile>.service`         |

The default profile keeps the historical names: `ai.hermes.gateway.plist` /
`hermes-gateway.service`.

## Viewing logs

Each profile writes to its own log files:

```bash
# Default profile
tail -f ~/.hermes/logs/gateway.log
tail -f ~/.hermes/logs/gateway.error.log

# Named profile
tail -f ~/.hermes/profiles/<name>/logs/gateway.log
tail -f ~/.hermes/profiles/<name>/logs/gateway.error.log
```

Stream every profile's log simultaneously:

```bash
tail -f ~/.hermes/logs/gateway.log ~/.hermes/profiles/*/logs/gateway.log
```

The CLI also has a structured log viewer:

```bash
hermes logs -f                  # follow default profile
hermes -p coder logs -f         # follow one profile
hermes logs --help              # filters, levels, JSON output
```

## Identify what's actually running

```bash
hermes profile list             # profiles + model + gateway state
hermes-gateways status          # full status across every profile
launchctl list | grep hermes    # macOS — PIDs and labels
systemctl --user list-units 'hermes-gateway-*'   # Linux — units
```

## Editing configuration

Every profile keeps its config inside its own directory:

```
~/.hermes/profiles/<name>/
├── .env              # API keys, bot tokens (chmod 600)
├── config.yaml       # model, provider, toolsets, gateway settings
└── SOUL.md           # personality / system prompt
```

The default profile uses `~/.hermes/` directly with the same three files.

Edit them with any editor or via the CLI:

```bash
hermes config set model.model anthropic/claude-sonnet-4    # default profile
coder config set model.model openai/gpt-5                  # named profile
```

After editing `.env` or `config.yaml`, restart the affected gateway:

```bash
coder gateway restart
# or, for everything:
hermes-gateways restart
```

## Keeping the host awake

The gateway process can run all day, but the operating system will still try
to sleep when idle. Two patterns:

### macOS — `caffeinate`

`caffeinate` is built into macOS and prevents sleep while it runs. No install.

```bash
caffeinate -dis                    # block display, idle, and system sleep
caffeinate -dis -t 28800           # same, auto-exit after 8 hours
caffeinate -i -w $(cat ~/.hermes/gateway.pid) &   # awake while default gateway runs

# Persistent: run in background and forget
nohup caffeinate -dis >/dev/null 2>&1 &
disown

# Inspect / stop
pmset -g assertions | grep -iE 'caffeinate|prevent|user is active'
pkill caffeinate
```

| Flag   | Effect                                            |
| ------ | ------------------------------------------------- |
| `-d`   | block display sleep                               |
| `-i`   | block idle system sleep (default)                 |
| `-m`   | block disk sleep                                  |
| `-s`   | block system sleep (AC-powered Macs only)         |
| `-u`   | simulate user activity (prevents screen lock)     |
| `-t N` | auto-exit after `N` seconds                       |
| `-w P` | exit when PID `P` exits                           |

:::warning Lid-close still sleeps the Mac
`caffeinate` cannot override the hardware-driven lid-close sleep on MacBooks.
For lid-closed operation, change your Energy Saver / Battery preferences or
use a third-party tool.
:::

### Linux — `systemd-inhibit` or `loginctl`

```bash
# Inhibit suspend while a command runs
systemd-inhibit --what=idle:sleep --who=hermes --why="gateways running" \
  sleep infinity &

# Allow user services to keep running after logout (recommended)
sudo loginctl enable-linger "$USER"
```

After enabling lingering, your systemd user units (including
`hermes-gateway-<profile>.service`) continue running across SSH disconnects
and reboots.

## Token-conflict safety

Each profile must use unique bot tokens for each platform. If two profiles
share a Telegram, Discord, Slack, WhatsApp, or Signal token, the second
gateway refuses to start with an error naming the conflicting profile.

To audit:

```bash
grep -H 'TELEGRAM_BOT_TOKEN\|DISCORD_BOT_TOKEN' \
     ~/.hermes/.env ~/.hermes/profiles/*/.env
```

## Updating the code

`hermes update` pulls the latest code once and syncs new bundled skills into
every profile:

```bash
hermes update
hermes-gateways restart
```

User-modified skills are never overwritten.

## Troubleshooting

### "Could not find service in domain for user gui: 501"

You ran `hermes gateway start` after a previous `hermes gateway stop`. The
CLI's `stop` does a full `launchctl unload`, which removes the service from
launchd's registry. The CLI catches this specific error on `start` and
automatically re-loads the plist (`↻ launchd job was unloaded; reloading
service definition`). The service starts normally. Nothing to fix.

### Stale PID after a crash

If a profile's gateway shows `not running` but a process is still alive:

```bash
ps -ef | grep "hermes_cli.*-p <profile>"
cat ~/.hermes/profiles/<profile>/gateway.pid
kill -TERM <pid>          # graceful
kill -KILL <pid>          # if that fails after a few seconds
<profile> gateway start
```

### Forcing a hard reset of one service

```bash
# macOS
launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist
launchctl load   ~/Library/LaunchAgents/ai.hermes.gateway-<profile>.plist

# Linux
systemctl --user restart hermes-gateway-<profile>.service
```

### Health check

```bash
hermes doctor                  # default profile
hermes -p <profile> doctor     # one profile
```

### Host sweep & restart recovery

A Hermes fleet is supervised as **one gateway** in Bot Mode, not as a bank of
per-bot daemons and not by a periodic host "sweep" loop. The default profile's
gateway runs with `gateway.multiplex_profiles: true` and a
`multiplex_profile_allowlist` naming the roster, so that single multiplexing
process serves every allowlisted coworker. Each responsibility a NanoClaw-style
host `sweep()` used to carry — inbound `processing_ack` reconciliation, outbound
at-least-once delivery, stale-claim detection, due-message wake, recurrence, and
crash-loop backoff — is owned by a first-class Hermes mechanism (mapped in the
table at the end of this section), and the gateway process itself is kept alive
by the OS service supervisor: systemd on Linux, launchd on macOS, s6 inside the
fleet container.

Every behaviour described here is stock v2026.8.31, proven by the hermetic
adopt-proof tests in `tests/gateway/test_iso_f17_supervision_acceptance.py`
(`AC-ISO-F17-1` … `AC-ISO-F17-11`). The fails-on-base documentation contract for
this section is `tests/hermes_cli/test_iso_f17_doc_contract.py`.

#### One gateway, per-profile boot reconcile

On container boot, `container_boot.py` reconciles a per-profile s6 service slot
for each profile from that profile's persisted `desired_state`. When the
`GATEWAY_MULTIPLEX_PROFILES` environment flag is set (the fleet default), only
the **default** slot auto-starts and owns all inbound; every named coworker slot
is *registered but held down* (a `down` marker is written) even when its
persisted `desired_state` is `running`. Boot reconcile **does not mutate** the
persisted `desired_state` — it only reads it and suppresses the named-profile
autostart — so the fleet image should separately keep every coworker profile at
a **non-running** desired state, so that the same tree also does the right thing
if the multiplexer flag is ever cleared (`AC-ISO-F17-1`, `AC-ISO-F17-2`).

A legacy transient `gateway_state` of `draining` or `degraded` normalizes to
running-eligible during this read; an explicit `desired_state` (including
`startup_failed`) is honoured verbatim and is **not** normalized to `running`.

#### Restarting the gateway (exit-code contract)

The generated service unit restarts that one gateway and distinguishes an
intentional restart from a fatal config error by exit code
(`AC-ISO-F17-3`, `AC-ISO-F17-4`):

- **systemd** (`hermes gateway install`): `Restart=always` with
  `RestartForceExitStatus=75` (drain-and-restart — a via-service reload stamps
  exit `75`) and `RestartPreventExitStatus=78` (fatal config — the unit will not
  restart). With `gateway.systemd_watchdog_seconds` > 0 the unit is emitted as
  `Type=notify` with `WatchdogSec=<n>s` and the loop heartbeat sends
  `WATCHDOG=1` only on a timely tick, so a frozen loop is starved of its
  heartbeat and systemd restarts it; the default is `0` (`Type=simple`, watchdog
  off — opt-in).
- **s6** (fleet container): the finish script maps a child exit of `78` to `125`
  (s6's "permanent down"), so a fatal-config gateway is not respawned; other
  non-zero exits restart normally.
- **launchd** (macOS): `KeepAlive` is **unconditional** — launchd relaunches the
  gateway on *any* exit. launchd has **no** exit-78 permanent-stop equivalent
  (the systemd/s6 78-stop is the systemd/s6 guarantee only). On macOS, launchd
  relaunches and re-hits the fatal error until the configuration is fixed:
  `restart_loop_guard` does not stop launchd from relaunching the gateway; it only
  suppresses automatic session replay after chained crash-boots.

Separately from the systemd watchdog, an out-of-loop **`shutdown_watchdog`**
loop-liveness thread probes the running event loop and, if the loop misses its
probe budget (frozen), hard-exits the process with the restart exit code `75` so
the supervisor revives it; a live loop that answers the probe is left alone
(`AC-ISO-F17-5`).

#### `hermes -p <bot> gateway start` under the multiplexer

While the default multiplexer is serving a named profile, that named profile
**cannot run its own gateway** — but the two entry points behave differently, so
state it precisely (`AC-ISO-F17-11`):

- **Foreground** `hermes -p <bot> gateway run` refuses **synchronously**: the
  guard sees a running multiplexer already serving `<bot>` and exits `78`
  (fatal config). `--force` bypasses the guard; with no running multiplexer it
  does not refuse.
- **Supervised** `hermes -p <bot> gateway start` dispatches to the service
  manager: it issues `s6-svc -u` for the slot, persists `desired_state=running`,
  and **returns 0**. The spawned gateway child then hits the same guard and
  exits `78`, and under s6 the finish script maps `78 → 125` so the supervisor
  **permanently stops** the slot. The named profile therefore never serves — the
  refusal is operationally a hard error — but note that the `start` *command*
  returns before its spawned child is stopped; do not expect a non-zero status
  from `gateway start` itself in the s6 path.

To actually move inbound to a different profile, change which profile the one
multiplexer serves (its `multiplex_profile_allowlist`), rather than starting a
second gateway.

#### Interrupted-session and lease recovery

At startup the gateway re-arms sessions that a crash or restart left mid-turn:
`resume_pending` sessions are scheduled for exactly one auto-resume turn, gated
so that an already-running (slot-claimed), suspended, wrong-reason, or
unauthorized session is skipped. The auto-resume reason taxonomy is
`restart_interrupted` / `restart_timeout` / `shutdown_timeout`, and the in-process
respawn guard (below) caps chained restart-interrupted boots so a crash-looping
turn does not replay forever (`AC-ISO-F17-6`).

A durable per-session **turn lease** fences concurrent turns. A lease whose
holder PID is dead is reclaimed by the next caller; a lease held by a live PID
is retained (owner-fenced), so a crashed worker's lease is freed without
stealing a live worker's turn (`AC-ISO-F17-7`).

#### Kanban claim reclaim and respawn guards

The kanban dispatcher runs embedded in the gateway (`kanban.dispatch_in_gateway`,
default on — the standalone dispatcher unit is deprecated). Its
`release_stale_claims` pass reclaims a dead worker's expired claim (writing a
`reclaimed` event) but **extends** a live worker's claim when its heartbeat is
fresh (a `claim_extended` event); a claim whose heartbeat is older than the
max-stale window with a still-live PID is not extended — the dispatcher signals
the worker to terminate and, if it survives, defers the reclaim with a grace (a
`reclaim_deferred` event, the claim retained) rather than yanking a live
worker's task (`AC-ISO-F17-8`).

Before re-dispatching a task, `check_respawn_guard` defers across three signals
(`AC-ISO-F17-9`): a recent rate-limited run holds the task under a cooldown
(the rate-limit sentinel exit code is `75`), an auth/quota `last_failure_error`
yields `blocker_auth`, and a protocol-violation streak (a worker exiting `rc=0`
while the task is still running) keeps the task `ready` below the failure limit
but `blocked` at it. A clean task is respawnable.

#### Sandbox orphan reaping

Sandbox orphans are the terminal backend's job, not a daemon's: the Docker
terminal backend's `reap_orphan_containers` runs **once per interpreter** (a
process-level guard makes a second call a no-op), gated by
`terminal.docker_orphan_reaper`. Its selection filter removes only containers
that are `status=exited`, carry the `hermes-agent=1` label, and are older than
the max age (scoped to `hermes-profile=<p>` when a profile is given); a running,
too-young, unlabelled, or other-profile container is never selected
(`AC-ISO-F17-10`).

#### NanoClaw sweep item → Hermes owner

There is no single sweep daemon to port. Each item a NanoClaw host `sweep()`
carried maps to a Hermes mechanism owned by a specific requirement row:

| NanoClaw sweep item | Hermes owner | Mechanism |
|---|---|---|
| Inbound `processing_ack` reconciliation | ISO-F11 | webhook delivery-id cache + `hermes kanban create --idempotency-key` (there is no one-for-one inbound ACK table) |
| Outbound at-least-once delivery | ISO-F11 | the delivery-obligation ledger (`gateway/delivery_ledger.py`), swept on startup |
| Stale detection / claim reclaim | ISO-F11 | `release_stale_claims` / `detect_crashed_workers` / `check_respawn_guard`, plus the cron in-flight sweep (`cron/scheduler.py` `sweep_stale_inflight`) |
| Due-message wake + recurrence | SCHED-F32 | the cron ticker over `get_due_jobs` (`cron/jobs.py`); `compute_next_run` anchored on `last_run_at` |
| Crash-loop backoff | ISO-F17 (the gateway) + SCHED-F33 (cron jobs) | Gateway: the start-storm breaker (`gateway/status.py`) applies exponential backoff (5 s base, capped at 300 s) once starts exceed its threshold within its window, and the respawn guard (`gateway/restart_loop_guard.py`) stops replaying auto-resume after 3 restart-interrupted boots that chain within 300 s of each other. Cron jobs: fixed-schedule re-firing with a failure-streak review nudge and auto-pause for structurally unrunnable jobs — not a backoff curve. |

NanoClaw's fixed `0/0/10/30/120/300/900` crash-loop backoff array is **not**
ported as a live retry schedule. Gateway restarts are bounded by the OS
supervisor plus the start-storm breaker's **exponential backoff**
(`gateway/status.py`, capped at 300 s) and the respawn guard; cron jobs re-fire
on their fixed schedule and are nudged or auto-paused rather than backed off. So
the fixed array is *replaced* by supervisor restart + the storm breaker + the
respawn guard, not reimplemented.

#### Recovery drill (operator)

To confirm the supervision stack end-to-end on a booted fleet:

```bash
# 1. Health: the one multiplex gateway is up and serving the roster.
hermes doctor
hermes gateway status

# 2. Kill the gateway process and watch the supervisor bring it back.
#    Linux (systemd):
#    (the default profile's unit has no suffix; a named profile is hermes-gateway-<profile>.service)
systemctl --user kill hermes-gateway.service
systemctl --user status hermes-gateway.service    # Active: active (running) again
#    macOS (launchd) — KeepAlive relaunches unconditionally:
launchctl kill TERM gui/$(id -u)/ai.hermes.gateway

# 3. Confirm the restart re-armed any interrupted session and resumed serving.
#    Each resume_pending session gets exactly one auto-resume turn (reason
#    restart_interrupted); watch the gateway log for that auto-resume, then send
#    a test message to the bot to confirm it answers again.
hermes doctor
hermes logs gateway -f | grep -i "resume"    # follow the GATEWAY log (default is agent.log)
```

A gateway that exits `78` (fatal config) is **not** brought back on
systemd/s6 (`RestartPreventExitStatus=78` / s6 `78 → 125`); fix the config and
restart the unit by hand. On launchd it will relaunch and re-hit the fatal
error until the config is fixed — check the log rather than the exit status.
