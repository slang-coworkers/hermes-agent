---
title: Per-Profile Config and Multi-Provider Runner
description: Give each coworker its own model, provider, effort, container image, mounts, MCP servers, timezone and toolsets through its own versioned config.yaml — and switch provider or model with no runner to restart.
---

# Per-Profile Config and Multi-Provider Runner

When you run a fleet of coworkers as [profiles](./profiles.md) of one gateway, each
coworker needs its **own** runtime configuration: its model and provider, how hard
it thinks, which container image and mounts its tools run in, which MCP servers it
talks to, its timezone, and which toolsets it exposes. In some other agent runtimes
this lives in a central, mutable "container config" record per group, and changing
the inference backend means switching a separate multi-provider "runner".

Hermes needs neither a central record nor a runner switch. **A coworker's
declarative configuration lives in that profile's own `config.yaml`**, shipped
and versioned as a [profile distribution](./profile-distributions.md). Because session storage is
provider-neutral, changing a coworker's provider or model — even mid-session, even
per kanban card — requires no runner to restart.

This page maps each field of a typical per-group container-config record onto the
Hermes config key that already provides it, and states the honest limitations.

## Mapping: per-group container config → Hermes config

Each row is a field you might expect in a central per-group container-config record,
and the native Hermes config key (or mechanism) that resolves it per profile.

