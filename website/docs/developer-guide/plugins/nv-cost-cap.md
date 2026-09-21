# nv-cost-cap — two-tier per-session cost cap

`nv-cost-cap` is a standalone directory plugin that enforces a **per-session**
spend cap using Hermes's own cost accounting. It accrues each session's spend,
computes a two-tier cap, and — for a session that crosses the hard ceiling —
stops that session **without ever raising**.

## Accrual — two sources, kept monotonic

- **Fast path** — the `post_api_request` hook prices each response's `usage`
  through `estimate_usage_cost` and accumulates the per-session total in the
  plugin's own `plugin_db`, keyed idempotently by `api_request_id` (re-delivering
  the same response never double-counts).
- **Authoritative** — a recursive `parent_session_id` **lineage** read of the
  read-only core `state.db`. For the session and every descendant, a node's spend
  is `max(sessions.estimated_cost_usd, SUM(main-loop usage)) + SUM(aux usage)`, so
  parent main-loop, parent aux, and delegated-subagent cost are each counted
  exactly once (codex included — it persists `session_model_usage` per turn).

The effective spend is `max(previous_effective, reconciled_total)`: because core
enqueues accounting asynchronously, a reconcile can transiently read a *lower*
total than the fast path already accrued — taking the max stops a stale reconcile
from lowering spend and re-permitting a paid call, while still avoiding
double-counting (both sources estimate the same calls).

**Unknown pricing is fail-CLOSED.** A call whose price is unknown is recorded
`unpriced` and treated as a Tier-2 breach for a non-immortal session — never as
free — so unbounded unpriced spend cannot bypass the cap. A mixture-of-agents
(`provider="moa"`) aggregator turn is unpriceable at the fast path, so it too is
fail-closed; pricing it exactly at the moment of the call would need a generic
core hook field exposing the resolved aggregator provider/model (a documented
upstream ask), so until then the lineage reconcile is what folds in the real
per-advisor MoA spend.

## Two tiers

**Tier-2 and the immortal daily cap are evaluated on the window delta**, never the
lifetime total; **Tier-1 compares the reconciled session total** against the p90.

- **Tier-1 `capUsd`** — the profile's rolling **p90** (nearest-rank) over its
  *completed* historical session totals (an in-flight session and out-of-window
  older sessions excluded). A session whose reconciled **total** crosses Tier-1
  (while under Tier-2) records an **escalation episode** carrying a monotonic
  `budgetGen` — it is *not* hard-stopped.
- **Tier-2 `ceilingUsd`** — the resolved hard ceiling. A **non-immortal** session
  has a per-**fire** window (one agent run): the baseline advances at each fire
  boundary (`pre_gateway_dispatch` for gateway/codex fires, `on_session_end` for
  non-gateway fires), so two separate fires each under the ceiling are both
  allowed even if their cumulative exceeds it; only a single fire whose own delta
  crosses is stopped. An **immortal** session has a per-**UTC-day** window and is
  **never** hard-stopped — it records the daily crossing once per (session, day).

## Enforcement never raises

For a non-immortal session over Tier-2 the stop is a per-session **in-band
refusal keyed by `session_id`**, so sibling sessions of the same profile keep
running:

- the registered `llm_execution` **middleware** returns a synthetic terminal
  response valid for the active `api_mode` **without** calling `next_call` and
  without raising;
- `pre_tool_call` returns a `block` directive;
- `pre_gateway_dispatch` returns `{"action": "skip"}` for that session's ordinary
  inbounds — **recovery/steering slash-commands** (`/new`, `/resume`, `/pause`, …,
  validated against the built-in command registry) are always let through so an
  over-cap session can be recovered.

Because cron fires and kanban spawns are **not** session-scoped, a non-immortal
breach also engages the profile **ESTOP** belt when `settings.estop_on_breach` is
true (the default). ESTOP pauses the whole profile (every new turn, cron and
kanban) until it is disengaged; an operator running a heavily multiplexed profile
who wants only the precise per-session stop sets `estop_on_breach: false` — the
in-band per-session refusals still apply. The ESTOP sentinel is written under the
**serving** profile home, not a *named* peer's. One edge case follows from core
ESTOP: when the serving profile **is** `default`, its home is the fleet root, so a
breach there writes the canonical-root sentinel every named profile checks and
pauses the whole fleet. Default-profile sessions are not the Bot-Mode norm; if you
run cost-capped work on the `default` profile and want a per-session-only stop,
set `estop_on_breach: false` there.

## Runtime policy — `hermes cost-cap`, no env var

The Tier-2 ceiling for a profile resolves in precedence order:

1. per-profile override `plugins.entries.nv-cost-cap.settings.profile_ceiling_usd`;
2. the fleet ceiling pinned in **managed scope** (operator-owned, immutable to the CLI);
3. the **owner-pinned fleet default** — a value written into the *default*
   profile's `plugin_db` (under a context-local home override), so an
   orchestrator-set runtime ceiling is observed by every profile;
4. the plugin default.

No `HERMES_*` environment variable is ever a source of truth.

`hermes cost-cap set` writes policy at runtime and **self-gates**: it applies a
`--fleet-ceiling` write (owner-pinned default) and a `--profile <p> --ceiling Y`
per-profile override **only** when the active profile is the configured
orchestrator (`settings.orchestrator_profile`, falling back to
`kanban.orchestrator_profile`); any other profile is refused with an explicit
result and no write. A `--fleet-ceiling` write is refused when the fleet ceiling
is pinned by managed scope (no stray write to the owner-pinned default).
`hermes cost-cap show` prints the resolved ceiling for the active profile.

## Configuration

