---
title: "Bot Mode delivery follow-up (nv-delivery-redrive)"
description: "Follow an admitted local message_agent delivery, verify it in the target's Bot Chat, and send typed outcome notices when needed. Never re-sends."
---

# Bot Mode delivery follow-up

Core admits a local [`message_agent`](../bot-mode.md#bot-to-bot-messaging) delivery to the target's live Bot Chat
owner and answers with a `delivery_id` and a mailbox receipt. The `nv-delivery-redrive` plugin follows that receipt,
verifies the message in the target's Bot Chat, and reports how the delivery ended, also after the sender's turn is
over. It **never re-sends** a message. It is a fleet plugin: it lives in the fork's `plugins/` tree. To use it as a
standalone plugin, place it under `~/.hermes/plugins/nv-delivery-redrive`.

## What happens to a tracked delivery

1. The sender's `message_agent` call runs exactly as without the plugin: full authorization, then core's own
   delivery. When core admits it (an ack `queued`, `claimed` or `settled` with a receipt), the plugin records a ticket
   and adds one key to core's ack: `"redrive": {"tracked": true, "delivery_id": …, "deadline": …}`. All other fields
   keep core's values.
2. The gateway's kanban dispatcher tick (`kanban.dispatch_interval_seconds`) is the clock. On each tick the plugin
   reads the receipt under the target's profile.
3. A receipt that core ends `failed` or `cancelled` ends the ticket with core's state and reason.
4. A `settled` receipt counts as `settled` only once the target's Bot Chat holds this delivery's message, newer than
   the transcript as it stood when the delivery was sent. An identical earlier message never vouches for a later one.
   Otherwise the delivery ends `ambiguous`/`unknown` as unverified. When the sender's runner completes with a
   `settled` that cannot be verified, its completion is rewritten to that outcome before it is published.
5. At `redrive_window_seconds` an admitted receipt that is still queued or claimed is **not** cancelled, because its
   owner may still run it. The ticket ends `ambiguous`/`unknown`. Only a legacy record that core queued without an
   admission id is cancelled at the bound, and it ends `failed`/`queued_expired`.

A `failed`, `cancelled` or `ambiguous` outcome is reported in one notice to the sender and one in the orchestrator's
Bot Chat. The sender's notice goes into the session that sent the DM, or into the sender's Bot Chat when no session was
recorded. A `settled` outcome is reported to both too, unless core's own completion already carried it to the sender.
Then the plugin sends no notice.

A final notice whose delivery can still change ends with one more sentence:

- while the owner has not settled: `A later outcome will be reported if the owner settles.`
- when the receipt settled but the message is not verified: `A later outcome will be reported if the message becomes verifiable.`

The two notices for those cases read exactly:

```text
Delivery <delivery_id> to @<target> outcome unknown [reason: unknown]: the live owner has not reported this delivery's outcome. Do NOT resend; check their Bot Chat before resending. A later outcome will be reported if the owner settles.
```

```text
Delivery <delivery_id> to @<target>: Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. Check the target before considering another send. A later outcome will be reported if the message becomes verifiable.
```

At most one late notice follows a final one, to both the sender and the orchestrator, and it starts
`Late outcome: delivery <delivery_id> to @<target>`:

- an unverified delivery whose message turns up in the target's Bot Chat while the ticket is kept turns `settled`;
- when the owner settles a delivery that ended `unknown` at the bound, the message is verified within
  `late_verify_seconds`. If it is not found in that window, the late notice says the outcome is still unknown;
- a receipt that core ends `failed` or `cancelled` after the bound is reported with that outcome.

## DMs core answers without tracking

Core answers some DMs itself instead of admitting them: held because another session writer owns the target's Bot
Chat, a receipt that is already `failed` or `cancelled`, or an admission refusal whose outcome is unknown
(`queue_full`, a draining owner). While tracking is live, and once the answered recipient is confirmed to be the
target's own profile, the orchestrator alone gets one notice per `delivery_id`. The sender's tool result stays core's,
byte for byte. The notices read exactly:

```text
Delivery <delivery_id> to @<target> <state> [reason: <reason>]: <error>. The sender has core's own answer; the plugin does not re-send it.
```

```text
Delivery <delivery_id> to @<target> outcome unknown [reason: unknown]: <error>. The sender was told not to resend; the plugin does not track this delivery.
```

A `runtime_unavailable` no-send gets no notice, and neither does an answer whose recipient cannot be confirmed, such
as a DM that a `pre_tool_call` `modify` sent to another profile.

## Requirements

- A gateway with the kanban dispatcher on (`kanban.dispatch_in_gateway: true`, the default). Without a fresh drainer
  heartbeat the plugin tracks nothing, and core's ack and completion pass unchanged.
- `plugins.entries.nv-delivery-redrive.settings.orchestrator_profile` set to an existing profile. If it is unset or
  unknown, the plugin tracks nothing.
- The plugin in `plugins.enabled` of the gateway's launch profile, where the drainer runs, **and** of every sender
  profile, where the send side runs.
- POSIX. The plugin serializes on `fcntl.flock`, so on Windows it registers nothing. It also registers nothing when a
  core name it depends on is missing.

## Settings

All settings live under `plugins.entries.nv-delivery-redrive.settings` in `config.yaml`:

| key | default | meaning |
|---|---|---|
| `orchestrator_profile` | none | required; receives the orchestrator notices |
| `redrive_window_seconds` | `14400` | how long a delivery is followed |
| `late_verify_seconds` | `300` | how long a delivery that ended `unknown` at the bound is verified after its owner settles |
| `heartbeat_stale_seconds` | `180` | the drainer counts as live while its last tick is this recent |
| `retention_seconds` | `604800` | how long a finished ticket is kept |
| `max_attempts`, `retry_backoff_seconds` | `48`, `300` | accepted for older configs and ignored |

State is one sqlite db at `<root>/plugin-data/nv-delivery-redrive/data.db`, under the default profile's home. It
survives gateway restarts.

## Known limits

- **One ticket per `delivery_id`.** A later answer under the same id is not tracked again.
- **The completion rewrite is bounded.** It runs in a `transform_terminal_output` hook. If core's hook bound
  (`plugins.hook_callback_timeout`) runs out first, core's text passes unchanged. A send that is not tracked (a stale
  drainer heartbeat, no valid `orchestrator_profile`) is not rewritten. A re-targeted send usually is not either. It
  still gets the `Could not verify …` completion when, before its bind, another open call of the same task names its
  actual recipient. Its ack stays untracked.
- **Durable work belongs on kanban.** The plugin follows *messages* into the target's Bot Chat. Use
  [Kanban](./kanban.md) for work that must survive as a task.
