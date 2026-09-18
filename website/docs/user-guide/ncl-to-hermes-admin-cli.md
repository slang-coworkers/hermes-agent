---
title: ncl admin CLI → Hermes native admin CLI
description: Maps every NanoClaw admin CLI resource and verb onto the native `hermes` CLI surface — profiles, cron, sessions and the kanban board are adopted verbs, the rest are owned by named requirements or removed by the baseline — and records the one resource with no native CLI analog (room/group administration → the P8 upstream ask).
---

# ncl admin CLI → Hermes native admin CLI

NanoClaw exposed operator administration through `ncl <resource> <verb>`, run over
a `0600` Unix socket on the host (or the in-container `cli_request` transport),
with each verb classified `open | approval | hidden`. It covered `groups`,
`messaging-groups`, `wirings`, `users`, `roles`, `members`, `destinations`,
`dropped-messages`, `sessions`, `tasks`, `policies`, `cost-cap` and
`pr-mappings`.

The Hermes stance is **adopt the native `hermes` verb surface and build nothing
called ncl**. Where Hermes already has a native equivalent this page adopts that
command; the remaining resources are owned by a named requirement, removed by
the baseline, or recorded as gaps. The adopted operations are native built-in
subcommands — the `_BUILTIN_SUBCOMMANDS` allowlist at `hermes_cli/main.py:12405`
carries `profile`, `cron`, `sessions`, `kanban`, `pairing`, `approvals`,
`config`, `gateway`, `webhook`, `secrets`, `egress`, `monitoring`, `insights`,
`plugins` and `skills` (and, notably, **no** `rooms` or `groups` member). Per the
footprint ladder, a capability Hermes already ships is documented and locked with
a guard test, not re-implemented as a plugin.

That verb surface is the **operator's**: it runs on the gateway host or from the
desktop app / dashboard (fleet topology rule 6). A sandboxed coworker's shell has
no `$HERMES_HOME` mount, so those verbs are inert inside a coworker (fleet
topology rule 3); the fleet-admin refusal for the gateway-side tools is the
`nv-fleet-gates` veto by tool name (GOV-F26 / LOOP-F37), whose proof rides
`AC-GOV-F26` on LOOP-F37 — this page does not re-prove scope. For the underlying
entity model (users / groups / wirings / roles as Hermes objects) see the
companion page [Fleet Entity Model](./fleet-entity-model.md) (RT-F01).

**Citation convention.** Every `Native anchor` is a `path:line` at the pinned
tag `v2026.8.31` (`29112bef`), the sole re-verified citation source; where a row
is owned or deferred elsewhere the anchor is that owning requirement id
(`A2A-F18`, `GOV-F25`, `RT-F06`, …). No moving-branch (`main`) line numbers are
cited.

## Resource mapping

| NanoClaw resource / verb | Hermes equivalent | Native anchor | Disposition / owner |
|---|---|---|---|
| groups | hermes profile create, hermes profile list, hermes profile delete | `hermes_cli/subcommands/profile.py:12` build_profile_parser | ADOPTED (native) |
| messaging-groups | hosted rooms + per-platform Bot Chats; backend groups.create RPC only, no stock room-admin CLI or membership-mutation verb | `tui_gateway/methods_groups.py:508` groups.create | GAP (P8, this row) |
| wirings | hermes wire add — bidirectional agent edges in the nv-fleet-gates plugin | `A2A-F18` / `A2A-F19` | OWNED (A2A-F18/F19) |
| users | hermes pairing list, hermes pairing approve, hermes pairing revoke; plus SessionSource identity | `hermes_cli/subcommands/pairing.py:21` pairing list | ADOPTED (native) |
| roles | pairing tiers + the elevated-orchestrator predicate; no separate verb | `GOV-F26` | ADOPTED (GOV-F26) |
| members | per-profile config + the authoritative hosted-room roster; no stock room-membership CLI verb | `gateway/hosted_rooms.py:1414` create_room | GAP (P8, this row) |
| destinations | hermes wire — nv-fleet-gates wire edges | `A2A-F18` / `CH-F51` | OWNED (A2A-F18/CH-F51) |
| dropped-messages | no native analog — the baseline removed the concept: wired-only fail-closed delivery + rooms replace the silent-drop ledger | `RT-F06` | NO ANALOG (RT-F06 DEFER) |
| pr-mappings | PR ownership recorded via report_pr_created | `GOV-F25` | OWNED (GOV-F25) |
| cost-cap | two-tier per-session cost cap; orchestrator-only tool | `COST-F29` / `COST-F30` | OWNED (COST-F29/F30) |
| policies | per-edge approval policy (nv-fleet-gates edges) + hermes approvals suggest, hermes approvals test | `hermes_cli/subcommands/approvals.py:14` (A2A-F19) | OWNED (A2A-F19) |
| tasks | hermes cron create, hermes cron list, hermes cron remove | `hermes_cli/subcommands/cron.py:15` build_cron_parser | ADOPTED (native) |
| sessions | hermes sessions list, hermes sessions export | `hermes_cli/sessions_cmd.py:124` cmd_sessions | ADOPTED (native) |
| board | hermes kanban boards create, hermes kanban boards list, hermes kanban boards rm | `hermes_cli/kanban.py:216` build_parser | ADOPTED (native) |
| mcp-tools | per-profile tools.include config | `GOV-F27` | OWNED (GOV-F27) |
| timezone | hermes config + per-profile cron timezone | `SCHED-F34` | OWNED (SCHED-F34) |
| config / service / migrations / uninstall / doctor | hermes config migrate, hermes gateway install, hermes uninstall, hermes doctor | `hermes_cli/main.py:12405` _BUILTIN_SUBCOMMANDS (OPS-F58.a) | ADOPTED (native) |
| webhook / secrets / egress / monitoring / insights / plugins / skills | hermes webhook, hermes secrets, hermes egress, hermes monitoring, hermes insights, hermes plugins, hermes skills | `hermes_cli/main.py:12405` _BUILTIN_SUBCOMMANDS | ADOPTED (native) |

