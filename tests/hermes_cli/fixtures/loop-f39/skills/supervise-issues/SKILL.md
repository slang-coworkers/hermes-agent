---
name: supervise-issues
description: Nudge silent in-flight issue/PR chains while Hermes-native services reap stuck work.
---

# supervise-issues

Run on the orchestrator every 12h. Sweep the fleet's in-flight work and nudge what has gone quiet;
escalate to the human only after two unanswered nudges — never a third.

1. **Enumerate.** `kanban_list` for every non-terminal card. Do not rely on `stuck_in_blocked` alone —
   a card you have already commented on no longer flags — so walk the full list yourself.
2. **Inspect.** For each blocked card, `kanban_show` it and read its comments. The card's title carries
   its identifying token (for example `F39A-<nonce>`); use that exact token verbatim in anything you post
   or send about the card.
3. **Judge.** Count the prior SUPERVISOR nudge comments on the card — those whose body begins
   `[supervise-issues nudge]`. The assignee is unresponsive iff there is no assignee-authored comment or
   activity after the block.
4. **Nudge — a card with fewer than two prior nudges.** If the card is blocked/stale, the assignee is
   unresponsive, and it has **fewer than two** prior `[supervise-issues nudge]` comments: post exactly one
   `kanban_comment` whose body begins `[supervise-issues nudge]` and names the concrete next action and the
   card's token; and emit in your final response one per-assignee action — the target bot plus the exact
   message to send it, and that message must begin with the card's token. Do **not** call `message_agent`
   in this cron turn — it is Bot-Chat-gated and a direct call is refused.
5. **Deliver.** `deliver: bot-chat` turns that final response into a fresh inbound in your OWN canonical
   Bot Chat; that Bot Chat turn (which is Bot-Chat-gated-in, so `message_agent` is allowed) sends each
   per-assignee message with `message_agent`, landing the nudge in the assignee bot's canonical Bot Chat.
6. **Escalate — a card already nudged twice.** If a blocked card's assignee is still unresponsive and the
   card **already has two** `[supervise-issues nudge]` comments, do **not** send a third nudge. Instead
   escalate to the human in your room: emit a message whose **very first characters are** `F39-ESC:` followed
   immediately (no space) by the card's token — for example `F39-ESC:F39B-<nonce>` — and then the card id,
   the stuck duration, and the blocker. The leading `F39-ESC:<card-token>` marker is mandatory and must be
   the literal start of the message; a mention of the marker mid-sentence does not count.
7. **Reconcile — fail loud.** Count the nudges you actually sent this sweep; it must equal the number of
   cards that required a nudge (`sent_nudges == required_nudges`). If they differ, say so loudly and
   escalate.

Never reap or force-transition a card here — worker liveness is the kanban dispatcher's job
(`release_stale_claims`, `detect_crashed_workers`) and worktree reaping is `hermes worktree prune`.
This skill only observes, comments, and pings.
