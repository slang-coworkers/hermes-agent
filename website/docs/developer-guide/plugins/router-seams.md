---
sidebar_label: "Router Seams"
slug: /developer-guide/plugins/router-seams
title: "Router seams: intercept, gate, observe"
description: "How NanoClaw's router seams map onto Hermes plugin hooks — the pre_gateway_dispatch interceptor, the pre_tool_call gate, and the on_session_* lifecycle observers — and the colon-named gateway event bus they are not."
---

# Router seams: intercept, gate, observe

A NanoClaw operator bolts behaviour onto message routing without editing the router core, through three pluggable interception points: an **interceptor** that can rewrite or drop an inbound before it reaches an agent, a **tool gate** that can veto a tool call, and **session-lifecycle** notifications. Hermes already provides all three as first-class [plugin hooks](/user-guide/features/hooks#plugin-hooks) that a plugin registers with `ctx.register_hook(...)` in its [`register(ctx)`](/developer-guide/plugins) function — no core change is needed to add router behaviour. This page maps each NanoClaw seam onto the Hermes hook that already implements it, names the production code path that emits it, and warns about the one place the names collide (the colon-named gateway event bus, which is a *different* mechanism).

This is a mapping/porting page. The authoritative, per-hook reference — payload fields, directive shapes, timeout classes, worked examples — lives in the [Event Hooks catalog](/user-guide/features/hooks#plugin-hooks); this page cross-links it rather than duplicating it.

## NanoClaw → Hermes mapping

Line numbers are given for the pinned release tag **`v2026.8.31`** (commit `29112bef`) and are authoritative. Where a symbol has been relocated on the moving `main` branch, the `main:` equivalent follows after `|`, resolved as-of **`main @08b140d14`** (the gap-matrix evidence snapshot, 2026-09-07). `main` moves continuously, so on a later checkout locate the symbol by name rather than by the recorded `main:` line. Symbols cited tag-only were not relocated in that evidence.

| NanoClaw router seam | Hermes plugin hook | Emitted by (production path) |
|---|---|---|
| Interceptor — rewrite / drop an inbound before dispatch | `pre_gateway_dispatch` | `GatewayRunner._handle_message` — `gateway/run.py:18141` (fire site `gateway/run.py:18131` \| main: `gateway/run_inbound.py:49`) |
| Tool gate — veto / approve a tool call | `pre_tool_call` | single-fire dispatcher `_dispatch_pre_tool_call_hooks` — `hermes_cli/plugins.py:6831` (hook emitted at `hermes_cli/plugins.py:6635`) |
| Session created / ended / reset | `on_session_start` / `on_session_end` / `on_session_reset` | `agent/conversation_loop.py:1100` / `agent/turn_finalizer.py:833` / `gateway/slash_commands.py:333` |

Every production emit above flows through the shared dispatch entry `hermes_cli.lifecycle.invoke_hook` (`hermes_cli/lifecycle.py:11-22`) → `plugins.invoke_hook` → the active `PluginManager`. Registering the hook on a discovered, enabled plugin is enough; nothing else has to be wired.

The five hook names are members of `VALID_HOOKS` (`hermes_cli/plugins.py:163`, the set closing at `:389`): `pre_tool_call` (`:164`), `on_session_start` (`:219`), `on_session_end` (`:220`), `on_session_reset` (`:222`), `pre_gateway_dispatch` (`:235`). A plugin registers them with `PluginContext.register_hook(name, callback)` (`hermes_cli/plugins.py:3387`) and they are dispatched by `PluginManager.invoke_hook(name, **kwargs)` (`hermes_cli/plugins.py:5564`).

## The interceptor seam — `pre_gateway_dispatch`

`pre_gateway_dispatch` is the earliest plugin interception point for an eligible user-originated message. It fires **once for each non-internal user message that reaches the hook** — after the gateway's own earlier guards have run (profile-route rejection at `gateway/run.py:18085`, ignored-channel filtering at `:18102`, startup-restore queueing at `:18115`) and **before** authorization/pairing and agent dispatch. This is deliberately narrower than the catalog's summary wording ([`hooks.md` `pre_gateway_dispatch`](/user-guide/features/hooks#pre_gateway_dispatch), tag: `:1170` \| main: `:1170`, which says "once per incoming `MessageEvent`"): messages that an earlier guard already dropped never reach the hook, and internal (non-user) events are excluded. In the fleet topology it is therefore the per-webhook-delivery observation point for eligible user messages.

A callback returns a directive dict; the **first recognized action wins**:

- `{"action": "skip", "reason": "..."}` — drop the message; the pipeline short-circuits before authorization and agent dispatch (`gateway/run.py:18165`).
- `{"action": "rewrite", "text": "..."}` — replace `event.text`, then continue.
- `{"action": "allow"}` or `None` — normal dispatch.

The fire site loops the returned actions and applies the first recognized one (`gateway/run.py:18154-18173`; `GatewayRunner._handle_message` def `:18034`). See the catalog for the full payload and directive reference.

## The gate seam — `pre_tool_call`

`pre_tool_call` fires before every **model-originated** tool execution and lets a plugin block or approve the call. The **single production invocation point** is `_dispatch_pre_tool_call_hooks` (`hermes_cli/plugins.py:6831`), which returns `(block_message, modified_args)`; the hook itself is emitted one level down in `_get_pre_tool_call_directive_details` via `invoke_lifecycle_hook("pre_tool_call", ...)` (`hermes_cli/plugins.py:6635`). The three agent tool-execution paths all route through that one dispatcher — `agent/tool_executor.py:647-649`, `agent/agent_runtime_helpers.py:3475-3476`, and `model_tools.py:1435-1436` — so a gate registered once covers every tool the **model** invokes.

It is **not** a universal chokepoint for all tool execution. A plugin that runs a tool directly through `ctx.dispatch_tool(...)` reaches `registry.dispatch` (`hermes_cli/plugins.py:2286` → `tools/registry.py:1128-1153`), which calls the tool handler without firing `pre_tool_call`. The gate governs the model's tool calls; internal / plugin-initiated dispatch bypasses it, so do not treat `pre_tool_call` as a complete enforcement chokepoint for *every* tool execution.

The **first valid `block` or `approve` directive wins**; separate `modify` directives **accumulate** (they are applied before the block/approve gate), and the gate **fails closed on timeout**:

- `{"action": "block", "message": "..."}` — veto the call outright; `message` becomes the tool result the model sees (a `block` must carry a message, or it is ignored).
- `{"action": "approve", "message": "...", "rule_key": "..."}` — **escalate to the existing human-approval gate** (the `[o]nce/[s]ession/[a]lways/[d]eny` prompt), *not* a silent allow; the optional `rule_key` chooses the allowlist grain for an `[a]lways` decision.
- `{"action": "modify", "args": {...}}` — shallow-merge changes into the tool arguments; `modify` directives accumulate across hooks and are applied even when a later hook blocks.

Any other (or observer-only) return value is ignored. See [`hooks.md` `pre_tool_call`](/user-guide/features/hooks#pre_tool_call) (`:529`; hook entry `hermes_cli/plugins.py:164`; directive semantics `hermes_cli/plugins.py:6600-6624`, dispatch/accumulation `:6650-6679`) for the full contract.

## The session-lifecycle observers — `on_session_start` / `on_session_end` / `on_session_reset`

These three are **observer** hooks: their return value is ignored (`invoke_hook` collects only non-`None` results, and an observer returns `None`). They cover NanoClaw's session-created / session-ended / session-reset notifications. Each has a distinct firing condition:

- **`on_session_start`** — emitted by `_restore_or_build_system_prompt` on its **fresh-build path** (`agent/conversation_loop.py:1100`, def `:916`). That path is taken for the first turn of a new session **and** when a continuing session must rebuild from an unusable, empty, or stale stored prompt (`:1078-1092`) — i.e. it also covers stored-prompt recovery. The inline comment at `:1095` reads "not on continuation"; read that as "not on a *normal* continuation that reuses the stored prompt", because the fresh-build path still runs during recovery. Treat `on_session_start` as "a fresh system prompt was built", not strictly "a brand-new session began". See [`hooks.md` `on_session_start`](/user-guide/features/hooks#on_session_start) (`:886`).
- **`on_session_end`** — emitted by `finalize_turn` (`agent/turn_finalizer.py:833`, def `:129`). It fires at the **end of every `run_conversation` turn** (`:828-830`), **not** only at session termination. A plugin that needs to react to real session teardown must not assume this hook means "the session is over". See [`hooks.md` `on_session_end`](/user-guide/features/hooks#on_session_end) (`:927`).
- **`on_session_reset`** — emitted by `_handle_reset_command` (`gateway/slash_commands.py:333`, def `:144`, class `GatewaySlashCommandsMixin`) on the `/new` | `/reset` boundary. See [`hooks.md` `on_session_reset`](/user-guide/features/hooks#on_session_reset) (`:1018`).

## ⚠️ Not these: the colon-named gateway event bus

There is a **separate** drop-in event mechanism whose event names look almost identical — `session:start`, `session:end`, `session:reset` (colon-separated). **These are not the plugin hooks above.** They belong to the gateway event bus (`gateway/hooks.py:1-19`, tag: `gateway/hooks.py:11` \| main: `gateway/hooks.py:5`), where a handler is a drop-in `async def handle(event_type, context)` under `$HERMES_HOME/hooks/<name>/` (`HOOK.yaml` + `handler.py`); the directory is profile-aware via `get_hermes_home()` (`gateway/hooks.py:51`), not a hard-coded `~/.hermes/hooks/`.

Two consequences a plugin author must know:

1. **The buses do not cross.** Emitting `session:start` on the gateway event bus never reaches a plugin's `on_session_start` callback, and vice-versa. The colon-named events and the underscore plugin hooks are dispatched by different code with different registries — this is proven by `test_ac_rt_f09_4` in `tests/hermes_cli/test_router_seams_acceptance.py`.
2. **Neither session-start surface carries `chat_id`.** The colon `session:start` emit carries only `platform` / `user_id` / `session_id` / `session_key` (`gateway/run.py:20589-20594`, tag: `gateway/run.py:20589` \| main: `gateway/run_turn.py:379-384`), and the plugin `on_session_start` **emit site** supplies `session_id` / `model` / `platform` (`agent/conversation_loop.py:1100-1105`); `invoke_hook` adds `telemetry_schema_version` before callback-signature filtering (`hermes_cli/plugins.py:5537-5563`, `:5602-5603`), so callbacks that accept `**kwargs` receive it — neither session-start payload includes `chat_id`. Do not infer chat context from either session-start payload; a plugin that needs `chat_id` at inbound time should read it from the `pre_gateway_dispatch` payload's `event.source` (`event: MessageEvent`; kwargs documented at `hermes_cli/plugins.py:234`).

**For a session-created plugin seam, register the underscore `on_session_*` name.** Use the colon-named bus only for the drop-in `handler.py` mechanism it is designed for.

## Authorization is not a router seam

`pre_gateway_dispatch` can **drop** or **rewrite** an inbound, but it cannot **authorize** one. Authorization stays with the built-in pairing / allowlist gate (`gateway/authz_mixin.py:488` `_is_user_authorized()`, main `:473`; `:966` `_get_unauthorized_dm_behavior()`, main `:546`; `gateway/slash_access.py`) and, in a fleet, with the orchestrator toolset — it runs *after* `pre_gateway_dispatch` (the source-authorization gate is at `gateway/run.py:18184`, main: `gateway/run_inbound.py:185`). An earlier proposal for a pluggable `authorize_source` override at that point was **withdrawn**: there is no plugin seam for authorization, and none should be re-proposed. Use `pre_gateway_dispatch` to filter or transform, and the pairing/allowlist configuration to decide who is allowed.

## Forward-compatibility contract

Router-seam callbacks follow the same additive contract as every plugin hook:

- **Every callback accepts `**kwargs`.** `invoke_hook` signature-inspects each callback and passes the full payload to a `**kwargs` callback, or filters to its declared parameters otherwise (`hermes_cli/plugins.py:5537-5562`); new payload fields are therefore always safe to add. A callback that omits `**kwargs` is flagged as an **error** by `hermes plugins doctor` (`hermes_cli/plugin_dev.py:394-400`), so write `def cb(**kwargs): ...`.
- **A raising callback is swallowed and logged, not propagated** (`hermes_cli/plugins.py:5692-5698`). Do not rely on a raise to signal a decision — return the directive dict (`skip` / `rewrite` / `block` / `approve`) instead. For an observer hook this also means you must assert its **side effect**, never its return value.

## Proof

`tests/hermes_cli/test_router_seams_acceptance.py` is a hermetic acceptance test that loads a fixture plugin through the real discovery path, makes it the active manager, and drives the **production** dispatch paths — the gateway pipeline `GatewayRunner._handle_message` (interceptor, AC-RT-F09-1), `_dispatch_pre_tool_call_hooks` (gate, AC-RT-F09-2), and `lifecycle.invoke_hook` (lifecycle observers, AC-RT-F09-3) — plus the bus-separation check (AC-RT-F09-4). Because Hermes already provides these seams, the test **passes on the stock release tree**; it stands as an executable record of the seam contract and a regression guard.

## References

Release tree `/workspace/extra/hermes-release` @ **v2026.8.31** (commit `29112bef`) — authoritative. Dual `tag: | main:` forms are given where the gap-matrix evidence supplies a `main:` relocation, resolved as-of `main @08b140d14` (2026-09-07); `main` has moved since, so treat the `main:` lines as advisory pointers to a relocated symbol, not current line numbers.

- `hermes_cli/plugins.py:163` — `VALID_HOOKS` (closes `:389`); entries `pre_tool_call :164`, `on_session_start :219`, `on_session_end :220`, `on_session_reset :222`, `pre_gateway_dispatch :235` (semantics comment `hermes_cli/plugins.py:228-234` \| main: `hermes_cli/plugins.py:129-132`)
- `hermes_cli/plugins.py:3387` — `register_hook`; `:5564` — `invoke_hook` (kwarg-filter `:5537-5562`, `telemetry_schema_version` injection `:5602-5603`, exception swallow `:5692-5698`)
- `hermes_cli/lifecycle.py:11-22` — shared production dispatch entry `invoke_hook`
- `hermes_cli/plugin_dev.py:394-400` — hook callback must accept `**kwargs` (doctor error)
- Interceptor: `gateway/run.py:18141` emit in `GatewayRunner._handle_message` (def `:18034`, action loop `:18154-18173`); fire site `gateway/run.py:18131` \| main: `gateway/run_inbound.py:49`; earlier guards `:18085/:18102/:18115`; short-circuit on skip `:18165`
- Gate: `hermes_cli/plugins.py:6831` `_dispatch_pre_tool_call_hooks`; hook emit `:6635`; model-path callers `agent/tool_executor.py:647-649`, `agent/agent_runtime_helpers.py:3475-3476`, `model_tools.py:1435-1436`; gate bypass `ctx.dispatch_tool` → `registry.dispatch` (`hermes_cli/plugins.py:2286`, `tools/registry.py:1128-1153` — no `pre_tool_call`)
- Lifecycle emits: `agent/conversation_loop.py:1100` (`_restore_or_build_system_prompt`, def `:916`; fresh-build path `:1078-1092`); `agent/turn_finalizer.py:833` (`finalize_turn`, def `:129`; every turn `:828-830`); `gateway/slash_commands.py:333` (`_handle_reset_command`, def `:144`)
- Colon-named event bus: `gateway/hooks.py:1-19` (handler `:7`, `HOOKS_DIR :51`); tag: `gateway/hooks.py:11` \| main: `gateway/hooks.py:5`; `session:start` emit without `chat_id` `gateway/run.py:20589-20594`, tag: `gateway/run.py:20589` \| main: `gateway/run_turn.py:379-384`
- Authorization (not a seam): `gateway/run.py:18184` source-authorization gate, main: `gateway/run_inbound.py:185`; `gateway/authz_mixin.py:488` (main `:473`), `:966` (main `:546`); `gateway/slash_access.py`
- Catalog: [Event Hooks](/user-guide/features/hooks) — Plugin Hooks `:361`, shipped catalog `:435`, `pre_gateway_dispatch :1168-1229` (tag: `:1170` \| main: `:1170`), `pre_tool_call :529`, `on_session_start :886`, `on_session_end :927`, `on_session_reset :1018`; [Build a Plugin](/developer-guide/plugins)
- Proof: `tests/hermes_cli/test_router_seams_acceptance.py`
