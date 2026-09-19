---
name: supervise-issues
description: Nudge silent in-flight issue/PR chains while Hermes-native services reap stuck work.
---

# supervise-issues

Run on the orchestrator every 12h. Sweep the fleet's in-flight work and nudge what has gone quiet.

1. **Enumerate.** `kanban_list` for every non-terminal card; `hermes kanban diagnostics` for the
   per-card signals (a `stuck_in_blocked` card blocked past the stale threshold is the primary target).
   Then `gh` (inside the orchestrator's own sandbox) for open issues/PRs with no card yet.
2. **Check PR health.** For every PR-bearing chain, use `gh` to resolve the current head, latest
   non-skipped CI run, and merge state. Treat an unchanged failed/cancelled run as stale and a
   green-but-BEHIND PR as requiring a rebase; never rerun CI here. Verify that every chain has a
   resumable issue, PR, or comment artifact; surface a missing artifact as a blocker.
3. **Judge.** A chain is silent if it is blocked or running with no comment/heartbeat past its stale
   window, or an assigned card whose assignee has not acted. Skip cards a human touched since the block.
4. **Nudge (record + intent).** Post a durable `kanban_comment` on the card naming the concrete next
   action (rebase, CI stale, review pending). Do **not** call `message_agent` from this run — this is a
   cron turn, not a Bot Chat turn, and `message_agent` is Bot-Chat-gated, so a direct call is refused.
   Instead emit, in your final response, one explicit per-assignee action for each chain that needs a
   ping: the target bot and the exact message to send it.
5. **Deliver.** `deliver: bot-chat` turns that final response into a fresh inbound in the orchestrator's
   own canonical Bot Chat; that Bot Chat turn (which is Bot-Chat-gated-in, so `message_agent` is allowed)
   sends the per-assignee messages, landing each nudge in the assignee bot's canonical Bot Chat.
6. **Escalate.** On repeated non-response (a second sweep with no movement), message the human in the
   orchestrator's room with the card, the stuck duration, and the blocker.

Never reap or force-transition a card here — worker liveness is the kanban dispatcher's job
(`release_stale_claims`, `detect_crashed_workers`) and worktree reaping is `hermes worktree prune`.
This skill only observes, comments, and pings.
