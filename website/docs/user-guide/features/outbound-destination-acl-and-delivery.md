---
title: "Outbound destination ACL and delivery retries"
description: "How Hermes controls which destinations an agent may send to (a native default-deny floor, the nv-fleet-gates wiring predicate, and hosted-room membership) and how it retries deliveries with a crash-safe at-least-once delivery ledger and a persistent dead-target registry."
---

# Outbound destination ACL and delivery retries

This page maps two related messaging behaviours — restricting **which destinations
an agent may send to** (the *outbound destination ACL*) and **retrying failed
deliveries** — onto the Hermes features that already implement them. Both are
native Hermes capabilities; the one fine-grained ACL layer is provided by the
merged `nv-fleet-gates` fleet plugin. **No new plugin, tool, or config key is
introduced by this page.**

Citations use three forms:

- **`tag:`** — `file:line` relative to the pinned release tree (tag `v2026.8.31`,
  commit `29112bef`). Authoritative.
- **`main:`** — the stable `path::symbol` form (function / config key) for the
  same surface on the moving `main` branch, where line numbers drift.
- **`baseline:`** — a fork-only surface (the merged `nv-fleet-gates` plugin) that
  does not exist at the pinned tag; it lives on the fork's merge-target branch
  `release/v2026.8.31-e2e-fixed`.

## Overview

| NanoClaw behaviour | Hermes feature |
|---|---|
| Outbound destination ACL — an agent may reach only the destinations it is permitted to | A native **default-deny floor** (no agent-callable cross-destination send) + the `nv-fleet-gates` **wiring predicate** (fine-grained per-edge) + **hosted-room membership** (roster-scoped `@mention` resolution) |
| Retry failed deliveries with a bounded attempt counter | The native **delivery-obligation ledger** (crash-safe, at-least-once, bounded by `MAX_ATTEMPTS`) + a persistent **dead-target registry** that short-circuits confirmed-unreachable destinations |

The retry half is **stronger** than NanoClaw's `delivery_attempts` counter: it is
a crash-safe at-least-once ledger with an explicit *may-be-duplicate* marker, a
capped attempt budget, startup and adapter-reconnect sweeps, and a persistent
dead-target registry with no NanoClaw counterpart.

## Outbound destination ACL

The covering surface is **room membership plus a wiring predicate**, layered on a
native default-deny floor.

### Default-deny floor (native)

An agent has **no** agent-callable outbound `send_message`: a coworker cannot
decide on its own to fire a cross-destination send. Outbound to arbitrary chats
runs only *outside* the agent loop — cron delivery, `hermes send`, the kanban
notifier. `send_message` is intentionally not registered as a model tool
(`tag: toolsets.py:418`-`:421` comment, absent from `_HERMES_CORE_TOOLS`
`:31`-`:88` and `TOOLSETS` `:103`; `main: toolsets.py::{_HERMES_CORE_TOOLS,TOOLSETS}`),
its shared send engine `_send_to_platform` is pure transport with no
authorization seam (`tag: tools/send_message_tool.py:1112`), and delegated
children are additionally denied it (`tag: tools/delegate_tool.py:55` in
`DELEGATE_BLOCKED_TOOLS`; `main: tools/delegate_tool_toolsets.py::DELEGATE_BLOCKED_TOOLS`).

The one agent-reachable direct-message edge is `message_agent`, which authorizes
on **roster / peer membership only** — there is no per-sender→per-target policy
table (`tag: tools/bot_mode_dm.py:241` `message_agent_tool`, peer check
`:300`-`:303`, roster resolution `:331`-`:333`, unknown-teammate error
`:344`-`:350`; `main: tools/bot_mode_dm.py::message_agent_tool`). A reply to the
originating chat copies that chat's id and thread directly — it is not a policy
target (`tag: gateway/delivery.py:244`-`:251`, the `origin` branch of
`DeliveryTarget.parse`). Only a *bare* platform target with no chat id (e.g. a
`hermes send <platform>` invocation outside the agent loop) resolves to that
platform's configured home channel via `get_home_channel`
(`tag: gateway/config.py:1104`, called at `tools/send_message_tool.py:450`;
`main: gateway/config.py::GatewayConfig.get_home_channel`).

### Fine-grained per-edge layer (wiring predicate)

