---
sidebar_position: 8
title: "Per-Agent File Memory & Persona (Fleet)"
description: "How a fleet of Hermes coworkers keeps per-profile memory and persona, and how to migrate an OKF memory/ tree onto the native SOUL.md / AGENTS.md / MEMORY.md / USER.md homes."
---

# Per-Agent File Memory & Persona

When you run a fleet of coworkers as [profiles](/user-guide/profiles) of one gateway
(see [Bot Mode](/user-guide/bot-mode)), each coworker needs its own identity, its own
standing instructions, and its own curated volatile memory — with no leakage between
profiles. Hermes already provides every store this needs; the fleet does not introduce
a new memory system. This page maps the file-based memory layout some agents carry as an
**OKF** (`memory/index.md`, `memory/system/definition.md`, dossiers, a persona prepend)
onto Hermes' native per-profile homes, gives the one-time migration runbook, and
records how the [`nv-coworker-compose`](/user-guide/fleet-entity-model) render keeps the
memory configuration safe for unattended bots.

## How isolation works

Every store below resolves under the profile's **Hermes home** (`HERMES_HOME`):
`MEMORY.md` and `USER.md` live in `<home>/memories/`, and the persona `SOUL.md` lives at
`<home>/SOUL.md`. Two profiles with distinct homes never share a file, so a memory
written under profile A's home is invisible to profile B. This is the same per-profile
scoping described in [Persistent Memory](/user-guide/features/memory) — the fleet simply
gives every coworker its own home.

## OKF → native mapping

An OKF `memory/` bundle is one Markdown concept per file, opened from a root
`index.md`. Each OKF role maps onto a Hermes-native home:

| OKF source | Native home | Notes |
|---|---|---|
| Persona prepend / agent identity | `SOUL.md` | Loaded as the slot-#1 stable identity, per profile, and [security-scanned](/user-guide/features/memory#security-scanning) before it reaches the prompt — an injection payload is blocked, not appended. |
| `memory/index.md` and role-scoped dossiers | `AGENTS.md` chain | Standing, role-scoped instructions belong in the git-versioned workspace `AGENTS.md` chain, not a private memory tree — they are reviewable and travel with the repo. |
| `memory/system/definition.md` (how memory behaves) | `MEMORY.md` + `USER.md` semantics | The OKF "how memory works" definition maps onto Hermes' built-in memory tool and its two curated files; there is no separate definition file to carry over. |
| Curated volatile agent notes | `MEMORY.md` | The agent's personal notes — environment facts, conventions, lessons — one per profile. |
| Curated volatile user profile | `USER.md` | The user profile — preferences, communication style — one per profile. |

The persona prepend and the dossiers are the two OKF artifacts with a genuinely
different native home (`SOUL.md` and the `AGENTS.md` chain, respectively); the volatile
`MEMORY.md` / `USER.md` files are a near one-to-one carry-over.

## Fleet memory configuration

The `nv-coworker-compose` render enforces a small memory policy into **every** rendered
profile's `config.yaml` (each coworker and the DEFAULT multiplexer), so a re-pin or a new
coworker type can never silently ship the cramped stock configuration:

- `memory.memory_char_limit` and `memory.user_char_limit` are floored up to the fleet
  baseline (4000 / 2000, above the stock defaults) — a type that legitimately declares a
  higher value keeps it, per key and never clamped down.
- `memory.write_approval` is forced **off**. Coworkers and the multiplexer are
  unattended: a memory write must not stall waiting for a human approval prompt. (For an
  attended, single-user agent, see [Controlling memory writes](/user-guide/features/memory#controlling-memory-writes-write_approval).)

These keys are read by the memory store and the write-approval gate at runtime, and they
are written explicitly rather than left to defaults.

### Surviving `hermes config migrate`

Because the render writes the memory keys **explicitly**, they survive
`hermes config migrate`: the migration ladder rewrites `config.yaml` through
`save_config`, and an explicitly-present key is preserved with its value and type even
when that value equals a schema default. A profile that was rendered with the raised
caps and `write_approval: false` therefore keeps them after a version bump — the render,
not the migration, is the source of truth for the memory policy.

## Migration runbook

One-time move of an existing OKF `memory/` tree into the native homes for a profile.
Do this once per coworker when onboarding it into the fleet; afterwards the agent
maintains `MEMORY.md` / `USER.md` itself.

1. **Identity → `SOUL.md`.** Take the persona prepend (the identity text at the top of
   the OKF bundle) and write it to `<profile home>/SOUL.md`. Keep it concise: it is
   prepended as the slot-#1 identity and is security-scanned on load. In a fleet, this
   file is produced by the compose render from the coworker type's `identity`.
2. **Dossiers + `index.md` → `AGENTS.md`.** Move role-scoped standing instructions and
   the OKF `index.md` map into the workspace `AGENTS.md` chain so they are git-versioned
   and reviewed, rather than living in a private memory tree.
3. **Volatile notes → `MEMORY.md` / `USER.md`.** Copy durable agent notes into
   `<home>/memories/MEMORY.md` and user-profile facts into
   `<home>/memories/USER.md`, trimming each to fit its char limit (the raised fleet
   limits give more headroom than the stock defaults). Drop anything that is stale or
   better expressed as a standing instruction.
4. **Retire the OKF `system/definition.md`.** Hermes' built-in memory tool supplies the
   "how memory works" behavior, so the OKF `system/definition.md` has no native file —
   discard it once its durable facts have been folded into steps 1–3.
5. **Verify isolation.** Start the profile and confirm its memory loads from its own
   home only. A sentinel written under one profile's home must not appear under another
   profile's home.

## See also

- [Persistent Memory](/user-guide/features/memory) — the per-profile `MEMORY.md` /
  `USER.md` stores and the `memory` tool.
- [External Memory Providers](/user-guide/features/memory-providers) — the escape hatch
  when file memory is not enough.
- [Profiles](/user-guide/profiles) and [Bot Mode](/user-guide/bot-mode) — how a fleet of
  coworkers runs as profiles of one gateway.
