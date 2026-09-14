---
title: "Porting agent templates and plugin MCP servers"
description: "Map a stamp-once agent-template system (container.json + copy-on-create) and paused recurring tasks onto Hermes profile distributions, and ship a coworker type's MCP servers and skills with it via Agent Plugins v1."
---

# Porting agent templates and plugin MCP servers

If you are coming from an agent-fleet system that authors each coworker **type** once —
a `container.json` describing the type plus a stamp-once copy of source into each new
agent's tree, and recurring tasks that ship **paused** — Hermes already gives you two
native mechanisms that cover the same ground, with no custom code:

1. **[Profile distributions](./profile-distributions.md)** — a coworker type authored once
   as a git-installable package (`distribution.yaml`, `SOUL.md`, `config.yaml`, `skills/`,
   `cron/`, `mcp.json`), installed and re-updated with `hermes profile install` / `update`.
2. **Agent Plugins v1** (see the [plugin developer guide](../developer-guide/plugins/index.md))
   — a portable package (`plugin.json` + `skills/` + `mcp.json`, no Python) that a coworker
   installs disabled and enables per profile, so a type's **skills and MCP servers travel
   with it**.

This page maps the agent-template concepts onto those two mechanisms. Working examples for
both live in the test fixtures `tests/hermes_cli/fixtures/self_f55_coworker_dist/` (a
distribution) and `tests/hermes_cli/fixtures/self_f55_agent_plugin/` (a portable package),
exercised by `tests/hermes_cli/test_self_f55_agent_templates_acceptance.py`.

## The mapping at a glance

| Agent-template concept | Hermes equivalent |
|---|---|
| A coworker **type** authored once | A **profile distribution** repo (or local directory) |
| `container.json`: name / description / metadata | `distribution.yaml`: `name`, `version`, `description`, `author`, `license`, `hermes_requires` |
| `container.json`: model / tool defaults / persona | `config.yaml` (model, providers, tool defaults) + `SOUL.md` (persona) |
| Stamp-once copy of source into a new agent | `hermes profile install <source>` copies distribution-owned files into a new profile |
| Re-stamp / update a type in place | `hermes profile update <name>` re-applies distribution-owned files, preserves user data |
| Required credentials, filled per agent | `env_requires` in `distribution.yaml` → a generated `.env.EXAMPLE`; a real `.env` is **never** shipped |
| Recurring tasks authored **paused** | `cron/jobs.json` with a job authored `enabled: false` |
| MCP servers that belong to the type | `config.yaml:mcp_servers` (live at runtime) **or** an Agent Plugins v1 `mcp.json` (portable, live at runtime) |

## Authoring the distribution