All settings live under `plugins.entries.nv-cost-cap.settings`.

| setting | meaning |
|---|---|
| `profile_ceiling_usd` | this profile's per-profile Tier-2 ceiling override (highest precedence). |
| `p90_window_sessions` | how many most-recently-completed sessions the Tier-1 p90 samples. |
| `immortal_platforms` | platforms whose sessions are immortal (per-day cap, never hard-stopped). |
| `immortal_session_ids` | explicit session ids that are immortal. |
| `estop_on_breach` | engage the profile ESTOP belt on a non-immortal breach so cron/kanban are gated too (default `true`; `false` = precise per-session refusals only). |
| `orchestrator_profile` | the profile allowed to run `hermes cost-cap set` (falls back to `kanban.orchestrator_profile`). |
| `default_ceiling_usd` | the plugin-default Tier-2 ceiling when no override, managed pin, or owner-pinned default is set. |

The fleet ceiling itself lives in managed scope and/or the owner-pinned fleet
default — it is not a per-profile settings key.

## Escalation decisions — the operator card (COST-F30)

A Tier-1 or Tier-2 crossing records an **escalation episode**. On top of that, an
operator can **Continue**, **Stop**, or **set an exact USD ceiling** for the session —
from the web dashboard, the Electron desktop app, or a messaging gateway. Every
resolution funnels through one plugin chokepoint (`resolve_escalation`) backed by a
durable exactly-once compare-and-set on the episode, so a double click (the ported
"pill double-grant" defect) applies the effect only once and a repeat reports
`already-resolved`. A Continue advances the kind's window baseline
(`day_start_total` for the immortal per-day kind, else `window_start_total`) by
`escalation_increment_usd` and clears `blocked`; a mortal Stop blocks the session; an
immortal (`daily`) session is **Continue-only** and a Stop is refused.

### Authorization — surface-namespaced operators

Every resolution is authorized against the `operators` list using a **surface-namespaced
principal**, never a bare or client-supplied `user_id`:

- **Dashboard / desktop:** `dashboard:<provider>:<org_id>:<user_id>`, derived server-side
  from the authenticated session (`request.state.session`) — never from the request body.
- **Gateway `/cost`:** `gateway:<platform>:<scope>:<user_id>`, derived from the platform
  event (`platform` is the adapter's platform value; `scope` is the workspace/tenant, so
  the same user in a different workspace is a different principal).

An operator entry is a full namespaced string, e.g.
`operators: ["dashboard:oauth:org-1:alice", "gateway:slack:T123:U456"]`. A bare `user_id`
is never an operator, so a click by a non-operator — or by the same user id in another
workspace — applies nothing.

**Loopback is fail-closed.** A dashboard in loopback/`--insecure` mode has only a shared
token and no per-user session, so no per-operator principal can be derived and panel
resolutions are refused by default. To use the panel there, either run the dashboard in
gated/OAuth mode, or opt in explicitly by setting `loopback_operator` to the namespaced id
the single trusted local token should authorize as (default off).

### Surfaces

- **Web dashboard + Electron desktop:** the plugin ships a card in a shell-wide panel slot
  (dashboard) and a contributed pane (desktop) over one shared backend
  (`/api/plugins/nv-cost-cap/`). The desktop half installs into the desktop host's
  `$HERMES_HOME/plugins/nv-cost-cap/` and is enabled in **Settings → Plugins** (off by
  default); the Python backend half must be enabled on the gateway host.
- **Messaging gateways:** `/cost continue | stop | ceiling <exact-usd>` resolves the
  session's pending episode (parsed through the adapter's own command API, so `/cost@bot`
  and iOS em-dash-corrected flags work). The plugin replies on the gateway's own outbound
  rail with a one-line outcome (`resumed`, `stopped`, `ceiling set to $X`, or the refusal
  reason), because a resolved command is dropped from dispatch and never becomes a model
  turn. A blocked session's ordinary inbound is likewise dropped, so the plugin delivers the
  pause notice naming these commands at that point; after an authorized `/cost continue` the
  session resumes on its next turn, and a repeat `/cost continue` reports `already-resolved`.
- **Orchestrator CLI (fallback):** `hermes cost-cap resolve --profile P --session S
  --episode E --budget-gen G --decision continue|stop|set-ceiling [--amount-usd U]`. Two
  independent gates, both required: it is reachable only from the orchestrator profile (an
  operational gate) **and** the recorded `cli:<orchestrator-profile>:<fleet-admin-id>` actor
  must itself be a configured `operators` entry of the target profile — the requirement that
  every resolution is authorized against the structured operators applies to the CLI too, so
  the operational gate is additive, not a substitute. It pins `--profile` around the full
  lookup / authorize / CAS / apply, and is the panel-independent path when the dashboard runs
  in loopback mode.

A reconciler makes mid-decision episodes consistent: a grant claimed but not applied (a
resolver crash) is applied exactly once, and an episode whose session closed mid-decision
is finalised so a later resolution no-ops.

### Additional settings

| setting | meaning |
|---|---|
| `operators` | list of surface-namespaced principals allowed to resolve escalations (dashboard/gateway). Never a bare `user_id`. |
| `escalation_increment_usd` | how much a Continue advances the kind's window baseline (headroom granted per Continue). |
| `escalation_reconcile_seconds` | interval for the periodic reconciler that re-applies crashed grants and finalises closed episodes. |
| `loopback_operator` | opt-in namespaced principal a loopback dashboard's single shared token authorizes as (default off/`null`; fail-closed when unset). |
| `profile_ceiling_usd` | also written by an operator **set-ceiling** resolution — the exact USD value scoped to the target profile. |
