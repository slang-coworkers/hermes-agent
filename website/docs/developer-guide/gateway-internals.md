---
sidebar_position: 7
title: "Gateway Internals"
description: "How the messaging gateway boots, authorizes users, routes sessions, and delivers messages"
---

# Gateway Internals

The messaging gateway is the long-running process that connects Hermes to 20+ external messaging platforms through a unified architecture.

## Key Files

| File | Purpose |
|------|---------|
| `gateway/run.py` | `GatewayRunner` — main loop, slash commands, message dispatch (large file; check git for current LOC) |
| `gateway/session.py` | `SessionStore` — conversation persistence and session key construction |
| `gateway/delivery.py` | Outbound message delivery to target platforms/channels |
| `gateway/pairing.py` | DM pairing flow for user authorization |
| `gateway/channel_directory.py` | Maps chat IDs to human-readable names for cron delivery |
| `gateway/hooks.py` | Hook discovery, loading, and lifecycle event dispatch |
| `gateway/mirror.py` | Cross-session message mirroring for `send_message` |
| `gateway/status.py` | Token lock management for profile-scoped gateway instances |
| `gateway/builtin_hooks/` | Extension point for always-registered hooks (none shipped) |
| `gateway/platform_registry.py` | Adapter registry, factories, and deferred (lazy) loaders for bundled platform plugins |
| `plugins/platforms/<name>/` | Bundled messaging adapters (most platforms: `adapter.py` + `plugin.yaml`) |
| `gateway/platforms/` | Shared `base.py` plus legacy/direct adapters (Signal, API server, webhooks, …) |

## Architecture Overview

```text
┌─────────────────────────────────────────────────┐
│                  GatewayRunner                  │
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │ Telegram │  │ Discord  │  │  Slack   │       │
│  │ Adapter  │  │ Adapter  │  │ Adapter  │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       │             │             │             │
│       └─────────────┼─────────────┘             │
│                     ▼                           │
│              _handle_message()                  │
│                     │                           │
│         ┌───────────┼───────────┐               │
│         ▼           ▼           ▼               │
│  Slash command   AIAgent    Queue/BG            │
│    dispatch      creation   sessions            │
│                     │                           │
│                     ▼                           │
│                 SessionStore                    │
│              (SQLite persistence)               │
└───────┴─────────────┴─────────────┴─────────────┘
```

## Message Flow

When a message arrives from any platform:

1. **Platform adapter** receives raw event, normalizes it into a `MessageEvent`
2. **Base adapter** checks active session guard:
   - If agent is running for this session → queue message, set interrupt event
   - If `/approve`, `/deny`, `/stop` → bypass guard (dispatched inline)
3. **GatewayRunner._handle_message()** receives the event:
   - Resolve session key via `_session_key_for_source()` (format: `agent:main:{platform}:{chat_type}:{chat_id}`)
   - Check authorization (see Authorization below)
   - Check if it's a slash command → dispatch to command handler
   - Check if agent is already running → intercept commands like `/stop`, `/status`
   - Otherwise → create `AIAgent` instance and run conversation
4. **Response** is sent back through the platform adapter

### Session Key Format

Session keys encode the full routing context:

```
agent:main:{platform}:{chat_type}:{chat_id}
```

For example: `agent:main:telegram:private:123456789`

Thread-aware platforms (Telegram forum topics, Discord threads, Slack threads) may include thread IDs in the chat_id portion. **Never construct session keys manually** — always use `build_session_key()` from `gateway/session.py`.

### Two-Level Message Guard

When an agent is actively running, incoming messages pass through two sequential guards:

1. **Level 1 — Base adapter** (`gateway/platforms/base.py`): Checks `_active_sessions`. If the session is active, queues the message in `_pending_messages` and sets an interrupt event. This catches messages *before* they reach the gateway runner.

2. **Level 2 — Gateway runner** (`gateway/run.py`): Checks `_running_agents`. Intercepts specific commands (`/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny`) and routes them appropriately. Everything else triggers `running_agent.interrupt()`.

Commands that must reach the runner while the agent is blocked (like `/approve`) are dispatched **inline** via `await self._message_handler(event)` — they bypass the background task system to avoid race conditions.

## Authorization