The merged `nv-fleet-gates` plugin registers the fleet's single `pre_tool_call`
veto, whose **WIRING predicate** enforces wired-only messaging over a
fleet-shared edges store. An attempt to reach a peer the bot is **not wired to**
is **refused** — `action: block`, with the refusal surfaced back to the caller
including the wired-teammates list — *not merely logged*. A `gated` edge instead
returns `action: approve` with `rule_key wire:<from>:<to>`. The predicate reads
`message_agent`→`target` and `kanban_create`→`assignee`
(`baseline: plugins/nv-fleet-gates/__init__.py:322` `_wiring_target`, `:331`
`_wiring_block`, block message `:338`, predicate chain `:444`;
`plugins/nv-fleet-gates/edges.py:36` `add_edge`). Edges are administered with
`hermes wire add|remove|list` (`baseline: plugins/nv-fleet-gates/cli.py:12`
`setup_wire`, subcommands `:16`/`:24`/`:28`). This layer is proven live as the merged criterion
**CH-F51-1** (LOOP-F37) and re-verified hermetically here by AC-CH-F51-2.

### Room-membership layer (native)

A hosted room carries a frozen 2–6 member roster, and an `@mention` is resolved
**only** against that roster — a non-member handle resolves to nothing and cannot
be addressed (`tag: gateway/hosted_room_discussion.py:360` `validate_roster`,
`:494` `resolve_mentions`, `default_all` `:498`;
`main: gateway/hosted_room_discussion.py::{validate_roster,resolve_mentions}`).
Rooms and their membership are created via the gateway `groups.*` JSON-RPC
(`tag: tui_gateway/methods_groups.py:508` `@method("groups.create")`), not
rendered by any plugin.

