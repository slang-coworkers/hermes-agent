---
title: slang-coworkers Loop Controls
description: How stock Hermes v2026.8.31 natively covers the four NanoClaw agentic-loop controls — plan gate, workflow-state reset, intent router, buddy monitor — as documented native substitutions with honest caveats.
---

# slang-coworkers Loop Controls

The slang-coworkers fleet ports four NanoClaw agentic-loop controls onto stock
Hermes: a **plan gate**, a **workflow-state reset**, an **intent router**, and a
**buddy monitor**. This page maps each control onto its adopted native Hermes
substitute, states where Hermes **deliberately differs**, and enumerates the
acceptance criteria (`AC-LOOP-F38-1` … `AC-LOOP-F38-9`) proven by the hermetic
test `tests/plugins/test_loop_f38_acceptance.py`. Nothing on this page is a
slang-coworkers plugin or a core patch — these mappings document existing Hermes
behaviour without claiming one-for-one parity (row disposition **ADOPT-verify**).

The **workflow-state reset** is the closest to parity; the other three controls
are **substitutions with explicit gaps**, stated in the per-section caveats: no
mechanical cross-turn write gate (LOOP-F37), no automatic per-turn intent
classifier, and no automatic next-turn `<buddy-note>` injection. This page makes
no unqualified parity claim.

## Citations