| Per-group container-config field | Hermes config key / mechanism | Reference |
|---|---|---|
| `model` / `provider` | `model.default` / `model.provider` | [Configuring Models](./configuring-models.md) |
| `effort` | `agent.reasoning_effort` (plus per-model `agent.reasoning_overrides`) | [Reasoning Effort](./configuration.md#reasoning-effort) |
| `fastMode` | `agent.service_tier` (what `/fast` toggles) | [Configuration](./configuration.md) |
| fallback runner chain | `fallback_providers` — a top-level list of `{provider, model}` entries | [Fallback Providers](./features/fallback-providers.md) |
| `mcpServers` | `mcp_servers` (per-profile config source) | [MCP](./features/mcp.md) |
| `additionalMounts` | `terminal.docker_volumes` | [Configuration](./configuration.md) |
| `imageTag`, `apt` / `npm` package lists | `terminal.docker_image` — a pinned image the fleet builds and versions | [Configuration](./configuration.md) |
| `timezone` | `timezone` | [Configuration](./configuration.md) |
| `skills` | the profile's own `skills/` directory | [Profile Distributions](./profile-distributions.md) |
| runner switch | **none** — provider-neutral `state.db` plus `/model` hot-swap | [Session Storage](../developer-guide/session-storage.md) |
| per-run / per-card override | kanban per-card `model` / `provider` / reasoning-effort override | [Kanban](./features/kanban.md) |
| ship / revert the config | `hermes profile install` / `hermes profile update --force-config` | [Profile Distributions](./profile-distributions.md) |

Two coworkers installed side by side each resolve **their own** values from their own
`config.yaml`. This is configuration-level separation: each profile's declarative
config resolves independently.
Terminal and MCP *runtime* state remain process-global inside a shared multiplex gateway, as described under [Limitations](#limitations).
The mutable per-group package arrays of a central-record model become a single pinned
`terminal.docker_image` that the fleet builds and versions, rather than per-group data
mutated at runtime.

## How it resolves

Every key above is read from the profile's own `config.yaml`. Selecting a profile with
`hermes -p <name>` points `HERMES_HOME` at that profile's directory, so the loader
reads `$HERMES_HOME/config.yaml` (the same happens for a kanban worker spawned with a
profile-scoped `HERMES_HOME`, or a cron subprocess). Hermes deep-merges that file over
the built-in defaults and resolves the profile's model, providers, `fallback_providers`,
`agent.*`, `terminal.*`, `mcp_servers`, `timezone` and `toolsets` from it. Profile
values override the built-in defaults; a managed-policy overlay, if one is configured,
can in turn override profile values.

## Shipping and reverting config

A profile distribution is just a git repo (or a local directory) with a
`distribution.yaml` manifest and the profile's files, including `config.yaml`:

```bash
# Install a coworker's whole configuration, versioned in a repo
hermes profile install github.com/you/orchestrator-bot --alias

# Later, pull the author's new version. Your local config.yaml is PRESERVED
# by default (you may have tuned the model), so ordinary updates keep your edits:
hermes profile update orchestrator-bot

# Reset the config back to the distribution's shipped version:
hermes profile update orchestrator-bot --force-config
```

`--force-config` is the switch that reverts an edited `config.yaml` to the
distribution's version; without it, `update` preserves your local `config.yaml`.
The `config.yaml` the installer writes is byte-identical to the distribution's, so
two fresh installs of the same source produce the same config. (The written
`distribution.yaml` carries a fresh install timestamp, so reproducibility is scoped
to the `config.yaml` payload, not to a hash of the whole installed tree.) See
[Profile Distributions](./profile-distributions.md) for the full flag set and the
distribution-owned vs user-owned rules.

## Switching provider or model — no runner to restart

There is no separate multi-provider "runner" to switch. Session storage is
**provider-neutral**: a session records its `model` and `model_config` (including
the provider) as data, so switching a live session from provider A to provider B
with `/model` keeps the **same session id** — the conversation continues, and the
session simply resolves provider B afterwards. See
[Session Storage](../developer-guide/session-storage.md) and the `/model` hot-swap
in [Configuring Models](./configuring-models.md). `fallback_providers` adds automatic
cross-provider failover on top, tried in order when the primary fails.

## Per-card overrides

The kanban board can override a task's model, provider and reasoning effort per
card, so a frontier orchestrator can dispatch cheap workers without any of them
changing their profile default. Setting a provider override requires a model
override too (a bare provider is rejected); clearing an override returns the card to
the profile default. See [Kanban](./features/kanban.md).

## Limitations

These are honest facts about this release, not blockers — each runtime-isolation
item below is owned by a separate piece of work; this page describes per-profile
**configuration** and its shipping/versioning.

- **Runtime terminal isolation is process-global.** `terminal.*` resolves per profile
  only when a profile runs as its **own** process (`hermes -p <name>`, a kanban worker
  with a profile-scoped `HERMES_HOME`, or a cron subprocess). Inside a single shared
  multiplex gateway the terminal environment is process-global and frozen on first
  use, so in-gateway turns use the launching profile's terminal config. Per-profile
  in-gateway terminal isolation is separate work.
- **Runtime MCP isolation is process-global too.** MCP discovery and connections are
  established once at gateway start and held in process-global state, so per-profile
  `mcp_servers` in `config.yaml` is the effective **runtime** source for a
  profile-as-process; in a shared gateway the launching profile's MCP servers apply.
  Config-level `mcp_servers` still resolves per profile.
- **A distribution's top-level `mcp.json` is inert at runtime.** The runtime MCP
  source is `config.yaml`'s `mcp_servers` (plus a portable Agent-Plugin's `mcp.json`).
  A distribution-root `mcp.json` is copied as opaque payload; it is not read at
  runtime. Put runtime MCP servers under `mcp_servers` in `config.yaml`.
- **A distribution copies `config.yaml` verbatim — keep secrets out of it.** `install`
  (and `update --force-config`) copy `config.yaml` byte-for-byte, including the whole
  `providers.*` block, and the runtime will honor an **inline** `providers.<name>.api_key`
  when no `key_env` is set. So a credential written directly into `config.yaml` **would**
  ship with the distribution. Keep provider secrets in the profile's `.env` — which
  `install` / `update` never copy — and reference them from `config.yaml` via `key_env`
  (or a `${ENV}` placeholder); never inline a key. Only `.env`-style credentials are
  excluded from a distribution, so a freshly installed profile cannot authenticate a
  provider call until its per-profile `.env` credential is supplied.
- **`hermes profile install` has no `--ref` flag.** Pinning a distribution to an exact
  commit rests on source-branch immutability, not a recorded SHA.

## See also

- [Profile Distributions: Share a Whole Agent](./profile-distributions.md) — how the config ships and reverts
- [Profiles: Running Multiple Agents](./profiles.md) — the per-profile model
- [Configuring Models](./configuring-models.md) — providers, `/model` hot-swap, fallback
- [Fallback Providers](./features/fallback-providers.md) — the `fallback_providers` chain
- [MCP](./features/mcp.md) — `mcp_servers` config
- [Session Storage](../developer-guide/session-storage.md) — provider-neutral sessions
- [Kanban](./features/kanban.md) — per-card model / provider / effort overrides
