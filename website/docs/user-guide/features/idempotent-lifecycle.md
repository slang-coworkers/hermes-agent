---
sidebar_position: 22
title: "Idempotent Claim/Ack Lifecycle & At-Least-Once Delivery"
description: "How Hermes makes inbound work, ownership, delivery, and restart recovery idempotent — and the one accepted difference from an exactly-once mailbox."
---

# Idempotent claim/ack lifecycle & at-least-once delivery

## What this is

A durable agent has to answer the same four questions a host mailbox does: has this
inbound already been handled, who owns this unit of work right now, was the reply
actually delivered, and what happens to work that was mid-flight when the process
restarted. Hermes answers all four with mechanisms it already ships — a webhook
delivery-id cache, the kanban claim/heartbeat/reclaim path, the outbound delivery
ledger, and the durable resume marker plus the per-session turn lease.

This page maps a **two-database mailbox claim/ack lifecycle** — the model used by
NanoClaw's host, where an inbound row moves `claimed → processing → completed`, carries
a `tries` counter, and is redriven on a fixed backoff — onto its Hermes counterpart. The
goal is to **adopt and document** what Hermes does, not to reimplement a mailbox: every
row below is a mechanism that exists in the current tree, and the [one accepted
difference](#the-one-accepted-difference) is named explicitly.

:::note Authoritative source
Line citations use two forms. **`tag:`** cites the pinned release **v2026.8.31**
(commit `29112be`) — the authoritative source for this page. **`main:`** cites upstream
`main` at commit `08b140d1` for cross-reference only; where a module was refactored on
`main`, the `main:` **path** differs from the release path (crash/respawn detection and the
session-lifecycle methods live in their own modules on `main`). When the two disagree, the
`tag:` release is correct for this page.
:::

## The lifecycle at a glance

| NanoClaw state | Hermes counterpart | `tag:` (v2026.8.31) | `main:` (@08b140d1) |
|---|---|---|---|
| `claimed` — ownership, first-wins | `claim_task`, a compare-and-set on `status='ready' AND claim_lock IS NULL` (loses → returns `None`) | `hermes_cli/kanban_db.py:4628` | `hermes_cli/kanban_db.py:2137` |
| `processing` — keep-alive | `heartbeat_claim`, extends the claim TTL for the owning claimer only | `hermes_cli/kanban_db.py:4927` | `hermes_cli/kanban_db.py:2257` |
| `tries` — stale reclaim | `release_stale_claims` — a TTL-expired claim whose host-local worker is dead or heartbeat-stale is reclaimed to its **source phase** (`ready`/`review`); a still-alive, still-heartbeating local worker is *extended*, not reclaimed | `hermes_cli/kanban_db.py:4958` | `hermes_cli/kanban_db.py:2283` |
| `tries` — crash reclaim | `detect_crashed_workers` — a host-local running task with a recorded worker PID that is no longer alive is reclaimed to its source phase, immediately (no TTL wait) | `hermes_cli/kanban_db.py:8863` | `hermes_cli/kanban_db_dispatch.py:937` |
| `tries` — respawn deferral | `check_respawn_guard` (rate-limit cooldown / auth-blocker / recent-success / active-PR) | `hermes_cli/kanban_db.py:9400` | `hermes_cli/kanban_db_dispatch.py:1125` |
| `tries` — resume phase | `_retry_status_for_run` (which phase a reclaimed run resumes from) | `hermes_cli/kanban_db.py:4841` | `hermes_cli/kanban_db.py:2202` |
| `completed` — at-least-once | delivery ledger states `pending → attempting → delivered / failed / abandoned` | `gateway/delivery_ledger.py:16` | `gateway/delivery_ledger.py:1` |
| `completed` — retry bound | `MAX_ATTEMPTS = 3`, `STALE_AFTER_SECONDS = 86400` | `gateway/delivery_ledger.py:63` / `:64` | `gateway/delivery_ledger.py:31` / `:32` |
| `completed` — honest at-least-once | `RECOVERED_MARKER`, prepended to a re-claimed in-flight obligation on recovery | `gateway/delivery_ledger.py:70` | `gateway/delivery_ledger.py:39` |
| restart — successful-turn clear | `_should_clear_resume_pending_after_turn` (keeps the marker on a failed / partial / interrupted turn) | `gateway/run.py:4532` | `gateway/run.py:3074` |
| restart — turn serialization | `SessionTurnLeaseRegistry` (in-process async lease, per resolved session id) | `gateway/turn_lease.py:155` | `gateway/turn_lease.py:71` |

The rest of this page walks each stage. Three inbound-idempotency layers and the
session-lifecycle methods that back the last two rows are cross-referenced in prose below;
their `main:` anchors are resolved against the same `08b140d1` commit.

## Inbound idempotency

"Has this inbound already been handled?" has three answers in Hermes, at three layers.

**1. Webhook delivery-id cache (drops provider retries).** A provider that re-POSTs the
same webhook delivery is caught by `_record_delivery_id` (`tag: gateway/platforms/webhook.py:453`;
`main: gateway/platforms/webhook.py:299`): the first sighting of a delivery id returns
`True` (process it), a repeat within the idempotency TTL returns `False` (drop it) before a
run is ever spawned, and the same id after the TTL elapses is admitted again. The default
TTL is one hour (`_idempotency_ttl`, `tag: gateway/platforms/webhook.py:223`). This layer is
**process-local**: the cache lives in the adapter's memory and resets when the adapter
restarts, so it deduplicates a provider's retry burst, not deliveries seen by a previous
process — that durability belongs to the run reservation below.

**2. Kanban `--idempotency-key` (best-effort task dedup).** `create_task` with a repeated
`idempotency_key` looks up and returns the existing task id instead of inserting a duplicate
row (`tag: hermes_cli/kanban_db.py:3412`, column `tag: hermes_cli/kanban_db.py:1355`; CLI flag
`tag: hermes_cli/kanban.py:387`, `main: hermes_cli/kanban_parser.py:164`).

:::caution Best-effort, not a uniqueness guarantee
This is a **lookup-before-insert** over a non-unique index, so it dedups reliably for
sequential creation but two genuinely concurrent creates with the same key can still race to
two rows. Use it as a cheap retry key, not as an exactly-once inbound gate.
:::

**3. Durable run reservation (API-driven runs).** `RunIdempotencyStore.reserve` gives an
API-driven run an atomic, persisted reservation on `(scope, idempotency_key)`
(`tag: gateway/platforms/api_server_run_idempotency.py:117`; `main: :131`), taken under a
`BEGIN IMMEDIATE` transaction (`tag: :134`) against a `PRIMARY KEY (scope, idempotency_key)`
(`tag: :69`; `main: :98`). Reserving the same key with the same request fingerprint returns
`reused` (echoing the original `run_id`); a different fingerprint returns `conflict`; a fresh
key returns `created`. Because the store is backed by a database file it is **durable**
(`durable` is `True` for a file path, `False` for a `:memory:` store — `tag: :30`), so the
reservation survives a process restart: a fresh store on the same file still resolves
`reused`. This is the layer that gives inbound idempotency an exactly-one-admission property
across restarts — for as long as the reservation is retained (terminal records are eventually
pruned by the store's retention policy).

## Ownership & heartbeat

Ownership is first-claim-wins. `claim_task` (`tag: hermes_cli/kanban_db.py:4628`;
`main: hermes_cli/kanban_db.py:2137`) is a compare-and-set: it sets the claim lock and moves
the task to `running` only `WHERE status = 'ready' AND claim_lock IS NULL`. The first claimer
gets the `Task`; a concurrent second claim finds the row no longer `ready` and returns `None`.
The claim carries a TTL — `DEFAULT_CLAIM_TTL_SECONDS` is 15 minutes
(`tag: hermes_cli/kanban_db.py:367`).

A long-running worker keeps its claim alive with `heartbeat_claim`
(`tag: hermes_cli/kanban_db.py:4927`; `main: hermes_cli/kanban_db.py:2257`), which pushes the
claim expiry forward — but only for the **owning** claimer; a heartbeat from a non-owner
returns `False` and changes nothing. The heartbeat-stale backstop
(`DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`, `tag: hermes_cli/kanban_db.py:377`) applies only
*after* a claim's TTL has expired: `release_stale_claims` would normally extend a TTL-expired
claim whose local worker is still alive, but if that worker's last heartbeat is older than the
threshold it is reclaimed anyway — a process that is running yet making no observable progress.
The backstop never reclaims before the TTL lapses.

## Reclaim & retry

A dead or stuck worker must not hold a task forever. Two functions reclaim, and one gates the
respawn.

- **`release_stale_claims`** (`tag: hermes_cli/kanban_db.py:4958`;
  `main: hermes_cli/kanban_db.py:2283`) looks only at claims whose TTL has already expired. A
  TTL-expired claim whose host-local worker PID is gone — or whose last heartbeat is stale past
  `DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS` — is reclaimed to its **source phase** (`ready` or
  `review`); a TTL-expired claim whose local worker is still alive and heartbeating is
  **extended** (a `claim_extended` event), not reclaimed, so a slow tool-free LLM call is not
  torn down mid-flight. A non-expired claim is untouched. The return count is the claims
  actually reclaimed — extensions do not count.
- **`detect_crashed_workers`** (`tag: hermes_cli/kanban_db.py:8863`;
  `main: hermes_cli/kanban_db_dispatch.py:937`) reclaims a **host-local** running task that has
  a recorded worker PID which is no longer alive (`_pid_alive`, `tag: hermes_cli/kanban_db.py:8200`),
  checking liveness immediately with no TTL wait, and restores the task's source phase. A launch
  grace (`HERMES_KANBAN_CRASH_GRACE_SECONDS`) skips the liveness check for a freshly-spawned
  worker whose PID is not yet visible on `/proc`, so it is not reclaimed before it can be seen.
- **`check_respawn_guard`** (`tag: hermes_cli/kanban_db.py:9400`;
  `main: hermes_cli/kanban_db_dispatch.py:1125`) runs before re-spawning a ready or review
  task and returns a machine-readable reason to **defer** this tick, or `None` to allow it. The
  reasons are `rate_limit_cooldown` (the last run hit a provider quota wall), `blocker_auth`
  (the last failure matches a quota / auth pattern), `recent_success` (a completed run inside
  the guard window), and `active_pr` (a recent PR-URL comment). Stale or dead claim locks are
  **not** a guard reason — those are handled by the two reclaim functions above.

:::note The consecutive-failure breaker is a separate mechanism
`check_respawn_guard` does **not** implement the crash-loop / consecutive-failure breaker. That
is the `consecutive_failures` counter on the task (`tag: hermes_cli/kanban_db.py:1082` field,
`:1360` schema) tripping the dispatcher's `kanban.failure_limit` (`DEFAULT_FAILURE_LIMIT`,
`tag: hermes_cli/kanban_db.py:1115` / `:1394`) into an auto-block after enough consecutive
failures. The guard defers a *tick*; the breaker blocks a *task*.
:::

Once a task is reclaimed, `_retry_status_for_run` (`tag: hermes_cli/kanban_db.py:4841`;
`main: hermes_cli/kanban_db.py:2202`) decides which phase the run resumes from — a
normally-claimed run resumes from `ready`, a review-lane claim from `review`.

## Outbound at-least-once

The reply side is a durable ledger for gateway **final responses**. `record_obligation` logs an
eligible outbound response, then the send drives it through the states
`pending → attempting → delivered`, or `failed`, or
`abandoned` (state machine documented at `tag: gateway/delivery_ledger.py:16`;
`main: gateway/delivery_ledger.py:1`), via `mark_attempting`
(`tag: gateway/delivery_ledger.py:261`), `mark_delivered` (`tag: :265`), and `mark_failed`
(`tag: :269`).

Retry is bounded, not open-ended: `MAX_ATTEMPTS = 3` and `STALE_AFTER_SECONDS = 86400`
(`tag: gateway/delivery_ledger.py:63` / `:64`; `main: gateway/delivery_ledger.py:31` / `:32`).
`sweep_recoverable` (`tag: gateway/delivery_ledger.py:309`) reclaims obligations orphaned by a
dead owner and **abandons** one whose attempts have reached `MAX_ATTEMPTS`, so a permanently
failing target cannot be retried forever.

On recovery, `_redeliver_pending_obligations` (`tag: gateway/run.py:12990`) re-sends the
orphaned obligations. The honesty of the at-least-once contract lives here: a re-claimed
obligation that was already **in flight** (`attempting` / `failed`) is redelivered with
`RECOVERED_MARKER` prepended to its content (`needs_marker = state != "pending"`; prepend at
`tag: gateway/run.py:12953`), warning the recipient that they may be seeing a duplicate, while
a **never-started** (`pending`) obligation is redelivered plainly. An obligation already marked
`delivered` is never redelivered.

The ledger is **enabled by default** (`gateway.delivery_ledger`, default on — `ledger_enabled`
at `tag: gateway/delivery_ledger.py:527`, `gw.get("delivery_ledger", True)` at `:535`) and
**best-effort by design**: a ledger failure must never block or delay an actual send
(`tag: gateway/delivery_ledger.py:38`).

Bot-to-bot delivery follows the same at-least-once spirit with a tighter bound: `_run_delivery`
(`tag: tools/bot_mode_dm.py:565`; `main: tools/bot_mode_dm.py:391`) retries **exactly once**,
and only when the failure reason is retryable. The decision is keyed on a machine-readable
failure code, not on string-matching at the call site: `classify_agent_error`
(`tag: tools/bot_failure_reasons.py:159`) produces the reason and `retry_action`
(`tag: tools/bot_failure_reasons.py:88`) maps a reason in `AUTO_RETRYABLE`
(`tag: tools/bot_failure_reasons.py:63`; `main: :39`) to a retry and everything else to
`RETRY_NONE` (`tag: tools/bot_failure_reasons.py:85`). A transient overload is retried once; a
configuration error such as "No LLM provider configured" is not.

## Restart safety

A turn that a restart interrupts is recovered across the process boundary. While a turn runs,
`mark_turn_active` (`tag: gateway/session.py:3148`; `main: gateway/session_lifecycle.py:224`)
persists exact ownership of that turn to `state.db` before publishing the marker in memory. On
the next boot a fresh store's `recover_interrupted_turns`
(`tag: gateway/session.py:3207`; `main: gateway/session_lifecycle.py:246`) handles each durable
active-turn marker: a fresh, valid, non-suspended marker is promoted to `resume_pending` (a new
promotion gets `resume_reason = "restart_interrupted"`), while a stale, invalid, or suspended
marker is cleared without resuming so a downgrade/re-upgrade cycle cannot revive arbitrarily old
work. Every handled marker has its stale turn token cleared. `mark_resume_pending` (`tag: gateway/session.py:3284`;
`main: gateway/session_lifecycle.py:293`) and `clear_resume_pending`
(`tag: gateway/session.py:3313`; `main: gateway/session_lifecycle.py:304`) set and clear the
marker; `_should_clear_resume_pending_after_turn` (`tag: gateway/run.py:4532`;
`main: gateway/run.py:3074`) deliberately **keeps** the marker after a failed, partial, or
interrupted turn, so a turn that did not truly finish is not silently marked done.

Concurrency across those recovery boundaries is serialized by the per-session turn lease.
`SessionTurnLeaseRegistry` (`tag: gateway/turn_lease.py:155`; `main: gateway/turn_lease.py:71`)
guards the `[load history → run → flush]` region per **resolved session id**: `acquire` returns
a token, a second `acquire` on a session that is already held fails closed with
`TurnLeaseTimeoutError` (`tag: gateway/turn_lease.py:75`), and `release` frees the session for
its successor. This is what stops two turns for the same session — a `/resume` from a second
chat, a delegation, a redriven inbound — from interleaving their flushes onto one transcript.

## The one accepted difference

Hermes implements **best-effort at-least-once** recovery, **not exactly-once**. After a
mid-delivery restart a message may be sent twice; rather than pretend otherwise, the redelivery
carries an honest `RECOVERED_MARKER` warning the recipient of a possible duplicate. The ledger
is best-effort by construction — it never blocks or delays a real send — so a ledger fault
degrades to "the message still goes out," not "the turn stalls."

Two NanoClaw mechanisms are **deliberately not ported**:

- the **two-database claim/ack table** for inbound messages — Hermes owns inbound idempotency
  at the three layers above and ownership through the kanban claim path, so a separate mailbox
  would duplicate mechanisms that already exist; and
- the **fixed 5-second-doubling redrive schedule** — Hermes has no fixed backoff timer. Kanban
  respawns happen on dispatcher ticks and may be *deferred* by `check_respawn_guard`'s reason
  codes (quota cooldown, auth blocker, recent success, active PR); the delivery ledger's recovery
  is a separate mechanism, bounded by dead-owner detection plus `MAX_ATTEMPTS` and
  `STALE_AFTER_SECONDS` — not by those reason codes.

## What Hermes still lacks

One piece of the NanoClaw model has no Hermes equivalent a plugin can reach today: a **durable
inbound-turn claim/ack/retry lifecycle** exposed to plugin code. The plugin hook surface
(`VALID_HOOKS`, `tag: hermes_cli/plugins.py:163`–`389`) fires `pre_gateway_dispatch`
(`tag: hermes_cli/plugins.py:235`) **before** a turn is dispatched. A failed turn can still fire
agent-layer hooks — `post_llm_call` (`tag: agent/turn_finalizer.py:634`, but only when the turn
returned a non-empty response and was not interrupted) and `on_session_end`
(`tag: agent/turn_finalizer.py:833`, with completion flags) — yet neither is a reliable
**gateway** post-dispatch callback that hands the original `MessageEvent` and the terminal
outcome to a plugin, and a gateway-level exception around the turn can bypass agent finalization
entirely. A plugin therefore cannot reliably observe *every* failed inbound event to re-drive it.
Closing that gap would take a single additive gateway hook — a post-dispatch callback fired once
per inbound turn on both the classified-result and raised-exception paths, carrying the original
event and an outcome — which is an upstream request, not something this page's mechanisms
provide. Until then, treat inbound-turn retry as a gateway / kanban concern, not a plugin one.

## Acceptance-criteria traceability

Each mechanism above is proven by a hermetic acceptance test in
`tests/gateway/test_iso_f11_lifecycle_acceptance.py` (one `test_ac_iso_f11_<n>` per id):

- `AC-ISO-F11-1` — durable request idempotency (`RunIdempotencyStore.reserve`/`lookup`)
- `AC-ISO-F11-2` — first-claim-wins ownership + heartbeat (`claim_task`, `heartbeat_claim`)
- `AC-ISO-F11-3` — stale + crashed-worker reclaim (`release_stale_claims`, `detect_crashed_workers`)
- `AC-ISO-F11-4` — outbound at-least-once + recovered marker (`delivery_ledger`, `RECOVERED_MARKER`)
- `AC-ISO-F11-5` — restart recovery + per-session turn lease (`recover_interrupted_turns`, `SessionTurnLeaseRegistry`)
- `AC-ISO-F11-6` — inbound webhook delivery-id dedup (`_record_delivery_id`)
- `AC-ISO-F11-7` — kanban idempotency-key dedup (`create_task`)
- `AC-ISO-F11-8` — respawn guard reason + retry phase (`check_respawn_guard`, `_retry_status_for_run`)
- `AC-ISO-F11-9` — bot-to-bot single retry keyed on a machine-readable code (`_run_delivery`)
