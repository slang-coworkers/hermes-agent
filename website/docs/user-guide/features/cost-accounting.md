---
sidebar_position: 13
sidebar_label: "Cost accounting"
title: "Cost accounting"
description: "Where per-coworker cost comes from, why it is durable, and why NanoClaw's cost machinery was deleted rather than ported — the native token-bin ledger, the versioned pricing engine, and the reconciled lineage total the fleet's cost cap reads."
---

# Cost accounting

An operator or contributor asking "where does per-coworker cost come from, is it
durable, and why did we delete NanoClaw's cost machinery?" gets one answer:
**Hermes's native accounting and pricing are the base of record.** Every API
call's token usage and its priced dollar cost are accrued per call — main loop,
auxiliary models, mixture-of-agents advisors, subagents, and codex-native runs —
and persisted into a durable, route-grained token-bin ledger that is
**re-priceable without re-scanning transcripts**. Because Hermes already does
this, the slang-coworkers fleet **adopts it as-is**: there is no cost plugin and
no core patch. NanoClaw's transcript scan, its verbatim LiteLLM rate table, and
`cost-thresholds.json` are **deleted, not ported** — the native ledger plus the
versioned pricing engine supersede all three.

The fleet runs the pinned release **`v2026.8.31`** (commit `29112bef`).

## How to read the citations

Every citation is `file:line` **relative to the release tree**, in the form
`tag: <path>:<line> | main: <path>:<line>`:

- **`tag:`** is the pinned release `v2026.8.31` (commit `29112bef`) — what the
  fleet actually runs. These lines are the authoritative anchors, verified
  against the pinned tree.
- **`main:`** is upstream `main` at snapshot `08b140d14`, carried so the mapping
  can be re-diffed when the pin moves. Where a specific `main` line was
  resolved it is shown (`main: <path>:<line>`); where the module is retained on
  `main` but no specific line was resolved, the anchor is **path-level**
  (`main: <path>@08b140d14`). No `main` line is guessed; where the tree and a
  carried `main` anchor disagree, the pinned tree wins.

One symbol moved between the pin and `main` and is called out where it appears:
the main-loop response-usage accrual is **inline** on the pin
(tag: `agent/conversation_loop.py:4436-4454`) and is extracted into a
`record_response_usage` helper on `main` (`agent/turn_usage.py`).

## Native accounting is the base of record

Cost is accrued **per API call, for every provider**, at the point the call
returns:

- **Main loop** — the response-usage accrual is inline in the conversation loop:
  it prices the call with `estimate_usage_cost`, folds the amount into the
  session's running estimate, and records the call's `cost_status`/`cost_source`
  (tag: `agent/conversation_loop.py:4436-4454` | main: `agent/turn_usage.py:66,217,227`).
- **Auxiliary models** (vision, summarisation, and other side calls) — priced by
  `record_aux_usage`, which estimates the cost (tag: `agent/aux_accounting.py:116`)
  and writes the ledger via `record_auxiliary_usage`
  (entry tag: `agent/aux_accounting.py:71`; storage call tag: `:124` | main: `agent/aux_accounting.py:45,84` — `record_aux_usage`, `record_auxiliary_usage` call).
- **Mixture-of-agents advisors** — priced in the conversation loop and persisted
  under the **agent's** model/provider (not per advisor route) via
  `queue_token_counts` (tag: `agent/conversation_loop.py:4239-4255,4420-4505`
  | main: `agent/turn_usage.py:41-63,209-249` — `_fold_moa_usage` + aggregator pricing); see caveat (b) below.
- **Subagents** — each child session accrues its own calls; the delegation path
  captures the children's cost and rolls it into the parent's in-memory total
  (tag: `tools/delegate_tool.py:3309-3323,3653-3700`
  | main: `tools/delegate_tool_results.py:345-390` — `_fire_subagent_stop_hooks` rollup); see caveat (a) below.
- **Codex-native runs** — priced and added to the session estimate directly
  (tag: `agent/codex_runtime.py:203-211` | main: `agent/codex_runtime.py:134-139`).

Two grain caveats the accounting model imposes, stated plainly because they
change what "a session's cost" means:

- **(a) A subagent's durable cost lives on the child session, not the parent.**
  A subagent runs as its own session with a dedicated `SessionDB` opened on the
  **parent's** `db_path` (tag: `tools/delegate_tool.py:2049-2058`) and
  `parent_session_id=<parent>` (tag: `:2096,3669`), so its calls persist under
  the *child* session's `session_model_usage` rows. The parent receives only an
  **in-memory** rollup of the children's cost for its returned usage/footer
  (tag: `tools/delegate_tool.py:3683-3700`); no production writer persists that
  rolled-up amount onto the parent's summary row — `_persist_and_drain` flushes
  only the already-queued per-call deltas (tag: `run_agent.py:2064-2073`). So
  durable subagent cost is reached through **`parent_session_id` lineage
  traversal**, never by reading the parent summary row alone.
- **(b) MoA cost is aggregate-grain, not per-advisor-route.** Mixture-of-agents
  combines advisor and aggregator tokens *and* their separately-priced dollars
  and persists the aggregate under the **agent's** model/provider, not per
  advisor route (tag: `agent/conversation_loop.py:4239-4255,4420-4505`) — so a
  MoA reprice is aggregate-grain.

## The token-bin ledger

Per-call accrual lands in **`session_model_usage`**, a durable SQLite table keyed
by `(session_id, model, billing_provider, billing_base_url, billing_mode, task)`
(tag: `hermes_state_common.py:483`, primary key at `:502` | main: `hermes_state_common.py:369`).
Each row stores **tokens** (`input_tokens`, `output_tokens`, and cache/reasoning
counts) **and** the priced figures `estimated_cost_usd`, `actual_cost_usd`,
`cost_status`, `cost_source`, bounded by `first_seen`/`last_seen`. Writes are an
additive upsert on that key (tag: `hermes_state.py:9563-9600` | main: `hermes_state_usage.py:52`).

