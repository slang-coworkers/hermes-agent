---
title: "Bot Mode re-drive (nv-delivery-redrive)"
description: "Retry a message_agent delivery automatically when the target Bot is busy: one owner in the gateway, bounded re-attempts under the same delivery_id, and one typed final outcome to the sender and the orchestrator."
---

# Bot Mode re-drive

A local [`message_agent`](../bot-mode.md#bot-to-bot-messaging) delivery fails with `target_busy` when the target
Bot is busy. Core's delivery runner waits on the target's turn lock for `bot_mode.turn_wait_seconds` (default 120 s).
If the lock is still held, the runner reports `target_busy` and exits. The same reason is reported when the target's
Bot Chat is open on another surface. Without this plugin nobody tries again, and a hand-off can sit idle until a human
relays it.

The `nv-delivery-redrive` plugin gives that delivery an owner that outlives the sender. The owner re-drives the
delivery once the target is free and reports one typed final outcome. It is a fleet plugin: it lives in the fork's
`plugins/` tree. To use it as a standalone plugin, place it under `~/.hermes/plugins/nv-delivery-redrive`.

## What happens on `target_busy`

1. The sender's `message_agent` call runs exactly as before: full authorization, then core's own delivery. The plugin
   only observes the call. It records a ticket and adds one key to core's ack:
   `"redrive": {"tracked": true, "delivery_id": …, "deadline": …}`. All other fields keep core's values.
2. When core's runner reports `target_busy`, the sender's completion notification reads
   `status: queued` under the same `delivery_id`, with "Queued for automatic re-delivery … do NOT resend". The
   "retry shortly" and "Try again later" text is removed, because a manual resend would create a second message under
   a new id.
3. The gateway's kanban dispatcher tick (`kanban.dispatch_interval_seconds`, default 60 s) is the clock. On each
   tick the plugin re-starts **core's own delivery runner** on the first attempt's DM file. Core therefore derives
   the same `delivery_id`, uses the same turn lock and retry policy, and admits the message to a live Bot Chat owner
   when one appears.
4. The message lands in the target's Bot Chat **at most once**. Before any re-drive, the plugin reads the target's
   transcript read-only and checks for the message there. It never re-sends while an earlier attempt might still be
   writing, or while the transcript cannot be read.
5. The final outcome arrives as one delivery notice in the sender's Bot Chat and, for anything other than `settled`,
   in the orchestrator's Bot Chat. A notice reads `Delivery <id> to @<target> <outcome>: … Do NOT resend.`

Possible outcomes, all from the closed reason vocabulary:

| outcome | meaning |
|---|---|
| `settled` | the message landed: the target's Bot Chat holds it as a new user message; the reply follows |
| `failed` / `queued_expired` | the target stayed busy for the whole window; the message was **not** delivered |
| `failed` / `<reason>` | the target's turn ran and failed (provider error, …); not retried |
| `cancelled` / `runtime_offline` | a live Bot Chat owner left before claiming it, and core cancelled the receipt |
| `ambiguous` / `unknown` | the outcome cannot be proven (an attempt still running at the bound, an unreadable transcript); check the target's Bot Chat before resending |

A target turn can report success without its message ever reaching the target's Bot Chat. A live owner settles from
its turn status even when persisting the message failed, and a text-only CLI turn whose writes failed still prints
its reply and exits 0. So a reported success counts as `settled` only once the target's Bot Chat holds this
delivery's message, newer than the transcript as it stood when the delivery was sent. Otherwise the sender's
completion is rewritten before it is published, and the notices read exactly:

> Delivery `<id>` to @`<target>`: Could not verify your message in the target's Bot Chat; delivery outcome is
> unknown. Do NOT resend. Check the target before considering another send.

An identical earlier message never vouches for a later one. If the message turns up in the target's Bot Chat
afterwards, while the ticket is still kept, the delivery turns `settled` and one more notice says so.

Once a delivery reaches a live Bot Chat owner (Desktop or TUI), it is **followed, never re-sent**. The plugin watches
the mailbox receipt past core's 30-minute runner limit and reports how it ends. Its one action on a receipt is at the
window's end: it cancels a receipt that is still `queued` under the mailbox lock, which ends it as
`failed`/`queued_expired`.

## Requirements

- A gateway with the kanban dispatcher on (`kanban.dispatch_in_gateway: true`, the default). Without a live drainer
  heartbeat the plugin tracks nothing, and Hermes behaves exactly as without it.
- `plugins.entries.nv-delivery-redrive.settings.orchestrator_profile` set to an existing profile. If it is unset or
  unknown, the plugin logs one error and tracks nothing. No delivery is promised a re-drive whose failure could not
  be reported.
- The plugin in `plugins.enabled` of the gateway's launch profile, where the drainer runs, **and** of every sender
  profile, where the send side runs.
- POSIX. Core's turn lock needs `fcntl`, so on Windows the plugin registers nothing.

## Settings

All settings live under `plugins.entries.nv-delivery-redrive.settings` in `config.yaml`:

| key | default | meaning |
|---|---|---|
| `orchestrator_profile` | none | required; receives every non-`settled` outcome |
| `redrive_window_seconds` | `14400` | a ticket's total bound |
| `max_attempts` | `48` | CLI attempt cap, attempt 1 included |
| `retry_backoff_seconds` | `300` | minimum gap between attempts |
| `heartbeat_stale_seconds` | `180` | the drainer counts as live while its last tick is this recent |
| `retention_seconds` | `604800` | how long a finished ticket is kept |

`bot_mode.turn_wait_seconds` stays core's key. Each re-drive waits on the turn lock for that long, never longer.

State is one sqlite db at `<root>/plugin-data/nv-delivery-redrive/data.db`, under the default profile's home. It
survives gateway restarts: re-drive runners run outside the process registry, so a restart does not kill them.

## Known limits

- **Send order.** Re-drives of messages that were refused busy run in send order per sender → target. Three cases can
  still overtake:
  - a message sent after the target frees up, but before the drainer's next re-drive, lands first;
  - two first attempts racing on core's turn lock land in either order, as they do without the plugin;
  - a first attempt whose runner died while its target CLI kept running can be overtaken while that orphan finishes.

  Each message still lands exactly once.
- **Two copies of a failure.** When core's own completion already carried a typed failure, the sender also gets the
  plugin's notice for it. A carried `settled` reply is never repeated.
- **Publication gap.** If core fails to publish a completion after it has written the result, the sender's notice
  arrives only after the next gateway restart. The orchestrator still gets every non-`settled` outcome.
- **Unverified success, three gaps.** The rewrite above is not universal:
  - a sender that cannot receive completion notifications (an API server session, a one-shot run, a kanban worker)
    first gets core's raw `settled` row, then the plugin's correction notice on a later tick;
  - the rewrite runs inside a hook bounded by `plugins.hook_callback_timeout`. If the hook fails or runs out of time
    first (a timeout below 10 s does), core's text passes unchanged;
  - an untracked send (no fresh drainer heartbeat, no valid `orchestrator_profile`) is never rewritten.
- **Skipped rewrites.** Core runs one copy of the rewrite per call at a time and keeps its own text when it skips one.
  Each `message_agent` call carries its own call id, so two deliveries finishing together are both rewritten. Five
  paths can still feed the sender agent core's raw `settled`:
  - a process watcher armed after its launching turn, at a gateway restart or before the gateway was serving;
  - that watcher's chat receipt while the launching turn is still busy (it shows output only on a nonzero exit);
  - a kill or reconcile from a context with no call id: gateway shutdown, `/stop`, a goal's process listing;
  - any call within the window after one rewrite timed out (60 s, longer while timed-out workers are still hung);
  - a rewrite whose worker thread fails to start.

  A tracked delivery still unverified at a drainer tick then ends `ambiguous`/`unknown`, with one correction to the
  sender and one to the orchestrator, and is not re-sent. A verified or untracked call gets no such correction.
- **Unpublished exits.** The gateway waits for a finished process to publish its result before it reports it. If the
  result has not been published 60 s after the exit, the chat gets one notice that the outcome is unknown, without any
  output; the normal report follows once the result is published.
- **Re-targeted sends.** A `message_agent` call whose recipient a `pre_tool_call` `modify` rewrites is not re-driven:
  it behaves as on 0.22.2, and its ack is core's, byte for byte. Its completion is core's too, except that it becomes
  the "Could not verify …" text above when, before the call's bind, another open call of the same task names the same
  real recipient.
- **Durable work belongs on kanban.** The plugin re-delivers *messages* into the target's Bot Chat. Use
  [Kanban](./kanban.md) for work that must survive as a task.