A coworker-type distribution is a directory with a `distribution.yaml` manifest. Only
`name` is required; everything else has a sensible default (see
[Profile distributions](./profile-distributions.md#step-2--add-a-distributionyaml)). The
layout the fleet's compose tooling emits is:

```
self-f55-coworker/
├── distribution.yaml     # manifest: name, version, description, env_requires
├── SOUL.md               # the coworker type's persona / system prompt
├── config.yaml           # model + runtime MCP servers (mcp_servers) + tool defaults
├── skills/
│   └── <cap>/SKILL.md    # capabilities that travel with the type
├── cron/
│   └── jobs.json         # recurring tasks — authored enabled:false (paused)
└── mcp.json              # distribution-owned MCP artifact (see the caveat below)
```

### Credentials: `.env.EXAMPLE`, never a committed `.env`

Declare the credentials a type needs under `env_requires`:

```yaml
env_requires:
  - name: SELF_F55_REQUIRED_TOKEN
    description: "Required API token for this coworker type"
    required: true
  - name: SELF_F55_OPTIONAL_ENDPOINT
    description: "Optional endpoint override"
    required: false
    default: "https://example.invalid/api"
```

On install, Hermes renders a `.env.EXAMPLE` in the installed profile (from a shipped
`.env.template`, or synthesized from `env_requires` when none is shipped). Each installer
copies `.env.EXAMPLE` → `.env` and fills in their own values. A `.env` in the source is
one of the [hard-excluded paths](./profile-distributions.md#whats-not-in-a-distribution-ever)
the installer never copies — credentials stay per-installer and never live in the repo.

### Paused recurring tasks

Author recurring tasks in `cron/jobs.json` (the single store file the scheduler reads) with
`enabled: false`, so the type ships them **paused** the way a template's recurring tasks do:

```json
{
  "jobs": [
    { "id": "…", "name": "nightly-digest", "prompt": "…",
      "schedule": {"kind": "cron", "expr": "0 9 * * *"},
      "enabled": false, "state": "paused" }
  ]
}
```

A job authored `enabled: false` remains paused until the installer explicitly enables it
with the profile-scoped cron commands.

### Install and update ownership

```bash
hermes profile install ./self-f55-coworker --name my-coworker --alias   # local dir or any git URL
hermes profile update my-coworker                                        # re-apply the type
```

`update` re-applies the distribution-owned files (`SOUL.md`, `mcp.json`, `skills/`, `cron/`,
`distribution.yaml`) from the new source. `config.yaml` is distribution-owned but is
**preserved by default** — an installer may have tuned it — unless you pass `--force-config`;
and user-owned data (`.env`, `memories/`, `sessions/`, `auth.json`, state, logs) is **never
touched**. That is exactly the "re-stamp the type, keep the agent's own data" behaviour a
template update needs.

## The two MCP lanes — and which one is live

A coworker type can carry MCP configuration in three locations. Only two are consumed at
runtime, so choose deliberately:

| Where the servers are authored | Live at runtime in v0.21.0? |
|---|---|
| The distribution's **`config.yaml:mcp_servers`** | **Yes** — read by runtime MCP discovery |
| An enabled **Agent Plugins v1 package's `mcp.json`** | **Yes** — merged into runtime MCP discovery, namespaced |
| The distribution's **profile-root `mcp.json`** | **No** — copied as a distribution-owned artifact, but **not read at runtime** |

:::caution The profile-root `mcp.json` is inert at runtime
A profile distribution's top-level `mcp.json` is copied into the installed profile (it is
distribution-owned), but in v0.21.0 nothing reads a profile-root `mcp.json` at runtime, and
the desktop/web MCP editor persists servers to the `config.yaml:mcp_servers` key — not to a
profile-root `mcp.json`. For a coworker type's MCP servers to actually run, author them in
the distribution's **`config.yaml:mcp_servers`**, or ship them as a portable Agent Plugin
(below). Treat the distribution's `mcp.json` as documentation/compatibility only.
:::

### Portable MCP servers that travel with a type — Agent Plugins v1

When you want a coworker type's MCP servers (and skills) to be an installable, enable-per-
profile unit, package them as an [Agent Plugins v1](../developer-guide/plugins/index.md)
portable package: `plugin.json` + `skills/<name>/SKILL.md` + `mcp.json`, no Python.

```json
// plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "self-f55-agent-plugin"
}
```

```json
// mcp.json — exactly {$schema, mcpServers}; servers may reference the package's own paths
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "f55travel": {
      "type": "stdio",
      "command": "echo",
      "args": ["--root", "${PLUGIN_ROOT}/bin/server", "--data", "${PLUGIN_DATA}/state.json"]
    }
  }
}
```

Lifecycle and behaviour:

- **Installed disabled.** `hermes plugins install <source> --no-enable` lands the package
  under `<HERMES_HOME>/plugins/<name>/` and does **not** add it to `plugins.enabled`.
- **Enabled per profile.** Add the package name to `plugins.enabled` in that profile's
  `config.yaml`; the profile then loads it. Different coworker types enable different
  packages.
- **Skills are namespaced.** The package's skill is registered under a deterministic
  `agent-plugin-<slug>-<hash>` namespace, so two types can ship skills with the same short
  name without collision.
- **MCP servers are merged, namespaced, and path-expanded.** Each enabled package's `mcp.json`
  server is merged into runtime MCP discovery under `<namespace>__<server>`. In the server's
  `args`, `env`, and `cwd`, `${PLUGIN_ROOT}` expands to the installed package root and
  `${PLUGIN_DATA}` to a profile-scoped data directory (`<HERMES_HOME>/plugin-data/<namespace>`),
  and both are injected into the server's environment. `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` are
  reserved and must not be set as `env` keys yourself. A native `config.yaml:mcp_servers`
  entry wins on any name collision.

## Pinning a type to an exact version

Ref pinning on `hermes profile install` — `install <source>#<ref>` — is **not** available in
v0.21.0: install shallow-clones the source's default branch, and `.git` is discarded, so no
resolved commit is recorded (`hermes profile info` prints the source string and install
timestamp, not a commit SHA). Until a `source_revision` field lands upstream, pin a type by
**source immutability**, not by a client-side ref:

- Render each release into an **immutable source** the profiles point at — a per-release
  branch (`rel/<date>`) or a release-tag-only mirror — and never rewrite it.
- Resolve the commit **out of band** at render time (`git ls-remote` / `git rev-parse`
  against that ref) and record it in your own release ledger.

(By contrast, the Agent Plugins v1 installer *does* pin: `hermes plugins install --ref <sha>`
takes a full 40-character commit SHA. Portable MCP servers can therefore be pinned today; a
distribution's source cannot.)

## Optional: stricter pre-install hardening

An agent-template loader that rejects **all** symlinks or enforces tighter byte caps has one
property the **Agent Plugin installer** (`hermes plugins install`) does not enforce by
default: its guard already caps a plugin tree (file count, total size, per-file size) and
blocks symlinks that **escape** the plugin directory, but it does not reject in-tree symlinks
outright. That extra strictness is an optional future pre-install check, not a blocker for
this port; add it as a guard extension if your threat model needs it.

## See also

- [Profile Distributions: Share a Whole Agent](./profile-distributions.md)
- [Plugin developer guide](../developer-guide/plugins/index.md) — Agent Plugins v1, namespacing, portable MCP
- [Profile Commands reference](../reference/profile-commands.md#distribution-commands)
