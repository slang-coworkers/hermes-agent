---
title: Coworker fleet dashboard
description: How NanoClaw's Pixel Office / Coworkers / Timeline / Admin dashboard views map onto native Hermes surfaces.
---

# Coworker fleet dashboard

Hermes already ships the operator panel a coworker fleet needs. It is **two
native surfaces on one gateway**: the desktop app's **Bot Mode** (the
`hermes-bots` pane) and the FastAPI **web dashboard** (`hermes dashboard`). Under
the one-gateway-per-fleet baseline every coworker is a *profile* of that single
gateway, so the web dashboard's own "machine-level" management surface *is* the
fleet-level panel — one server managing every profile on the machine
(`website/docs/user-guide/features/web-dashboard.md:9`,
`website/docs/user-guide/features/web-dashboard.md:46-47`).

This page maps the four views of NanoClaw's bespoke operator dashboard — **Pixel
Office**, **Coworkers**, **Timeline**, and **Admin** — onto those native Hermes
surfaces, records the one piece that is deliberately not ported, and points at
the upstream ask that a live "Pixel Office" animation would need.

## Citation convention

Each mapping row carries a **dual citation**: a `tag:` pin and a `main:` pin.

- `tag:` — `path:line` in the pinned release **v2026.8.31** (commit
  `29112bef099274229cadff79cdff7bf7b99c4b77`). These are immutable and are the
  authoritative reference.
- `main:` — the same symbol on `upstream/main`, a **moving branch**. The line
  numbers below were re-verified against `upstream/main` **as of commit
  `f5d1926110`** (2026-09-17). (The originating ADR verified an earlier snapshot,
  `upstream/main` @ `8429a54`; `main:` is inherently a moving target, so the
  commit stamp is what keeps it re-verifiable.)

## View mapping

