---
title: Fleet Entity Model
description: How a NanoClaw-style relational entity model (users, groups, wirings, roles) maps onto Hermes-native objects — profiles under one multiplexing gateway, per-platform allowlists, and the pairing store — with no plugin owning identity.
---

# Fleet Entity Model

A NanoClaw fleet models who-can-do-what as relational tables: `users`,
`messaging_groups`, `wirings`, `agent_groups`, and the two membership/role join
tables `user_roles` and `agent_group_members`. Hermes does **not** re-create
those tables. The same facts already exist as first-class Hermes objects, so the
`nv-coworker-compose` render *configures* them rather than building a parallel
identity subsystem. No plugin owns identity, and nothing authorizes a sender
from a hook — human privilege is the pairing store plus the per-platform
allowlist resolved by the gateway's native `_is_user_authorized`, and agent
privilege is the elevated-orchestrator predicate.

This page maps each NanoClaw entity onto its Hermes object and records where the
mapping is thin.

## Entity mapping

| NanoClaw entity | Hermes object | Notes |
|---|---|---|
| `users` | A `SessionSource` identity — `(platform, user_id)` resolved per inbound message (`gateway/session.py`). | One principal per platform id; no `users` table. |
| `messaging_groups` | The conversations a fleet exposes: Bot Chats, hosted rooms, and the DEFAULT profile's webhook routes. | A group is a surface, not a row. |
| `wirings` | Each profile's own `config.yaml` — engagement rules plus `profile_routes` routing (which profile answers which surface). | Wiring lives in per-profile config, not a join table. |
| `agent_groups` | Coworker **profiles** under one gateway in multiplex mode, named by `gateway.multiplex_profile_allowlist`. | The roster is the allowlist; the render enforces it. |
| `user_roles` | For humans: the `PairingStore` plus the per-platform allowlist (`allow_from`) plus the `slash_access` admin/user command tier. For agents: the elevated-orchestrator / GOV-F26 predicate. | Privilege is resolved live, not stored as a role table. |
| `agent_group_members` | Room membership held in the authoritative `hosted_rooms` store, projected to the desktop `ui_meta`. | Membership is gateway state, not a render artifact. |

## Human privilege

A human's access is decided at intake by the native gateway authorization
path, `_is_user_authorized` (`gateway/authz_mixin.py`), in this order:

1. The per-profile `PairingStore` (an approved, paired principal).
2. The per-platform allowlist. The render writes each coworker's allowlist to
   `config.yaml` at `platforms.<x>.allow_from` (a **per-platform allowlist**,
   never a `.env` secret — user ids are not credentials). At load time Hermes's
   generic shared-key bridge copies that top-level `allow_from` into
   `PlatformConfig.extra`, which the `_is_user_authorized` config fallback
   reads. This is the multiplex-correct path: for a secondary profile the
   platform's YAML→env bridge is skipped, so the config fallback is what
   authorizes that coworker's own users.
3. The global allow-all flag (`GATEWAY_ALLOW_ALL_USERS`), off by default.

The fleet therefore **default-denies** an unknown sender: with no pairing, no
matching `allow_from` entry, and no allow-all flag, `_is_user_authorized`
returns false. The `slash_access` tier layers the admin-vs-user command split on
top of this, but only for slash commands.

## Agent privilege

Agent-side privilege is not a role table either. Only the orchestrator profile
carries the fleet-admin toolset; every other profile is denied admin tools by
the elevated-orchestrator predicate (GOV-F26). Bot-to-bot messaging is Bot Chat
inside the one gateway, so cross-agent privilege is a property of the profile,
resolved live — not a `user_roles` row.

## Thin spots

Two places where the Hermes mapping is genuinely thinner than the NanoClaw
tables, recorded here so operators are not surprised:

- **Operator roles scope per profile, not per (human, project).** A profile is
  admin or not; there is no built-in notion of "user X is an admin of project Y
  but a plain member of project Z". Role scoping is per profile, not per
  (human, project).
- **No stock room-administration surface.** There is no stock, general-purpose
  room administration CLI verb or render surface: `nv-coworker-compose`'s own
  `hermes onboard coworker` performs a one-time `groups.create` for the fleet's
  standing rooms, but nothing renders or administers arbitrary room membership
  afterwards.

## Rooms are a one-time operator bring-up

Rooms are a one-time operator bring-up (the desktop app, or the fleet's `hermes onboard coworker` `groups.create` call), not a per-conversation render artifact, so their membership is verified from the authoritative `hosted_rooms` store — the source of truth the gateway enforces — and never from the desktop `ui_meta` projection, which is only a cached view for the Bots list and can lag. A fleet runbook that checks "is coworker X in room Y?" must read the `hosted_rooms` member set, not the `ui_meta` projection.
