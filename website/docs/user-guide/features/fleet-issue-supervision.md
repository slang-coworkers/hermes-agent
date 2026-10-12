---
title: Fleet issue supervision and worktree reaping
description: How NanoClaw's supervise-issues.py nudger and worktree-gc.py reaper map onto native Hermes surfaces — the kanban dispatcher liveness ladder, hermes worktree prune, and a rendered 12h supervise-issues cron.
---

# Fleet issue supervision and worktree reaping

A coworker fleet accumulates two kinds of debris over a long run: **stale git
worktrees** left by finished tasks, and **in-flight issue/PR chains that go
quiet** (a worker crashed, a PR's CI went stale, a review is pending). NanoClaw
handled both with two standalone scripts — `worktree-gc.py` (a reaper) and
`supervise-issues.py` (a 12-hourly supervisor that nudged silent chains). Hermes
already ships every primitive both need; this page maps the port onto the native
surfaces so the fleet uses **Hermes-native services, not a bespoke reaper or a
polling loop of its own**.

There is no new runtime. The reaper is entirely native; the supervisor is an
orchestrator-profile **skill** driven by a recurring **cron job rendered into the
orchestrator's distribution**.

## Self-healing reaping (native, never destroys unpushed work)

Two independent reapers run with no code of our own.

### Accumulated worktrees — `hermes worktree list | prune`

`hermes worktree prune` audits every worktree and reaps only what is provably
disposable. It **removes** a reapable tree with `git worktree remove --force`
after **archiving its untracked scratch first** (to
`~/.hermes/archive/worktree-prune/`); it **refuses** to reap anything that holds
real work. The verdicts (`hermes_cli/worktree_gc.py` — `audit_worktrees`,
`reclaim_worktrees`, `_archive_untracked`):

| Worktree state | Verdict | What happens |
|---|---|---|
| Uncommitted **tracked** changes | `keep` | Never reaped — real work in progress. |
| Unique **unpushed** commits (not on any upstream) | `keep` | Never reaped — work not yet preserved on a remote. |
| Live lock (an agent is using it) | `keep` | Never touched. |
| Clean + merged/pushed, carrying **untracked** scratch | `reap-archive` | Untracked files archived, **then** the tree is removed. |
| Clean + merged/pushed, no scratch | `reap` | Removed. |

The guarantee that matters: **tracked modifications and unique unpushed commits
are never destroyed**, and untracked scratch inside a doomed tree is archived
before removal — the reaper only reclaims trees whose work is already safe. A
silent startup pruner runs the same audit when a session starts, and each task
worktree (`hermes -w`, `/worktree new`) is based on the freshly-fetched remote
tip so a new run never inherits a stale base.

:::note The requirement row says "archived, never deleted" — the precise behaviour
`hermes worktree prune` *deletes* a reapable (clean, merged/pushed) tree with
`git worktree remove --force`; it *archives* only the untracked scratch inside it
first, and it *refuses* to reap a tree with tracked or unique-unpushed work. So
nothing with real work is ever deleted, and no scratch is ever lost — but a clean,
already-preserved tree is removed, not archived wholesale.
:::

### Worker liveness — the kanban dispatcher ladder

Dead and stuck kanban workers are reclaimed by the dispatcher's own liveness
ladder — all native config, no supervisor involvement:

| Rung | Mechanism | Reclaims |
|---|---|---|
| Claim TTL + live-PID extension | `release_stale_claims` (`hermes_cli/kanban_db.py`) | A `running` card whose claim TTL expired **and** whose worker PID is dead → back to `ready`. A TTL-expired card whose worker PID is **alive with a fresh heartbeat** is *extended*, not reclaimed. |
| Crash detection | `detect_crashed_workers` | A `running` card whose worker PID is gone (past the launch grace `HERMES_KANBAN_CRASH_GRACE_SECONDS`) → `ready`. |
| Heartbeat-dead running cards | `kanban.dispatch_stale_timeout_seconds` → `detect_stale_running` | A running card with no heartbeat past the timeout. |
| Per-task runtime kill | `max_runtime_seconds` | A task that overruns its budget. |
| Consecutive-failure breaker | `kanban.failure_limit` | A card that keeps failing. |
| Orphan claim bookkeeping | `kanban.reconcile_orphans` | Broken claim records. |

The supervisor never force-transitions a card: **worker liveness is the
dispatcher's job, worktree reaping is `hermes worktree prune`**, and the nudger
only observes, comments, and pings.

## The supervise-issues nudger (a rendered 12h cron)

The supervisor is an orchestrator-profile **skill** (`supervise-issues`) driven by
a recurring **12-hourly, skill-backed agent cron job**. Both are shipped as
distribution data on the orchestrator coworker type and rendered by
[`nv-coworker-compose`](./built-in-plugins.md): the skill body renders to
`<profile>/skills/supervise-issues/SKILL.md`, and the cron entry renders to
`<profile>/cron/jobs.json`.

Each run (see the skill body for the exact steps):

1. **Enumerate** in-flight work — `kanban_list` for every non-terminal card,
   `hermes kanban diagnostics` for the per-card signals (a `stuck_in_blocked`
   card blocked past the stale threshold is the primary target), and `gh` (inside
   the orchestrator's own sandbox) for open issues/PRs with no card yet.
2. **Check PR health** — head, latest non-skipped CI run, merge state; a stale
   failed run or a green-but-behind PR needs a rebase nudge; a chain missing a
   resumable artifact is a blocker. It never reruns CI.
3. **Judge** which chains are silent (blocked/running past their stale window, or
   an assigned card the assignee hasn't acted on), skipping any a human touched.
4. **Nudge** — post a durable `kanban_comment` on the card (the on-card artifact
   the assignee's worker consumes), and emit a per-assignee ping.
5. **Escalate** to the human in the orchestrator's room on repeated non-response.

### The nudge is two native seams, not one

- The **durable, per-card** nudge is a `kanban_comment` — recorded straight from
  the cron run and readable back on the card. This is the topology baseline's
  "durable hand-offs are kanban".
- The **live ping to each assignee bot** is `message_agent`, which is gated to a
  canonical **Bot Chat** session (`tools/bot_mode_dm.py`) — a cron run (platform
  `cron`) **cannot** call it directly. The native route is the cron job's
  **`deliver: bot-chat`** target: the cron run's final response is re-injected as
  a fresh inbound turn in the orchestrator's **own** Bot Chat
  (`cron/scheduler.py`), and *that* Bot Chat turn — where `message_agent` is
  allowed — sends the per-assignee messages. So the cron run **emits per-assignee
  intent in its output; it does not call `message_agent` itself**. No core edit is
  needed: the scheduler already routes `deliver: bot-chat` and consumes the job's
  `skills`.

### Why the cron is *rendered*, not created by a command

`cron/` is a [distribution-owned](../profile-distributions.md) directory: on
`hermes profile update` the whole directory is **replaced from source**. A cron
job created by a one-off `hermes cron create` command (or at onboard in the
installed home) would therefore be **wiped on the next update**. Only a
**source-rendered** `cron/jobs.json` survives. So the supervise-issues cron is
declared on the orchestrator coworker type and rendered:

```yaml
# orchestrator coworker type in coworker-types.yaml
orchestrator:
  extends: [base]
  skills: [supervise-issues]          # renders to skills/supervise-issues/SKILL.md
  cron_jobs:
    - name: supervise-issues
      schedule: "every 12h"           # interval, 720 min — recurring, re-arms after firing
      prompt: "Run the supervise-issues sweep over the fleet's in-flight work."
      skills: [supervise-issues]      # skill-backed AGENT job (not a no_agent script)
      deliver: "bot-chat"             # wakes the orchestrator's own Bot Chat turn
```

The `skills` and `deliver` keys on a `cron_jobs` entry are rendered into the job
record exactly as a runtime `hermes cron create --skill … --deliver bot-chat`
would persist them — see [Declaring gated jobs in the compose
spec](./cron.md#pre-task-gates-for-a-coworker-fleet) for the full `cron_jobs`
schema.

## Acceptance-criteria traceability

This capability's behaviour is pinned by
`tests/hermes_cli/test_loop_f39_supervise_reaping_acceptance.py`:

| Criterion | What it proves | Surface |
|---|---|---|
| AC-LOOP-F39-1 | The worktree reaper keeps tracked-dirty and unpushed trees and archives untracked scratch before removing a reapable tree. | `hermes_cli/worktree_gc.py` (`audit_worktrees`, `reclaim_worktrees`) |
| AC-LOOP-F39-2 | Dead-PID/TTL-expired kanban workers are reclaimed; a live-PID, fresh-heartbeat worker is extended, not reclaimed. | `hermes_cli/kanban_db.py` (`release_stale_claims`, `detect_crashed_workers`) |
| AC-LOOP-F39-3 | `nv-coworker-compose` renders the supervise-issues skill (byte-identical) and its 12h `--deliver bot-chat` agent cron into the orchestrator distribution, and the rendered job loads and re-arms. | `plugins/nv-coworker-compose/compose.py`; `cron/jobs.py` |
| AC-LOOP-F39-4 | A nudge recorded via the `kanban_comment` tool is durable and readable back on the card. | `tools/kanban_tools.py` (`_handle_comment`) |
| AC-LOOP-F39-5 | `compute_task_diagnostics` flags a card stuck in `blocked` past the stale threshold; a healthy card is not flagged. | `hermes_cli/kanban_diagnostics.py` |

## See also

- [Scheduled Tasks (Cron)](./cron.md) — the cron engine, `deliver: bot-chat`, and the compose `cron_jobs` schema.
- [Git Worktrees](../git-worktrees.md) — `hermes worktree list | prune` and safe cleanup.
- [Kanban](./kanban.md) and [Kanban worker lanes](./kanban-worker-lanes.md) — the dispatcher and its liveness ladder.
- [Coworker fleet dashboard](./coworker-fleet-dashboard.md) — the operator panel the fleet is watched from.
