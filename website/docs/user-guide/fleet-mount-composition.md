---
sidebar_position: 77
title: "Fleet mount composition (policed docker_volumes)"
description: "How nv-coworker-compose renders each coworker profile's terminal.docker_volumes as an explicit, closed, policed mount set — the workspace rw, the shared-learnings clone, install surfaces ro, nothing else — refuses any mount that reaches the fleet Hermes root, and forces docker_mount_cwd_to_workspace + an explicit per-role container_persistent. Includes the breaking coworker-types.yaml schema migration."
---

# Fleet mount composition (policed docker_volumes)

This page covers **which host paths a coworker's podman sandbox can see** — the
`terminal.docker_volumes` set `nv-coworker-compose` renders on every coworker
profile — and why the render *composes and polices* that set rather than passing
the spec's mounts through. It is scoped to the **coworker** profiles the plugin
renders; the DEFAULT multiplexer profile is the gateway host process and is out of
scope (its tool-bearing work runs on the board/cron in per-profile sandboxes,
which is exactly what this policy governs).

Mount composition is what makes the per-profile sandbox a real boundary: no
profile mounts `$HERMES_HOME`, its own profile dir, or a sibling profile's dir, so
per-profile policy (`config.yaml`, `.env`, `state.db`, role map, toolsets, MCP
includes) stays unreachable to the agent running inside the sandbox. Because the
mount set is rendered — not policed at container launch — the render is the single
place this closed set is decided and validated.

## What the render writes on every coworker profile

`_enforce_mount_composition` runs per coworker type in the render's Phase-B loop,
positioned as the **single final owner** of `terminal.docker_volumes` — after
`_enforce_shared_learnings` (the shared-learnings clone mount, the only earlier
`docker_volumes` writer), so the two compose rather than collide. It writes an
explicit, ordered, **closed** set and forces two more terminal keys:

| key | rendered value | reader |
|---|---|---|
| `terminal.docker_volumes` | the closed set: the profile workspace root mounted **rw** same-path (`<ws>:<ws>:rw`, `<ws> = <workspace_root>/<profile>/workspace`), then the MEM-F43 shared-learnings clone mount when a clone is configured (rw for the orchestrator, `:ro` for every other coworker, at `/mnt/shared-learnings`), then each declared install surface mounted **ro** same-path (`<surface>:<surface>:ro`) — and nothing else | Appended verbatim to `docker run -v` after a colon check, no allowlist (`tools/environments/docker.py`); an entry containing the substring `:/workspace` sets `workspace_explicitly_mounted`. |
| `terminal.docker_mount_cwd_to_workspace` | forced `true` (overwrites a spec `false`) | Read as `auto_mount_cwd` (`tools/terminal_tool.py`); mounts the launch cwd at `/workspace` **only when** no volume already claims `/workspace`. A kanban worker launched with `cwd=<task workspace>` then gets that card's checkout at `/workspace`. |
| `terminal.container_persistent` | **not written by the render** — required from the spec, per role (see below) | Read at `tools/terminal_tool.py`; `true` = persistent bind mounts + a reused container, `false` = a fresh sandbox per card. |

The workspace bind mount is deliberate: a rw host mount makes `_docker_has_host_access`
true (`tools/terminal_tool.py`), so `_should_skip_container_guards` stays false
(`tools/approval.py`) and the dangerous-command approval chain stays **on** inside
the sandbox rather than being waived for containers. Forcing
`docker_mount_cwd_to_workspace: true` (and refusing any `:/workspace` destination
that would suppress it) is what keeps that property live for kanban workers.

## Mandatory fleet-spec keys — a breaking `coworker-types.yaml` migration

Two keys are now **required** and the render **fails closed** without them. This is
a **breaking `coworker-types.yaml` schema change**: a spec written for an earlier
render that omits either key now raises `CompositionError` instead of rendering
with a default.

- **`workspace_root`** (top level, uniform across the fleet) — the parent under
  which each profile's workspace is `<workspace_root>/<profile>/workspace`. An
  **absent, `null`, or blank** `workspace_root` is refused — never treated as
  "skip enforcement" (that fail-open path would leave a coworker with no policed
  workspace mount and silently waive the approval chain). It must resolve
  **outside** the fleet Hermes root and must **not** begin with `/workspace`.