Together, wired edges plus room membership map NanoClaw's per-agent destination
allow-list onto Hermes. The one gap Hermes core still lacks is described under
[Known limitation](#known-limitation-upstream-ask).

## Delivery retries

Final agent responses are recorded in a durable **delivery-obligation ledger**
(`state.db`) around each platform send, gated by the `gateway.delivery_ledger`
config key (default on; `tag: gateway/delivery_ledger.py:527` `ledger_enabled`).
An obligation moves through `pending → attempting → delivered / failed →
abandoned`. Retries are bounded by `MAX_ATTEMPTS` (default 3;
`tag: gateway/delivery_ledger.py:63`), after which the obligation is abandoned
rather than retried indefinitely (`:354`/`:467` `state='abandoned'`).

Two sweeps drive redelivery, each after a different recovery event:

- **Startup sweep** — `sweep_recoverable` (`tag: gateway/delivery_ledger.py:309`),
  wrapped by `GatewayRunner._redeliver_pending_obligations`
  (`tag: gateway/run.py:12990`). This is the path proven by ISO-F11.
- **Adapter-reconnect sweep** — `sweep_failed_for_runtime`
  (`tag: gateway/delivery_ledger.py:394`), wrapped by
  `GatewayRunner._redeliver_failed_obligations_for_platform`
  (`tag: gateway/run.py:13003`), which performs the actual re-send when a
  platform adapter reconnects (`main:
  gateway/run_startup.py::GatewayStartupMixin._redeliver_failed_obligations_for_platform`,
  moved out of `run.py` on `main`).

Semantics are **honest at-least-once**, not exactly-once. Ambiguity is *labelled*,
never silently re-sent: a redelivered response carries an explicit
*may-be-duplicate* marker — `RECOVERED_MARKER` on startup
(`tag: gateway/delivery_ledger.py:70`-`:73`) or `RECONNECTED_MARKER` on
adapter reconnect (`tag: gateway/delivery_ledger.py:78`-`:81`). Which failures are
eligible for automatic retry is a **fail-closed allowlist**:
`_RUNTIME_RETRYABLE_ERRORS = {"send_path_degraded"}`
(`tag: gateway/delivery_ledger.py:87`). A non-retryable failure (e.g.
`forbidden`) stays `failed` and is not re-sent.

This is stronger than NanoClaw's per-destination `delivery_attempts` counter: the
ledger is crash-safe, survives a restart, and marks ambiguous deliveries rather
than either dropping them or blindly re-sending.

## Dead targets

A destination that returns a **permanent** failure (a forbidden or deleted chat)
is recorded in the persistent `DeadTargetRegistry` (JSON-backed under
`$HERMES_HOME/gateway/dead_targets.json`; `tag: gateway/dead_targets.py:48`
the class, `:62` the store path), and
a subsequent delivery to it is **short-circuited before any adapter call**
(`skipped == "dead_target"`) inside the real `DeliveryRouter.deliver` path
(`tag: gateway/delivery.py:294` `DeliveryRouter`, `:318` `deliver`, `:359`
`"skipped": "dead_target"`; `main: gateway/delivery.py::DeliveryRouter.deliver`).
The dead flag survives a gateway restart — a freshly reconstructed
`DeadTargetRegistry` over the same on-disk store still reports the target dead.

The error taxonomy is **chat-level only**: `_DEAD_ERROR_KINDS =
{"forbidden", "not_found"}` (`tag: gateway/dead_targets.py:40`); a transient
failure (timeout) never marks a target dead, and thread- or message-level
failures never kill a whole chat. Clearing is **not** an automatic probe: a
marked target is short-circuited *before* any send, so its dead flag is cleared
only when a delivery actually reaches the success path (`deliver` clears the flag
on a successful send).

## Known limitation (upstream ask)

Hermes core has **no first-class, profile-scoped outbound destination policy**
consumed at the two agent-reachable outbound dispatch points — `message_agent`
and `kanban_create`. The fine-grained outbound ACL is reconstructed entirely
inside the fleet's `nv-fleet-gates` `pre_tool_call` veto (its WIRING predicate),
because `message_agent` authorizes on roster membership only
(`tag: tools/bot_mode_dm.py:241`-`:350`), `kanban_create`'s `assignee` is passed
through unchecked beyond a non-empty test (`tag: tools/kanban_tools.py:1343`
`_handle_create`, `:1355`-`:1359`, `:1436`-`:1440`), and every native allow-list
is **inbound** (who may message the bot), not outbound
(`tag: gateway/authz_mixin.py:356` `dm_policy`, `:394`/`:420` `group_policy`).
Neither `message_agent` nor `kanban_create` routes through the shared send engine
`_send_to_platform` (`tag: tools/send_message_tool.py:1112`), so a send-engine
hook would not cover them. This is recorded as CH-F51's upstream ask for the
Orchestrator to file: a
profile-scoped outbound destination policy (an `agent_destinations`-style
allow-list, or a documented outbound-authorization callback) consulted natively
at those dispatch points, so a fleet need not rebuild it per-fleet.

Per-destination / per-profile retry-policy tunability (the retry budget is the
module constant `MAX_ATTEMPTS`, gated by the single boolean
`gateway.delivery_ledger`) is tracked separately as follow-up **CH-F51.a**, not
addressed here.

## Acceptance-criteria traceability

The behaviours on this page are verified by
`tests/plugins/test_ch_f51_acceptance.py`:

| Criterion | Behaviour | Test |
|---|---|---|
| AC-CH-F51-2 | Wired-only outbound ACL: an unwired `message_agent` / `kanban_create` destination is refused (`action: block`, surfaced with the wired-teammates list); a wired destination is allowed. Drives the merged `nv-fleet-gates` wiring predicate. | `test_ac_ch_f51_2` |
| AC-CH-F51-3 | A permanent-failure destination is recorded dead (persisting across a registry reconstruction) and a later delivery is short-circuited (`skipped == "dead_target"`) without re-invoking the adapter; a transient failure / distinct target stays deliverable. | `test_ac_ch_f51_3` |
| AC-CH-F51-4 | On reconnect a retryable failed obligation is re-sent with the `RECONNECTED_MARKER` and marked delivered; a non-retryable failure is not re-sent; an obligation at `MAX_ATTEMPTS` is abandoned. | `test_ac_ch_f51_4` |
| AC-CH-F51-5 | Room-membership leg: an `@mention` of a roster member resolves to that member; an `@mention` of a non-member resolves to nothing (`validate_roster` + `resolve_mentions`). | `test_ac_ch_f51_5` |

`AC-CH-F51-3`, `AC-CH-F51-4`, and `AC-CH-F51-5` cover native surfaces and pass on
the pinned release tree; `AC-CH-F51-2` drives the merged `nv-fleet-gates` plugin
and passes on the fork's `release/v2026.8.31-e2e-fixed` baseline. Criterion
CH-F51-1 (the wired-only refusal proven *live*) merged with its carrier LOOP-F37
and is cross-referenced here rather than re-proven.
