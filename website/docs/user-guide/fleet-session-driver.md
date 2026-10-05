---
sidebar_position: 76
title: "Fleet session driver (per-profile podman sandbox)"
description: "The two terminal keys nv-coworker-compose pins on every fleet profile so each profile runs in its own labelled podman container, why local is banned fleet-wide, and the process-global gateway caveat the pin-move resolves."
---

# Fleet session driver (per-profile podman sandbox)

This page covers how the fleet decides **where a tool call runs** — the terminal
backend each profile resolves — and why `nv-coworker-compose` pins it to the
built-in `docker` backend on every profile it renders. It is scoped to fleets
rendered by the `nv-coworker-compose` plugin (the DEFAULT multiplexer profile
plus every coworker profile).

The fleet standardises on **one execution surface**: every profile runs its tool
calls inside its **own** labelled podman container. Hermes reaches podman through
its built-in `docker` backend pointed at a podman wrapper — no new backend name is
invented, and no NanoClaw-style "session driver" mechanism is ported. What the
render owns is the *configuration* that makes `docker` the resolved backend and
keeps each profile's container identity isolated.

## The two pinned terminal keys

`nv-coworker-compose` writes these two keys into **every** profile `config.yaml`
it renders — the DEFAULT multiplexer and every coworker alike — **explicitly and
never by assumption**. They are written whenever the input spec omits the key or
already sets the required value; a spec that declares a **conflicting** value is
**rejected** — the render fails closed, refusing the whole fleet before a single
profile is written to disk (below).

| key | rendered value | what reads it | why this value |
|---|---|---|---|
| `terminal.backend` | `docker` | Bridged to the process env var `TERMINAL_ENV` (`TERMINAL_CONFIG_ENV_MAP`); the terminal tool resolves the backend from that env var, and `docker` is one of the container backends (`_CONTAINER_BACKENDS`). | Every profile runs in a podman container. The stock schema default is `local` (a no-sandbox host shell); pinning `docker` **explicitly on every profile** is what bans `local` fleet-wide, regardless of which profile happens to seed the gateway's process-global terminal env first. |
| `terminal.docker_shared_container_key` | `""` (empty) | Bridged to `TERMINAL_DOCKER_SHARED_CONTAINER_KEY`, fed into the container-identity label `hermes-profile=<identity>` used for container reuse/orphan cleanup. | **When the key is empty (the default), the identity is the per-turn active profile name** — i.e. per-profile isolation. A **non-empty** key collapses that identity to a single shared container across every profile carrying the same key. Forcing it empty on every profile keeps each profile's podman sandbox isolated. |

The podman wrapper itself is selected by the `HERMES_DOCKER_BINARY` environment
variable (`find_docker()`; podman is a documented drop-in for docker). That is
**fleet-global deployment configuration**, not a `config.yaml` key — `.env` is
secrets-only — so the render does **not** write it. Wiring the wrapper is a
deployment/CRED-F28 concern, handled once for the whole fleet, not per profile.

## What the render refuses (fail-closed, all-or-nothing)

The session-driver pre-pass validates these two keys on every profile before any
profile is written, so a bad value on one profile never leaves a half-rendered
fleet on disk:

- **`terminal.backend`** — only the exact string `docker` is accepted. Every
  other value raises `CompositionError`: the other built-in backends (`local`,
  `ssh`, `modal`, `singularity`, `daytona`, `vercel_sandbox`), the plausible
  operator mistake `podman` (podman is selected through `HERMES_DOCKER_BINARY` —
  the Hermes backend stays exactly `docker`), any other string, and any
  non-string value (a mistyped YAML list or mapping raises `CompositionError`,
  never a raw `TypeError`).
- **`terminal.docker_shared_container_key`** — only an omitted key or the exact
  empty string `""` is accepted. Every other value raises `CompositionError`,
  including a non-empty string **and** falsey non-strings (`false`, `0`, `[]`,
  `{}`, `null`): the config→env bridge serialises those to a non-empty env
  string (`false` → `"False"`), which would silently activate a shared container
  identity.

Each `CompositionError` names the offending **profile**, the dotted **key**, and
the rejected **value**, e.g.:

```
reviewer: terminal.backend must be 'docker' (the fleet's per-profile podman sandbox backend); got 'local'
default: terminal.docker_shared_container_key must be empty; got 'team-shared'
```

Because the check runs as a pre-pass over every coworker type **and** the DEFAULT
profile before any profile is written, an invalid value on the last-resolved
profile (DEFAULT) still leaves **zero** `config.yaml` files on disk — the fleet
is rendered whole or not at all.

## Why every profile, including DEFAULT

The `terminal.*` values are meaningful on the DEFAULT/launch profile, not just on
coworkers. Gateway startup seeds the **process-global** `TERMINAL_*` environment
from the launch profile's config, and the config→env bridge is guarded once per
process — the first profile that bridges freezes those values for the process
lifetime. Forcing `docker` on **every** rendered profile guarantees the frozen
process-global backend is `docker` (never the stock `local`) whatever the bridge
order. It does not collapse container identity: the reuse label stays the
per-turn active profile as long as the shared-container key is empty, which the
render also enforces.

## Where per-profile sandboxes apply today (and the pin-move)

The fleet's durable, tool-bearing work runs on the **kanban board** and on
**cron** — each a separate `HERMES_HOME`-scoped subprocess (`hermes chat -q`
workers, cron chat subprocesses) that bridges **its own** profile's full
`terminal.*` config at its own startup. For that work — where the fleet's real
tool calls live — this render is complete: each profile resolves its own
per-profile podman sandbox, and `local` is banned everywhere.

Turns served **inside the shared multiplexed gateway process** (Bot Chat, rooms,
platform DMs/channels, webhook sessions) are the exception at the current release
pin. At this pin the gateway resolves the terminal backend from the process-global
`TERMINAL_ENV`, and there is no per-session terminal scope to select the active
profile's *full* `terminal.*` configuration per turn (the `tools/terminal_scope.py`
module that provides it exists only on upstream `main`). Two consequences hold
until the operator moves the pin to a tree that carries that module:

1. **Tool-bearing work runs on the board or cron, not in Bot Chat.** This render
   is configuration only — it does not add a runtime cross-profile guard for
   shared-gateway turns, which stay outside the per-profile `terminal.*`
   guarantee until the pin moves. (The runtime sandbox predicate checks backend
   compatibility and task-environment readiness, and separately rejects dangerous
   terminal commands; it does not compare the active profile with the profile
   whose full `terminal.*` configuration was frozen.)
2. **Moving the pin** to a `terminal_scope`-bearing tree is the prerequisite for
   gateway-served per-profile tool calls. Setting `TERMINAL_ENV=docker` in the
   gateway unit is **not** a substitute — every `TERMINAL_*` value is equally
   process-global, so it cannot express per-profile config authority.

This is recorded as an operator pin-move decision, not a code change to Hermes;
the render described here is unaffected by it.

## See also

- [Docker terminal environment](./docker.md) — the `docker` backend, the
  `HERMES_DOCKER_BINARY` override, and container identity/reuse.
- [Multi-profile gateways](./multi-profile-gateways.md) — how the DEFAULT
  multiplexer profile serves coworker profiles in one gateway process.
- [Fleet MCP scope](./fleet-mcp-scope.md) — the companion per-profile isolation
  boundary for MCP servers rendered by the same plugin.
- [Fleet transcript retention (on-call runbook)](./fleet-transcript-retention.md)
  — another set of keys the same render pins on every profile.