- **`terminal.container_persistent`** (per role, in each type's `config.terminal`
  — or inherited once from a shared spine) — an explicit bool. A profile whose
  resolved config **omits** it, or sets a **non-boolean** value, is refused. It is
  never defaulted, so a boundary role can never silently inherit the core default
  `true`.

An optional top-level **`install_surfaces`** (a list, default `[]`) names shared
host directories mounted read-only into every coworker.

### Minimal migration

```yaml
# top level of coworker-types.yaml — required
workspace_root: /data/coworkers
# optional, read-only shared surfaces
install_surfaces:
  - /data/shared-skills

# per role (in each type's config.terminal, or once in a shared spine) — required
config:
  terminal:
    container_persistent: true   # true: long-lived shared sandbox; false: per-card boundary
```

`workspace_root` (and every install surface) must be an **absolute path**, must
**not** contain a `:` or a `..` component, must **not** begin with `/workspace`,
and must resolve **outside** the fleet Hermes root.

## What the render refuses (fail-closed)

Each refusal raises `CompositionError` naming the profile, the offending key, and
the value:

- A missing/`null`/blank `workspace_root`, a missing/non-bool
  `terminal.container_persistent`, or a non-list `install_surfaces`.
- Any declared mount path (`workspace_root`, an install surface, or a configured
  `shared_learnings_root`) that is not an absolute string, contains `:`, or
  contains `..`.
- Any mount host path that begins with `/workspace` (its volume string would carry
  `:/workspace` and suppress the auto cwd bind), or any completed volume entry that
  still contains `:/workspace`.
- Any mount whose host path **resolves** (symlinks followed) **into or over** the
  fleet Hermes root `get_default_hermes_root()` — the containment check is
  bidirectional, so it rejects a workspace under the root, a sibling profile dir
  `<root>/profiles/<name>`, an ancestor of the root, and a symlink pointing at any
  of them.
- Any pre-existing `terminal.docker_volumes` entry that is not the permitted
  shared-learnings clone mount (recomputed and compared by value) — a
  spec-injected mount is rejected.
- Two install surfaces (or an install surface and the workspace) that resolve to
  the same destination.

### Why the fleet Hermes root, not the active profile home

Containment is checked against `get_default_hermes_root()`, **not**
`get_hermes_home()`. Under the fleet's multiplex layout the render can run with
`HERMES_HOME = <root>/profiles/<name>`, where `get_hermes_home()` returns that one
profile's own home and a sibling `<root>/profiles/<other>` is neither under it nor
above it — so a mount exposing another profile's `config.yaml`/`state.db` would
pass. `get_default_hermes_root()` returns `<root>` in that mode, which contains
every `<root>/profiles/<name>` dir, so one bidirectional check against `<root>`
covers "no path under `$HERMES_HOME`, the profile dir, or their parents".

## Not policed at container launch (by design)

Stock Hermes appends `docker_volumes` entries verbatim after a colon check, with
no host-path resolution and no hook in the `-v` composition path
(`tools/environments/docker.py`). Re-policing the set at every container spawn would
require a generic core widening (a mount-composition seam in the docker backend),
which this row does not take: it is a CONFIGURE row, and the per-profile sandbox
boundary this policy establishes already makes each profile's `config.yaml`
unreachable to its own agent, so there is no in-sandbox actor that can hand-edit the
mount set. Render-time policing of the sets the plugin itself produces is
sufficient and hermetically testable. Launch-time enforcement, if a future release
wants it, is a separate operator-approved core change.

## See also

- [Fleet session driver (per-profile podman sandbox)](./fleet-session-driver.md)
  — the `terminal.backend`/`docker_shared_container_key` keys the same render pins.
- [Fleet shared learnings](./fleet-shared-learnings.md) — the `/mnt/shared-learnings`
  clone mount this closed set preserves (rw orchestrator / ro worker).
- [Docker terminal environment](./docker.md) — the `docker` backend, `-v` handling,
  the `:/workspace` auto-cwd bind, and `container_persistent`.
- [Multi-profile gateways](./multi-profile-gateways.md) — how the DEFAULT
  multiplexer serves coworker profiles in one gateway process.