| NanoClaw view | Hermes surface | Citation (dual tag: / main:) |
|---|---|---|
| Pixel Office | Desktop **Bots pane** (`hermes-bots:pane`) — renders the per-profile roster (loaded from `profiles.list`, which returns each profile's `ui_meta['hermes-bots']`) with presence, Bot Chat, Routines and group rooms. Bot Mode as a whole is switched on install-wide by `is_bot_mode_managed`; the roster loads independently. It is not a pixel-art canvas — a Pixel-Office *visual* would be a drop-in dashboard-UI plugin (see Not ported). | gate tag: tools/bot_mode_probe.py:95; main: tools/bot_mode_probe.py:124; pane tag: apps/desktop/src/plugins/hermes-bots/plugin.tsx:359; main: apps/desktop/src/plugins/hermes-bots/plugin.tsx:367; roster tag: apps/desktop/src/plugins/hermes-bots/data.ts:634; main: apps/desktop/src/plugins/hermes-bots/data.ts:642 |
| Coworkers | Web **Profiles page** — create, edit SOUL + model, set-active, rename, delete — plus the desktop Bots roster. This is the roster CRUD of the fleet. | roster API tag: hermes_cli/web_routers/profiles.py:777; main: hermes_cli/web_routers/profiles.py:665; page doc website/docs/user-guide/features/web-dashboard.md:298-307 |
| Timeline | Web **Sessions page** — list, FTS5 search, rename, pin, archive (via `PATCH /api/sessions/{id}`), export, prune, delete — plus the **Analytics page** (tokens and cost by day and by model). The Analytics UI surfaces the figures only when `dashboard.show_token_analytics: true` (default off, a local lower-bound estimate), but the `/api/analytics/usage` API computes them regardless. This is not the live hook-event tool-call feed — see Not ported and Upstream gap. | sessions tag: hermes_cli/web_routers/sessions.py:89; main: hermes_cli/web_routers/sessions.py:166; analytics tag: hermes_cli/web_server.py:15810 (route :15882); main: hermes_cli/web_routers/analytics.py:79; opt-in gate tag: website/docs/user-guide/configuration.md:2710; main: website/docs/user-guide/configuration.md:2900 |
| Admin | Web **System page** (gateway start/stop/restart, doctor / audit / backup / restore / migrate, shell-hook management) plus the **Cron**, **Skills** and **Pairing** pages. The pairing (admission) flow is a real operator state transition served here. | pairing tag: hermes_cli/web_server.py:13905; main: hermes_cli/web_routers/ops.py:76; cron tag: hermes_cli/web_routers/cron.py:63; main: hermes_cli/web_routers/cron.py:212 |

**Supplementary citations** (secondary symbols the rows above reference; `main:`
lines re-verified against `upstream/main` @ `f5d1926110`):

- Archive verb — tag: hermes_state.py:10389 `set_session_archived`; main: hermes_state_sessions.py:849.
- Models analytics — tag: hermes_cli/web_server.py:15894 (route :16070); main: hermes_cli/web_routers/analytics.py:231.
- `ui_meta` persist — tag: tui_gateway/methods_profiles.py:780; main: tui_gateway/methods_profiles.py:465; readback tag: tui_gateway/methods_profiles.py:304; main: tui_gateway/methods_profiles.py:233.
- Hosted-room contract — tag: tui_gateway/methods_groups.py:17; main: tui_gateway/methods_groups.py:19; create tag: tui_gateway/hosted_room_service.py:668; main: tui_gateway/hosted_room_service.py:440.
- Bot-Mode protocol injection — tag: agent/system_prompt.py:748; main: agent/system_prompt.py:343 (called at :635).

## The `ui_meta['hermes-bots']` gate

The Bots roster and the Bot-Mode teammate protocol both key off a per-profile
`ui_meta['hermes-bots']` block in the profile's `profile.yaml` (LOOP-F35). A
profile that carries the block flips the install-wide `is_bot_mode_managed`
switch **on**; a profile without it does not — that switch is what gates the
Bot-Mode protocol/tooling for the whole install, while the roster itself is
loaded separately from `profiles.list`.

`profile.yaml` is **not** in the default distribution-owned set
(`DEFAULT_DIST_OWNED` is `SOUL.md`, `config.yaml`, `mcp.json`, `skills`, `cron`
and the manifest — `hermes_cli/profile_distribution.py:88`), but it is **not
hard-excluded** either: `USER_OWNED_EXCLUDE`
(`hermes_cli/profile_distribution.py:101`) does not name `profile.yaml`, so a
**legacy distribution** with no `distribution_owned` list copies every staged
entry outside that exclude set, and a custom `distribution_owned` allowlist may
name `profile.yaml` explicitly (`hermes_cli/profile_distribution.py:576`). A
distribution therefore *can* carry the block — it is not guaranteed
desktop-only. In the normal desktop flow, the block is written via the
`profiles.configure` JSON-RPC and read back on `profiles.list`.

- Probe / gate — tag: tools/bot_mode_probe.py:60 (`_is_bot_managed`), tools/bot_mode_probe.py:95 (`is_bot_mode_managed`); main: tools/bot_mode_probe.py:124.
- Persist — tag: tui_gateway/methods_profiles.py:780; main: tui_gateway/methods_profiles.py:465.
- Read back — tag: tui_gateway/methods_profiles.py:304; main: tui_gateway/methods_profiles.py:233.

## Room administration — the `groups.*` thin spot

Room (group-chat) membership has **no standalone web-dashboard page and no
`hermes` CLI verb**. The hosted-room contract is the gateway's `groups.*`
JSON-RPC method set, and the desktop Bot Mode is what renders and manages group
chats. Room creation is therefore a one-time operator step performed in the
desktop Bot Mode UI, re-checked by a runbook item (RT-F01 records the thin spot;
A2A-F18 carries the acceptance criterion).

- Contract — tag: tui_gateway/methods_groups.py:17-34; main: tui_gateway/methods_groups.py:19.
- Create room — tag: tui_gateway/hosted_room_service.py:668 (`create_room`); main: tui_gateway/hosted_room_service.py:440.

## Not ported

These NanoClaw pieces are deliberately **not** ported, because Hermes either
already provides the capability natively or the piece is NanoClaw-specific
plumbing:

- `POST /api/hook-event` — the generic hook-event telemetry ingest. Hermes has no
  equivalent generic ingest; see Upstream gap below.
- The NanoClaw **dashboard app** itself — Hermes ships its own web dashboard and
  desktop app, so the whole dashboard application is not re-implemented.
- **`dashboard-as-a-channel`** — a browser/desktop chat surface already exists in
  Hermes, so dashboard-as-a-channel adds nothing.
- The **codex hook bridge** — the NanoClaw-side codex hook bridge is NanoClaw
  plumbing with no Hermes analogue.

A Pixel-Office-style live *visual*, if ever wanted, is a drop-in dashboard-UI
plugin — `dashboard/manifest.json` + a JS bundle + `dashboard/plugin_api.py`, the
pattern the bundled kanban plugin already follows
(`plugins/kanban/dashboard/plugin_api.py:2977`;
discovery `website/docs/user-guide/features/extending-the-dashboard.md:807`) —
never a service of its own.

## Upstream gap (P8 / UA-18)

A live "Pixel Office" animation or a hook-fed Timeline tool-call feed would need
a **host-owned, authenticated bridge** from Hermes's in-process event buses to
browser/desktop dashboard clients — which does not exist today. Hermes has the
buses (`PluginContext.emit` / `subscribe` at hermes_cli/plugins.py:3491 / :3543;
the process-wide `MonitoringEmitter` at agent/monitoring/emitter.py:53), and
`VALID_HOOKS` (hermes_cli/plugins.py:163) lets a plugin observe the lifecycle —
but there is no path from a hook callback to a live dashboard event source, and
no `PluginContext.register_dashboard_event_source` seam
(`register_dashboard_auth_provider` at hermes_cli/plugins.py:2455 is auth-only).
The only core-owned server→browser pub/sub, `/api/pub` + `/api/events`, is
**chat-scoped** (comment hermes_cli/web_server.py:17646; `events_ws` at
hermes_cli/web_server.py:17685; main: hermes_cli/web_routers/chat_ws.py:620). A
plugin can still stand up its own authenticated WebSocket (kanban does), but then
it owns both ends of its socket.

This gap is recorded as the **P8 upstream ask, filed as UA-18** (operator-owned).
It is not built in this fork; this page documents it so the mapping is complete.
