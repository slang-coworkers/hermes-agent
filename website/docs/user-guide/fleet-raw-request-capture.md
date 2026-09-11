---
sidebar_position: 72
title: "Fleet raw-request capture (on-call runbook)"
description: "How to turn Hermes's built-in raw request capture (HERMES_DUMP_REQUESTS) on across a Bot-Mode fleet: the two activation surfaces, why a secondary profile's .env cannot enable the gateway-served path, the restart requirement, the retention dependency, and the privacy and disk trade-offs."
---

# Fleet raw-request capture (on-call runbook)

This runbook covers how a Bot-Mode fleet turns on Hermes's built-in raw request
capture and keeps the captured evidence on disk. It is scoped to fleets rendered
by the `nv-coworker-compose` plugin (the DEFAULT multiplexer profile plus every
coworker profile).

Capture is Hermes-native: setting `HERMES_DUMP_REQUESTS=true` in a process's
environment makes that process write a `request_dump_<session_id>_*.json` file
beside its session files, secret-redacted, for every provider. There is no
external proxy and no patched binary to install.

## What is captured (and what is not)

Be precise with operators about the scope, so "raw request/response capture"
is not over-read:

- **Captured:** the **preflight request** payload sent to the model provider on
  every turn (when the toggle is on), plus **terminal API-error response
  bodies** — the response from a non-retryable client error or a
  max-retries-exhausted failure, which are dumped **unconditionally** (no toggle)
  so a failure is always diagnosable.
- **Not captured:** **successful response bodies.** The toggle does not record a
  full request+response transcript of a successful turn. A per-session
  request/response viewer keyed by request id is a separate, deferred capability,
  not this setting.

All dumps are redacted with Hermes's forced secret redaction before they touch
disk.

## The two activation surfaces

`HERMES_DUMP_REQUESTS` is read **process-global** (via `os.getenv`), so it must
be present in the environment of whichever process runs the turn. A fleet has
two distinct process families, and each needs the toggle in a different place.

### Surface 1 — the gateway process (gateway-served turns)

Every turn served by the running gateway reads the **gateway process
environment**. That environment is established at gateway launch, so enabling
capture here is an operator/runbook step — it cannot be rendered into a profile
by the compose plugin. Any one of the following sets it, and all of them require
a gateway **restart** to take effect:

1. **The launch/DEFAULT profile's active `.env`.** The gateway is started as
   `hermes -p <launch> gateway run`; `-p <launch>` sets `HERMES_HOME`, and that
   launch profile's active `.env` is loaded into the process environment at
   startup (with override). So adding `HERMES_DUMP_REQUESTS=true` to the
   launch/DEFAULT profile's active `.env` enables capture for every
   gateway-served turn. Gateway-spawned children inherit it.

2. **A systemd service unit.** When the gateway runs under systemd (the unit
   `hermes gateway install` writes), add the variable to the unit:

   ```ini
   [Service]
   Environment=HERMES_DUMP_REQUESTS=true
   # or, to keep it in a file the unit reads:
   EnvironmentFile=/etc/hermes/.env
   ```

   Then `systemctl daemon-reload` and `restart` the service.

3. **A launchd plist (macOS).** Add the variable to the service's
   `EnvironmentVariables` dictionary:

   ```xml
   <key>EnvironmentVariables</key>
   <dict>
     <key>HERMES_DUMP_REQUESTS</key>
     <string>true</string>
   </dict>
   ```

   Then unload/load (restart) the launchd job.

4. **A managed install.** On a package-managed install, set the key in the
   machine-global managed environment file at `/etc/hermes/.env`. This wins over
   a profile `.env` at load and is the correct place when policy is administered
   centrally. A restart is still required.

> **A secondary profile's `.env` cannot enable the gateway-served path.** In a
> Bot-Mode gateway, only the **launch/DEFAULT** profile's `.env` reaches the
> gateway process environment. Every other (**secondary**) multiplexed profile
> runs inside a per-profile **secret scope**: its `.env` is loaded only into that
> scope, never into the gateway's `os.environ`. Putting `HERMES_DUMP_REQUESTS` in
> a secondary profile's `.env` therefore does **not** turn on capture for
> gateway-served turns — use one of the four process-environment surfaces above.

### Surface 2 — separately-spawned coworker processes

Some fleet work runs in short-lived processes spawned outside the gateway loop —
kanban workers (`hermes -p <bot> chat -q`) and the cron bot-chat delivery
subprocess. Each of these loads the **active `.env`** of the invoked coworker
profile into its own environment at startup.

`nv-coworker-compose` handles this surface automatically: on install it upserts
exactly one `HERMES_DUMP_REQUESTS=true` line into each coworker profile's active
`.env` (idempotently — a re-install collapses any prior conflicting assignment to
the one canonical line and never mutates the running process's environment). No
operator step is needed for these surfaces on an unmanaged install; the render
also carries the toggle in each distribution's `.env.EXAMPLE` as the documented
default.

On a **managed install**, the profile write defers to machine policy: if the
managed scope already sets `HERMES_DUMP_REQUESTS` to a truthy value, install
treats the profile as already enabled and writes nothing; if managed policy pins
the key falsy or the install is package-managed, install surfaces an explicit
error instead of silently overriding policy — set `HERMES_DUMP_REQUESTS=true` in
the managed environment (`/etc/hermes/.env`) and re-run onboarding.

> In-process cron **agent** jobs tick inside a secret scope, like secondary
> profiles, and are covered by the gateway process environment (surface 1), not
> by a coworker `.env`.

## Retention dependency (keep the dumps on disk)

Capture is only useful if the `request_dump_*` files survive. Hermes's session
auto-prune sweep deletes `request_dump_*` files alongside pruned sessions, so the
fleet pins `sessions.auto_prune: false` on every rendered profile. That key is
owned by the fleet transcript-retention invariant (**MEM-F44**); this runbook
depends on it and does not re-assert it. If you re-pin the release or run a
config migration, confirm `sessions.auto_prune: false` still holds before relying
on captured evidence.

## Privacy warning

Forced redaction removes **recognized secrets** (API keys, tokens, and similar
patterns) from every dump. It does **not** remove ordinary prompt content, tool
arguments, tool output, or user-authored text — a `request_dump_*` file contains
the full preflight payload. Treat the `sessions/` directory as sensitive:

- Restrict file permissions on the profile `sessions/` directories.
- Do not ship raw dumps off-box without review.
- Enable capture only where you actually need the diagnostic trail.

## Disk warning

With `sessions.auto_prune: false` (required above), the `request_dump_*` files
are **not** garbage-collected — they grow without bound for as long as capture is
on. Operators must:

- **Monitor disk** on the volume holding each profile's `sessions/` directory.
- **Disable capture when done** — set `HERMES_DUMP_REQUESTS=false` (or remove the
  line) in whichever surface enabled it, and restart the affected process.
- **Clean up intentionally** — delete old `request_dump_*` files deliberately
  (they are safe to remove; they are diagnostic artifacts, not session state)
  rather than relying on auto-prune, which is deliberately off.

## See also

- [Fleet transcript retention (on-call runbook)](./fleet-transcript-retention.md)
  — the retention keys (including `sessions.auto_prune: false`) that keep these
  dumps on disk.
