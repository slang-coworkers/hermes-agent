---
title: slang-coworkers Scheduled Tasks
description: Durable, recurring scheduled tasks for the slang-coworkers Hermes fleet — the ncl tasks → hermes cron mapping, recurrence, per-profile durability, and the two run-log surfaces. Native Hermes; no plugin, no core patch.
---

# slang-coworkers Scheduled Tasks

The slang-coworkers fleet needs durable scheduled tasks with cron recurrence and
per-run logs — the capability NanoClaw exposes as `ncl tasks`. **Hermes cron
already provides this**, so the fleet **adopts it as-is**: this page maps each
`ncl tasks` verb onto its `hermes cron` equivalent and pins the durability,
recurrence, and run-history behaviour to the source. Nothing here is a
slang-coworkers plugin or a core patch — Hermes cron even adds operability the
requirement does not ask for (failure incidents, a per-job notepad, monitor
mode).

The fleet runs the pinned release **`v2026.8.31`** (commit `29112bef`).

## How to read the citations

Every citation is `file:line` **relative to the release tree**, in the form
`tag: <path>:<line> \| main: <path>:<line>`:

- **`tag:`** is the pinned release `v2026.8.31` (commit `29112bef`) — what the
  fleet actually runs. These lines are the authoritative anchors.
- **`main:`** is upstream `main` at snapshot `08b140d14`, given so the mapping
  can be re-diffed when the pin moves. Where a specific `main` line was
  resolved it is shown (`main: <path>:<line>`); where the module is retained on
  `main` but no specific line was resolved, the anchor is **path-level**
  (`main: <path>@08b140d14`). No `main` line is guessed.