The gateway uses a multi-layer authorization check, evaluated in order:

1. **Per-platform allow-all flag** (e.g., `TELEGRAM_ALLOW_ALL_USERS`) — if set, all users on that platform are authorized
2. **Platform allowlist** (e.g., `TELEGRAM_ALLOWED_USERS`) — comma-separated user IDs
3. **DM pairing** — authenticated users can pair new users via a pairing code
4. **Global allow-all** (`GATEWAY_ALLOW_ALL_USERS`) — if set, all users across all platforms are authorized
5. **Default: deny** — unauthorized users are rejected

### DM Pairing Flow

```text
Admin: /pair
Gateway: "Pairing code: ABC123. Share with the user."
New user: ABC123
Gateway: "Paired! You're now authorized."
```

Pairing state is persisted in `gateway/pairing.py` and survives restarts.

## Slash Command Dispatch

All slash commands in the gateway flow through the same resolution pipeline:

1. `resolve_command()` from `hermes_cli/commands.py` maps input to canonical name (handles aliases, prefix matching)
2. The canonical name is checked against `GATEWAY_KNOWN_COMMANDS`
3. Handler in `_handle_message()` dispatches based on canonical name
4. Some commands are gated on config (`gateway_config_gate` on `CommandDef`)

### Running-Agent Guard

Commands that must NOT execute while the agent is processing are rejected early:

```python
if _quick_key in self._running_agents:
    if canonical == "model":
        return "⏳ Agent is running — wait for it to finish or /stop first."
```

Bypass commands (`/stop`, `/new`, `/approve`, `/deny`, `/queue`, `/status`) have special handling.

## Config Sources

The gateway reads configuration from multiple sources:

| Source | What it provides |
|--------|-----------------|
| `~/.hermes/.env` | API keys, bot tokens, platform credentials |
| `~/.hermes/config.yaml` | Model settings, tool configuration, display options |
| Environment variables | Override any of the above |

Unlike the CLI (which uses `load_cli_config()` with hardcoded defaults), the gateway reads `config.yaml` directly via YAML loader. This means config keys that exist in the CLI's defaults dict but not in the user's config file may behave differently between CLI and gateway.

## Platform Adapters

Most messaging platforms ship as plugin adapters under `plugins/platforms/<name>/adapter.py`; a few legacy adapters still live directly in `gateway/platforms/`. All extend `BasePlatformAdapter` from `gateway/platforms/base.py`:

```text
plugins/platforms/                  # plugin-packaged adapters (one dir each)
├── telegram/adapter.py     # Telegram Bot API (long polling or webhook)
├── discord/adapter.py      # Discord bot via discord.py
├── slack/adapter.py        # Slack Socket Mode
├── whatsapp/adapter.py     # WhatsApp Business Cloud API
├── matrix/adapter.py       # Matrix via mautrix (optional E2EE)
├── mattermost/adapter.py   # Mattermost WebSocket API
├── email/adapter.py        # Email via IMAP/SMTP
├── sms/adapter.py          # SMS via Twilio
├── dingtalk/adapter.py     # DingTalk WebSocket
├── feishu/adapter.py       # Feishu/Lark WebSocket or webhook
├── wecom/adapter.py        # WeCom (WeChat Work) callback
├── line/adapter.py         # LINE Messaging API
├── teams/adapter.py        # Microsoft Teams
├── irc/adapter.py          # IRC (canonical scoped-lock example)
├── homeassistant/adapter.py # Home Assistant conversation integration
└── …                       # google_chat, ntfy, photon, raft, simplex, …

gateway/platforms/                  # core base + legacy direct adapters
├── base.py              # BasePlatformAdapter — shared logic for all platforms
├── signal.py            # Signal via signal-cli REST API
├── weixin.py            # Weixin (personal WeChat) via iLink Bot API
├── bluebubbles.py       # Apple iMessage via BlueBubbles macOS server
├── qqbot/               # QQ Bot (Tencent QQ) via Official API v2 (sub-package)
├── yuanbao.py           # Yuanbao (Tencent) DM/group adapter
├── msgraph_webhook.py   # Microsoft Graph change-notification webhook (Teams, Outlook, etc.)
├── webhook.py           # Inbound/outbound webhook adapter
└── api_server.py        # REST API server adapter
```