Every `tag:` anchor is verified at the pinned release tree
`NousResearch/hermes-agent` tag **`v2026.8.31`** (commit
`29112bef099274229cadff79cdff7bf7b99c4b77`) — a function or config-key
**definition**, or a documented hook-contract row in `features/hooks.md`. Every
`main:` anchor is an **advisory** carry-forward from `gap-matrix-evidence.md §
LOOP-F38` (resolved 2026-09-07 against `main@08b140d14`): the moving branch is not
mounted here, so a `main:` anchor may not land on the same line as its `tag:`
peer; the `decompose_task` `main:` anchor (`hermes_cli/kanban_decompose.py:298`)
was confirmed first-hand at that commit. The full mapping is the
[dual-cite ledger](#dual-cite-ledger) at the end.

## Plan gate

- **NanoClaw control:** edits blocked until a plan is visible.
- **Native surface:** `/plan` (`build_plan_prompt`), the edit ledger
  (`mark_workspace_edited`), and the **optional** verify-on-stop follow-up
  (`build_verify_on_stop_nudge`).

`/plan` builds a plan-mode prompt that instructs the model to save the plan under
`.hermes/plans/`, edit only the plan markdown file, and not implement code that
turn; the prompt is injected as an ordinary turn. Workspace edits **in a recognised
project** are recorded by `mark_workspace_edited` regardless of any switch. The
verify-on-stop **follow-up** is the opt-in part: stock `agent.verify_on_stop`
defaults to `false` (`verify_on_stop_enabled`), and only the follow-up is gated.
When enabled, `build_verify_on_stop_nudge` turns an eligible unverified **code**
edit into a bounded follow-up that names the changed path and stops once the attempt
budget is spent — documentation-only changes carry no verifiable behaviour and are
excluded. `AC-LOOP-F38-2` exercises the nudge builder directly, proving the
mechanism independently of that runtime switch.

**Caveat:** `/plan` is **prompt-only** — it instructs the model, it does not
mechanically block tools, and even the per-turn discipline is prompt-enforced. The
mechanical **cross-turn** write gate (refusing `write_file` / `patch` / mutating
`terminal` until a plan exists on any later turn) is **not native** and is **not
built here**: it is a criterion on **LOOP-F37**'s single `nv-fleet-gates`
`pre_tool_call` callback, cross-referenced and not re-implemented.

## Workflow-state reset

- **NanoClaw control:** clear per-run scratch state at session boundaries and
  re-inject the durable checklist across compaction.
- **Native surface:** the core session-boundary reset (`HermesCLI.new_session` →
  `reset_session_state()` + a fresh `TodoStore`), surfaced through the
  `on_session_reset` / `on_session_start` hooks, the per-turn
  `pre_llm_call(is_first_turn)` re-entry signal, and `TodoStore.format_for_injection`.

`new_session` zeroes session state and replaces the `TodoStore`, then fires the
`on_session_reset` hook. The `on_session_reset` and `on_session_start` hooks are
the plugin **signals** around that boundary; `pre_llm_call` re-enters each turn
carrying `is_first_turn` and injects into the user message — never the system
prompt — preserving the per-conversation prompt cache. After compaction the todo
checklist is re-emitted by `format_for_injection` under the `TODO_INJECTION_HEADER`
preservation header, dropping completed items.

**Caveat:** this is the control closest to parity, with **no enforcement gap** —
but the reset itself is performed by **core** (`new_session`); the documented hooks
fire **around** that boundary as plugin signals, they are not the reset themselves.

## Intent router

- **NanoClaw control:** a per-turn LLM classifier that picks the workflow to run.
- **Native surface:** the shipped routing surfaces — the skills index, `/goal`, and
  kanban `decompose` — plus `complete_structured` as a plugin extension seam.

This is where Hermes **differs most**: it ships **no per-turn intent classifier**
(a deliberate scope decision). Native routing is the shipped selection surfaces.
The one this row proves end-to-end is kanban `decompose_task`, which asks the
auxiliary model for a task graph and routes each decomposed child to the specialist
profile the model names, rewriting an invalid choice to the configured default. The
skills index and `/goal` are the other shipped selection surfaces.
`complete_structured` is the **plugin extension seam** a custom classifier would
call — documented, not a shipped router.

**Caveat:** no automatic per-turn **classifier** ships; a custom one would be a
plugin built on `complete_structured`, and building one is explicitly out of scope
for this row.

## Buddy monitor

- **NanoClaw control:** a background watcher that injects a `<buddy-note>` into the
  *next* turn.
- **Native surface:** `/review` (a user-triggered independent reviewer) plus the
  config-gated post-turn `background_review` fork, with the kanban dispatcher
  providing the fleet-level watch (owned by LOOP-F39).

`/review` spawns an independent, full-privilege background subagent from a snapshot
of the recent user/assistant messages (`snapshot_recent_messages`) and re-enters
the session with its result. The automatic post-turn fork is a memory/skill-
maintenance fork whose spawn path reads `auxiliary.background_review.enabled`
through `load_background_review_settings`; `is_background_review_enabled` exposes
the same switch as a predicate. The switch is **necessary but not sufficient**: a
memory/skill nudge must also trip, and delegation subagents never auto-review.

**Caveat:** two deliberate differences. (1) `background_review` is a memory/skill-
maintenance fork, **not** a general second-opinion monitor. (2) `background_review`
**does not inject its result into a later main-conversation turn** — it never
touches the main conversation or the prompt cache, because per-conversation prompt
caching is a sacred invariant. Automatic next-turn `<buddy-note>` injection is
therefore **out of scope**, not claimed here.

## Acceptance criteria

All criteria are `pytest:` — agent-loop internals exercised through public Python
surfaces against a sandboxed `HERMES_HOME`; one `test_ac_loop_f38_<n>` per id in
`tests/plugins/test_loop_f38_acceptance.py`.

- **AC-LOOP-F38-1** — Plan gate: `build_plan_prompt()` names the `.hermes/plans/`
  save location, the "edit only the plan markdown file" rule, and the "do not
  implement code" rule.
- **AC-LOOP-F38-2** — Edit-tracking + verify-on-stop: a recorded-but-unverified
  edit yields a bounded nudge naming the path; the nudge stops once the budget is
  spent; an empty change set yields no nudge.
- **AC-LOOP-F38-3** — Workflow-state reset boundary: `HermesCLI.new_session` fires
  `on_session_reset` with the session id and `platform="cli"`.
- **AC-LOOP-F38-4** — Per-turn re-entry: `pre_llm_call` delivers `is_first_turn`
  to a loaded plugin (True on the first turn, False on the next).
- **AC-LOOP-F38-5** — Compaction-surviving checklist:
  `TodoStore.format_for_injection()` re-emits active todos under the preservation
  header and drops completed items.
- **AC-LOOP-F38-6** — Intent routing (shipped): kanban `decompose` routes a child
  to the aux-named specialist profile, and an invalid assignee falls back to the
  configured default.
- **AC-LOOP-F38-7** — Buddy monitor, on-demand: `/review` snapshots recent
  user/assistant messages, builds an independent reviewer task carrying that
  content, and its dispatch note says the result re-enters the session.
- **AC-LOOP-F38-8** — Buddy monitor, post-turn gate:
  `auxiliary.background_review.enabled` is exposed through
  `is_background_review_enabled`, and the master switch is present in the config
  schema.
- **AC-LOOP-F38-9** — (derived) This page exists under `website/docs/`, maps the
  four controls onto their native surfaces, enumerates `AC-LOOP-F38-1` …
  `AC-LOOP-F38-9`, and carries the exact dual `tag:` / `main:` citations enumerated
  by `_DUAL_CITES`.

## Dual-cite ledger

The acceptance test's `_DUAL_CITES` list drives this ledger: one row per listed
symbol, each carrying **both** its verified `tag:` anchor and its advisory `main:`
anchor. Not every symbol named in the sections above is a ledger row — only those
the test pins.

| Symbol | Verified anchor | Advisory anchor | Control |
|---|---|---|---|
| `build_plan_prompt` | tag: `agent/plan_prompt.py:78` | main: `agent/plan_prompt.py:20` | Plan gate |
| `mark_workspace_edited` | tag: `agent/verification_evidence.py:677` | main: `agent/verification_evidence.py:27` | Plan gate |
| `build_verify_on_stop_nudge` | tag: `agent/verification_stop.py:233` | main: `agent/verification_stop.py:1` | Plan gate |
| `on_session_reset` | tag: `website/docs/user-guide/features/hooks.md:461` | main: `website/docs/user-guide/features/hooks.md:461` | Workflow-state reset |
| `on_session_start` | tag: `website/docs/user-guide/features/hooks.md:458` | main: `website/docs/user-guide/features/hooks.md:458` | Workflow-state reset |
| `is_first_turn` | tag: `website/docs/user-guide/features/hooks.md:446` | main: `website/docs/user-guide/features/hooks.md:446` | Workflow-state reset |
| `format_for_injection` | tag: `tools/todo_tool.py:151` | main: `tools/todo_tool.py:1` | Workflow-state reset |
| `TODO_INJECTION_HEADER` | tag: `tools/todo_tool.py:42` | main: `tools/todo_tool.py:21` | Workflow-state reset |
| `complete_structured` | tag: `agent/plugin_llm.py:811` | main: `agent/plugin_llm.py:460` | Intent router |
| `snapshot_recent_messages` | tag: `agent/review_engine.py:63` | main: `agent/review_engine.py:1` | Buddy monitor |
| `is_background_review_enabled` | tag: `agent/background_review.py:291` | main: `agent/background_review.py:1` | Buddy monitor |
| `auxiliary.background_review.enabled` | tag: `hermes_cli/config_defaults.py:1356` | main: `hermes_cli/config_defaults.py:741` | Buddy monitor |
| `decompose_task` | tag: `hermes_cli/kanban_decompose.py:271` | main: `hermes_cli/kanban_decompose.py:298` | Intent router |

## Related

- **LOOP-F37** — the mechanical cross-turn write gate (the `nv-fleet-gates`
  `pre_tool_call` callback) that this page cross-references but does not build.
- [Hooks reference](./features/hooks.md) — `on_session_reset`, `on_session_start`,
  `pre_llm_call` / `is_first_turn`.
- [Profiles](./profiles.md) — the specialist profiles kanban `decompose` routes to.
