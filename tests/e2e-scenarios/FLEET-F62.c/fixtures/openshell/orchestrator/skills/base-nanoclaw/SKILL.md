---
name: base-nanoclaw
description: Host tools for coworker messaging, scheduling, learnings.
---

# Host Tools

Cross-cutting helpers for status updates, scheduling, elicitation, and durable
learning capture, available to every coworker in the fleet.

## When to use each

- Send a short progress update mid-task on a long-running turn; stay quiet on a
  one or two step turn.
- Schedule a recurring sweep or deferred check as a cron job rather than a busy
  loop; each fire is a fresh session, so state that must survive belongs in a
  file, not the conversation.
- Ask the operator a bounded multiple-choice question only when you genuinely
  cannot proceed; a read-only status panel that needs no answer returns at once.
- Record a durable discovery for future coworkers right after the task, with a
  one-line summary, the evidence, and the path that proves it.

## Nuance

- Pacing: one early acknowledgement on a long turn, then updates at meaningful
  transitions, never every step.
- Deliver artifacts (reports, charts) as files, not pasted into chat.
- Delegate builds and high-volume reads to a sub-agent, never a background loop.

The NanoClaw host transport that backed these tools has no one-to-one Hermes
equivalent; see the fleet port notes for how each behaviour maps onto the
Hermes gateway and what is dropped.