**Deferred loading:** Bundled `kind: platform` plugins register cheap `register_deferred` loaders in `gateway/platform_registry.py` (via `hermes_cli/plugins.py`) so platform SDKs import only when the gateway starts, delivers, or runs setup/status — not on plain `hermes chat`. Resolution loads one adapter on lookup; full enumeration runs pending loaders only on paths that need every platform.

Experimental connector-backed platforms use the generic relay adapter in `gateway/relay/` instead of a direct platform module. When `GATEWAY_RELAY_URL` or `gateway.relay_url` is configured, the gateway registers the `relay` platform, dials the connector over an outbound WebSocket, and receives `descriptor`, `inbound`, and `interrupt_inbound` frames on that same socket. The connector advertises a `CapabilityDescriptor`; Hermes can send normal outbound replies, token-less `follow_up` operations, and interrupt frames back through the relay. The source-grounded wire contract lives in [`docs/relay-connector-contract.md`](https://github.com/NousResearch/hermes-agent/blob/main/docs/relay-connector-contract.md).

Adapters implement a common interface:
- `connect()` / `disconnect()` — lifecycle management
- `send()` — outbound message delivery
- inbound events are normalized into a `MessageEvent` and forwarded via `handle_message()`

### Token Locks

Adapters that connect with unique credentials call `acquire_scoped_lock()` in `connect()` and `release_scoped_lock()` in `disconnect()`. This prevents two profiles from using the same bot token simultaneously.

## Delivery Path

Outgoing deliveries (`gateway/delivery.py`) handle:

- **Direct reply** — send response back to the originating chat
- **Home channel delivery** — route cron job outputs and background results to a configured home channel
- **Explicit target delivery** — the send engine specifying `telegram:-1001234567890`, exposed via the [`hermes send` CLI](/guides/pipe-script-output) for shell scripts and via cron `deliver:` targets
- **Cross-platform delivery** — deliver to a different platform than the originating message

Cron job deliveries are NOT mirrored into gateway session history — they live in their own cron session only. This is a deliberate design choice to avoid message alternation violations.

## Hooks

Gateway hooks are Python modules that respond to lifecycle events:

### Gateway Hook Events

| Event | When fired |
|-------|-----------|
| `gateway:startup` | Gateway process starts |
| `session:start` | New conversation session begins |
| `session:end` | Session completes or times out |
| `session:reset` | User resets session with `/new` |
| `agent:start` | Agent begins processing a message |
| `agent:step` | Agent completes one tool-calling iteration |
| `agent:end` | Agent finishes and returns response |
| `command:*` | Any slash command is executed |

Hooks are discovered from `gateway/builtin_hooks/` (an extension point — currently empty in the shipped distribution; `_register_builtin_hooks()` is a no-op stub) and `~/.hermes/hooks/` (user-installed). Each hook is a directory with a `HOOK.yaml` manifest and `handler.py`.

## Memory Provider Integration

When a memory provider plugin (e.g., Honcho) is enabled:

1. Gateway creates an `AIAgent` per message with the session ID
2. The `MemoryManager` initializes the provider with the session context
3. Provider tools (e.g., `honcho_profile`, `viking_search`) are routed through:

```text
AIAgent._invoke_tool()
  → self._memory_manager.handle_tool_call(name, args)
    → provider.handle_tool_call(name, args)
```

4. On session end/reset, `on_session_end()` fires for cleanup and final data flush

### Memory Flush Lifecycle

When a session is reset, resumed, or expires:
1. Built-in memories are flushed to disk
2. Memory provider's `on_session_end()` hook fires
3. A temporary `AIAgent` runs a memory-only conversation turn
4. Context is then discarded or archived

## Background Maintenance

The gateway runs periodic maintenance alongside message handling:

- **Cron ticking** — checks job schedules and fires due jobs
- **Session expiry** — cleans up abandoned sessions after timeout
- **Memory flush** — proactively flushes memory before session expiry
- **Cache refresh** — refreshes model lists and provider status

## Process Management

The gateway runs as a long-lived process, managed via:

- `hermes gateway start` / `hermes gateway stop` — manual control
- `systemctl` (Linux) or `launchctl` (macOS) — service management
- PID file at `~/.hermes/gateway.pid` — profile-scoped process tracking

**Profile-scoped vs global**: `start_gateway()` uses profile-scoped PID files. `hermes gateway stop` stops only the current profile's gateway. `hermes gateway stop --all` uses global `ps aux` scanning to kill all gateway processes (used during updates).

## NanoClaw parity: cold-DM cache & outbound addressing

NanoClaw kept a `user_dms` table and an `ensureUserDm(user_id)` resolver with **two-class resolution**: *direct-addressable* platforms (telegram, whatsapp, signal, sms, email, matrix) set `dm_chat_id = handle`; *resolution-required* platforms (slack, discord, teams) open a DM through the platform client (Slack `conversations.open`) and cache the route. A *cold DM* addresses a user the agent has no prior inbound from, so the outbound address is resolved from scratch. Hermes provides the same shape natively — one send-target resolver whose pure grammar splits direct-addressable from resolution-required targets, a per-`(team,user)` cold-DM open-and-cache on Slack's live adapter, a channel/contact discovery cache, and a per-platform default-destination fallback — but does **not** add a `user_dms` route table or a unified `ensureUserDm`: the DM route for a direct-addressable platform *is* the id, and the Slack cold-DM cache is adapter-local (the `send_message` tool / `hermes send` path re-resolves per call, sharing no cache with it).

Each NanoClaw concept maps to a shipped Hermes surface. Citations are given in `tag:` (exact `file:line` on the pinned release) and `main:` (path + symbol on the moving upstream `main`) form:

| NanoClaw | Hermes surface | tag: (`v2026.8.31` @ `29112bef`) | main: (upstream-main snapshot @ `0a8d4caef4`) |
|---|---|---|---|
| Outbound target addressing (one resolver for every caller) | `resolve_send_target` + pure `_parse_target_ref` grammar | `tools/send_message_tool.py:643` (`resolve_send_target`), `:539` (`_parse_target_ref`) | `tools/send_message_targets.py` (`resolve_send_target`, `_parse_target_ref`; resolver relocated on main) |
| Direct-addressable class — `dm_chat_id = handle` (id used verbatim as chat_id) | `_parse_target_ref` returns the target as chat_id: telegram username, email, whatsapp JID, E.164 phone, matrix id | `tools/send_message_tool.py:549-551` (telegram), `:593-596` (email), `:597-602` (whatsapp JID), `:619-624` (E.164), `:633-635` (matrix) | `tools/send_message_targets.py` (same branches) |
| Resolution-required class — Slack markers, DM opened downstream | `_parse_target_ref` Slack branch → `user:<id>` / `user_name:<handle>`, conversation id verbatim | `tools/send_message_tool.py:560-572` (`:567-572` markers, `:564-566` conversation-id passthrough) | `tools/send_message_targets.py` (Slack branch) |
| `ensureUserDm` open + cache the DM route (resolution-required) | `SlackAdapter._ensure_dm_conversation` — opens via `conversations.open` on a cold miss, caches per `(team,user)`, serves cached on hit (bounded insertion-order dict, oldest-first eviction). Adapter-local; the tool path re-resolves per call via `_resolve_slack_user_target`. | `plugins/platforms/slack/adapter.py:2650` (`_ensure_dm_conversation`), `:1184-1185` (`_dm_conversation_cache` + `_DM_CONVERSATION_CACHE_MAX`); tool re-resolve `tools/send_message_tool.py:1903` (`_resolve_slack_user_target`) | `plugins/platforms/slack/adapter.py` (`_ensure_dm_conversation`; same module); tool re-resolve `tools/send_message_senders.py` (`_resolve_slack_user_target`; relocated on main) |
| Channel/contact directory cache | `gateway/channel_directory.py` — on-disk `channel_directory.json` discovery cache, refreshed every 5 min; `resolve_channel_name` (name→id), `load_directory` | `gateway/channel_directory.py:554` (`resolve_channel_name`), `:526` (`load_directory`), `:21` (`DIRECTORY_PATH`); refresh `gateway/run.py:32111` (`CHANNEL_DIR_EVERY = 5`) | `gateway/channel_directory.py` (`resolve_channel_name`, `load_directory`; same module) |
| Default outbound destination (fallback) | `HomeChannel` per-platform default destination carrying `user_id`/`scope_id` provenance; `from_dict`, served by `get_home_channel`, consumed by the send fallback | `gateway/config.py:474` (`HomeChannel`), `:508` (`from_dict`), `:714-718` (`PlatformConfig.from_dict` wiring), `:1104` (`get_home_channel`); consumed `tools/send_message_tool.py:449-467` | `gateway/config.py` (`HomeChannel`, `from_dict`, `get_home_channel`; same module) |
| Who calls it — the outbound consumers (the requirement's "used by") | `hermes send` CLI; cron delivery's home-channel resolution; the gateway kanban notifier + its home-channel subscriptions; the live Slack adapter send that reaches the cold-DM cache | `hermes_cli/send_cmd.py:328` (`cmd_send`); `cron/scheduler.py:2232` (`_get_config_home_channel`), `:2493` (`_resolve_single_delivery_target`); `gateway/kanban_watchers.py:224` (`_kanban_notifier_watcher`); `plugins/kanban/dashboard/plugin_api.py:2058` (`_configured_home_channels`), `:2142` (`subscribe_home`); `plugins/platforms/slack/adapter.py:2918` (`SlackAdapter.send` → `:2935` `_ensure_dm_conversation`) | `hermes_cli/send_cmd.py` (`cmd_send`); `cron/scheduler_delivery.py` (`_get_config_home_channel`, `_resolve_single_delivery_target`); `gateway/kanban_watchers.py` (`_kanban_notifier_watcher`); `plugins/kanban/dashboard/plugin_api.py` (`_configured_home_channels`, `subscribe_home`); `plugins/platforms/slack/adapter.py` (`SlackAdapter.send` → `_ensure_dm_conversation`) |

**Citation provenance.** Every `tag:` coordinate was read directly in the pinned tree (`v2026.8.31` @ `29112bef`); the merge base is that tag plus additional fork commits merged after it (plugin, test, and doc PRs), so a reader on the merge base may find these files shifted a few lines — use `git show v2026.8.31:<file>` for the exact lines. The `main:` cells are a path + symbol snapshot of the moving upstream `main` (`0a8d4caef4`), not the merge base and not an exact line: on that snapshot the resolver has relocated to `tools/send_message_targets.py`, the Slack DM-open helper `_resolve_slack_user_target` split out into `tools/send_message_senders.py`, and cron's home-channel resolution moved to `cron/scheduler_delivery.py` (the `tag:` column keeps the `cron/scheduler.py` locations the merge base ships). The fork's own `main` may still predate these moves; reconfirm against the actual merge base when this doc is revised.

**No agent-initiated cross-platform send (baseline conformance).** Core deliberately does **not** expose `send_message` as a model tool — "agents do NOT get an agent-callable send_message tool — outbound platform messaging is handled outside the agent loop (cron delivery, the gateway kanban notifier, and the `hermes send` CLI)" (`toolsets.py:418-421`). A coworker reaches a human through one of three declared surfaces only: the opt-in Hermes MCP server enabled per profile (GOV-F27), `hermes send` in the profile's own sandbox, or an orchestrator-only plugin-registered tool gated by the veto. Human decision cards do **not** use this path — approvals ride the native approval transport (GOV-F23) and the cost decision rides the approval gate (COST-F30); the resolver here is for nudges and notices.

**Scope & non-goals.** A `user_dms` cache/table and a unified `ensureUserDm` resolver are **not** ported — the DM route for direct-addressable platforms is the id itself, and the Slack cold-DM cache is adapter-local; a shared table would be re-added only if resolution latency ever showed up in traces. `channel_directory.json` is a **discovery** cache (name → id), **not** a per-user DM route — do not describe it as one. For the runtime path these surfaces feed, see the [Delivery Path](#delivery-path) and [Config Sources](#config-sources) sections above.

## Related Docs

- [Session Storage](./session-storage.md)
- [Cron Internals](./cron-internals.md)
- [ACP Internals](./acp-internals.md)
- [Agent Loop Internals](./agent-loop.md)
- [Messaging Gateway (User Guide)](/user-guide/messaging)
