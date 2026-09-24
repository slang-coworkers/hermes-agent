---
title: "Fleet metrics & outcome analytics"
description: "How Hermes natively covers fleet health telemetry (OTLP export plane) and per-profile usage/cost analytics, and where outcome analytics live."
---

# Fleet metrics & outcome analytics

This page maps a common fleet-operations need — *"emit fleet health metrics and
compute outcome/usage analytics across all my bots"* — onto the features Hermes
already ships. It is an **adopt**-track mapping: the health-telemetry plane and
the per-profile usage/cost analytics are native to Hermes at v2026.8.31, so
there is **no plugin** to install and nothing to build to get them — you turn on
config and point your observability stack at the endpoint. The one capability
Hermes genuinely lacks (publishing a plugin's *own* custom monitoring
event/gauge through the shipped OTLP exporter) is recorded below as an upstream
ask, not worked around.

## What this maps

A typical external fleet-metrics setup runs a short periodic job (e.g. a 60-second
systemd oneshot) that scrapes each process's health and emits it as line protocol
to a time-series database, alongside a separate pipeline that computes
GitHub-funnel outcome analytics (funnel / win-rate / cost-per-merge) across the
fleet. Hermes replaces the **health-telemetry half** with an in-process,
content-free OpenTelemetry (OTLP) export plane that carries metrics, traces and
logs and is DataDog / OTel-Collector ready; it supersedes the external oneshot
because the gauges are produced and exported from inside the running gateway with
no sidecar. The **usage/cost analytics half** is served natively by the
`InsightsEngine` over the `session_model_usage` table and by the dashboard
analytics tab. The **outcome-analytics half** (the funnel/win-rate/cost-per-merge
ledger and its CLI verbs) is delivered by a *different* deliverable —
`nv-artifact` (GOV-F25) — and is described only for boundary clarity in
[§ Outcome analytics — where it lives](#outcome-analytics--where-it-lives).

Because both adopted halves are shipped and native, this row builds no code: the
deliverables are this documentation page and one hermetic acceptance test that
proves Hermes does what is claimed here.

## Fleet health metrics (native, adopt)

Hermes's health telemetry lives under `agent/monitoring/`. It is an **egress
path, not a store** — nothing is persisted locally
(tag: `agent/monitoring/__init__.py:10` | main: `agent/monitoring/__init__.py:1`).
The plane is **content-free** (payloads are redacted to an allow-list of keys),
**fail-open** (if the OTLP SDK is not installed, export disables cleanly instead
of raising), and needs **no sidecar** (OTLP is emitted from inside the gateway
process).

### The three export paths

The plane uses three distinct in-process paths, all fail-isolated, with the
OpenTelemetry SDK imported lazily as the optional `[otlp]` extra:

1. **Numeric health gauges → OTLP metrics.** Sixteen hardcoded gauges are
   registered as OTel *observable gauges* by the metric provider
   (tag: `agent/monitoring/gateway_health_export.py:437` | main: `agent/monitoring/gateway_health_export.py:1`;
   `_start_metric_provider` at `gateway_health_export.py:417`, `create_observable_gauge`
   at `:467`). These do **not** flow through the event emitter — they are pull-based
   callbacks read at export time, and their exporter targets the metrics endpoint
   (`/v1/metrics`).
2. **Health events → OTLP spans.** `gateway_health` and `cron_execution` events are
   mapped to OTel spans by an `OTLPStreamer` subscribed to the emitter
   (tag: `agent/monitoring/otlp_exporter.py:20` | main: `agent/monitoring/otlp_exporter.py:1`;
   attribute mapping `_span_attrs` at `otlp_exporter.py:137`, event filter
   `_gateway_health_event` at `gateway_health_export.py:583` wired in at `:617`).
3. **Diagnostic events → OTLP logs.** `gateway_diagnostic` events go out as OTel
   logs via the diagnostic log streamer.

The event emitter itself is fire-and-forget: it is **born disabled**, `emit()`
never blocks or raises into gateway code, and it is a no-op until a plane
subscribes (`agent/monitoring/emitter.py`). Numeric gauges (path 1) are carried by
the observable-gauge callbacks, not by the emitter; the emitter carries the
health/diagnostic *events* (paths 2–3).

### The sixteen gauges

- **Gateway (8):** `hermes.gateway.up`, state, busy, drainable, active_agents,
  background_work, background_delegations, restart_requested.
- **Platform (2, per configured platform):** `hermes.platform.up`, degraded.
- **Cron (6):** heartbeat, last-success age, catch-up, `hermes.cron.jobs.enabled`,
  running, `hermes.cron.jobs.overdue`.

**Scope matters.** Gateway and platform gauges are **gateway-instance/process-scoped**
— one gateway serves N profiles, so these describe the gateway process, not any
one profile, and carry no `profile` attribute. The **cron** gauges, by contrast,
reflect the **active/default profile** only: the cron snapshot reads the active
cron store (`cron/jobs.py:142` `_current_cron_store()`), so under a
one-gateway/N-profiles fleet the cron gauges are *not* cross-profile aggregated
the way the gateway/platform gauges are process-scoped. Operators watching cron
health across a multi-profile fleet should be aware of this per-profile scoping.

### Enabling export

Export is gated by **all three** of these config keys — the runtime `_enabled()`
check requires every one of them, and a non-empty endpoint
(tag: `agent/monitoring/gateway_health_export.py:178-181` | main: `agent/monitoring/gateway_health_export.py:1`):

```yaml
monitoring:
  gateway_health_export:
    enabled: true            # monitoring.gateway_health_export.enabled
  export:
    otlp:
      enabled: true          # monitoring.export.otlp.enabled
      endpoint: "http://collector:4318/v1/traces"   # monitoring.export.otlp.endpoint (must be non-empty)
```

Naming only two of `monitoring.gateway_health_export.enabled`,
`monitoring.export.otlp.enabled` and a non-empty `monitoring.export.otlp.endpoint`
would leave export **off**. Defaults are all disabled/empty
(`hermes_cli/config_defaults.py:3145`, `:3161`, `:3162`).

Inspect the resolved export state with:

```console
$ hermes monitoring status
```

(`hermes monitoring status` handler: `hermes_cli/main.py:12976`, parser
`hermes_cli/subcommands/monitoring.py:31`.)

**Operator wiring is config-only, no code:** point an OTel Collector, Grafana
Agent or DataDog agent at the OTLP endpoint. Nothing is persisted locally on the
Hermes side, and if the `[otlp]` extra is absent the plane fails open (export
reports `enabled=false`, `reason="otlp_unavailable"`) rather than crashing the
gateway.

## Usage & cost analytics (native, adopt)

Per-session and per-profile token/cost analytics are computed natively by the
`InsightsEngine`, which takes a `SessionDB` and produces a report from the
`session_model_usage` table
(tag: `agent/insights.py:140` | main: `agent/insights.py:1`;
`InsightsEngine` class at `agent/insights.py:97`, DDL at
`hermes_state_common.py:483`). `InsightsEngine.generate()` returns
`{overview, models, platforms, tools, skills, activity, top_sessions}`, attributing
tokens and cost to the model that actually incurred them — a mid-session model
switch is split across the two models, not dumped on the initial one — and the
overview cost equals the sum of the per-model costs.

- **Per-profile rollup.** `SessionDB.usage_totals()` sums tokens and cost across a
  profile's root sessions with billed-then-estimate precedence
  (`COALESCE(actual_cost_usd, estimated_cost_usd, 0)`) and is consumed by the
  profiles sidebar route (`hermes_state.py:10825`, consumed at
  `hermes_cli/web_routers/profiles.py:494`). Because `usage_totals` is
  root-summary-based it **excludes auxiliary usage** (auxiliary-client calls and
  sub-agent child sessions) — treat it as a per-profile headline, not a reconciled
  total.
- **Dashboard.** The dashboard exposes profile-scoped analytics under
  `/analytics` (each scoped to one selected profile's `state.db`): the usage view
  (`/api/analytics/usage`, `hermes_cli/web_server.py:15882`, handler
  `get_usage_analytics`) uses `InsightsEngine` for the tool/skill breakdown, while
  the models view (`/api/analytics/models`, `:16070`, handler
  `get_models_analytics`) queries the usage tables directly. The per-profile
  Analytics tab is **disabled by default** and requires opting in with
  `dashboard.show_token_analytics: true` (`hermes_cli/config_defaults.py:1696`).

**Caveats to recall when reading these numbers:** `usage_totals` is root-summary
based and so excludes auxiliary usage; the `session_model_usage` table has no
`pricing_version` column, so a re-priced backfill cannot be distinguished from the
original estimate (a separate upstream candidate, not this row's ask); and, as
noted above, the cron health gauges are active-profile-scoped rather than
fleet-aggregated under multiplex.

## Outcome analytics — where it lives

The GitHub-funnel **outcome** analytics — the
`outcomes(artifact, terminal_outcome, cost_usd, profile)` ledger and the
`hermes outcomes funnel | winrate | cost-per-merge` CLI verbs — are **not** part
of this page's native surface. They are delivered by the **`nv-artifact`** plugin
under **GOV-F25**; this row does not rebuild them. `hermes outcomes --json` (from
`nv-artifact`) is the machine-readable feed those verbs expose, referenced in the
gap section below only as the source for an interim Grafana-parity recipe. This
page covers only what Hermes ships natively (health telemetry + usage/cost
analytics); the outcome ledger's home is GOV-F25.

## Gap / upstream ask (P8)

There is one thing Hermes does **not** do today, and it is recorded here as a
**P8** upstream ask for the Orchestrator to file rather than worked around here: a
plugin has **no supported surface**
to publish its *own* custom monitoring event, gauge or attribute through the
shipped OTLP export plane. The plane is deliberately a **closed event set** with an
attribute **allow-list**, verified first-hand in the release tree:

- **Event-level closure.** The span export's `event_filter` (`_gateway_health_event`,
  `agent/monitoring/gateway_health_export.py:583`, wired at `:617`) admits only
  `gateway_health` and `cron_execution`; any other kind — including a plugin's
  custom event — is dropped before a span is emitted.
- **Attribute allow-list.** Even if a custom event reached export, `_span_attrs`
  (`agent/monitoring/otlp_exporter.py:137`) keeps only allow-listed attributes per
  known kind; an unknown kind maps to the empty tuple, so a custom event's span
  carries only `{"hermes.event": <kind>}` (zero payload attributes,
  `otlp_exporter.py:140`).
- **Frozen resource/diagnostic sets + hardcoded gauge list.** The resource and
  diagnostic attribute key sets are `frozenset`s
  (`agent/monitoring/gateway_health_export.py:22`), and the exported gauge names are
  a hardcoded list turned into observable gauges (`:437`, `:467`) with no config key
  or plugin hook to append one.

Consequently, **plugin-emitted fleet-outcome gauges are not publishable through the
shipped OTLP exporter** — the closed event set and attribute allow-list mean such a
gauge or event exports nothing even if forced through. (The `PluginContext.register_*`
surface exposes no monitoring/gauge/emitter-subscriber registration and there is no
`VALID_HOOKS` entry for a monitoring event, so this cannot be closed from a plugin.)
The minimal generic fix is an additive, generic registration seam upstream — an
event-schema registration that admits a plugin's custom event kind and its
attribute keys, plus (separately) a metric-observer registration for custom numeric
gauges — never a special case for one plugin. That is the OBS-F48 P8 upstream ask;
this row records it for the Orchestrator to file and builds nothing for it.

**Interim Grafana parity (recipe, not shipped).** Until the P8 seam exists, the
honest way to get fleet-outcome gauges into Grafana/Prometheus is a **script-only**
cron job that writes a **Prometheus textfile** from `hermes outcomes --json` (the
`nv-artifact` / GOV-F25 feed) for the node-exporter textfile collector to scrape.
This is a deployment recipe, not an OBS-F48 deliverable and not part of the OTLP
plane; it is documented here only as the sanctioned interim while the gap remains
open.

## Citations

Release-tree citations are `file:line` relative to the pinned tree
(`/workspace/extra/hermes-release`, tag **v2026.8.31** @
`29112bef099274229cadff79cdff7bf7b99c4b77`) and are verified first-hand. The
`main:` side of each pair below is **advisory** — reproduced from the gap-matrix
evidence (`gap-matrix-evidence.md § OBS-F48`), not independently re-verified here
(this environment has no `main` checkout), and where a symbol moved between the tag
and `main` the pair records the move.

| NanoClaw fleet-metrics behaviour | Hermes-native feature | tag: v2026.8.31 @29112bef | main: @08b140d14 |
|---|---|---|---|
| systemd oneshot emits fleet health (InfluxDB line protocol) | content-free OTLP egress plane, fail-open, no sidecar | `agent/monitoring/otlp_exporter.py:20` (+ `__init__.py:10`) | `agent/monitoring/otlp_exporter.py:1` |
| gateway/platform/cron health gauges | 16 hardcoded gauges | `agent/monitoring/gateway_health_export.py:437-454` (`metric_names`) | `agent/monitoring/gateway_health_export.py` (`metric_names`) |
| exporter enable + destination | config `monitoring.gateway_health_export.enabled` / `monitoring.export.otlp.enabled` / `monitoring.export.otlp.endpoint` (all three required by `_enabled()`) | `hermes_cli/config_defaults.py:3145` / `:3161` / `:3162`; `gateway_health_export.py:178-181` | `hermes_cli/config_defaults.py` (`monitoring` block) |
| operator inspects export state | `hermes monitoring status` | `hermes_cli/main.py:12976` | `hermes_cli/main.py` (cmd_monitoring) |
| per-profile usage/cost analytics | `InsightsEngine.generate()` over `session_model_usage` | `agent/insights.py:140` (`generate`) / `hermes_state_common.py:483` | `agent/insights.py:1` (module, per evidence) |
| dashboard usage/models analytics | `/api/analytics/usage`, `/models` | `hermes_cli/web_server.py:15882` / `:16070` | `hermes_cli/web_routers/analytics.py:77` / `:228` |
| per-profile usage rollup (profiles sidebar) | `SessionDB.usage_totals()` (root-summary), consumed by `web_routers/profiles.py:494` | `hermes_state.py:10825` (`usage_totals`) | `hermes_state.py` (`usage_totals`) |

The mapping table above is a convenience; the machine-checkable evidence is the
inline dual-citation pairs used throughout this page, at least one of which names
an `agent/monitoring/` source file on both sides — for example the OTLP
closed-plane docstring: tag: `agent/monitoring/otlp_exporter.py:20` | main: `agent/monitoring/otlp_exporter.py:1`.