Because the row stores **tokens**, a pricing correction is a **reprice, not a
re-scan**: the persisted token bins can be re-priced at their persisted route
with the versioned engine (see [the invariant](#the-pinned-invariant) and
[Verifying the adopt claim](#verifying-the-adopt-claim)). This is the sharp
contrast with NanoClaw, whose figure was derived by scanning transcripts — there
is no transcript to scan here, only durable token bins.

## Pricing parity via a versioned engine

Dollar cost comes from `agent.usage_pricing`, a **versioned** engine:

- **`PricingEntry`** carries a `pricing_version` string plus cache read/write,
  request-cost, and context-tier rates (tag: `agent/usage_pricing.py:120`,
  `pricing_version` at `:128` | main: `agent/usage_pricing.py:94`).
- **Resolution is route-specific** (`get_pricing_entry` tag: `agent/usage_pricing.py:1263` | main: `:427`;
  `estimate_usage_cost` tag: `:1448` | main: `:537`): a `subscription_included`
  route prices to zero; an **OpenRouter** route consults OpenRouter's models API
  (an HTTP fetch); a route **with a `base_url`** probes that endpoint's `/models`
  then falls back to the bundled snapshot; a **direct-provider** route
  (`anthropic`/`openai`/`google`) with **no `base_url`** resolves **offline**
  against the **bundled official-docs snapshot** (`_lookup_official_docs_pricing`
  tag: `agent/usage_pricing.py:1186,1290`; snapshot entries `:221-364`, core
  Anthropic `pricing_version` `anthropic-pricing-2026-05` at `:221`). The
  determinant is the resolved **route**, not merely whether a `base_url` was
  passed: the acceptance test's `anthropic/…` candidates resolve offline via the
  snapshot, whereas an OpenRouter or `base_url` route may hit the network.
- **`cost_status`** is `actual | estimated | included | unknown` — a `Literal`
  vocabulary (`CostStatus` tag: `agent/usage_pricing.py:61`; `CostSource` `:62-70`)
  stored on the ledger row in the `session_model_usage` columns
  `cost_status` / `cost_source` (tag: `hermes_state_common.py:498-499`). An
  unpriced model prices to `amount_usd = None` and reads as `$0` in a dollar
  **sum**, but its row is still tagged `unknown` — the provenance the fleet's
  cost cap needs to fail *closed* rather than silently bill `$0`.

Two honest edges of the parity, both recorded as an upstream ask under
[Known limitations](#known-limitations--upstream-ask):

- `session_model_usage` has **no `pricing_version` column**
  (tag: `hermes_state_common.py:483-503`); the `pricing_version` string lives on
  the `sessions` summary row (the `update_token_counts` write path,
  tag: `hermes_state.py:9334`, `pricing_version` param at `:9347`), not on the
  usage row.
- Auxiliary rows persist `cost_status`/`cost_source` as **`NULL`**, because
  `record_aux_usage` passes only tokens + `estimated_cost_usd` to the ledger
  (tag: `agent/aux_accounting.py:124`).

So usage-grain pricing provenance is *partial* today — enough to reprice, not
enough to reprice **as-of** a historical rate. Contrast NanoClaw's flat LiteLLM
rate table copied verbatim between runner and dashboard: here there is a single
versioned engine, not a vendored table to keep in sync.

## Deleted, not ported

NanoClaw's three cost surfaces are **dropped**, not carried into the fleet:

- the **transcript scan** that re-derived token counts — superseded by the
  durable per-bin token ledger (`session_model_usage`), which needs no scan;
- the **verbatim LiteLLM rate table** copied runner↔dashboard — superseded by the
  single versioned pricing engine (`agent.usage_pricing`);
- **`cost-thresholds.json`** — its p90 cap logic is what COST-F29 needs, and the
  fleet's `nv-cost-cap` plugin computes that **from the native accounting** (the
  reconciled ledger read below), rather than vendoring a thresholds file.

(NanoClaw paths are context for the port decision, not citations into this tree.)

## Observability — where dollar cost is read

Per-coworker **dollar** cost is read from the profile-scoped analytics surfaces,
not from the ledger tables directly:

- the dashboard **Analytics** page is served by **`/api/analytics/usage`**
  (`_get_usage_analytics` tag: `hermes_cli/web_server.py:15810`; route `:15882`);
  its summary cards and daily chart are session-level rollups read from the
  `sessions` table, the by-task breakdown reads the `session_model_usage` ledger,
  and the per-model breakdown is a `sessions` base with auxiliary ledger rows
  merged in add-only (`_aux_usage_rows` tag: `hermes_cli/web_server.py:15701-15727`);
- the separate **Models** page is served by **`/api/analytics/models`**
  (`_get_models_analytics` tag: `hermes_cli/web_server.py:15894`; route `:16070`)
  — a distinct route, not the Analytics page (see [the web dashboard](./web-dashboard.md#analytics));
- the cross-profile **all-profiles sidebar** sums each profile's
  `SessionDB.usage_totals()` into `profiles_usage` / `totalCostUsd`
  (tag: `hermes_cli/web_routers/profiles.py:494,539,614`).

The **`/usage` slash command** is a different surface: it shows the current
session's **tokens**, rate limits, and Nous credits — **not** local dollar cost
(tag: `cli.py:14250-14315`, `_show_usage`). Do not conflate `/usage` (tokens)
with the analytics API (dollars). The dashboard's rendering of these figures is
proven by its own acceptance row (**OBS-F45**); this page does not re-prove the
UI.

## The pinned invariant

A session's authoritative, aux- and subagent-inclusive total is the
**reconciled lineage sum** — for each session in the `parent_session_id`
lineage:

> `max(sessions.estimated_cost_usd, Σ session_model_usage[task=''])`  (the
> main-loop figure — the larger of the summary fast-path and the main-loop
> ledger, so an `absolute=True` residual that lives only on the summary row is
> not lost)  `+ Σ session_model_usage[task<>'']`  (auxiliary rows, always
> additive)

summed over the session **and its descendants**. This is the read the fleet's
cost cap (`nv-cost-cap`, COST-F29) computes over the ledger, and the read the
[acceptance test](#verifying-the-adopt-claim) pins.

The three trivial native readers each miss part of it, which is exactly why the
reconciled read is spelled out:

- A **raw `SUM(session_model_usage)`** omits the **absolute-update main-loop
  residual**: an `absolute=True` cumulative update writes the `sessions` summary
  row but **no** usage row (tag: `hermes_state.py:9447`, where
  `record_model_usage = (not absolute) and …`), so a ledger-only sum undercounts.
- **`SessionDB.usage_totals`** (tag: `hermes_state.py:10825`) is
  **root-summary-based** — it sums `sessions` rows where `parent_session_id IS
  NULL` using `COALESCE(actual_cost_usd, estimated_cost_usd)`, so it **excludes
  auxiliary spend** (which never writes a summary row) and **non-root subagent
  sessions**.
- **`InsightsEngine.generate()`** is ledger-first and aux-inclusive in the
  simple case (its overview `estimated_cost` is the per-model breakdown over
  `session_model_usage`, tag: `agent/insights.py:528-535,684-739`), but it
  computes its residual as `max(0, summary − Σusage)` with aux **inside**
  `Σusage`; on a session carrying **both** an `absolute=True` residual **and**
  aux spend it folds the aux into the residual subtraction and undercounts, and
  it exposes no root-scoped lineage total regardless.

Durable subagent cost is never on the parent summary row (only the in-memory
footer rollup), so the aux- and subagent-inclusive figure is reachable **only**
through this reconciled lineage read.

## Known limitations → upstream ask {#known-limitations--upstream-ask}

Two capabilities the row does **not** build; they are recorded as a single P8
upstream ask (filed by the Orchestrator), both generic core widenings, neither
required for the invariant this page pins:

1. **Date-effective ("as-of") pricing + usage-grain provenance.**
   `get_pricing_entry` / `estimate_usage_cost` take no effective-date parameter
   and `PricingEntry` has no effective-date range (tag: `agent/usage_pricing.py:120,1263,1448`);
   the offline resolver returns the single current snapshot
   (tag: `:1186,221-364`). And `session_model_usage` aggregates many calls into
   one bin with no `pricing_version` column (tag: `hermes_state_common.py:483-503`),
   while aux rows carry `NULL` provenance. Consequence: re-pricing historical
   token bins uses the pricing entry the **current runtime** resolves, not
   necessarily the rate in effect when the call was made. A faithful as-of
   reprice needs per-call (or era-partitioned) usage rows
   carrying the pricing identity and timestamp — not merely a version string on
   the aggregate row.
2. **An authoritative, ledger-based, aux-inclusive fleet aggregate.** A
   cross-profile aggregate already exists — the all-profiles sidebar sums each
   profile's `usage_totals()` (tag: `hermes_cli/web_routers/profiles.py:494,539,614,622`)
   — but it is **summary-based** (root `sessions` rows, aux-excluding,
   tag: `hermes_state.py:10825`). The ask is a fleet total that sums the
   `session_model_usage` **ledger** so the single-panel figure matches the
   enforcement figure COST-F29 reads.

## Verifying the adopt claim

A hermetic pytest pins the invariant by importing and **calling** the real
native API (`SessionDB`, `agent.usage_pricing`) — it never reads source as text
and never touches the network (pricing resolves via the bundled offline
snapshot; no `base_url` is passed). Because the capability is **native**, the
test **passes on the unmodified release tree**: there is no plugin, so no
fail-before/pass-after step — it is a **regression pin** that goes red only if
future cost work breaks the invariant.

`tests/hermes_state/test_cost_f31_acceptance.py`, one test per acceptance
criterion:

- `test_ac_cost_f31_1` — priced calls persist to `session_model_usage` at
  `(session, model, provider, mode)` grain and survive close + reopen (three
  routes → three durable rows).
- `test_ac_cost_f31_2` — the aux + subagent total is the reconciled lineage sum,
  and neither `SUM(session_model_usage)` nor `usage_totals` returns it.
- `test_ac_cost_f31_3` — a persisted row is re-priceable at its persisted route
  (a reprice, not a re-scan).
- `test_ac_cost_f31_4` — `cost_status` (`actual|estimated|included|unknown`) is
  carried through per row, and aux rows persist it as `NULL` (the documented
  gap).

Run it via the sanctioned runner:

```bash
scripts/run_tests.sh tests/hermes_state/test_cost_f31_acceptance.py
# expect: 4 passed
```

A one-command smoke check that the pricing engine resolves and prices a known
model offline:

```bash
uv run python -c "from agent.usage_pricing import estimate_usage_cost, CanonicalUsage, has_known_pricing as h; m=next(x for x in ('anthropic/claude-sonnet-4-20250514','anthropic/claude-sonnet-4-5','anthropic/claude-sonnet-4-6') if h(x)); r=estimate_usage_cost(m, CanonicalUsage(input_tokens=1000, output_tokens=500)); print(m, r.status, r.amount_usd)"
# expect: a priced model, status 'estimated', a positive USD amount
```

## References

Paths are relative to the release tree; `tag` = `v2026.8.31` @ `29112bef`,
`main` = the upstream snapshot `08b140d14`. A path-level `main` anchor
(`<path>@08b140d14`) means the module is retained on `main` at that snapshot with
no specific line re-resolved.

- tag: `hermes_state_common.py:355`(SCHEMA_VERSION = 26),`410`(sessions.parent_session_id),`483-503`(`session_model_usage` DDL; PK `:502`; `cost_status`/`cost_source` columns `:498-499`; no `pricing_version` column) | main: `hermes_state_common.py:369`.
- tag: `hermes_state.py:9334`(update_token_counts),`9347`(pricing_version param on the summary row),`9447`(absolute=True → no usage row),`9563-9600`(session_model_usage upsert),`9616`(record_auxiliary_usage — usage row only),`10825`(usage_totals — root sessions, aux-excluding) | main: `hermes_state_usage.py:52`(upsert),`275`(update path); other lines `hermes_state.py@08b140d14`.
- tag: `agent/aux_accounting.py:71`(record_aux_usage),`116`(estimate_usage_cost call),`124`(record_auxiliary_usage write — tokens + estimated_cost_usd only) | main: `agent/aux_accounting.py:45`(record_aux_usage),`79`(estimate call),`84`(record_auxiliary_usage call).
- tag: `agent/usage_pricing.py:61`(CostStatus vocabulary),`62-70`(CostSource vocabulary),`74`(CanonicalUsage),`120`(PricingEntry; pricing_version `:128`; no effective-date range),`221-364`(anthropic-pricing-2026-05 snapshot),`1186,1290`(_lookup_official_docs_pricing),`1263`(get_pricing_entry — route-specific resolution),`1448`(estimate_usage_cost),`1547`(has_known_pricing) | main: `agent/usage_pricing.py:94`(PricingEntry),`427`(get_pricing_entry),`537`(estimate_usage_cost); other lines `agent/usage_pricing.py@08b140d14`.
- tag: `agent/conversation_loop.py:4239-4255,4420-4505`(MoA advisor+aggregator combined, persisted under agent model/provider),`4436-4454`(inline main-loop response-usage accrual) | main: `agent/turn_usage.py:66,217,227`(record_response_usage — main-loop),`41-63,209-249`(_fold_moa_usage + MoA aggregator pricing/persistence).
- tag: `tools/delegate_tool.py:2049-2058`(subagent dedicated SessionDB on the parent db_path),`2096,3669`(parent_session_id),`3309-3323,3653-3700`(subagent accrual + children's cost rolled into the parent's in-memory total only) | main: `tools/delegate_tool_results.py:345-390`(_fire_subagent_stop_hooks rollup); other lines `tools/delegate_tool.py@08b140d14`.
- tag: `run_agent.py:2064-2073`(_persist_and_drain flushes queued per-call deltas only — no rolled-up child amount on the parent summary) | main: `run_agent.py@08b140d14`.
- tag: `agent/codex_runtime.py:203-211`(codex-native accrual) | main: `agent/codex_runtime.py:134-139`.
- tag: `agent/insights.py:97`(InsightsEngine),`528-535`(overview estimated_cost over the per-model breakdown),`684-739`(per-session SUM + `max(0, summary − Σusage)` residual) | main: `agent/insights.py@08b140d14`.
- tag: `hermes_cli/web_server.py:15701-15727`(_aux_usage_rows — task-dimension session_model_usage rows),`15810`(_get_usage_analytics; route `:15882` /api/analytics/usage — Analytics page: daily/totals from `sessions`, by_task from the ledger, by_model = `sessions` base + aux merged),`15894`(_get_models_analytics; route `:16070` /api/analytics/models — Models page) | main: `hermes_cli/web_server.py@08b140d14`.
- tag: `hermes_cli/web_routers/profiles.py:494,539`(profiles_usage via per-profile usage_totals),`614,622`(totalCostUsd merge in /api/profiles/projects/tree) | main: `hermes_cli/web_routers/profiles.py@08b140d14`.
- tag: `cli.py:14250-14315`(/usage slash — session tokens + rate limits + Nous credits, not dollar cost) | main: `cli.py@08b140d14`.

See also [the web dashboard's Analytics page](./web-dashboard.md#analytics) for
where these figures surface, and the fleet cost cap (COST-F29) for the
enforcement read this invariant guards.
