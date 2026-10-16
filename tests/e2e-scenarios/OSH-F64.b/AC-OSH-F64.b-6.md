---
ac: AC-OSH-F64.b-6
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
api_key: sk-OPENSHELL-PROXY-REWRITE
fixtures:
  - fixtures/osh-f64b-gateway
  - fixtures/osh-f64b-worker-architect
  - fixtures/osh-f64b-worker-builder
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-OSH-F64.b-6 — served turn 200 through the OpenShell inference provider + per-profile attribution

A served coworker's model turn returns HTTP 200 through the OpenShell inference REST provider
while the sandbox holds ONLY the fixed rewrite-trigger placeholder (never the real credential),
the NemoClaw `hermes` wrapper's `[SECURITY]` env-secret startup guard passes unchanged, AND two
distinct served profiles' turns resolve to two distinct profile records in the OpenShell/inference
logs (attribution is visible — scope-5(i)).

INFRA-GATED (§Gating): run only after the Orchestrator confirms (a) the stacked build on
`plugin/osh-f64-landing` and (b) the operator PROVIDER READY on `hermes-OSH-F64.b`. PROVIDER READY
is in place (operator msg 26 + 112 + 126): `inference.local` serves the fleet model, placeholder-only
auth proven E2E from inside brev-hermes.

## Process boundary (composed from OSH-F63/F64, not re-solved)
The served-profile MODEL turn runs in that profile's OWN profile-scoped dashboard chat child inside
the GATEWAY sandbox (`hermes_cli/web_server.py:16552-16597`; scope `/api/pty?profile=<role>`), which
reads its child `os.environ` — its model-provider `base_url` = `https://inference.local/v1` + the
placeholder `api_key`. NO worker sandbox issues a model call (workers ship no Hermes runtime).

## Setup
PROVIDER READY is in place (operator msg 26 + 112 + 126). Provision the throwaway `osh-f64b-gw` and
the served worker sandboxes from the OSH-F64.b render on the stacked head (as OSH-F64 AC-3/4 Setup
provisions its fleet, minus ALL OneCLI chain-dial steps — superseded). The OpenShell inference
provider behind `inference.local` is operator-configured so a placeholder request is
credential-rewritten (via the OpenShell-side `COMPATIBLE_API_KEY`) and forwarded. Each served
profile's provider config holds ONLY its placeholder; NO `secrets.onecli` repoint, NO CA-bundle
force — those are removed by OSH-F64.b.

- **RUN the scenario with `HTTPS_PROXY=10.200.0.1:3128` set** (the real OpenShell launch env, operator
  msg 112): the served 200 then proves proxy-env-agnosticism end-to-end — the rendered profile has no
  legacy loopback dial, so it does not fall into the path that FAILS under this proxy env.
- **(a)** the gateway CA bundle must carry the "OpenShell Sandbox CA" (`/etc/openshell-tls/ca-bundle.pem`)
  or TLS to `inference.local` fails with curl exit 60 (already fixed on brev-hermes).
- **(b)** a profile `.env` carrying `API_SERVER_*` makes the multiplexed gateway SKIP that profile — do
  NOT seed one (the operator removed it for orchestrator + reviewer).
- **(c)** `openshell sandbox exec` lands in a DIFFERENT netns than the gateway — start fleet processes
  from the IN-SANDBOX watchdog, not via `sandbox exec`.
- The live turn uses NO forced `tool_choice` (opus-5-5 rejects a forced tool_choice with 400; `auto`
  only — the aux title-gen's benign warning is expected).

## Steps
1. Drive one live model turn as a served coworker (profile A, e.g. `architect`) through its
   profile-scoped chat child (`POST /v1/chat/completions`, `model_id`
   `aws/anthropic/bedrock-claude-opus-5-5` — the operator-verified 200 path) → expect: HTTP 200 from
   the OpenShell inference REST provider (a real model reply), spending ≤ the scenario cap.
2. Verify the sandbox holds only the placeholder: read the served profile's rendered config + the
   child's `os.environ` → expect: the inference `api_key` is the FIXED rewrite-trigger placeholder
   `sk-OPENSHELL-PROXY-REWRITE`, and `COMPATIBLE_API_KEY` (the credential the OpenShell rewrite actually
   reads) is present NOWHERE on disk or in env — the real credential lives only OpenShell-side.
3. Confirm the NemoClaw `hermes` wrapper's `[SECURITY]` env-secret startup guard passed (the
   gateway/child started) with the OSH-F64.b env (no raw secret-shaped inference env name) → expect:
   startup succeeded, the guard did not refuse.
4. Drive one live model turn as a SECOND served coworker (profile B, e.g. `builder`) through ITS own
   profile-scoped chat child → expect: HTTP 200 (a second real model reply).
5. **Per-profile attribution (scope-5(i)).** NO per-profile credential exists at this layer (the
   placeholder is the single fixed `sk-OPENSHELL-PROXY-REWRITE` trigger), and both profile children run
   in the ONE gateway sandbox, so per-sandbox source alone cannot distinguish them. Read the
   OpenShell/inference logs for the two turns → expect: TWO records that resolve to profile A and
   profile B via, in priority order, (a) the per-request `X-Hermes-Profile` custom-provider header
   carrying the per-profile tag (`osh-f64b-<role>`, applied under `chat_completions`, rendered as
   `providers.<p>.extra_headers`), or (b) another per-profile non-secret request-correlated field the
   operator names as logged. Record which field carried the attribution. If NEITHER is log-visible for
   a shared-credential gateway, ESCALATE (§Gating item 2) — never a silent pass.

## Pass
_Amended to the operator ruling 05:22Z, §Gating 2 option 3 (waiver applied) as adjudicated in OSH-F64.b ADR 5367ee0d; the carried half is in `tests/e2e-scenarios/OSH-F64.e/`._

the served coworker's turn returns 200 through the OpenShell provider, the sandbox held only the fixed
placeholder trigger (`sk-OPENSHELL-PROXY-REWRITE`, never `COMPATIBLE_API_KEY` or any raw key), the
wrapper env guard passed unchanged, AND step 5 passes on TWO DISTINCT Hermes-native
`session_model_usage` records (D5) resolving to the CORRECT profiles (architect turn → architect,
builder turn → builder). The OpenShell-log attribution half (two distinct correct-profile records in
the OpenShell/inference logs via request-tag/per-sandbox correlation) is WAIVED for this row and
CARRIED to OSH-F64.e (UA-37); `X-Hermes-Profile` remains the design-level carrier but is not the pass
bar (OpenShell does not log it). The frozen scenario file is adjudicated under this ADR's ruling.
(Pre-ruling bar, now the carried half: a run whose turns cannot be shown to resolve to the correct
profile does not pass — but the proof-of-record is now the `session_model_usage` pair, not the
OpenShell log.)

## Evidence
- `scenario-AC-OSH-F64.b-6/evidence.txt`: the two 200-turn transcripts, the placeholder-only config/env
  reads (via `python3 -c`), the wrapper-guard startup log.
- `scenario-AC-OSH-F64.b-6/openshell.log`: the ALLOW line for each inference REST POST showing
  method+path, and the two distinct per-profile `X-Hermes-Profile` records proving attribution.