Two symbols moved on `main` relative to the pin and are called out inline where
they appear: the model tool was renamed `cronjob` → `cronjob_manage`, and the
per-job notepad's path resolution was fixed to be per-profile (upstream #86519)
— see [Carried state](#carried-state) and [Known limitations](#known-limitations).

## Verb mapping: `ncl tasks` → `hermes cron`

All verbs share two anchors — the cron subparser surface
(tag: `hermes_cli/subcommands/cron.py:23` \| main: `hermes_cli/subcommands/cron.py:20`;
the parser builder is tag: `hermes_cli/subcommands/cron.py:15` \| main: `hermes_cli/subcommands/cron.py:14`)
and the CLI dispatch (tag: `hermes_cli/cron.py:954` \| main: `hermes_cli/cron.py:745`).
The per-verb subparser lines are listed in [References](#references).

| NanoClaw `ncl tasks` | Hermes `hermes cron` | Cite (tag ↔ main) |
|---|---|---|
| `list` | `cron list` | subparser+dispatch (shared) |
| `create` | `cron create` (alias `add`) | subparser+dispatch (shared); model tool schema name tag: `tools/cronjob_tools.py:1956` (`cronjob`) \| main: `tools/cronjob_tools.py:900` (renamed `cronjob_manage`) |
| `update` | `cron edit` (**not** `update`) | subparser+dispatch (shared) |
| `pause` | `cron pause` | subparser+dispatch (shared) |
| `resume` | `cron resume` | subparser+dispatch (shared) |
| `run` | `cron run` | subparser+dispatch (shared) |
| `append-log` | *no direct verb* — see below | runs tag: `hermes_cli/cron.py:276-291` \| main: `hermes_cli/cron.py:254-257`; output files tag: `cron/jobs.py:4250-4278` \| main: `cron/jobs.py:3032`; notepad tag: `cron/notepad.py:1` \| main: `cron/notepad.py:1` |

Two honest naming deltas:

- **There is no `update` verb** — the in-place edit verb is `cron edit`.
- **There is no `append-log` verb.** The append-style run log is the union of the
  durable **executions ledger** and the **per-run output files** under
  `cron/output/<job>/*.md` (see [Run logs](#run-logs)); `cron runs`/`history`
  reads the ledger's attempt metadata only — `cron_runs()` calls
  `list_executions()`, **not** the output files (tag: `hermes_cli/cron.py:276`
  \| main: `hermes_cli/cron.py:254-257`). The per-job **notepad**
  (`cron notepad <job> set <k> <v>`) is a key/value store for state carried
  across runs, not an append log — see [Carried state](#carried-state).

## Recurrence

`parse_schedule` accepts the full NanoClaw-equivalent schedule grammar
(tag: `cron/jobs.py:962`, grammar `:988-1121` \| main: `cron/jobs.py:733`):

- a relative **one-shot** — `in 30m`;
- a recurring **interval** — `30m` or `every 30m`;
- a **5/6-field cron** expression and natural language — `*/30 * * * *`,
  `every monday 9am`;
- an **ISO timestamp** one-shot.

Each parses to a schedule **dict** whose `kind` is `once`, `interval`, or
`cron`. Recurring kinds (`interval`/`cron`) re-arm to their next run;
`compute_next_run` returns the strictly-later next fire, or `None` for a spent
one-shot (tag: `cron/jobs.py:1390` \| main: `cron/jobs.py:1096`).
`compute_next_run` applies **no** minimum-interval floor — see the
[recurrence-guard note](#recurrence-guard).

A recurring schedule is re-armed **inside the fire-claim lock**, so a stale
re-delivery for the old time cannot re-fire: `claim_job_for_fire` takes
`_fire_job_lock`, and `_claim_job_for_fire_locked` takes `_jobs_lock()` and
bumps `next_run_at` via `compute_next_run` then `save_jobs`
(tag: `cron/jobs.py:3447,3458,3488,3529-3532` \| main: `cron/jobs.py@08b140d14`).
The pre-dispatch batch form, `advance_next_run`/`advance_next_runs`, is
at-most-once for recurring jobs and **leaves one-shots unchanged** so they can
still retry after a crash
(tag: `cron/jobs.py:3367-3406` \| main: `cron/jobs.py@08b140d14`). Next-run
times are anchored to the profile's configured timezone (see the fleet's
timezone handling, SCHED-F34).

## Durability and per-profile ownership

Jobs live in an atomic `jobs.json` — written to a temp file then swapped in with
`atomic_replace` and fsync (tag: `cron/jobs.py:80-85,142-170,1894-1976`
\| main: `cron/jobs.py@08b140d14`) — guarded by a cross-process `flock`
(tag: `cron/jobs.py:268-270,334` \| main: `cron/jobs.py@08b140d14`). This is the
durable analog of `ncl tasks create`: a created job survives a store reload and
a gateway restart.

Each **profile owns its own store**. Named profiles keep their jobs at
`<hermes-root>/profiles/<name>/cron/jobs.json`
(tag: `hermes_cli/profiles.py:293,2515` \| main: `hermes_cli/profiles.py@08b140d14`),
where `<hermes-root>` is the fleet's unprofiled Hermes home. Once a profile is
selected, `HERMES_HOME` **is** that profile directory, so the active store is
`$HERMES_HOME/cron/jobs.json`; the default profile uses
`<hermes-root>/cron/jobs.json`. Two profiles never see each other's jobs. The
multiplex ticker fires **every served profile's own store**
(tag: `cron/scheduler_provider.py:576-594,674`
\| main: `cron/scheduler_provider.py@08b140d14`), which is why per-profile
ownership holds under the fleet's one-gateway Bot Mode.

## Creating jobs (and optional packaging)

Map `ncl tasks create` → `hermes cron create`. The durable store is the
per-profile `cron/jobs.json` described above.

A [profile distribution](./profile-distributions.md) can *package* job
**definitions** in its `cron/` directory
(tag: `website/docs/user-guide/profile-distributions.md:52,257-258`
\| main: `website/docs/user-guide/profile-distributions.md@08b140d14`), but at
`v2026.8.31` install is a verbatim copy, **not** a state merge, with two
consequences to state plainly:

- **Install does not force shipped jobs disabled.** `is_job_runnable` defaults
  `enabled` to `True` (tag: `cron/jobs.py:625-633` \| main: `cron/jobs.py:485,494`),
  so a shipped job without `enabled: false` runs once the scheduler ticks. The
  "enable them explicitly" guidance in the distributions doc
  (tag: `website/docs/user-guide/profile-distributions.md:718`
  \| main: `website/docs/user-guide/profile-distributions.md@08b140d14`) is an
  authoring convention, not an enforced install-disabled step.
- **`hermes profile update` is not a durable-state deployment path.** It
  replaces a distribution-owned `cron/` directory by `shutil.rmtree`-ing the
  destination first (tag: `hermes_cli/profile_distribution.py:584-587`
  \| main: `hermes_cli/profile_distribution.py:383`), which can delete
  `cron/output/`, `executions.db`, and `notepad.db`.

So distributions ship job **definitions**; the durable runtime store is
`cron/jobs.json` plus the ledger/output/notepad files, which `profile update`
does **not** merge. Do not present `profile update` as a way to deploy durable
scheduled-task state — see [Known limitations](#known-limitations).

## Scheduler durability

An in-process **60-second ticker** drives due jobs
(tag: `cron/scheduler_provider.py:539-618` \| main: `cron/scheduler_provider.py@08b140d14`;
`TICKER_INTERVAL_SECONDS = 60` at tag: `cron/jobs.py:99` \| main: `cron/jobs.py:81`),
and each tick takes a single-tick `flock` so two gateway processes cannot
double-fire (tag: `cron/scheduler.py:7829,7869,7881-7892` \| main: `cron/scheduler.py@08b140d14`).

Agent-backed runs execute in a **fresh `AIAgent` session** with no current-chat
context (tag: `cron/scheduler.py:6427-6465` \| main: `cron/scheduler.py@08b140d14`;
tool schema note tag: `tools/cronjob_tools.py:1958-1959` \| main: `tools/cronjob_tools.py:900-903`),
so a job prompt must be self-contained. A `no_agent` script job bypasses
`AIAgent` entirely — the script *is* the job, with no LLM involvement
(tag: `cron/scheduler.py:5513,5530` \| main: `cron/scheduler.py@08b140d14`).

## Run logs

Two durable surfaces together provide the `ncl tasks` run-log / append-log
parity:

1. **Executions ledger** — a SQLite ledger with **immutable terminal states**.
   `finish_execution` only transitions a `claimed`/`running` row and refuses to
   rewrite a terminal one (tag: `cron/executions.py:181-202`
   \| main: `cron/executions.py:256-274`). `create_execution`,
   `mark_execution_running`, `list_executions`, and `latest_execution` are the
   read/write surface (tag: `cron/executions.py:141,163,242,266`
   \| main: `cron/executions.py@08b140d14`; ledger plumbing `open_ledger`
   main: `cron/executions.py:35`). This is the attempt metadata
   `cron runs`/`history` reports.
2. **Per-run output files** — the full output of each run is retained under the
   profile's `cron/output/<job>/*.md`, pruned by `cron.output_retention`
   (default 50) (tag: `cron/jobs.py:4207-4276` \| main: `cron/jobs.py:3032`; default
   tag: `hermes_cli/config_defaults.py:2798` \| main: `hermes_cli/config_defaults.py:1670-1672`).

On top of these, failures raise **incidents** keyed by an error signature with
dedup + ack (tag: `cron/incidents.py:1-21` \| main: `cron/incidents.py@08b140d14`),
and a `failure_streak` review nudge fires at `cron.failure_nudge_threshold`
(default 3) (tag: `cron/scheduler.py:152-191` \| main: `cron/scheduler.py@08b140d14`;
streak persisted on the job record tag: `cron/jobs.py:3057-3060` \| main: `cron/jobs.py:2153-2176`).

## Carried state

A per-job **notepad** is a single-profile key/value store for state carried
across runs (tag: `cron/notepad.py:1-22,96-154` \| main: `cron/notepad.py@08b140d14`).
Its only write path is the CLI — `hermes cron notepad <job> set <k> <v>`
(tag: `hermes_cli/cron.py:911` \| main: `hermes_cli/cron.py@08b140d14`) — and its
contents are rendered into each run's prompt
(tag: `cron/notepad.py:169` \| main: `cron/notepad.py@08b140d14`).

**On the pinned release the notepad is not multiplex-profile-isolated.**
`NOTEPAD_FILE` is resolved **once at import**
(tag: `cron/notepad.py:34` \| main: `cron/notepad.py:26,33-34`) and `_connect`
uses it directly, so under a shared multiplex gateway it does not follow profile
switches — unlike `cron/executions.py`, which re-resolves `get_hermes_home()`
per call. This is why the `ncl tasks append-log` parity is mapped to the
profile-scoped run-log surfaces (ledger + output files) rather than to the
notepad. **This limitation is fixed on upstream `main`** (#86519): there
`NOTEPAD_FILE` defaults to `None` and `_current_notepad_file()` resolves the path
per transaction (main: `cron/notepad.py:26,33-34`), matching `cron/executions.py`.

## Recurrence-guard note {#recurrence-guard}

Hermes core applies **no minimum recurrence interval** today: `parse_schedule`
accepts `1m` and `* * * * *` (tag: `cron/jobs.py:962-1121` \| main: `cron/jobs.py:733`),
`compute_next_run` applies no floor (tag: `cron/jobs.py:1390-1449`
\| main: `cron/jobs.py:1096`), and the cron config block carries no
minimum-interval key (tag: `hermes_cli/config_defaults.py:2707-2816`
\| main: `hermes_cli/config_defaults.py@08b140d14`). The fleet's plan is to guard
the coworker-facing (model-tool) job-creation path via the `nv-fleet-gates`
`pre_tool_call` callback (LOOP-F37), and a **native** floor — a
`cron.min_interval_seconds` key enforced on every path (model tool, CLI,
distribution file, direct API) — is filed as an upstream ask (UA-20). The
precedent for such a key already exists in the same codebase:
`loops.min_interval_seconds: 30` (tag: `hermes_cli/config_defaults.py:2200`
\| main: `hermes_cli/config_defaults.py@08b140d14`). Nothing for that ask is
built in the fleet fork; it is recorded here as a forward pointer.

## Known limitations

Honest edges of the adopt claim:

- **`profile update` is not a durable-state deployment path.** It
  `shutil.rmtree`s a distribution-owned `cron/` directory before recopying
  (tag: `hermes_cli/profile_distribution.py:584-587`
  \| main: `hermes_cli/profile_distribution.py:383`), deleting `cron/output/`,
  `executions.db`, and `notepad.db`; and install does not force shipped jobs
  disabled (`is_job_runnable` defaults `enabled=True`,
  tag: `cron/jobs.py:625-633` \| main: `cron/jobs.py:485,494`). Distributions
  ship job **definitions**; the durable store is `cron/jobs.json` plus the
  ledger/output/notepad files.
- **The per-job notepad is not multiplex-profile-isolated on `v2026.8.31`.**
  `NOTEPAD_FILE` is resolved once at import
  (tag: `cron/notepad.py:34` \| main: `cron/notepad.py:26,33-34`), so it is
  single-profile carried-state; the `ncl tasks append-log` parity is the
  profile-scoped run-log surfaces (ledger + output files). Fixed upstream on
  `main` (#86519, main: `cron/notepad.py:26,33-34`).

## Verifying the adopt claim

A hermetic pytest test pins the adopt behaviour by importing and **calling** the
real cron API (never reading source as text):
`tests/cron/test_sched_f32_acceptance.py`, with one test per acceptance
criterion — recurrence re-arm (`test_ac_sched_f32_1`), per-profile durable
`jobs.json` round-trip (`test_ac_sched_f32_2`), and the two run-log surfaces
(`test_ac_sched_f32_3`). Because the capability is native, the test passes on
the unmodified release tree — there is no plugin, so no fail-before/pass-after
step. Run it via the sanctioned runner:

```bash
scripts/run_tests.sh tests/cron/test_sched_f32_acceptance.py
```

A one-command smoke check that the recurrence surface loads and parses:

```bash
uv run python -c "from cron.jobs import parse_schedule, compute_next_run; s=parse_schedule('*/30 * * * *'); print(s['kind']); print(compute_next_run(s))"
# expect: cron  <ISO timestamp>
```

## References

Paths are relative to the release tree; `tag` = `v2026.8.31` @ `29112bef`,
`main` = the upstream snapshot `08b140d14`. A path-level `main` anchor
(`<path>@08b140d14`) means the module is retained on `main` at that snapshot with
no specific line re-resolved.

- tag: `cron/jobs.py:80-85,99,142-170,268-270,334,625-633,962,1390,1894-1976,3057-3060,3367-3406,3447,3458,3488,3529-3532,4207-4276` \| main: `cron/jobs.py:81`(TICKER_INTERVAL_SECONDS),`485,494`(is_job_runnable),`733`(parse_schedule),`1096`(compute_next_run),`2153-2176`(failure streak),`3032`(save_job_output); other lines `cron/jobs.py@08b140d14` — store, ticker interval, atomic write, flock, runnable default, schedule grammar, recurrence + pre-dispatch advance, fire-claim re-arm, failure streak, output retention.
- tag: `cron/scheduler.py:152-191,5513,5530,6427-6465,7829,7869,7881-7892` \| main: `cron/scheduler.py@08b140d14` — failure nudge, `no_agent` short-circuit, fresh `AIAgent` per run, single-tick flock.
- tag: `cron/scheduler_provider.py:539-618,576-594,674` \| main: `cron/scheduler_provider.py@08b140d14` — 60s ticker, multiplex per-profile tick.
- tag: `cron/executions.py:141,163,181-202,242,266` \| main: `cron/executions.py:256-274`(finish_execution, immutable terminal state),`35`(open_ledger); other lines `cron/executions.py@08b140d14` — durable ledger, immutable terminal state, read surface.
- tag: `cron/incidents.py:1-21` \| main: `cron/incidents.py@08b140d14` — failure incidents dedup + ack.
- tag: `cron/notepad.py:1-22,34,96-154,169` \| main: `cron/notepad.py:26,33-34` (per-call resolution, #86519) — per-job notepad KV.
- tag: `hermes_cli/cron.py:276-291,911,954` \| main: `hermes_cli/cron.py:254-257`(cron_runs),`745`(dispatch); `hermes_cli/cron.py@08b140d14` (notepad set) — CLI dispatch, `runs` (reads the ledger), notepad `set`. Per-verb subparsers tag: `hermes_cli/subcommands/cron.py:15`(builder),`23`(list),`27`(create/add),`137`(edit),`259`(pause),`262`(resume),`267`(run),`309`(notepad) \| main: `hermes_cli/subcommands/cron.py:14`(builder),`20`(list); other verbs `hermes_cli/subcommands/cron.py@08b140d14`.
- tag: `tools/cronjob_tools.py:1956,1958-1959,2105-2112` \| main: `tools/cronjob_tools.py:900-903` — model-facing tool (`cronjob` on the tag; renamed `cronjob_manage` on main; fresh-session note at main `:903`).
- tag: `hermes_cli/profiles.py:293,2515` \| main: `hermes_cli/profiles.py@08b140d14` — per-profile home path.
- tag: `hermes_cli/profile_distribution.py:584-587` \| main: `hermes_cli/profile_distribution.py:383` — distribution copy / rmtree.
- tag: `hermes_cli/config_defaults.py:2200,2707-2816,2798` \| main: `hermes_cli/config_defaults.py:1670-1672`(output_retention default); other lines `hermes_cli/config_defaults.py@08b140d14` — `loops.min_interval_seconds` precedent, cron config block (no min-interval key), `output_retention` default.
- tag: `website/docs/user-guide/profile-distributions.md:52,257-258,718` \| main: `website/docs/user-guide/profile-distributions.md@08b140d14` — distribution `cron/` dir, install-not-auto-scheduled convention.