**Dispositions.** **ADOPTED** — the resource maps onto an existing native
`hermes` verb (or, for `roles`, a native predicate) that this page documents and
the acceptance test locks against regression. **OWNED** — the capability exists
but is delivered and proven by another requirement row, named in the anchor.
**GAP** — no native analog and this row raises the ask (see below).
**NO ANALOG** — the baseline deliberately removed the concept.

## No native analog — the room-administration gap (P8 upstream ask)

In stock Hermes the hosted-room creation API is the authenticated
`groups.create` (`groups.*`) JSON-RPC of the **gateway**-hosted hosted-room
service, which takes an initial **membership** roster. There is **no** built-in
`hermes rooms` / `hermes groups` CLI verb, and at the pinned release `groups.*`
has no stock in-tree client transport caller: the **desktop** app's group chat
is a renderer-owned `profiles.configure` / `ui_meta` projection rather than a
`groups.*` caller, and neither the **dashboard** nor the CLI administers rooms
over this RPC. This is the OPS-F58 **P8** upstream ask (tracked upstream as
**UA-23**).

Evidence at the pinned tag `v2026.8.31` (`29112bef`):

- **Absence proof.** The production top-level `hermes --help` parser lists no
  `rooms` / `groups` verb — asserted by the acceptance test's source-free
  `--help` oracle as `{'rooms','groups'} ∩ live == ∅`. The
  `_BUILTIN_SUBCOMMANDS` allowlist (`hermes_cli/main.py:12405`) is consistent
  with this but is not itself the proof: a missing allowlist entry merely
  triggers a one-time discovery pass (`hermes_cli/main.py:12401`), so absence
  from that frozenset would not, on its own, prove the verb cannot parse.
- **No stock client.** At the pinned release `groups.*` has no in-tree client
  transport caller — the desktop group chat syncs via `profiles.configure` into a
  `ui_meta` projection (`apps/desktop/src/plugins/hermes-bots/group-chat.ts:1012`),
  a cached view, not a `groups.*` caller; the only in-tree invocations of the
  methods are the gateway's own tests.
- **Only surface.** The registered `groups.*` methods
  (`tui_gateway/methods_groups.py:16-34`, `groups.create` at `:508`) delegate to
  `HostedRoomService.create_room` (`tui_gateway/hosted_room_service.py:668`),
  which validates the roster, assigns authority, persists, and wakes the
  runtime; directly wrapping the lower-level
  `gateway/hosted_rooms.py:1414` create_room would bypass that behaviour.
- **Gateway-hosted.** The room worker runs inside the gateway process
  (`gateway/run.py:13348` `_start_hosted_room_worker_sync`,
  `gateway/run.py:13364` `_ensure_hosted_room_worker`), not only the dashboard
  backend.
- **No membership mutation.** The `groups.*` allowlist has no add / remove /
  replace-member operation, and recreating an existing room with a changed
  roster conflicts (`gateway/hosted_rooms.py:1463`
  `RoomConflictError("room_id already exists with different state")`) — so there
  is no membership-mutation path even over the RPC.

**The ask (filed upstream; nothing built here).** Add a built-in `hermes rooms`
(or `groups`) subcommand **over the existing authenticated `groups.*`
control/service layer** for list / create / state / rename / disband, **plus a
generic validated membership-mutation operation** on that layer — never a direct
write to the hosted-room DB, which would bypass roster validation, authority
assignment and the runtime wake. This is a generic CLI-over-service widening, not
a special case for any plugin. RT-F01 recorded this thin spot without raising the
ask; OPS-F58 formally owns it.

## Quick reference

| Task | Hermes command | Native anchor |
|---|---|---|
| Create / list / delete an agent group (profile) | `hermes profile create <name>`, `hermes profile list`, `hermes profile delete <name> -y` | `hermes_cli/subcommands/profile.py:12` |
| Schedule / list / remove a task (cron job) | `hermes cron create "<schedule>" "<prompt>"`, `hermes cron list`, `hermes cron remove <id>` | `hermes_cli/subcommands/cron.py:15` |
| List / export sessions | `hermes sessions list`, `hermes sessions export` | `hermes_cli/sessions_cmd.py:124` |
| Create / list / remove a kanban board | `hermes kanban boards create <slug>`, `hermes kanban boards list`, `hermes kanban boards rm <slug>` | `hermes_cli/kanban.py:216` |
| Manage user pairing | `hermes pairing list`, `hermes pairing approve`, `hermes pairing revoke` | `hermes_cli/subcommands/pairing.py:21` |
| Inspect / test approval policy | `hermes approvals suggest`, `hermes approvals test` | `hermes_cli/subcommands/approvals.py:14` |
| Create a room / manage room membership | *(no native CLI verb — gateway `groups.create` RPC only, no membership-mutation op; P8 / UA-23)* | `tui_gateway/methods_groups.py:508` |
